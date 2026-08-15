"""Parquet Modular Encryption for the landing zone, written from Python.

ADR 0017 found the hole this module closes: ingestion wrote the landing zone as
plaintext Parquet before Bronze encrypted anything, so raw source records —
including direct identifiers — sat readable on disk. The planner reported it as a
boundary violation because it *was* one.

**Why this could not simply reuse the Bronze path.** Bronze is written by Spark,
which encrypts through the Java ``PropertiesDrivenCryptoFactory`` and
``LocalKmsClient``. Ingestion is pandas — a landing part is one file written by one
process, and standing up a Spark session per batch to encrypt a 60,000-row frame
would cost more than the ingest. So the landing zone is encrypted by pyarrow
instead, and the two engines have to agree on the bytes.

They do, because :class:`LocalKmsClient` here implements the *same envelope* as
``LocalKmsClient.java``: AES-256-GCM, ``base64(nonce || ciphertext || tag)``, with
the master key identifier bound in as additional authenticated data. Both sides
then use the Parquet key-tools ``PKMT1`` key material format with double wrapping,
which is a published interchange format rather than either engine's private one.

The consequence is the property that matters: **Spark reads what pandas wrote.**
Bronze promotes encrypted landing batches with an unchanged ``spark.read.parquet``
call, because the session already carries the crypto factory and the same derived
keys. ``tests/integration/test_landing_encryption.py`` proves the round trip in
both directions rather than assuming it, since a wrapping format that drifts on one
side would otherwise show up as an unreadable lake months later.

Keys come from the broker, so a landing write is attestation-gated exactly as a
Bronze write is. That is the point of the fix: the landing zone is not merely
encrypted, it is inside the same boundary, under the same policy, with the same
audit trail.
"""

from __future__ import annotations

import base64
import os
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from confidentia.common.logging import get_logger
from confidentia.config.settings import Sensitivity
from confidentia.crypto.keys import KeyId

if TYPE_CHECKING:  # pragma: no cover
    import pyarrow.parquet.encryption as pe

    from confidentia.catalog.contracts import SchemaContract
    from confidentia.crypto.broker import ReleasedKeys

logger = get_logger(__name__)

NONCE_BYTES = 12
AAD_UNUSED = b""

# Both sides default these to "DEFAULT" when a real KMS instance is not addressed.
# They are written into the file's key material and matched on read, so they are a
# storage contract, not a connection detail.
KMS_INSTANCE_ID = "DEFAULT"
KMS_INSTANCE_URL = "DEFAULT"

PARQUET_ENCRYPTED_MAGIC = b"PARE"
PARQUET_PLAIN_MAGIC = b"PAR1"


class LandingEncryptionError(RuntimeError):
    """A landing write could not be proved encrypted. Always fatal."""


def _kms_client_class() -> type:
    """Build the pyarrow KMS client lazily.

    ``pyarrow.parquet.encryption`` pulls in the Arrow encryption component, and
    importing it at module scope would make every import of the ingestion package
    pay for it — including the CLI's ``--help``.
    """
    import pyarrow.parquet.encryption as pe

    class LocalKmsClient(pe.KmsClient):
        """The Python half of the wrapping format LocalKmsClient.java implements.

        Master keys are held in memory for the lifetime of one ingest run and are
        never written anywhere. ``wrap_key``/``unwrap_key`` must stay
        byte-compatible with the Java class; the integration test is what enforces
        that, because a mismatch is silent until something tries to read.
        """

        def __init__(self, config: Any, material: dict[str, bytes]) -> None:
            super().__init__()
            self._material = material

        def _master(self, key_id: str) -> bytes:
            try:
                return self._material[key_id]
            except KeyError as exc:
                raise LandingEncryptionError(
                    f"master key {key_id!r} was not released to this process "
                    f"(held: {sorted(self._material)})"
                ) from exc

        def wrap_key(self, key_bytes: bytes, master_key_identifier: str) -> str:
            nonce = secrets.token_bytes(NONCE_BYTES)
            sealed = AESGCM(self._master(master_key_identifier)).encrypt(
                nonce, key_bytes, master_key_identifier.encode("utf-8")
            )
            return base64.b64encode(nonce + sealed).decode()

        def unwrap_key(self, wrapped_key: str, master_key_identifier: str) -> bytes:
            envelope = base64.b64decode(wrapped_key)
            try:
                return AESGCM(self._master(master_key_identifier)).decrypt(
                    envelope[:NONCE_BYTES],
                    envelope[NONCE_BYTES:],
                    master_key_identifier.encode("utf-8"),
                )
            except LandingEncryptionError:
                raise
            except Exception as exc:  # GCM tag failure, or a truncated envelope
                raise LandingEncryptionError(
                    f"could not unwrap a data key under {master_key_identifier!r}: "
                    "wrong master key, or the key material has been tampered with"
                ) from exc

    return LocalKmsClient


@dataclass
class LandingCrypto:
    """Encryption properties for one tenant's landing writes.

    Built once per ingest run — the key release is audited against that run — and
    reused for every part. Constructing one per part would be correct but would put
    a broker round trip and an attestation measurement in the inner write loop.
    """

    tenant: str
    material: dict[str, bytes]
    hardware_rooted: bool = False
    version: int = 1

    @classmethod
    def for_tenant(
        cls, tenant: str, *, run_id: uuid.UUID | None = None, version: int = 1
    ) -> LandingCrypto:
        """Request landing keys through the broker.

        Deliberately the same call Bronze makes. If the running code's measurement
        is not approved, ingestion fails to write rather than falling back to
        plaintext — which is the behaviour that distinguishes this from a
        configuration flag.
        """
        from confidentia.crypto.broker import request_keys

        keys: ReleasedKeys = request_keys(tenant, run_id=run_id)
        return cls(
            tenant=tenant,
            material=dict(keys.material),
            hardware_rooted=keys.hardware_rooted,
            version=version,
        )

    # ------------------------------------------------------------------ ids

    @property
    def footer_key_id(self) -> str:
        return str(KeyId.footer(self.tenant, self.version))

    def key_id(self, sensitivity: Sensitivity) -> str:
        return str(
            KeyId(tenant=self.tenant, sensitivity=sensitivity.value, version=self.version)
        )

    # ----------------------------------------------------------- properties

    def _factory(self) -> pe.CryptoFactory:
        import pyarrow.parquet.encryption as pe

        client_class = _kms_client_class()
        return pe.CryptoFactory(lambda config: client_class(config, self.material))

    def _connection(self) -> pe.KmsConnectionConfig:
        import pyarrow.parquet.encryption as pe

        return pe.KmsConnectionConfig(
            kms_instance_id=KMS_INSTANCE_ID, kms_instance_url=KMS_INSTANCE_URL
        )

    def strictest_key_id(self, contract: SchemaContract) -> str | None:
        """The key protecting the most sensitive class this contract declares."""
        declared = set(contract.columns_by_sensitivity())
        for sensitivity in (Sensitivity.PHI, Sensitivity.PII, Sensitivity.INTERNAL):
            if sensitivity in declared:
                return self.key_id(sensitivity)
        return None

    def column_key_map(
        self, contract: SchemaContract, present: set[str] | None = None
    ) -> dict[str, list[str]]:
        """Map key id → columns, from the contract's sensitivity classification.

        The same classification Bronze uses, so a column protected in Bronze is
        protected in landing under the identical key. ``present`` restricts the map
        to columns the frame actually carries: pyarrow rejects an encryption config
        naming a column that is not in the schema, and a landing part legitimately
        lacks columns when a source omits an optional field.

        PUBLIC columns are omitted, exactly as in Bronze — that omission is what
        keeps partition and filter columns readable.

        **Columns the contract does not declare are encrypted under the strictest
        key the contract uses.** This is where landing differs from Bronze, and it
        has to: Bronze casts to the contract first, so an undeclared column never
        reaches the writer, while landing is deliberately pre-contract and does
        receive them. Experiment E8 showed additive drift arriving unannounced —
        under a plaintext-by-default rule, a new ``ssn_alt`` column would land in
        the clear and nothing would say so. Over-encrypting an unknown column costs
        a key lookup; under-encrypting it is the bug this whole module exists to
        remove.
        """
        mapping: dict[str, list[str]] = {}
        classified: set[str] = set()
        for sensitivity, names in contract.columns_by_sensitivity().items():
            classified.update(names)
            if sensitivity is Sensitivity.PUBLIC:
                continue
            selected = sorted(n for n in names if present is None or n in present)
            if selected:
                mapping.setdefault(self.key_id(sensitivity), []).extend(selected)

        if present is not None:
            # Envelope columns are ingestion's own provenance — run id, batch
            # digest, ordinal — and carry no source data. They stay readable so a
            # part can be attributed to a batch without holding a key.
            from confidentia.ingestion.base import ENVELOPE_COLUMNS

            undeclared = sorted(present - classified - set(ENVELOPE_COLUMNS))
            fallback = self.strictest_key_id(contract)
            if undeclared and fallback:
                logger.warning(
                    "landing_undeclared_columns_encrypted",
                    tenant=self.tenant,
                    dataset=contract.dataset,
                    columns=undeclared,
                    key_id=fallback,
                    hint="the source sent columns the contract does not declare",
                )
                mapping.setdefault(fallback, []).extend(undeclared)

        return {key: sorted(set(columns)) for key, columns in mapping.items()}

    def encryption_properties(
        self, contract: SchemaContract, *, present: set[str] | None = None
    ) -> Any:
        import pyarrow.parquet.encryption as pe

        column_keys = self.column_key_map(contract, present)
        if not column_keys:
            # A contract with nothing but PUBLIC columns would encrypt only the
            # footer. That is a legitimate configuration, but it has never been the
            # case here, and silently producing it would hide a contract mistake.
            raise LandingEncryptionError(
                f"contract {contract.tenant}/{contract.dataset} classifies no column "
                "above PUBLIC, so a landing write would protect nothing. Check the "
                "contract before disabling this guard."
            )
        config = pe.EncryptionConfiguration(
            footer_key=self.footer_key_id,
            column_keys=column_keys,
            double_wrapping=True,
            plaintext_footer=False,
            # The envelope columns (_ingest_run_id, _batch_digest, ...) are not in
            # the contract and stay readable, which is what lets the batch reconciler
            # in bronze/writer.py identify a part without a key.
        )
        return self._factory().file_encryption_properties(self._connection(), config)

    def unclassified_encryption_properties(self, present: set[str]) -> Any:
        """Encrypt every source column, for data no contract classifies.

        The migration found ``healthcare/patient_change`` in the landing zone: 900
        rows of patient address changes, landed once by the file collector in
        August, belonging to no schema contract and therefore never promoted,
        never classified, and never protected. It had been sitting in plaintext
        the entire time precisely because nothing knew it was there.

        Refusing to encrypt it because it lacks a classification would be the same
        mistake in a new place, so everything but the ingestion envelope goes under
        the tenant's PHI key. That is over-protection by design: the data cannot be
        used until someone writes a contract for it, at which point re-encrypting
        under the right per-column keys is one command.
        """
        import pyarrow.parquet.encryption as pe

        from confidentia.ingestion.base import ENVELOPE_COLUMNS

        columns = sorted(present - set(ENVELOPE_COLUMNS))
        if not columns:
            raise LandingEncryptionError(
                "an unclassified landing part carries only envelope columns; "
                "there is nothing to protect and nothing to promote"
            )
        config = pe.EncryptionConfiguration(
            footer_key=self.footer_key_id,
            column_keys={self.key_id(Sensitivity.PHI): columns},
            double_wrapping=True,
            plaintext_footer=False,
        )
        return self._factory().file_encryption_properties(self._connection(), config)

    def decryption_properties(self) -> Any:
        import pyarrow.parquet.encryption as pe

        return self._factory().file_decryption_properties(
            self._connection(), pe.DecryptionConfiguration()
        )


def footer_magic(path: Path) -> bytes:
    """Read the four-byte footer magic off disk.

    The same check Bronze makes, for the same reason: configuration that looks
    correct and silently produces plaintext is the failure mode being guarded
    against, so the evidence has to be the bytes.
    """
    if path.stat().st_size < 8:
        return b""
    with path.open("rb") as handle:
        handle.seek(-4, os.SEEK_END)
        return handle.read(4)


def assert_encrypted(path: Path) -> None:
    magic = footer_magic(path)
    if magic != PARQUET_ENCRYPTED_MAGIC:
        raise LandingEncryptionError(
            f"{path} was written with footer magic {magic!r}, expected "
            f"{PARQUET_ENCRYPTED_MAGIC!r}. The landing write produced plaintext and "
            "the file has been removed."
        )


def plaintext_parts(root: Path) -> list[Path]:
    """Every landing Parquet file that still carries a plaintext footer.

    Used by the placement planner and by ``confidentia landing verify``. Returning
    paths rather than a boolean is deliberate: an operator who is told the landing
    zone is unencrypted needs to know which batches to re-ingest.
    """
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.rglob("*.parquet")
        if path.is_file() and footer_magic(path) != PARQUET_ENCRYPTED_MAGIC
    )


# ------------------------------------------------------------------ migration


@dataclass
class ReencryptResult:
    """One landing part, rewritten from plaintext to encrypted."""

    path: Path
    tenant: str
    dataset: str
    rows: int
    plaintext_bytes: int
    encrypted_bytes: int
    applied: bool
    unclassified: bool = False

    @property
    def growth(self) -> float:
        return self.encrypted_bytes / self.plaintext_bytes if self.plaintext_bytes else 0.0


def _dataset_dirs(landing_root: Path) -> list[tuple[str, str, Path]]:
    return [
        (tenant_dir.name, dataset_dir.name, dataset_dir)
        for tenant_dir in sorted(p for p in landing_root.iterdir() if p.is_dir())
        for dataset_dir in sorted(p for p in tenant_dir.iterdir() if p.is_dir())
    ]


def reencrypt_landing(
    lake_root: Path,
    *,
    tenant: str | None = None,
    dataset: str | None = None,
    dry_run: bool = False,
    run_id: uuid.UUID | None = None,
) -> list[ReencryptResult]:
    """Rewrite plaintext landing parts in place, encrypted.

    Needed because the landing zone accumulated plaintext for eleven stages before
    ADR 0022. Re-ingesting instead would also work, but it would discard the batch
    registry's record of what arrived when — which is the property the landing zone
    exists for, so the migration preserves it.

    Each part is written to a sibling temporary file, verified from its bytes, and
    only then moved over the original with ``os.replace``, which is atomic within a
    filesystem. An interrupted migration therefore leaves either the old plaintext
    part or the new encrypted one, never a truncated file — and re-running it is
    safe, because an already-encrypted part is never selected.

    **This does not shred the plaintext.** ``os.replace`` unlinks the old inode; the
    blocks it occupied stay on the device until reused, and on a copy-on-write
    filesystem or an SSD doing wear levelling they may survive far longer. Erasing
    them is a disk-level operation this function has no business attempting, and
    saying so is more useful than a guarantee it could not keep.
    """
    import pyarrow.parquet as pq

    from confidentia.catalog.contracts import ContractError, get_contract

    landing_root = lake_root / "landing"
    if not landing_root.exists():
        return []

    results: list[ReencryptResult] = []
    crypto_by_tenant: dict[str, LandingCrypto] = {}

    for found_tenant, found_dataset, directory in _dataset_dirs(landing_root):
        if tenant and found_tenant != tenant:
            continue
        if dataset and found_dataset != dataset:
            continue

        offenders = plaintext_parts(directory)
        if not offenders:
            continue

        try:
            contract: SchemaContract | None = get_contract(found_tenant, found_dataset)
        except (ContractError, KeyError):
            # Not fatal, and deliberately so. An unclassified dataset is the worst
            # case for this migration, not a reason to skip it: nothing has ever
            # protected these columns because nothing knew they existed.
            contract = None
            logger.warning(
                "landing_unclassified_dataset",
                tenant=found_tenant,
                dataset=found_dataset,
                parts=len(offenders),
                hint="no schema contract; every source column will be encrypted",
            )

        for path in offenders:
            # `ParquetFile(...).read()`, not `read_table`. Landing parts live at
            # `batch=<digest>/part-*.parquet`, and `read_table` defaults to
            # `partitioning="hive"`: it reads the path as a one-file dataset and
            # materialises the directory name as a column. This migration then
            # wrote that column into the file, so a rewrite meant to change
            # nothing but encryption silently added a `batch` column to every part
            # it touched -- and the collision with its own partition directory is
            # what makes Spark warn COLUMN_ALREADY_EXISTS on a landing read.
            #
            # Reading the file directly has no dataset layer and therefore no
            # partition inference, whatever pyarrow's defaults become.
            table = pq.ParquetFile(path).read()
            plaintext_bytes = path.stat().st_size

            if dry_run:
                results.append(
                    ReencryptResult(
                        path=path,
                        tenant=found_tenant,
                        dataset=found_dataset,
                        rows=table.num_rows,
                        plaintext_bytes=plaintext_bytes,
                        encrypted_bytes=0,
                        applied=False,
                        unclassified=contract is None,
                    )
                )
                continue

            if found_tenant not in crypto_by_tenant:
                crypto_by_tenant[found_tenant] = LandingCrypto.for_tenant(
                    found_tenant, run_id=run_id
                )
            crypto = crypto_by_tenant[found_tenant]

            temporary = path.with_suffix(".reencrypting")
            try:
                present = set(table.schema.names)
                properties = (
                    crypto.encryption_properties(contract, present=present)
                    if contract is not None
                    else crypto.unclassified_encryption_properties(present)
                )
                with pq.ParquetWriter(
                    temporary,
                    table.schema,
                    compression="snappy",
                    encryption_properties=properties,
                ) as writer:
                    writer.write_table(table)
                assert_encrypted(temporary)
                encrypted_bytes = temporary.stat().st_size
                os.replace(temporary, path)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise

            results.append(
                ReencryptResult(
                    path=path,
                    tenant=found_tenant,
                    dataset=found_dataset,
                    rows=table.num_rows,
                    plaintext_bytes=plaintext_bytes,
                    encrypted_bytes=encrypted_bytes,
                    applied=True,
                    unclassified=contract is None,
                )
            )
            logger.info(
                "landing_part_reencrypted",
                tenant=found_tenant,
                dataset=found_dataset,
                part=path.name,
                rows=table.num_rows,
            )

    return results
