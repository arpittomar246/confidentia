"""Silver layer: conform, validate, deduplicate, tokenise, write.

Silver is the first layer where records are corrected rather than merely recorded.
It stays encrypted under the same per-column keys as Bronze, and adds tokenisation
for direct identifiers so that downstream joins work without the underlying values.

Partitioning moves from ingest date to **event date** here. Bronze answers "what did
we receive on Tuesday"; Silver answers "what happened on Tuesday", and analytical
queries filter on the latter.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from confidentia.catalog.contracts import SchemaContract, get_contract
from confidentia.common.logging import get_logger
from confidentia.config.settings import Layer, Tenant, get_settings
from confidentia.crypto.tokenisation import Tokeniser
from confidentia.pipelines.bronze.writer import bronze_path, verify_encrypted
from confidentia.pipelines.silver.transforms import (
    DQ_FAILURES,
    add_row_hash,
    conform,
    deduplicate,
    split_valid,
    validate,
)
from confidentia.spark.session import key_ids_by_sensitivity, session_for_tenant, set_column_keys
from confidentia.store import db
from confidentia.store.repositories import RunType, track_run

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import DataFrame

logger = get_logger(__name__)

# How many quarantined records to persist per run. Quarantine is for diagnosis and
# replay, not for storing a second copy of a broken dataset; a run that quarantines
# a million records has a systemic problem that a sample will reveal just as well.
QUARANTINE_SAMPLE_LIMIT = 5_000


class SilverQualityGateError(RuntimeError):
    """Too much of the batch failed validation to trust the output."""


@dataclass
class SilverResult:
    tenant: str
    dataset: str
    records_in: int = 0
    records_out: int = 0
    quarantined: int = 0
    duplicates_removed: int = 0
    major_failures: int = 0
    tokenised_fields: list[str] = field(default_factory=list)
    encryption_verified: bool = False
    duration_seconds: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def quarantine_rate(self) -> float:
        return self.quarantined / self.records_in if self.records_in else 0.0

    def summary(self) -> str:
        return (
            f"{self.tenant}/{self.dataset}: {self.records_in:,} in -> "
            f"{self.records_out:,} out, {self.duplicates_removed:,} dupes, "
            f"{self.quarantined:,} quarantined ({self.quarantine_rate:.2%}), "
            f"{self.major_failures:,} flagged, {self.duration_seconds:.1f}s"
        )


def silver_path(tenant: str, dataset: str) -> Path:
    return get_settings().layer_path(Layer.SILVER, Tenant(tenant), dataset)


def _event_date_column(frame: DataFrame, contract: SchemaContract) -> DataFrame:
    """Add the Silver partition column.

    Falls back to ingest date when the dataset has no event time, rather than
    leaving the table unpartitioned.
    """
    from pyspark.sql import functions as F

    source = contract.event_time_column
    if source and source in frame.columns:
        return frame.withColumn("event_date", F.to_date(F.col(source)))
    return frame.withColumn("event_date", F.col("ingest_date"))


def _persist_quarantine(
    quarantined: DataFrame, *, run_id, tenant: str, dataset: str, contract: SchemaContract
) -> int:
    """Record failed records in redacted form.

    Sensitive columns are dropped before anything reaches Postgres. Quarantine must
    not become a plaintext side channel around the encrypted lakehouse — it is the
    obvious place for exactly that mistake.
    """
    from pyspark.sql import functions as F

    safe_columns = [
        c.name
        for c in contract.columns
        if c.sensitivity.value in {"public", "internal"} and c.name in quarantined.columns
    ]
    key = contract.business_key[0] if contract.business_key else None

    sample = quarantined.limit(QUARANTINE_SAMPLE_LIMIT).select(
        *[F.col(c) for c in safe_columns],
        F.col(DQ_FAILURES),
        *( [F.col(key).alias("_business_key")] if key and key in quarantined.columns else [] ),
        *( [F.col("_batch_digest")] if "_batch_digest" in quarantined.columns else [] ),
    )

    rows = sample.collect()
    if not rows:
        return 0

    payload = []
    for row in rows:
        record = row.asDict()
        failures = record.pop(DQ_FAILURES, []) or []
        payload.append(
            (
                run_id,
                tenant,
                dataset,
                record.pop("_business_key", None),
                record.pop("_batch_digest", None),
                failures[0].split(":")[0] if failures else "unknown",
                json.dumps(failures),
                json.dumps(record, default=str),
            )
        )

    with db.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO platform.quarantine_record
                (run_id, tenant, dataset, business_key, batch_digest,
                 reason_code, failed_rule, redacted_record)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            payload,
        )
    return len(payload)


def write_silver(
    tenant: str,
    dataset: str,
    *,
    full_refresh: bool = True,
    enforce_gate: bool = True,
) -> SilverResult:
    """Build the Silver table for one dataset."""
    started = time.perf_counter()
    settings = get_settings()
    contract = get_contract(tenant, dataset)
    source = bronze_path(tenant, dataset)
    target = silver_path(tenant, dataset)

    if not source.exists():
        raise FileNotFoundError(f"no Bronze table at {source}; run bronze write first")

    with track_run(
        RunType.TRANSFORM,
        tenant=tenant,
        dataset=dataset,
        source="silver",
        metadata={"layer": "silver"},
    ) as run:
        with session_for_tenant(tenant, run_id=run.run_id) as (spark, _keys):
            from pyspark.sql import functions as F

            set_column_keys(spark, contract.column_keys_property(key_ids_by_sensitivity(tenant)))

            bronze = spark.read.format("delta").load(str(source))
            records_in = bronze.count()

            shaped = conform(bronze, contract)
            shaped = validate(shaped, contract)
            valid, quarantined = split_valid(shaped)

            quarantined_count = quarantined.count()
            major = valid.filter(F.size(F.col(DQ_FAILURES)) > 0).count()

            # Circuit breaker. A batch where most records fail has a systemic
            # problem -- a schema change, a broken upstream -- and publishing the
            # survivors would present a heavily biased subset as complete.
            rate = quarantined_count / records_in if records_in else 0.0
            if enforce_gate and rate > settings.quality.max_quarantine_rate:
                raise SilverQualityGateError(
                    f"{tenant}/{dataset}: {rate:.1%} of records quarantined, above the "
                    f"{settings.quality.max_quarantine_rate:.0%} threshold. Silver was "
                    "not written; investigate the source before promoting a biased subset."
                )

            if quarantined_count:
                _persist_quarantine(
                    quarantined,
                    run_id=run.run_id,
                    tenant=tenant,
                    dataset=dataset,
                    contract=contract,
                )

            hashed = add_row_hash(valid, contract)
            deduped, _ = deduplicate(hashed, contract)
            records_out = deduped.count()
            duplicates_removed = valid.count() - records_out

            tokenise_fields = [
                f for f in contract.tokenised_columns if f in deduped.columns
            ]
            tokenised = (
                Tokeniser(tenant).tokenise_frame(deduped, tokenise_fields)
                if tokenise_fields
                else deduped
            )

            final = _event_date_column(tokenised, contract)

            (
                final.write.format("delta")
                .mode("overwrite" if full_refresh else "append")
                .partitionBy("event_date")
                .option("overwriteSchema", "true")
                .save(str(target))
            )

            verified, detail = verify_encrypted(target)
            if not verified:
                raise RuntimeError(
                    f"Silver write for {tenant}/{dataset} produced unencrypted output ({detail})"
                )

        run.add(read=records_in, written=records_out, rejected=quarantined_count)
        run.note(
            duplicates_removed=duplicates_removed,
            major_failures=major,
            tokenised_fields=tokenise_fields,
            quarantine_rate=round(rate, 4),
        )

        result = SilverResult(
            tenant=tenant,
            dataset=dataset,
            records_in=records_in,
            records_out=records_out,
            quarantined=quarantined_count,
            duplicates_removed=duplicates_removed,
            major_failures=major,
            tokenised_fields=tokenise_fields,
            encryption_verified=True,
            duration_seconds=time.perf_counter() - started,
            detail={"encryption": detail},
        )
        logger.info("silver_written", **{
            "tenant": tenant, "dataset": dataset,
            "records_out": records_out, "quarantined": quarantined_count,
            "duplicates_removed": duplicates_removed,
        })
        return result


def build_patient_dimension(*, tenant: str = "healthcare") -> dict[str, Any]:
    """Build the SCD2 patient dimension from the captured change stream."""
    from confidentia.pipelines.silver.scd2 import PATIENT_SCD2, build_scd2, validate_scd2

    started = time.perf_counter()
    source = bronze_path(tenant, "patient_cdc")
    if not source.exists():
        raise FileNotFoundError(
            f"no CDC Bronze table at {source}; run `ingest cdc` then `bronze write`"
        )
    target = silver_path(tenant, "dim_patient")

    with track_run(
        RunType.TRANSFORM, tenant=tenant, dataset="dim_patient", source="silver"
    ) as run:
        with session_for_tenant(tenant, run_id=run.run_id) as (spark, _keys):
            contract = get_contract(tenant, "patient")
            set_column_keys(spark, contract.column_keys_property(key_ids_by_sensitivity(tenant)))

            changes = spark.read.format("delta").load(str(source))
            events_in = changes.count()

            conformed = conform(changes, contract)
            dimension = build_scd2(conformed, PATIENT_SCD2)

            tokenise_fields = [
                f for f in contract.tokenised_columns if f in dimension.columns
            ]
            if tokenise_fields:
                dimension = Tokeniser(tenant).tokenise_frame(dimension, tokenise_fields)

            dimension.write.format("delta").mode("overwrite").option(
                "overwriteSchema", "true"
            ).save(str(target))

            written = spark.read.format("delta").load(str(target))
            versions = written.count()
            checks = validate_scd2(written, PATIENT_SCD2)
            distinct_keys = written.select(PATIENT_SCD2.natural_key).distinct().count()

        run.add(read=events_in, written=versions)
        run.note(**checks, distinct_keys=distinct_keys)

        return {
            "change_events_in": events_in,
            "versions_out": versions,
            "distinct_patients": distinct_keys,
            "versions_per_patient": round(versions / max(distinct_keys, 1), 3),
            "checks": checks,
            "duration_seconds": round(time.perf_counter() - started, 1),
        }
