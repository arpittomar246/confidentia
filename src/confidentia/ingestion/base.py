"""Ingestion framework: envelopes, retries, and the landing zone.

Ingestion writes to a **landing zone**, not straight to Bronze. The two hops earn
their keep:

*landing* is exactly what arrived, byte-verifiable against the source's own
checksums, plus a provenance envelope. Nothing is cast, cleaned or reordered, so
"what did the source actually send us on Tuesday" is answerable.

*Bronze* (Stage 4) is the typed, partitioned, encrypted Delta table. Doing both in
one step would mean a schema change or an encryption bug could destroy the only
copy of what was received.

Every record gains an envelope at ingestion — run id, batch digest, source file,
ordinal. That envelope is the seed of the verifiable lineage chain (C3): without a
record-level link back to the batch that produced it, provenance can only be
asserted at table granularity.

**The landing zone is encrypted.** It was not, for eleven stages: the enclave
placement planner (C5) found raw identifiers sitting in plaintext Parquet here,
outside the confidentiality boundary that Bronze onward maintains. ADR 0022 records
the fix — landing parts are written with Parquet Modular Encryption under the same
attestation-released, per-sensitivity keys Bronze uses, so the two hops are now
protected identically and replay survives. ``write_landing`` therefore *requires*
key material; there is no plaintext fallback, because a fallback is how the hole
existed in the first place.
"""

from __future__ import annotations

import random
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import pandas as pd

from confidentia.common.logging import get_logger
from confidentia.store.repositories import BatchDecision

if TYPE_CHECKING:  # pragma: no cover
    from confidentia.catalog.contracts import SchemaContract
    from confidentia.crypto.landing import LandingCrypto

logger = get_logger(__name__)

T = TypeVar("T")

# Envelope columns. Underscore-prefixed so they cannot collide with a source field
# and are trivially strippable when comparing landing content against the source.
ENVELOPE_RUN_ID = "_ingest_run_id"
ENVELOPE_BATCH = "_batch_digest"
ENVELOPE_SOURCE_FILE = "_source_file"
ENVELOPE_SOURCE_SYSTEM = "_source_system"
ENVELOPE_INGESTED_AT = "_ingested_at"
ENVELOPE_ORDINAL = "_record_ordinal"

ENVELOPE_COLUMNS = (
    ENVELOPE_RUN_ID,
    ENVELOPE_BATCH,
    ENVELOPE_SOURCE_FILE,
    ENVELOPE_SOURCE_SYSTEM,
    ENVELOPE_INGESTED_AT,
    ENVELOPE_ORDINAL,
)


class IngestError(Exception):
    """Base for ingestion failures."""


class RetryableError(IngestError):
    """Transient: a timeout, a 503, a dropped connection. Worth another attempt."""


class TerminalError(IngestError):
    """Permanent: a checksum mismatch, a 401, a malformed manifest.

    Retrying would waste time and, worse, could mask corruption as a transient
    blip. These fail the run immediately.
    """


@dataclass
class RetryPolicy:
    """Exponential backoff with full jitter.

    Hand-rolled rather than pulled from a library because each attempt is recorded
    into the run's metadata, and because the retryable/terminal split is a
    deliberate design decision this project has to defend: a checksum mismatch must
    never be retried, since a retry that eventually "succeeds" would hide
    corruption.

    Full jitter (`sleep = random(0, backoff)`) rather than fixed backoff: when a
    source recovers, unjittered clients reconverge on the same instant and knock it
    over again.
    """

    max_attempts: int = 4
    base_seconds: float = 0.5
    max_seconds: float = 30.0
    multiplier: float = 2.0

    def delay(self, attempt: int) -> float:
        backoff = min(self.base_seconds * (self.multiplier ** (attempt - 1)), self.max_seconds)
        return random.uniform(0.0, backoff)


def with_retry(
    operation: Callable[[], T],
    *,
    policy: RetryPolicy | None = None,
    description: str = "operation",
    on_attempt: Callable[[int, Exception | None], None] | None = None,
) -> T:
    policy = policy or RetryPolicy()
    last: Exception | None = None

    for attempt in range(1, policy.max_attempts + 1):
        try:
            result = operation()
        except TerminalError:
            # Never retried. See RetryPolicy.
            raise
        except RetryableError as exc:
            last = exc
            if on_attempt:
                on_attempt(attempt, exc)
            if attempt == policy.max_attempts:
                break
            sleep_for = policy.delay(attempt)
            logger.warning(
                "retrying",
                operation=description,
                attempt=attempt,
                max_attempts=policy.max_attempts,
                sleep_seconds=round(sleep_for, 2),
                error=str(exc)[:200],
            )
            time.sleep(sleep_for)
        else:
            if on_attempt:
                on_attempt(attempt, None)
            return result

    raise RetryableError(
        f"{description} failed after {policy.max_attempts} attempts: {last}"
    ) from last


@dataclass
class IngestResult:
    source: str
    tenant: str
    dataset: str
    decision: BatchDecision
    batch_digest: str = ""
    records_read: int = 0
    records_written: int = 0
    records_rejected: int = 0
    bytes_read: int = 0
    files_verified: int = 0
    output_path: Path | None = None
    duration_seconds: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def skipped(self) -> bool:
        return self.decision is BatchDecision.DUPLICATE

    def summary(self) -> str:
        if self.skipped:
            return f"{self.tenant}/{self.dataset}: skipped (duplicate batch)"
        rate = self.records_written / self.duration_seconds if self.duration_seconds else 0
        return (
            f"{self.tenant}/{self.dataset}: {self.records_written:,} records "
            f"in {self.duration_seconds:.2f}s ({rate:,.0f}/s), "
            f"{self.records_rejected:,} rejected"
        )


def add_envelope(
    frame: pd.DataFrame,
    *,
    run_id: uuid.UUID,
    batch_digest: str,
    source_file: str,
    source_system: str,
    ordinal_offset: int = 0,
) -> pd.DataFrame:
    """Attach ingestion provenance to every record."""
    enveloped = frame.copy()
    enveloped[ENVELOPE_RUN_ID] = str(run_id)
    enveloped[ENVELOPE_BATCH] = batch_digest
    enveloped[ENVELOPE_SOURCE_FILE] = source_file
    enveloped[ENVELOPE_SOURCE_SYSTEM] = source_system
    enveloped[ENVELOPE_INGESTED_AT] = pd.Timestamp(datetime.now(UTC))
    enveloped[ENVELOPE_ORDINAL] = range(ordinal_offset, ordinal_offset + len(enveloped))
    return enveloped


def landing_path(lake_root: Path, tenant: str, dataset: str) -> Path:
    return lake_root / "landing" / tenant / dataset


def write_landing(
    frame: pd.DataFrame,
    *,
    lake_root: Path,
    tenant: str,
    dataset: str,
    batch_digest: str,
    part: int,
    crypto: LandingCrypto,
    contract: SchemaContract | None = None,
) -> Path:
    """Write one encrypted part into the landing zone.

    Named by batch digest, so re-ingesting identical content overwrites in place
    rather than accumulating duplicate parts. Combined with the batch registry this
    makes ingestion idempotent at the filesystem level too, not only in metadata —
    an interrupted run that already wrote three of five parts leaves no orphans.

    ``crypto`` is required. Its key material was released against an attestation
    for this run, and which columns it protects comes from the dataset's schema
    contract, so landing and Bronze protect the same columns under the same keys.

    The write is verified from the bytes before it is allowed to stand, and a part
    that fails verification is deleted rather than left for someone to find later —
    the one thing worse than an unencrypted landing zone is an unencrypted landing
    zone that reports itself as encrypted.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    from confidentia.catalog.contracts import ContractError, get_contract
    from confidentia.crypto.landing import LandingEncryptionError, assert_encrypted

    if contract is None:
        try:
            contract = get_contract(tenant, dataset)
        except (ContractError, KeyError) as exc:
            # Ingestion used to land any directory it was pointed at. That is how
            # healthcare/patient_change — a source-side mutation script, not a lake
            # dataset — spent months in the landing zone unclassified and in
            # plaintext: Bronze refused it for want of a contract, and nothing
            # upstream refused it at all. Now the refusal happens before the write.
            raise ContractError(
                f"{tenant}/{dataset} has no schema contract, so there is no "
                "sensitivity classification to encrypt it by and Bronze could never "
                "promote it. Declare the contract in catalog/contracts.py, or stop "
                "pointing a collector at this dataset."
            ) from exc
    target = landing_path(lake_root, tenant, dataset)
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"batch={batch_digest[:16]}" / f"part-{part:05d}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)

    table = pa.Table.from_pandas(frame, preserve_index=False)
    properties = crypto.encryption_properties(contract, present=set(table.schema.names))
    try:
        with pq.ParquetWriter(
            path, table.schema, compression="snappy", encryption_properties=properties
        ) as writer:
            writer.write_table(table)
        assert_encrypted(path)
    except LandingEncryptionError:
        path.unlink(missing_ok=True)
        raise
    return path


def read_landing_part(path: Path, crypto: LandingCrypto) -> pd.DataFrame:
    """Read one encrypted landing part back.

    Used by verification and migration, and by anything that wants to inspect what
    arrived without standing up Spark. Bronze does not go through here — it reads
    the same files through Spark, which is the interop this design turns on.
    """
    import pyarrow.parquet as pq

    return pq.read_table(path, decryption_properties=crypto.decryption_properties()).to_pandas()


def iter_frame_chunks(frame: pd.DataFrame, chunk_rows: int) -> Iterator[pd.DataFrame]:
    for start in range(0, len(frame), chunk_rows):
        yield frame.iloc[start : start + chunk_rows]
