"""E1 — streaming against batch throughput into the same Bronze table.

ADR 0014 chose to have the Kafka consumer write the **same** Delta table as the
batch collectors, rather than a separate streaming table, and noted the consequence
for this experiment: "Experiment E1 has to separate batch from streaming rows by
`_source_system` rather than by table. That is a `WHERE` clause, not a design
problem."

## Why this does not drive the real CLI

The obvious implementation — `confidentia stream consume` against
`confidentia bronze write` — cannot be repeated. Batch ingestion is
content-addressed and idempotent: the second run of an identical batch is
recognised and skipped, so repeats two through five would measure a no-op. The
streaming consumer has the same property through its checkpoint.

So both arms run the same Spark work against scratch outputs, with the checkpoint
cleared between repeats. What is being compared is the **ingestion path** — a Kafka
source with an `availableNow` trigger against a Parquet file read — writing the
same envelope into the same Delta format. That is what E1 asks about; the
idempotency machinery is Stage 3's subject, not this one's.

Both arms carry `_source_system`, so the comparison is exactly the `WHERE` clause
ADR 0014 anticipated.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

from confidentia.benchmarks.harness import ArmResult, record, run_arms, table
from confidentia.catalog.contracts import get_contract
from confidentia.common.logging import get_logger
from confidentia.config.settings import Settings, get_settings

logger = get_logger(__name__)

EXPERIMENT = "E1"
TENANT = "financial"
DATASET = "transaction"


def broker_running() -> bool:
    result = subprocess.run(
        ["pgrep", "-f", "kafka.Kafka"], capture_output=True, check=False
    )
    return result.returncode == 0


def run(
    records: int = 60_000,
    repeats: int = 3,
    warmup: int = 1,
    settings: Settings | None = None,
) -> list[ArmResult]:
    settings = settings or get_settings()
    if not broker_running():
        raise RuntimeError(
            "Kafka is not running. Start it with `bash scripts/kafka.sh start`; E1 "
            "measures the Kafka ingestion path and cannot substitute for it."
        )

    from pyspark.sql import functions as F

    from confidentia.crypto.broker import request_keys
    from confidentia.spark.session import apply_encryption, build_session
    from confidentia.streaming.consumer import payload_schema

    work = settings.data_root / "bench" / "e1"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    contract = get_contract(TENANT, DATASET)
    schema = payload_schema(contract)
    topic = settings.kafka.topic_transactions_raw
    landing = settings.lake_root / "landing" / TENANT / DATASET

    # with_kafka pulls the Structured Streaming connector, which is a Maven package
    # rather than part of pyspark.
    spark = build_session("bench-e1", settings=settings, with_kafka=True)
    # The batch arm reads the encrypted landing zone (ADR 0022), so this session
    # needs key material even though it writes nothing encrypted itself.
    apply_encryption(spark, request_keys(TENANT))

    try:

        def envelope(frame: Any, source_system: str) -> Any:
            """The same envelope both paths add, so the arms differ only in source."""
            return (
                frame.withColumn("_source_system", F.lit(source_system))
                .withColumn("_ingested_at", F.current_timestamp())
                .withColumn("_ingest_run_id", F.lit("bench-e1"))
            )

        def stream_arm() -> dict[str, Any]:
            target = work / "streaming"
            checkpoint = work / "streaming-checkpoint"
            # Cleared every repeat. A retained checkpoint makes the second run
            # find nothing to do and report an impossible throughput.
            shutil.rmtree(target, ignore_errors=True)
            shutil.rmtree(checkpoint, ignore_errors=True)

            raw = (
                spark.readStream.format("kafka")
                .option("kafka.bootstrap.servers", settings.kafka.bootstrap_servers)
                .option("subscribe", topic)
                .option("startingOffsets", "earliest")
                .option("maxOffsetsPerTrigger", 20_000)
                .load()
            )
            parsed = envelope(
                raw.select(
                    F.from_json(F.col("value").cast("string"), schema).alias("payload"),
                    F.col("topic"),
                    F.col("partition"),
                    F.col("offset"),
                ).select("payload.*", "topic", "partition", "offset"),
                "kafka",
            ).withColumn(
                "_source_file",
                F.concat_ws(":", F.col("topic"), F.col("partition"), F.col("offset")),
            ).drop("topic", "partition", "offset")

            query = (
                parsed.writeStream.format("delta")
                .outputMode("append")
                .option("checkpointLocation", str(checkpoint))
                .trigger(availableNow=True)
                .start(str(target))
            )
            query.awaitTermination()
            written = spark.read.format("delta").load(str(target)).count()
            return {"rows": written}

        # The contract's declared types, applied after the read rather than imposed
        # on it.
        #
        # Reading the landing Parquet with `.schema(payload_schema(contract))` fails:
        # the contract declares `amount` as string and the generator wrote a DOUBLE,
        # so Spark raises SchemaColumnConvertNotSupportedException. The Kafka arm
        # never sees this because its payload is JSON and `from_json` coerces
        # silently -- which is itself worth knowing, since it means the streaming
        # path accepts a type the batch path would reject.
        #
        # Casting after the read is what `bronze/_apply_contract` does, so both arms
        # end up with contract-typed columns and the comparison stays fair.
        payload_columns = [
            c for c in contract.columns
            if not c.name.startswith("_") and c.name != "ingest_date"
        ]

        def batch_arm() -> dict[str, Any]:
            target = work / "batch"
            shutil.rmtree(target, ignore_errors=True)
            raw = spark.read.parquet(str(landing)).limit(records)
            typed = raw.select(
                *[F.col(c.name).cast(c.dtype).alias(c.name) for c in payload_columns]
            )
            frame = envelope(typed, "file_drop").withColumn(
                "_source_file", F.input_file_name()
            )
            frame.write.format("delta").mode("overwrite").save(str(target))
            written = spark.read.format("delta").load(str(target)).count()
            return {"rows": written}

        arms = {"batch_file": batch_arm, "streaming_kafka": stream_arm}
        return run_arms(EXPERIMENT, arms, warmup=warmup, repeats=repeats)
    finally:
        spark.stop()


def main(records: int = 60_000, repeats: int = 3, persist: bool = True) -> None:
    results = run(records=records, repeats=repeats)
    print(table(results, baseline="batch_file"))

    batch = next((r for r in results if r.arm == "batch_file"), None)
    stream = next((r for r in results if r.arm == "streaming_kafka"), None)
    if batch and stream and batch.rows_per_second:
        print(
            f"\nbatch    {batch.rows_per_second:>10,.0f} rows/s over {batch.rows:,} rows"
            f"\nstreaming {stream.rows_per_second:>9,.0f} rows/s over {stream.rows:,} rows"
            f"\nratio     x{stream.p50 / batch.p50:.2f} (streaming vs batch wall clock)"
        )
    print(
        "\nMEASURED on this machine. Both arms write the same envelope into the same\n"
        "Delta format and differ only in source, which is what ADR 0014 set up."
    )

    if persist:
        record(results, metric="ingest_seconds", unit="s", label="MEASURED",
               notes=f"{records} records, availableNow trigger, checkpoint reset per repeat")
