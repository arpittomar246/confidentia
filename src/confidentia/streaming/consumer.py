"""Structured Streaming from Kafka into the **shared** Bronze table.

The consumer is a fourth source, not a second pipeline (ADR 0014). It writes into
the same `lake/bronze/financial/transaction` the file and REST collectors write,
sets `_source_system = 'kafka'`, and goes through the same schema contract. Silver
therefore deduplicates across it without knowing streaming exists, and Stage 7c's
per-source Bronze uniqueness already distinguishes a redelivery from a duplicate.

Three decisions carry the weight here.

**`foreachBatch`, not `writeStream.format("delta")`.** The batch path already
knows how to reconcile a frame against the contract, derive the column-to-key
mapping, and assert that the bytes on disk are actually encrypted. `foreachBatch`
hands each micro-batch over as an ordinary DataFrame, so all of that is reused
rather than reimplemented for streaming — and reimplementing the PME wiring is
precisely how a streaming writer would end up silently writing plaintext
(ADR 0007).

**Exactly-once is Delta's idempotent write, not a hope.** Each micro-batch is
written with `txnAppId`/`txnVersion`, so a batch replayed after a crash — the
checkpoint says "in progress" but the commit already landed — is a no-op instead
of a duplicate. Without it, at-least-once delivery plus a retry is a duplicated
record, and the only thing that would notice is a row count nobody checks.

**Micro-batch size is capped.** `maxOffsetsPerTrigger` bounds both memory and file
size. Stage 5 already fixed a small-file explosion in Silver; a 10-second trigger
writing unbounded batches is the same problem arriving from a new direction, so
the cap and the compaction are here from the first commit rather than after the
table has 100,000 files.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from confidentia.catalog.contracts import SchemaContract, get_contract
from confidentia.common.logging import get_logger
from confidentia.config.settings import get_settings
from confidentia.pipelines.bronze.writer import bronze_path, verify_encrypted
from confidentia.spark.session import (
    key_ids_by_sensitivity,
    session_for_tenant,
    set_column_keys,
)
from confidentia.store.repositories import RunType, track_run
from confidentia.streaming.watermark import WatermarkPolicy

if TYPE_CHECKING:  # pragma: no cover
    from pyspark.sql import DataFrame

logger = get_logger(__name__)

# Identifies this writer to Delta's transaction log for idempotent appends. It
# must be stable across restarts: a new value would make every already-committed
# micro-batch look new and duplicate the entire replay.
TXN_APP_ID = "confidentia-stream-bronze"

SOURCE_SYSTEM = "kafka"


@dataclass
class ConsumeResult:
    tenant: str
    dataset: str
    topic: str
    batches: int = 0
    records: int = 0
    seconds: float = 0.0
    encryption_verified: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def per_second(self) -> float:
        return self.records / self.seconds if self.seconds > 0 else 0.0

    def summary(self) -> str:
        return (
            f"{self.tenant}/{self.dataset} <- {self.topic}: {self.records:,} records "
            f"in {self.batches} micro-batches, {self.seconds:.1f}s "
            f"({self.per_second:,.0f}/s)  encrypted={self.encryption_verified}"
        )


def checkpoint_path(tenant: str, dataset: str) -> Path:
    return get_settings().data_root / "checkpoints" / "streaming" / tenant / dataset


def payload_schema(contract: SchemaContract) -> str:
    """DDL for the JSON on the wire: the contract minus the ingest envelope.

    Derived rather than written out, so adding a column to the contract does not
    require remembering to add it here. The envelope is excluded because the
    producer is a *source* — it does not know about this platform's ingest
    metadata, and a source that filled in `_ingest_run_id` would be fiction.
    """
    return ", ".join(
        f"`{c.name}` {c.dtype}"
        for c in contract.columns
        if not c.name.startswith("_") and c.name != "ingest_date"
    )


def _with_envelope(frame: DataFrame, run_id: Any) -> DataFrame:
    """Add the same ingest envelope the batch collectors add.

    The mapping from Kafka's coordinates onto it is deliberate:

    * `_source_file` becomes `topic:partition:offset`. A stream has no file, but it
      has an exact address for every record, and that is what the column is for —
      tracing a Bronze row back to what delivered it.
    * `_record_ordinal` becomes the Kafka offset, which is genuinely an ordinal
      within a partition rather than a synthesised row number.
    * `_batch_digest` identifies the micro-batch. It is not content-addressed the
      way a file batch's digest is, and it is not claimed to be: idempotency here
      comes from the Delta transaction, not from hashing the payload.
    """
    from pyspark.sql import functions as F

    return (
        frame.withColumn("_ingest_run_id", F.lit(str(run_id)))
        .withColumn(
            "_source_file",
            F.concat_ws(":", F.col("topic"), F.col("partition").cast("string"),
                        F.col("offset").cast("string")),
        )
        .withColumn("_source_system", F.lit(SOURCE_SYSTEM))
        .withColumn("_ingested_at", F.current_timestamp())
        .withColumn("_record_ordinal", F.col("offset").cast("bigint"))
    )


def consume(
    tenant: str = "financial",
    dataset: str = "transaction",
    *,
    topic: str | None = None,
    once: bool = True,
    trigger_seconds: int = 10,
    max_offsets_per_trigger: int = 20_000,
    starting_offsets: str = "earliest",
    timeout_seconds: int | None = None,
) -> ConsumeResult:
    """Stream a topic into Bronze.

    ``once`` drains everything currently in the topic and stops (Spark's
    ``availableNow`` trigger). That is the right default here: it is testable, it
    terminates, and it is what an orchestrated task needs. A continuous run is the
    same code with a processing-time trigger.
    """
    from pyspark.sql import functions as F

    settings = get_settings()
    contract = get_contract(tenant, dataset)
    topic = topic or settings.kafka.topic_transactions_raw
    target = bronze_path(tenant, dataset)
    checkpoint = checkpoint_path(tenant, dataset)
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    policy = WatermarkPolicy.from_settings()

    result = ConsumeResult(tenant=tenant, dataset=dataset, topic=topic)
    started = time.perf_counter()

    with track_run(
        RunType.STREAM,
        tenant=tenant,
        dataset=dataset,
        source="kafka",
        metadata={
            "topic": topic,
            "once": once,
            "watermark_minutes": policy.delay_minutes,
            "max_offsets_per_trigger": max_offsets_per_trigger,
        },
    ) as run:
        with session_for_tenant(
            tenant, run_id=run.run_id, app_name=f"confidentia-stream-{tenant}",
            with_kafka=True,
        ) as (spark, _keys):
            column_keys = contract.column_keys_property(key_ids_by_sensitivity(tenant))

            raw = (
                spark.readStream.format("kafka")
                .option("kafka.bootstrap.servers", settings.kafka.bootstrap_servers)
                .option("subscribe", topic)
                .option("startingOffsets", starting_offsets)
                # Bounds the micro-batch. Without it the first trigger of a
                # backlogged topic tries to read the whole thing into a 2 GB driver.
                .option("maxOffsetsPerTrigger", max_offsets_per_trigger)
                # A topic whose retention expired mid-run is a real condition and
                # must not be silently skipped past.
                .option("failOnDataLoss", "true")
                .load()
            )

            parsed = (
                raw.select(
                    F.col("topic"),
                    F.col("partition"),
                    F.col("offset"),
                    F.col("timestamp").alias("_kafka_ts"),
                    F.from_json(
                        F.col("value").cast("string"), payload_schema(contract)
                    ).alias("payload"),
                )
                .select("topic", "partition", "offset", "_kafka_ts", "payload.*")
            )

            # The watermark is declared even though this write is stateless, so the
            # policy is attached to the stream rather than living only in a report.
            if "event_ts" in {c.name for c in contract.columns}:
                parsed = parsed.withWatermark("event_ts", policy.spark_delay)

            def write_batch(batch: DataFrame, batch_id: int) -> None:
                from confidentia.pipelines.bronze.writer import _apply_contract

                if batch.rdd.isEmpty():
                    return
                enveloped = _with_envelope(batch, run.run_id).withColumn(
                    "_batch_digest", F.lit(f"{topic}#{batch_id:012d}")
                )
                shaped = _apply_contract(enveloped, contract)
                count = shaped.count()

                # Session-global, and re-applied per micro-batch: `foreachBatch`
                # runs on the driver but the mapping must be set on the Hadoop
                # Configuration in force for *this* write.
                set_column_keys(spark, column_keys)
                (
                    shaped.write.format("delta")
                    .mode("append")
                    .partitionBy(*contract.partition_columns)
                    .option("mergeSchema", "false")
                    # Idempotency: Delta refuses a (txnAppId, txnVersion) it has
                    # already committed, so a micro-batch replayed after a crash
                    # is dropped rather than duplicated.
                    .option("txnAppId", TXN_APP_ID)
                    .option("txnVersion", batch_id)
                    .save(str(target))
                )
                result.batches += 1
                result.records += count
                logger.info(
                    "micro_batch_written",
                    batch_id=batch_id, records=count, tenant=tenant, dataset=dataset,
                )

            writer = (
                parsed.writeStream.foreachBatch(write_batch)
                .option("checkpointLocation", str(checkpoint))
                .queryName(f"bronze-{tenant}-{dataset}")
            )

            if once:
                query = writer.trigger(availableNow=True).start()
            else:
                query = writer.trigger(processingTime=f"{trigger_seconds} seconds").start()

            query.awaitTermination(timeout=timeout_seconds)
            if query.isActive:
                query.stop()

            progress = query.lastProgress or {}
            result.detail = {
                "batch_id": progress.get("batchId"),
                "input_rows_per_second": progress.get("inputRowsPerSecond"),
                "sources": [s.get("description") for s in progress.get("sources", [])],
            }

            if result.records:
                verified, detail = verify_encrypted(target)
                result.encryption_verified = verified
                result.detail["encryption"] = detail
                if not verified:
                    # Same failure mode as the batch writer, and the reason the
                    # assertion is repeated rather than assumed to hold: a
                    # misconfigured streaming write produces readable plaintext
                    # with no error anywhere.
                    raise RuntimeError(
                        f"streamed Bronze at {target} is not encrypted: {detail}"
                    )

        run.add(read=result.records, written=result.records)
        run.note(batches=result.batches, topic=topic)

    result.seconds = time.perf_counter() - started
    logger.info(
        "stream_consumed",
        tenant=tenant, dataset=dataset, records=result.records,
        batches=result.batches, per_second=round(result.per_second, 1),
    )
    return result
