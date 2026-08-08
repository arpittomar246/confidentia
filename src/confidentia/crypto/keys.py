"""Key hierarchy and the sealed keystore.

Three levels, each derived rather than stored:

    MEK   master encryption key, 256-bit, the only secret at rest
     |    HKDF-SHA256, info = "tenant:<tenant>"
    KEK   one per tenant -- cryptographic tenant isolation
     |    HKDF-SHA256, info = "class:<sensitivity>"
    DEK   one per (tenant, sensitivity class) -- what Parquet uses as a master key

**Why derive instead of store.** A stored key per column per tenant means a key
database that must itself be backed up, replicated and access-controlled — a second
security-critical system. Deriving from one root means the only thing to protect is
the MEK, and adding a tenant or a sensitivity class requires no key ceremony at all.
The cost is that rotating one DEK independently is impossible; rotation is
per-MEK-version and re-encrypts everything derived from it. That trade is right here
because sensitivity classes are few and stable.

**Tenant isolation is real.** ``healthcare`` and ``financial`` derive from different
KEKs. A principal holding the healthcare PHI key cannot decrypt financial columns —
not by policy, but because the key material is unrelated.

**On "sealed".** Under the ``sim`` TEE backend the keystore is encrypted with a key
derived from a passphrase via scrypt, and the file is owner-only. That is passphrase
protection, **not** hardware sealing: anything running as this user with the
passphrase can read it. Under a real backend the MEK would be sealed to the enclave
measurement so that even root could not. The distinction is stated wherever it
matters and is never described as more than it is.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from confidentia.common.logging import get_logger
from confidentia.config.settings import Sensitivity, get_settings

logger = get_logger(__name__)

KEY_BYTES = 32          # AES-256
NONCE_BYTES = 12        # GCM standard
SCRYPT_N = 2**14        # ~16 MB, tuned to stay comfortable on a 4 GB instance
SCRYPT_R = 8
SCRYPT_P = 1
KEYSTORE_FILENAME = "master.json"

# Sensitivity classes that get their own encryption key. PUBLIC deliberately does
# not: leaving non-sensitive columns in the clear is what preserves predicate
# pushdown on partition and filter columns (ADR 0007), and encrypting data that
# needs no protection buys nothing but query cost.
ENCRYPTED_CLASSES = (Sensitivity.INTERNAL, Sensitivity.PII, Sensitivity.PHI)


class KeyPurpose(StrEnum):
    FOOTER = "footer"   # protects the Parquet schema and metadata
    COLUMN = "column"   # protects column data


@dataclass(frozen=True)
class KeyId:
    """Stable identifier for a derived key.

    Rendered into ``parquet.encryption.column.keys``, so its format is a storage
    contract: changing it makes previously written files unreadable.
    """

    tenant: str
    sensitivity: str
    version: int = 1

    def __str__(self) -> str:
        return f"k_{self.tenant}_{self.sensitivity}_v{self.version}"

    @classmethod
    def footer(cls, tenant: str, version: int = 1) -> KeyId:
        return cls(tenant=tenant, sensitivity="footer", version=version)


class KeystoreError(RuntimeError):
    pass


class Keystore:
    """Holds the master key at rest, encrypted."""

    def __init__(self, path: Path | None = None, passphrase: str | None = None) -> None:
        settings = get_settings()
        self.path = (path or settings.keystore_path) / KEYSTORE_FILENAME
        self._passphrase = passphrase or os.environ.get("CONFIDENTIA_KEYSTORE_PASSPHRASE")
        self._mek: bytes | None = None

    # ------------------------------------------------------------------

    def _require_passphrase(self) -> str:
        if not self._passphrase:
            raise KeystoreError(
                "CONFIDENTIA_KEYSTORE_PASSPHRASE is not set. The keystore cannot be "
                "opened without it, and there is deliberately no fallback: a default "
                "passphrase would make the encrypted keystore decorative."
            )
        return self._passphrase

    def _wrapping_key(self, salt: bytes) -> bytes:
        kdf = Scrypt(salt=salt, length=KEY_BYTES, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
        return kdf.derive(self._require_passphrase().encode("utf-8"))

    def exists(self) -> bool:
        return self.path.exists()

    def initialise(self, *, force: bool = False) -> KeyId:
        """Generate a new master key and seal it.

        Refuses to overwrite an existing keystore unless forced: every byte in the
        lakehouse encrypted under the previous MEK becomes permanently unreadable,
        which should never happen by accident.
        """
        if self.exists() and not force:
            raise KeystoreError(
                f"keystore already exists at {self.path}. Overwriting it makes every "
                "existing encrypted file unreadable; pass force=True only if that is "
                "intended."
            )

        mek = secrets.token_bytes(KEY_BYTES)
        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(NONCE_BYTES)
        wrapped = AESGCM(self._wrapping_key(salt)).encrypt(nonce, mek, b"confidentia-mek-v1")

        settings = get_settings()
        document = {
            "version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "kdf": {"algorithm": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
                    "salt": base64.b64encode(salt).decode()},
            "wrapped_mek": base64.b64encode(nonce + wrapped).decode(),
            "master_key_id": settings.crypto.master_key_id,
            # Recorded so nobody can later mistake a passphrase-protected keystore
            # for a hardware-sealed one.
            "sealed_by": settings.tee.backend.value,
            "hardware_sealed": settings.tee.is_measured,
        }

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
        self._mek = mek

        logger.info(
            "keystore_initialised",
            path=str(self.path),
            master_key_id=settings.crypto.master_key_id,
            hardware_sealed=settings.tee.is_measured,
        )
        return KeyId(tenant="_root", sensitivity="mek", version=1)

    def unseal(self) -> bytes:
        """Decrypt and cache the master key."""
        if self._mek is not None:
            return self._mek
        if not self.exists():
            raise KeystoreError(f"no keystore at {self.path}; run `confidentia keys init`")

        document = json.loads(self.path.read_text(encoding="utf-8"))
        salt = base64.b64decode(document["kdf"]["salt"])
        blob = base64.b64decode(document["wrapped_mek"])
        nonce, ciphertext = blob[:NONCE_BYTES], blob[NONCE_BYTES:]

        try:
            self._mek = AESGCM(self._wrapping_key(salt)).decrypt(
                nonce, ciphertext, b"confidentia-mek-v1"
            )
        except Exception as exc:
            raise KeystoreError(
                "could not unseal the keystore: wrong passphrase, or the file has "
                "been tampered with (the GCM tag did not verify)"
            ) from exc
        return self._mek

    def metadata(self) -> dict[str, object]:
        if not self.exists():
            return {"exists": False}
        document = json.loads(self.path.read_text(encoding="utf-8"))
        mode = stat.S_IMODE(self.path.stat().st_mode)
        return {
            "exists": True,
            "path": str(self.path),
            "created_at": document["created_at"],
            "master_key_id": document["master_key_id"],
            "sealed_by": document.get("sealed_by"),
            "hardware_sealed": document.get("hardware_sealed", False),
            "permissions": oct(mode),
            "owner_only": mode == 0o600,
        }


class KeyHierarchy:
    """Derives tenant and column keys from the master key."""

    def __init__(self, keystore: Keystore | None = None) -> None:
        self.keystore = keystore or Keystore()

    def _derive(self, parent: bytes, info: str) -> bytes:
        # Salt is intentionally None: HKDF's extract step with a high-entropy input
        # key does not need one, and a random salt would make derivation
        # non-deterministic -- which would mean storing every derived key after all.
        return HKDF(
            algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None, info=info.encode("utf-8")
        ).derive(parent)

    def tenant_key(self, tenant: str) -> bytes:
        return self._derive(self.keystore.unseal(), f"tenant:{tenant}")

    def column_key(self, tenant: str, sensitivity: str, version: int = 1) -> bytes:
        return self._derive(self.tenant_key(tenant), f"class:{sensitivity}:v{version}")

    def footer_key(self, tenant: str, version: int = 1) -> bytes:
        return self._derive(self.tenant_key(tenant), f"footer:v{version}")

    def key_material(self, tenant: str, version: int = 1) -> dict[str, bytes]:
        """Every key a writer for this tenant needs, by key id."""
        material = {str(KeyId.footer(tenant, version)): self.footer_key(tenant, version)}
        for sensitivity in ENCRYPTED_CLASSES:
            key_id = KeyId(tenant=tenant, sensitivity=sensitivity.value, version=version)
            material[str(key_id)] = self.column_key(tenant, sensitivity.value, version)
        return material

    def key_list_property(self, material: dict[str, bytes]) -> str:
        """Render key material for ``parquet.encryption.key.list``.

        The resulting string contains raw key material and must never be logged,
        written to a config file, or passed on a command line where it would land
        in the process table.
        """
        return ",".join(
            f"{key_id}:{base64.b64encode(key).decode()}" for key_id, key in material.items()
        )
