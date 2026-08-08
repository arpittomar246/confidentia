"""CDC collector: PostgreSQL logical replication.

Consumes the write-ahead log through a replication slot rather than polling an
``updated_at`` column. The difference is not stylistic:

* polling **cannot see deletes** at all — the row is simply gone;
* polling **misses updates inside the polling interval** — two changes to one row
  between polls look like one;
* polling **cannot distinguish** an update that restored a column's old value from
  no update at all;
* the WAL carries the *before* image (via ``REPLICA IDENTITY FULL``), which is what
  SCD2 needs to close the previous version correctly.

Written directly against psycopg2's replication protocol rather than deploying
Debezium. Debezium is the right answer at scale, but it is a ~1 GB JVM service on a
7.4 GB host, and implementing the consumer means the slot lifecycle, LSN feedback
and restart semantics are understood rather than configured. See ADR 0009.

**Acknowledgement ordering is the load-bearing detail.** The slot's flush LSN is
confirmed only after the batch is durably written, so the replication connection is
deliberately held open across the write. Acknowledging on receipt — the obvious
implementation, and what the common `send_feedback`-inside-the-consumer example
does — would mean a crash between receipt and write loses those changes for good:
Postgres considers them consumed and frees the WAL. Holding the stream open costs a
connection; the alternative costs data.
"""

from __future__ import annotations

import json
import select
import time
from dataclasses import dataclass
from typing import Any

import pandas as pd
import psycopg2
import psycopg2.extras

from confidentia.common.hashing import sha256_json
from confidentia.common.logging import get_logger
from confidentia.config.settings import get_settings
from confidentia.ingestion.base import (
    IngestResult,
    TerminalError,
    add_envelope,
    write_landing,
)
from confidentia.store import db
from confidentia.store.repositories import (
    BatchDecision,
    BatchRegistry,
    RunType,
    WatermarkStore,
    track_run,
)

logger = get_logger(__name__)

SOURCE = "cdc"
DEFAULT_SLOT = "confidentia_patient_slot"
DEFAULT_PUBLICATION = "confidentia_patient_pub"
DEFAULT_TABLES = "public.patient"
OUTPUT_PLUGIN = "wal2json"


@dataclass
class ChangeEvent:
    operation: str          # INSERT | UPDATE | DELETE
    schema_name: str
    table_name: str
    lsn: str
    commit_ts: str | None
    values: dict[str, Any]
    old_values: dict[str, Any]

    def to_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "_cdc_operation": self.operation,
            "_cdc_lsn": self.lsn,
            "_cdc_commit_ts": self.commit_ts,
            "_cdc_table": f"{self.schema_name}.{self.table_name}",
        }
        record.update(self.values)
        # The before-image is stored as JSON rather than as a parallel set of
        # columns: SCD2 needs it only to determine which attributes actually moved,
        # and widening the schema to hold every column twice would double the
        # storage cost of the change stream.
        if self.old_values:
            record["_cdc_old"] = json.dumps(self.old_values, default=str)
        return record


class CdcCollector:
    def __init__(
        self,
        *,
        slot_name: str = DEFAULT_SLOT,
        publication: str = DEFAULT_PUBLICATION,
        tables: str = DEFAULT_TABLES,
        tenant: str = "healthcare",
        dataset: str = "patient_cdc",
        max_events: int = 100_000,
        idle_timeout_seconds: float = 3.0,
    ) -> None:
        self.settings = get_settings()
        self.slot_name = slot_name
        self.publication = publication
        self.tables = tables
        self.tenant = tenant
        self.dataset = dataset
        self.max_events = max_events
        self.idle_timeout = idle_timeout_seconds
        self.lake_root = self.settings.lake_root
        self.registry = BatchRegistry()
        self.watermarks = WatermarkStore()

    # ------------------------------------------------------------------

    def _connect(self):
        """Open a logical replication connection.

        A replication connection must be used *only* for replication commands.
        Running ordinary SQL on it first — even a harmless catalogue lookup —
        opens a transaction snapshot, and ``CREATE_REPLICATION_SLOT`` then fails
        with "must not be called inside a transaction". Slot introspection
        therefore goes through the normal pooled connection instead.

        ``autocommit`` is not set here because psycopg2 forces it on this
        connection class and the attribute is read-only.
        """
        return psycopg2.connect(
            self.settings.postgres.replication_dsn(),
            connection_factory=psycopg2.extras.LogicalReplicationConnection,
        )

    def ensure_slot(self) -> bool:
        """Create the replication slot if absent. Returns True if newly created.

        A slot is a persistent server-side resource that pins WAL until consumed,
        so an abandoned slot will eventually fill the source system's disk.
        Creation is explicit and its state is mirrored into the platform database
        rather than being an invisible side effect.
        """
        created = False
        if not self.slot_exists():
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.create_replication_slot(self.slot_name, output_plugin=OUTPUT_PLUGIN)
                created = True
                logger.info(
                    "replication_slot_created", slot=self.slot_name, plugin=OUTPUT_PLUGIN
                )
            finally:
                conn.close()

        db.execute(
            """
            INSERT INTO platform.cdc_slot (slot_name, publication, source_database, tenant)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (slot_name) DO UPDATE SET updated_at = now()
            """,
            (self.slot_name, self.publication, self.settings.postgres.oltp_database, self.tenant),
        )
        return created

    def drop_slot(self) -> None:
        if self.slot_exists():
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.drop_replication_slot(self.slot_name)
                logger.info("replication_slot_dropped", slot=self.slot_name)
            finally:
                conn.close()
        db.execute("DELETE FROM platform.cdc_slot WHERE slot_name = %s", (self.slot_name,))

    def slot_exists(self) -> bool:
        row = db.fetch_one(
            "SELECT 1 AS present FROM pg_replication_slots WHERE slot_name = %s",
            (self.slot_name,),
            database=self.settings.postgres.oltp_database,
        )
        return row is not None

    def slot_lag_bytes(self) -> int | None:
        row = db.fetch_one(
            """
            SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)::bigint AS lag
              FROM pg_replication_slots WHERE slot_name = %s
            """,
            (self.slot_name,),
            database=self.settings.postgres.oltp_database,
        )
        return int(row["lag"]) if row and row["lag"] is not None else None

    # ------------------------------------------------------------------

    def collect(self) -> IngestResult:
        """Drain, write, then acknowledge — in that order, on one open stream."""
        started = time.perf_counter()
        self.ensure_slot()

        with track_run(
            RunType.INGEST,
            tenant=self.tenant,
            dataset=self.dataset,
            source=SOURCE,
            metadata={"slot": self.slot_name, "plugin": OUTPUT_PLUGIN},
        ) as run:
            events: list[ChangeEvent] = []
            last_lsn_int: int | None = None
            last_lsn_text: str | None = None

            conn = self._connect()
            cur = conn.cursor()
            try:
                try:
                    cur.start_replication(
                        slot_name=self.slot_name,
                        decode=True,
                        options={
                            # format-version 2 emits one message per change rather
                            # than one buffered message per transaction, so memory
                            # is bounded by max_events instead of by the largest
                            # transaction in the WAL.
                            "format-version": "2",
                            "include-lsn": "true",
                            "include-timestamp": "true",
                            "add-tables": self.tables,
                        },
                    )
                except psycopg2.ProgrammingError as exc:
                    raise TerminalError(
                        f"could not start replication on {self.slot_name}: {exc}"
                    ) from exc

                deadline = [time.monotonic() + self.idle_timeout]

                def consume(message) -> bool:
                    """Record one WAL message. Returns False when the batch is full."""
                    nonlocal last_lsn_int, last_lsn_text
                    deadline[0] = time.monotonic() + self.idle_timeout
                    payload = json.loads(message.payload)
                    action = payload.get("action", "").upper()
                    if action in {"I", "U", "D"}:
                        events.append(
                            ChangeEvent(
                                operation={"I": "INSERT", "U": "UPDATE", "D": "DELETE"}[action],
                                schema_name=payload.get("schema", "public"),
                                table_name=payload.get("table", "unknown"),
                                lsn=payload.get("lsn") or str(message.data_start),
                                commit_ts=payload.get("timestamp"),
                                values=_columns_to_dict(payload.get("columns", [])),
                                old_values=_columns_to_dict(payload.get("identity", [])),
                            )
                        )
                    last_lsn_int = message.data_start
                    last_lsn_text = payload.get("lsn") or str(message.data_start)
                    # NOTE: no send_feedback here. See the module docstring.
                    return len(events) < self.max_events

                # A read_message/select loop rather than consume_stream: the latter
                # blocks forever on an idle slot, and this collector is a batch job
                # that must return once the WAL is drained.
                while time.monotonic() < deadline[0]:
                    message = cur.read_message()
                    if message is None:
                        # Nothing buffered. Wait briefly on the socket rather than
                        # spinning, then re-check the idle deadline.
                        select.select([cur], [], [], 0.5)
                        continue
                    if not consume(message):
                        break

                run.add(read=len(events))

                if not events:
                    logger.info("no_changes", slot=self.slot_name)
                    return IngestResult(
                        source=SOURCE,
                        tenant=self.tenant,
                        dataset=self.dataset,
                        decision=BatchDecision.NEW,
                        duration_seconds=time.perf_counter() - started,
                        detail={"events": 0, "slot": self.slot_name},
                    )

                frame = pd.DataFrame([e.to_record() for e in events])
                batch_digest = sha256_json(
                    {
                        "slot": self.slot_name,
                        "first_lsn": events[0].lsn,
                        "last_lsn": events[-1].lsn,
                        "count": len(events),
                    }
                )

                decision = self.registry.offer(
                    batch_digest=batch_digest,
                    source=SOURCE,
                    tenant=self.tenant,
                    dataset=self.dataset,
                    file_count=1,
                    row_count=len(frame),
                    bytes_=int(frame.memory_usage(deep=True).sum()),
                    run_id=run.run_id,
                    metadata={"first_lsn": events[0].lsn, "last_lsn": events[-1].lsn},
                )

                if decision is not BatchDecision.DUPLICATE:
                    enveloped = add_envelope(
                        frame,
                        run_id=run.run_id,
                        batch_digest=batch_digest,
                        source_file=f"slot:{self.slot_name}",
                        source_system="EHR-OLTP-WAL",
                    )
                    write_landing(
                        enveloped,
                        lake_root=self.lake_root,
                        tenant=self.tenant,
                        dataset=self.dataset,
                        batch_digest=batch_digest,
                        part=0,
                    )
                    run.add(written=len(enveloped))
                    self.registry.mark(batch_digest, "ingested", run.run_id)

                # Durable. Only now is it safe to let Postgres discard this WAL.
                if last_lsn_int is not None:
                    cur.send_feedback(flush_lsn=last_lsn_int, reply=True)

            finally:
                cur.close()
                conn.close()

            if last_lsn_text:
                self.watermarks.advance(
                    source=SOURCE,
                    tenant=self.tenant,
                    dataset=self.dataset,
                    kind="lsn",
                    value=last_lsn_text,
                    run_id=run.run_id,
                )
                db.execute(
                    """
                    UPDATE platform.cdc_slot
                       SET last_lsn = %s, last_commit_ts = now(),
                           events_consumed = events_consumed + %s, updated_at = now()
                     WHERE slot_name = %s
                    """,
                    (last_lsn_text, len(events), self.slot_name),
                )

            operations = pd.Series([e.operation for e in events]).value_counts().to_dict()
            run.note(operations=operations, last_lsn=last_lsn_text)

            return IngestResult(
                source=SOURCE,
                tenant=self.tenant,
                dataset=self.dataset,
                decision=decision,
                batch_digest=batch_digest,
                records_read=len(events),
                records_written=len(events) if decision is not BatchDecision.DUPLICATE else 0,
                output_path=self.lake_root / "landing" / self.tenant / self.dataset,
                duration_seconds=time.perf_counter() - started,
                detail={"operations": operations, "last_lsn": last_lsn_text},
            )


def _columns_to_dict(columns: list[dict[str, Any]]) -> dict[str, Any]:
    return {c["name"]: c.get("value") for c in columns if "name" in c}
