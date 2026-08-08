"""Repositories over the platform metadata store.

Three concerns, each with one owner:

``track_run``      -- every execution is recorded, including the ones that crash.
``BatchRegistry``  -- content-addressed idempotency: the same bytes never load twice.
``WatermarkStore`` -- where to resume from, with the previous value retained so a
                      bad advance can be rolled back.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from psycopg.types.json import Jsonb

from confidentia.common.logging import get_logger, log_context
from confidentia.store import db

logger = get_logger(__name__)


class RunType(StrEnum):
    INGEST = "ingest"
    TRANSFORM = "transform"
    QUALITY = "quality"
    STREAM = "stream"
    BENCHMARK = "benchmark"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class BatchDecision(StrEnum):
    NEW = "new"              # never seen; ingest it
    DUPLICATE = "duplicate"  # identical content already ingested; skip
    RETRY = "retry"          # seen before but the previous attempt failed


@dataclass
class RunHandle:
    run_id: uuid.UUID
    run_type: RunType
    tenant: str
    dataset: str
    records_read: int = 0
    records_written: int = 0
    records_rejected: int = 0
    bytes_read: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def add(
        self,
        *,
        read: int = 0,
        written: int = 0,
        rejected: int = 0,
        bytes_read: int = 0,
    ) -> None:
        self.records_read += read
        self.records_written += written
        self.records_rejected += rejected
        self.bytes_read += bytes_read

    def note(self, **values: Any) -> None:
        self.metadata.update(values)


@contextmanager
def track_run(
    run_type: RunType,
    *,
    tenant: str,
    dataset: str,
    source: str | None = None,
    attempt: int = 1,
    metadata: dict[str, Any] | None = None,
) -> Iterator[RunHandle]:
    """Record an execution, whatever happens to it.

    A run that raises is closed as ``failed`` with the exception type and message,
    never left dangling in ``running``. A dashboard that shows perpetually-running
    ghost runs is worse than useless, because it hides the real ones.
    """
    handle = RunHandle(
        run_id=uuid.uuid4(),
        run_type=run_type,
        tenant=tenant,
        dataset=dataset,
        metadata=dict(metadata or {}),
    )
    db.execute(
        """
        INSERT INTO platform.run
            (run_id, run_type, source, tenant, dataset, status, attempt, metadata)
        VALUES (%s, %s, %s, %s, %s, 'running', %s, %s)
        """,
        (handle.run_id, run_type.value, source, tenant, dataset, attempt, Jsonb(handle.metadata)),
    )

    with log_context(run_id=str(handle.run_id), dataset=dataset, tenant=tenant):
        logger.info("run_started", run_type=run_type.value, source=source)
        try:
            yield handle
        except Exception as exc:
            _finish(handle, RunStatus.FAILED, type(exc).__name__, str(exc)[:2000])
            logger.error("run_failed", error_type=type(exc).__name__, error=str(exc)[:500])
            raise
        else:
            _finish(handle, RunStatus.SUCCEEDED, None, None)
            logger.info(
                "run_succeeded",
                records_read=handle.records_read,
                records_written=handle.records_written,
                records_rejected=handle.records_rejected,
            )


def _finish(
    handle: RunHandle, status: RunStatus, error_type: str | None, error_message: str | None
) -> None:
    db.execute(
        """
        UPDATE platform.run
           SET status = %s,
               finished_at = now(),
               records_read = %s,
               records_written = %s,
               records_rejected = %s,
               bytes_read = %s,
               error_type = %s,
               error_message = %s,
               metadata = %s
         WHERE run_id = %s
        """,
        (
            status.value,
            handle.records_read,
            handle.records_written,
            handle.records_rejected,
            handle.bytes_read,
            error_type,
            error_message,
            Jsonb(handle.metadata),
            handle.run_id,
        ),
    )


class BatchRegistry:
    """Content-addressed idempotency.

    A batch is identified by the SHA-256 over its ordered file digests. Source
    systems redeliver and rename; keying on filename would load the same data
    twice. Redeliveries are counted rather than discarded, because a rising
    redelivery rate is a real signal about the upstream system.
    """

    def offer(
        self,
        *,
        batch_digest: str,
        source: str,
        tenant: str,
        dataset: str,
        file_count: int,
        row_count: int,
        bytes_: int,
        run_id: uuid.UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BatchDecision:
        """Claim a batch for ingestion, or learn that it is already done.

        Single statement so that two collectors racing on the same batch cannot
        both decide it is new: the first INSERT wins, the second takes the
        conflict branch.
        """
        row = db.fetch_one(
            """
            INSERT INTO platform.ingest_batch
                (batch_digest, source, tenant, dataset, file_count, row_count, bytes,
                 status, run_id, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s)
            ON CONFLICT (batch_digest) DO UPDATE
                SET seen_count = platform.ingest_batch.seen_count + 1
            RETURNING status, seen_count,
                      (xmax = 0) AS inserted
            """,
            (
                batch_digest,
                source,
                tenant,
                dataset,
                file_count,
                row_count,
                bytes_,
                run_id,
                Jsonb(metadata or {}),
            ),
        )
        assert row is not None

        if row["inserted"]:
            return BatchDecision.NEW
        if row["status"] == "ingested":
            logger.info(
                "batch_already_ingested",
                batch_digest=batch_digest[:16],
                dataset=dataset,
                seen_count=row["seen_count"],
            )
            return BatchDecision.DUPLICATE
        # Previously seen but not completed: a failed or interrupted attempt.
        logger.info(
            "batch_retry",
            batch_digest=batch_digest[:16],
            dataset=dataset,
            previous_status=row["status"],
        )
        return BatchDecision.RETRY

    def record_files(self, batch_digest: str, files: list[dict[str, Any]]) -> None:
        with db.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO platform.ingest_file
                    (batch_digest, relative_path, file_digest, row_count, bytes, verified)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (batch_digest, relative_path) DO UPDATE
                    SET verified = EXCLUDED.verified,
                        file_digest = EXCLUDED.file_digest
                """,
                [
                    (
                        batch_digest,
                        f["relative_path"],
                        f["file_digest"],
                        f["row_count"],
                        f["bytes"],
                        f.get("verified", False),
                    )
                    for f in files
                ],
            )

    def mark(self, batch_digest: str, status: str, run_id: uuid.UUID | None = None) -> None:
        db.execute(
            """
            UPDATE platform.ingest_batch
               SET status = %s,
                   ingested_at = CASE WHEN %s = 'ingested' THEN now() ELSE ingested_at END,
                   run_id = COALESCE(%s, run_id)
             WHERE batch_digest = %s
            """,
            (status, status, run_id, batch_digest),
        )

    def get(self, batch_digest: str) -> dict[str, Any] | None:
        return db.fetch_one(
            "SELECT * FROM platform.ingest_batch WHERE batch_digest = %s", (batch_digest,)
        )


class WatermarkStore:
    """Resumption points, stored as text with a kind discriminator.

    The collectors resume on genuinely different things — a timestamp, a Postgres
    LSN, a Kafka offset, a monotonic sequence. A timestamp column would force three
    of the four to lie about what they store.
    """

    def get(self, source: str, tenant: str, dataset: str) -> dict[str, Any] | None:
        return db.fetch_one(
            """
            SELECT watermark_kind, watermark_value, previous_value, updated_at
              FROM platform.watermark
             WHERE source = %s AND tenant = %s AND dataset = %s
            """,
            (source, tenant, dataset),
        )

    def value(self, source: str, tenant: str, dataset: str, default: str | None = None) -> str | None:
        row = self.get(source, tenant, dataset)
        return row["watermark_value"] if row else default

    def advance(
        self,
        *,
        source: str,
        tenant: str,
        dataset: str,
        kind: str,
        value: str,
        run_id: uuid.UUID | None = None,
    ) -> None:
        db.execute(
            """
            INSERT INTO platform.watermark
                (source, tenant, dataset, watermark_kind, watermark_value, run_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (source, tenant, dataset) DO UPDATE
                SET previous_value  = platform.watermark.watermark_value,
                    watermark_value = EXCLUDED.watermark_value,
                    watermark_kind  = EXCLUDED.watermark_kind,
                    updated_at      = now(),
                    run_id          = EXCLUDED.run_id
            """,
            (source, tenant, dataset, kind, value, run_id),
        )
        logger.debug("watermark_advanced", source=source, dataset=dataset, value=value[:64])

    def rollback(self, source: str, tenant: str, dataset: str) -> bool:
        """Restore the previous watermark after a bad advance."""
        affected = db.execute(
            """
            UPDATE platform.watermark
               SET watermark_value = previous_value,
                   previous_value  = NULL,
                   updated_at      = now()
             WHERE source = %s AND tenant = %s AND dataset = %s
               AND previous_value IS NOT NULL
            """,
            (source, tenant, dataset),
        )
        return affected > 0


def record_reject(
    *,
    run_id: uuid.UUID,
    source: str,
    tenant: str,
    dataset: str,
    reason_code: str,
    detail: str | None = None,
    relative_path: str | None = None,
    raw_excerpt: str | None = None,
) -> None:
    """Nothing is dropped silently, at any layer."""
    db.execute(
        """
        INSERT INTO platform.ingest_reject
            (run_id, source, tenant, dataset, relative_path, reason_code, detail, raw_excerpt)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (run_id, source, tenant, dataset, relative_path, reason_code, detail, raw_excerpt),
    )
