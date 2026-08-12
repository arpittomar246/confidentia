"""Replay generated transactions into Kafka at a controlled rate.

This stands in for the payment switch. It reads the same landing-zone files the
file collector reads, so the streaming and batch paths see **identical records**
— which is what makes the two comparable at all. A producer that generated its own
data would make every difference between the paths ambiguous.

Two things are deliberate:

**Rate limiting is real, not decorative.** Replaying 60,000 rows as fast as the
loop runs measures how quickly Python can call `produce()`, which is not a
question anyone has. Pacing the send lets the consumer's lag mean something and
lets the diurnal curve in the generated data survive into the topic.

**The key is the business key.** Kafka partitions by key, so keying on
`transaction_id` puts every copy of a record — including a redelivery — on the
same partition and in the same order. Keying on nothing would scatter them and
make ordering guarantees within a record impossible to reason about.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from confidentia.common.logging import get_logger
from confidentia.config.settings import get_settings

logger = get_logger(__name__)


@dataclass
class ProduceResult:
    topic: str
    sent: int = 0
    failed: int = 0
    bytes_sent: int = 0
    seconds: float = 0.0
    files: list[str] = field(default_factory=list)

    @property
    def per_second(self) -> float:
        return self.sent / self.seconds if self.seconds > 0 else 0.0

    def summary(self) -> str:
        return (
            f"{self.topic}: {self.sent:,} sent, {self.failed:,} failed, "
            f"{self.bytes_sent / 1_048_576:.1f} MB in {self.seconds:.1f}s "
            f"({self.per_second:,.0f}/s)"
        )


def _source_files(tenant: str, dataset: str) -> list[Path]:
    root = get_settings().data_root / "raw" / tenant / dataset
    return sorted(root.glob("part-*.parquet"))


def _serialise(record: dict[str, Any]) -> bytes:
    """JSON, because the topic is a contract with systems we do not control.

    Avro or Protobuf with a schema registry would be the production answer and is
    out of scope: it adds a registry service to a 3.9 GB box to demonstrate a
    property — schema evolution on the wire — that this project tests at the
    Bronze contract instead.
    """

    def encode(value: Any) -> Any:
        # pandas/numpy scalars and Timestamps are not JSON-serialisable, and the
        # default `str()` fallback would silently change a timestamp's format
        # halfway through a run if one chunk happened to be typed differently.
        if hasattr(value, "isoformat"):
            return value.isoformat()
        if hasattr(value, "item"):
            return value.item()
        return value

    return json.dumps(
        {k: encode(v) for k, v in record.items()}, separators=(",", ":")
    ).encode("utf-8")


def produce(
    tenant: str = "financial",
    dataset: str = "transaction",
    *,
    topic: str | None = None,
    limit: int | None = None,
    rate: int = 2000,
    key_field: str = "transaction_id",
) -> ProduceResult:
    """Replay landing files into a topic, paced at ``rate`` records per second."""
    import pandas as pd
    from confluent_kafka import Producer

    settings = get_settings()
    topic = topic or settings.kafka.topic_transactions_raw
    files = _source_files(tenant, dataset)
    if not files:
        raise FileNotFoundError(
            f"no landing files for {tenant}/{dataset}; run `generate {tenant}` first"
        )

    result = ProduceResult(topic=topic, files=[f.name for f in files])
    producer = Producer(
        {
            "bootstrap.servers": settings.kafka.bootstrap_servers,
            # Wait for the leader only. On a single broker `acks=all` is the same
            # guarantee at a worse latency, and claiming otherwise in a report
            # would be a replication claim this topology cannot make.
            "acks": 1,
            "linger.ms": 20,
            "batch.size": 64 * 1024,
            "compression.type": "snappy",
            "queue.buffering.max.messages": 200_000,
        }
    )

    def on_delivery(err: Any, _msg: Any) -> None:
        if err is not None:
            result.failed += 1
            logger.warning("produce_failed", error=str(err))

    started = time.perf_counter()
    budget_start = started
    in_window = 0

    for path in files:
        frame = pd.read_parquet(path)
        for record in frame.to_dict("records"):
            if limit is not None and result.sent >= limit:
                break
            payload = _serialise(record)
            key = str(record.get(key_field, "")).encode("utf-8")
            while True:
                try:
                    producer.produce(topic, key=key, value=payload, callback=on_delivery)
                    break
                except BufferError:
                    # The local queue is full: the broker is slower than the loop.
                    # Serving the delivery callbacks drains it. Dropping the record
                    # instead would make the throughput number a lie.
                    producer.poll(0.5)
            result.sent += 1
            result.bytes_sent += len(payload) + len(key)
            in_window += 1
            producer.poll(0)

            if rate > 0 and in_window >= rate:
                elapsed = time.perf_counter() - budget_start
                if elapsed < 1.0:
                    time.sleep(1.0 - elapsed)
                budget_start = time.perf_counter()
                in_window = 0

        if limit is not None and result.sent >= limit:
            break

    # Block until every message is acknowledged, so `sent` means delivered rather
    # than enqueued. Without this the reported throughput would include records
    # still sitting in the local buffer when the process exits.
    remaining = producer.flush(timeout=60)
    if remaining:
        result.failed += remaining
        logger.warning("produce_unflushed", remaining=remaining)

    result.seconds = time.perf_counter() - started
    logger.info(
        "produced",
        topic=topic, sent=result.sent, failed=result.failed,
        per_second=round(result.per_second, 1),
    )
    return result


def topic_status(topic: str | None = None) -> dict[str, Any]:
    """Partition count and the current high-water marks."""
    from confluent_kafka import Consumer, TopicPartition

    settings = get_settings()
    topic = topic or settings.kafka.topic_transactions_raw
    consumer = Consumer(
        {
            "bootstrap.servers": settings.kafka.bootstrap_servers,
            "group.id": f"{settings.kafka.consumer_group}-status",
            "enable.auto.commit": False,
        }
    )
    try:
        metadata = consumer.list_topics(topic, timeout=10)
        if topic not in metadata.topics or metadata.topics[topic].error is not None:
            return {"topic": topic, "exists": False, "partitions": 0, "messages": 0}

        partitions = sorted(metadata.topics[topic].partitions)
        total = 0
        offsets = {}
        for partition in partitions:
            low, high = consumer.get_watermark_offsets(
                TopicPartition(topic, partition), timeout=10
            )
            offsets[partition] = {"low": low, "high": high}
            total += high - low
        return {
            "topic": topic,
            "exists": True,
            "partitions": len(partitions),
            "messages": total,
            "offsets": offsets,
        }
    finally:
        consumer.close()
