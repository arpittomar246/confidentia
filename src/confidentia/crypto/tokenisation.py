"""Deterministic tokenisation of direct identifiers.

**Why deterministic.** The same SSN must produce the same token everywhere, or the
patient in `encounter` can no longer be joined to the patient in `claim_line` and
the warehouse stops working. Random tokens would be more private and completely
useless.

**What that costs, stated plainly.** Deterministic tokenisation is vulnerable to a
dictionary attack whenever the input domain is small: an attacker who can compute
the tokenisation function can enumerate all 36,500 plausible dates of birth, or
all 50 state codes, and match tokens by brute force. The token key is what prevents
this, so:

* tokens are HMAC-SHA256 under a key derived from the MEK — an attacker without the
  key cannot compute candidate tokens at all;
* the key is released only through the broker, and never appears in a Spark query
  plan (see below);
* **low-cardinality fields must not be tokenised.** They are encrypted at rest
  instead. `date_of_birth`, `sex` and `state` are classified PHI/PII and protected
  by column encryption, not by tokens. Only high-cardinality direct identifiers
  (SSN, MRN, email, phone, name, account number, IP) are tokenised.

**Why a UDF and not `sha2(concat(...))`.** The obvious Spark-native approach puts
the key into the query plan as a literal, and the physical plan is displayed in the
Spark UI's SQL tab — the same leak class as ADR 0010's SparkConf finding. A UDF
carries the key in the closure sent to executors, which the UI does not render. The
cost is speed, mitigated by using a vectorised pandas UDF rather than row-at-a-time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pandas as pd
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from confidentia.common.logging import get_logger
from confidentia.crypto.keys import KeyHierarchy
from confidentia.store import db

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import DataFrame

logger = get_logger(__name__)

TOKEN_PREFIX = "tk"
TOKEN_HEX_LENGTH = 24  # 96 bits of the HMAC: collision-safe well past 10^12 values
NONCE_BYTES = 12

# Fields whose domain is small enough that deterministic tokens could be brute
# forced by anyone who obtains the key. These are protected by column encryption
# instead, and tokenising them is refused rather than silently allowed.
LOW_CARDINALITY_FIELDS = frozenset(
    {"sex", "gender", "state", "home_state", "country", "home_country", "currency",
     "date_of_birth", "dob", "postal_code", "terminal_country"}
)


class TokenisationError(RuntimeError):
    pass


def normalise(value: str) -> str:
    """Canonicalise before hashing.

    Without this, ``"  John "`` and ``"john"`` tokenise differently and the same
    person appears as two entities — which is precisely the duplication the Silver
    layer exists to remove. Normalisation happens *after* conforming, so the
    whitespace and case defects injected in Stage 2 are already repaired; this is
    a second line of defence.
    """
    text = unicodedata.normalize("NFKC", str(value)).strip()
    return " ".join(text.split()).casefold()


@dataclass
class TokenisationResult:
    field: str
    tokens_created: int
    vault_rows_written: int


def make_token_udf(key: bytes, field_name: str):
    """Build a vectorised tokenising UDF bound to one key and one field.

    Defined at module level on purpose. PySpark resolves a pandas UDF's signature
    with ``typing.get_type_hints``, which looks names up in the function's module
    globals; because this package uses ``from __future__ import annotations``, the
    hints are strings, and a locally-imported ``pd`` would not be resolvable. The
    failure is an opaque ``UNSUPPORTED_SIGNATURE`` error at call time.

    The field name is part of the HMAC input so that the same value appearing in
    two different fields does not collide into one token — an email and a phone
    number that happened to match must not merge two entities.
    """
    from pyspark.sql import functions as F
    from pyspark.sql.types import StringType

    @F.pandas_udf(StringType())
    def _tokenise(values: pd.Series) -> pd.Series:
        def one(value: object) -> str | None:
            if value is None or pd.isna(value):
                return None
            text = unicodedata.normalize("NFKC", str(value)).strip()
            text = " ".join(text.split()).casefold()
            digest = hmac.new(
                key, f"{field_name}\x00{text}".encode(), hashlib.sha256
            ).hexdigest()[:TOKEN_HEX_LENGTH]
            return f"{TOKEN_PREFIX}_{field_name}_{digest}"

        return values.map(one)

    return _tokenise


class Tokeniser:
    """Derives token and vault keys, and applies them."""

    def __init__(self, tenant: str, *, hierarchy: KeyHierarchy | None = None) -> None:
        self.tenant = tenant
        self.hierarchy = hierarchy or KeyHierarchy()

    # ------------------------------------------------------------------ keys

    def _token_key(self) -> bytes:
        # Separate from any column encryption key: compromising the token key must
        # not also decrypt the lakehouse, and vice versa.
        return self.hierarchy._derive(
            self.hierarchy.tenant_key(self.tenant), "purpose:tokenisation:v1"
        )

    def _vault_key(self) -> bytes:
        return self.hierarchy._derive(
            self.hierarchy.tenant_key(self.tenant), "purpose:token_vault:v1"
        )

    # ---------------------------------------------------------------- tokens

    def token_for(self, value: str | None, field: str) -> str | None:
        if value is None:
            return None
        digest = hmac.new(
            self._token_key(),
            f"{field}\x00{normalise(value)}".encode(),
            hashlib.sha256,
        ).hexdigest()[:TOKEN_HEX_LENGTH]
        return f"{TOKEN_PREFIX}_{field}_{digest}"

    @staticmethod
    def assert_tokenisable(field: str) -> None:
        if field in LOW_CARDINALITY_FIELDS:
            raise TokenisationError(
                f"{field!r} has too small a domain for deterministic tokenisation: "
                "an attacker with the key could enumerate it. Protect it with column "
                "encryption instead."
            )

    def tokenise_frame(self, frame: DataFrame, fields: list[str]) -> DataFrame:
        """Replace each field in place with its token.

        The key is captured in the UDF closure rather than embedded in the query
        plan; see the module docstring.
        """
        from pyspark.sql import functions as F

        for field in fields:
            self.assert_tokenisable(field)

        key = self._token_key()
        present = [f for f in fields if f in frame.columns]

        out = frame
        for field in present:
            out = out.withColumn(field, make_token_udf(key, field)(F.col(field)))

        logger.info("fields_tokenised", tenant=self.tenant, fields=present)
        return out

    # ----------------------------------------------------------------- vault

    def vault_store(self, pairs: list[tuple[str, str, str]]) -> int:
        """Persist token → encrypted original.

        ``pairs`` is ``(token, field, original)``. Originals are encrypted under a
        vault key distinct from both the token key and the column keys.
        """
        if not pairs:
            return 0
        aead = AESGCM(self._vault_key())
        import secrets

        rows = []
        for token, field, original in pairs:
            nonce = secrets.token_bytes(NONCE_BYTES)
            # The token is bound in as AAD, so a ciphertext cannot be moved to a
            # different token row without detection.
            blob = nonce + aead.encrypt(nonce, original.encode("utf-8"), token.encode())
            rows.append((token, self.tenant, field, base64.b64encode(blob).decode()))

        with db.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO platform.token_vault (token, tenant, field, ciphertext)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (token) DO UPDATE
                    SET last_seen_at = now(),
                        occurrence_count = platform.token_vault.occurrence_count + 1
                """,
                rows,
            )
        return len(rows)

    def detokenise(
        self, tokens: list[str], *, principal: str, justification: str
    ) -> dict[str, str]:
        """Re-identify. Always audited, including refusals.

        This is the one operation that undoes the platform's privacy guarantee, so
        it records who asked, for what, and why — before returning anything.
        """
        if not tokens:
            return {}

        rows = db.fetch_all(
            "SELECT token, field, ciphertext FROM platform.token_vault "
            "WHERE token = ANY(%s) AND tenant = %s",
            (tokens, self.tenant),
        )
        aead = AESGCM(self._vault_key())
        resolved: dict[str, str] = {}
        for row in rows:
            blob = base64.b64decode(row["ciphertext"])
            try:
                plaintext = aead.decrypt(
                    blob[:NONCE_BYTES], blob[NONCE_BYTES:], row["token"].encode()
                )
            except Exception:  # noqa: BLE001 - a tag failure means tampering
                logger.error("vault_entry_tampered", token=row["token"][:24])
                continue
            resolved[row["token"]] = plaintext.decode("utf-8")

        db.execute(
            """
            INSERT INTO platform.detokenisation_audit
                (principal, tenant, field, token_count, justification, granted, reason)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                principal,
                self.tenant,
                rows[0]["field"] if rows else "unknown",
                len(tokens),
                justification,
                bool(resolved),
                "resolved" if resolved else "no matching vault entries",
            ),
        )
        logger.warning(
            "detokenisation",
            principal=principal,
            tenant=self.tenant,
            requested=len(tokens),
            resolved=len(resolved),
        )
        return resolved
