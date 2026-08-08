"""Bronze layer: landing → encrypted Delta.

Bronze is the first *queryable* layer. It is typed, partitioned, and encrypted
per-column under keys released by attestation. It is still raw in the sense that
values are not corrected — a malformed diagnosis code arrives in Bronze exactly as
the source sent it. Cleaning happens in Silver, where the quality layer can
quarantine what fails rather than losing it.

**The write is verified, not trusted.** ADR 0007 established that Delta silently
writes plaintext when encryption is misconfigured: no exception, no warning, just
readable PHI on disk. Every Bronze write therefore ends by reading back the bytes
of a sample output file and asserting the ``PARE`` magic. A write that cannot prove
it encrypted is rolled back and the run fails.

**Partitioning is on ingest date, not event date.** A late-arriving record belongs
to the batch that delivered it. Partitioning raw data on a value inside the data
would mean a backfill rewrites historical partitions, which destroys the "what did
we receive on Tuesday" property that the landing zone and Bronze exist to preserve.
Event-time partitioning starts at Silver.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from confidentia.catalog.contracts import SchemaContract, get_contract
from confidentia.common.logging import get_logger
from confidentia.config.settings import Layer, Tenant, get_settings
from confidentia.spark.session import (
    key_ids_by_sensitivity,
    session_for_tenant,
    set_column_keys,
)
from confidentia.store import db
from confidentia.store.repositories import RunType, track_run

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import DataFrame, SparkSession

logger = get_logger(__name__)

PARQUET_ENCRYPTED_MAGIC = b"PARE"
PARQUET_PLAIN_MAGIC = b"PAR1"


class EncryptionNotAppliedError(RuntimeError):
    """The write completed but produced plaintext. Always a hard failure."""


@dataclass
class BronzeResult:
    tenant: str
    dataset: str
    records_written: int = 0
    batches_promoted: int = 0
    batches_skipped: int = 0
    files_written: int = 0
    bytes_written: int = 0
    encryption_verified: bool = False
    duration_seconds: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        state = "verified encrypted" if self.encryption_verified else "NOT VERIFIED"
        return (
            f"{self.tenant}/{self.dataset}: {self.records_written:,} records, "
            f"{self.batches_promoted} batch(es), {self.batches_skipped} skipped, "
            f"{self.duration_seconds:.1f}s, {state}"
        )


def bronze_path(tenant: str, dataset: str) -> Path:
    settings = get_settings()
    return settings.layer_path(Layer.BRONZE, Tenant(tenant), dataset)


def landing_path(tenant: str, dataset: str) -> Path:
    return get_settings().lake_root / "landing" / tenant / dataset


def verify_encrypted(table_path: Path, *, expect_encrypted: bool = True) -> tuple[bool, str]:
    """Read the bytes of the largest data file and check the Parquet magic.

    Byte-level rather than trusting configuration, because the failure mode being
    guarded against is precisely a configuration that looks correct and is not.
    """
    files = [p for p in table_path.rglob("*.parquet") if p.is_file() and p.stat().st_size > 8]
    if not files:
        return False, "no parquet files produced"

    target = max(files, key=lambda p: p.stat().st_size)
    with target.open("rb") as handle:
        head = handle.read(4)
        handle.seek(-4, 2)
        tail = handle.read(4)

    if head == PARQUET_ENCRYPTED_MAGIC and tail == PARQUET_ENCRYPTED_MAGIC:
        return (expect_encrypted, f"{target.name}: PARE (encrypted footer)")
    if head == PARQUET_PLAIN_MAGIC and tail == PARQUET_PLAIN_MAGIC:
        return (not expect_encrypted, f"{target.name}: PAR1 (plaintext footer)")
    return False, f"{target.name}: unrecognised magic {head!r}/{tail!r}"


def _promoted_batches(tenant: str, dataset: str) -> set[str]:
    """Batches already present in Bronze.

    Deliberately does NOT filter on ``status = 'ingested'``. Promotion to Bronze
    and the ingest registry's own status are independent facts, and coupling them
    caused a real duplication bug: a batch left ``pending`` by an interrupted
    ingest run was invisible to this check and got re-promoted on every subsequent
    run, doubling the table. The only question that matters here is whether this
    content is already in Bronze.
    """
    rows = db.fetch_all(
        """
        SELECT batch_digest FROM platform.ingest_batch
         WHERE tenant = %s AND dataset = %s
           AND metadata ? 'bronze_promoted_at'
        """,
        (tenant, dataset),
    )
    return {row["batch_digest"] for row in rows}


def _mark_promoted(batch_digests: list[str]) -> None:
    """Record promotion. No record count is stored per batch.

    An earlier version wrote the whole run's row count onto every batch, which
    claimed 120,798 rows for a batch that contributed 60,399. Per-batch counts
    would need a separate Spark job per batch; the run ledger already holds the
    accurate total, so the marker records only that promotion happened.
    """
    if not batch_digests:
        return
    db.execute(
        """
        UPDATE platform.ingest_batch
           SET metadata = metadata
               || jsonb_build_object('bronze_promoted_at', to_jsonb(now()::text))
         WHERE batch_digest = ANY(%s)
        """,
        (batch_digests,),
    )


def _apply_contract(frame: DataFrame, contract: SchemaContract) -> DataFrame:
    """Cast landing columns to the contract's types and add the partition column.

    Casts are permissive (``try_cast`` semantics via ``cast`` on a nullable column):
    a value that will not cast becomes NULL here rather than failing the batch,
    because Bronze must accept what arrived. The quality layer in Stage 7 detects
    those nulls against the contract and quarantines the records — losing them at
    this point would defeat the purpose of a raw layer.
    """
    from pyspark.sql import functions as F

    available = set(frame.columns)
    projected = []
    for spec in contract.columns:
        if spec.name == "ingest_date":
            continue
        if spec.name in available:
            projected.append(F.col(f"`{spec.name}`").cast(spec.dtype).alias(spec.name))
        else:
            # Declared but absent: a schema drift signal. Materialised as a typed
            # NULL so the table shape stays stable and Stage 7 can measure it.
            projected.append(F.lit(None).cast(spec.dtype).alias(spec.name))

    out = frame.select(*projected)
    return out.withColumn("ingest_date", F.to_date(F.col("_ingested_at")))


def write_bronze(
    tenant: str,
    dataset: str,
    *,
    full_refresh: bool = False,
    session: SparkSession | None = None,
) -> BronzeResult:
    """Promote landed batches into the encrypted Bronze Delta table."""
    started = time.perf_counter()
    contract = get_contract(tenant, dataset)
    source = landing_path(tenant, dataset)
    target = bronze_path(tenant, dataset)

    if not source.exists():
        raise FileNotFoundError(f"nothing landed at {source}; run ingestion first")

    with track_run(
        RunType.TRANSFORM,
        tenant=tenant,
        dataset=dataset,
        source="bronze",
        metadata={"layer": "bronze", "full_refresh": full_refresh},
    ) as run:
        already = set() if full_refresh else _promoted_batches(tenant, dataset)
        batch_dirs = sorted(p for p in source.glob("batch=*") if p.is_dir())
        pending = [p for p in batch_dirs if p.name.removeprefix("batch=") not in
                   {d[:16] for d in already}]

        if not pending:
            logger.info("bronze_up_to_date", tenant=tenant, dataset=dataset)
            return BronzeResult(
                tenant=tenant,
                dataset=dataset,
                batches_skipped=len(batch_dirs),
                encryption_verified=True,
                duration_seconds=time.perf_counter() - started,
                detail={"reason": "all batches already promoted"},
            )

        with session_for_tenant(tenant, run_id=run.run_id) as (spark, _keys):
            # Column-to-key mapping comes from the schema contract, so a column's
            # sensitivity classification is what decides which key protects it.
            column_keys = contract.column_keys_property(key_ids_by_sensitivity(tenant))
            set_column_keys(spark, column_keys)

            # Read each batch separately and reconcile it against the contract
            # before unioning. Batches of the same dataset genuinely arrive with
            # different physical types -- the file collector lands `admit_ts` as
            # an INT64 timestamp while the REST collector lands it as a string,
            # because the payload round-tripped through JSON. Reading them as one
            # path fails with SchemaColumnConvertNotSupportedException.
            #
            # The contract is the reconciliation point: every batch is cast to the
            # declared type, so Bronze has one stable schema regardless of which
            # collector produced the data. This also absorbs additive schema drift,
            # since missing columns materialise as typed NULLs.
            shaped = None
            for batch_dir in pending:
                batch_frame = _apply_contract(spark.read.parquet(str(batch_dir)), contract)
                shaped = batch_frame if shaped is None else shaped.unionByName(batch_frame)
            assert shaped is not None
            records = shaped.count()

            mode = "overwrite" if full_refresh else "append"
            (
                shaped.write.format("delta")
                .mode(mode)
                .partitionBy(*contract.partition_columns)
                .option("mergeSchema", "false")
                .save(str(target))
            )

            verified, detail = verify_encrypted(target)
            if not verified:
                # ADR 0007: Delta writes plaintext silently when misconfigured.
                # Refusing to leave that on disk is the whole point of this check.
                logger.error("bronze_encryption_unverified", tenant=tenant, dataset=dataset,
                             detail=detail)
                raise EncryptionNotAppliedError(
                    f"Bronze write for {tenant}/{dataset} produced unencrypted output "
                    f"({detail}). The table has been left in place for inspection but "
                    "must not be trusted. Check the column key mapping and that "
                    "encryption is set on the Hadoop Configuration, not writer options."
                )

            files = list(target.rglob("*.parquet"))
            bytes_written = sum(f.stat().st_size for f in files)

        promoted = [p.name.removeprefix("batch=") for p in pending]
        _mark_promoted_by_prefix(tenant, dataset, promoted)

        run.add(read=records, written=records, bytes_read=bytes_written)
        run.note(
            batches=len(pending),
            encryption_verified=True,
            encryption_detail=detail,
            column_keys=column_keys,
            plaintext_columns=list(contract.plaintext_columns),
        )

        result = BronzeResult(
            tenant=tenant,
            dataset=dataset,
            records_written=records,
            batches_promoted=len(pending),
            batches_skipped=len(batch_dirs) - len(pending),
            files_written=len(files),
            bytes_written=bytes_written,
            encryption_verified=True,
            duration_seconds=time.perf_counter() - started,
            detail={"encryption": detail, "column_keys": column_keys},
        )
        logger.info("bronze_written", **{
            "tenant": tenant, "dataset": dataset, "records": records,
            "batches": len(pending), "encrypted": True,
        })
        return result


def _mark_promoted_by_prefix(tenant: str, dataset: str, prefixes: list[str]) -> None:
    """Landing directories carry a 16-char digest prefix; the registry holds the
    full digest. Resolve prefixes back to full digests before recording promotion.

    Raises if a landing directory has no matching registry row: that means Bronze
    just ingested data the platform has no record of receiving, which would leave
    the batch permanently un-promotable and silently duplicating on every run —
    exactly the failure this function exists to prevent.
    """
    if not prefixes:
        return
    rows = db.fetch_all(
        "SELECT batch_digest FROM platform.ingest_batch WHERE tenant = %s AND dataset = %s",
        (tenant, dataset),
    )
    digests = [row["batch_digest"] for row in rows]
    matched = [d for d in digests if any(d.startswith(prefix) for prefix in prefixes)]

    unmatched = [p for p in prefixes if not any(d.startswith(p) for d in digests)]
    if unmatched:
        raise RuntimeError(
            f"{tenant}/{dataset}: landing batches {unmatched} have no registry row. "
            "They cannot be marked promoted and would be re-written on every run."
        )
    _mark_promoted(matched)
