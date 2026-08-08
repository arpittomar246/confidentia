# ADR 0009 — CDC implementation and ingestion guarantees

- **Status:** Accepted
- **Date:** 2026-08-08
- **Stage:** 3 (ingestion)

## Decision 1 — Log-based CDC, not polling

Patient changes are captured from PostgreSQL's write-ahead log through a logical
replication slot, not by querying `WHERE updated_at > :watermark`.

Polling is the obvious approach and it is wrong here for four separate reasons:

| Polling cannot | Consequence for this platform |
|---|---|
| See deletes | The row is simply absent next poll; SCD2 can never close the record |
| See intra-interval updates | Two changes between polls collapse to one; the intermediate version is lost forever |
| Distinguish a no-op update | A column set back to its previous value is indistinguishable from no change |
| Provide the before-image | SCD2 cannot determine *which* attributes moved, so it versions on every touch |

The last point is the decisive one. `REPLICA IDENTITY FULL` makes the WAL carry the
complete previous row, which is exactly what a Type-2 dimension needs. It costs WAL
volume, and that cost is accepted deliberately.

Verified: a captured UPDATE carries a 17-column before-image, and diffing it against
the new values correctly identifies `last_name` and `updated_at` as the fields that
moved.

## Decision 2 — Implement the consumer, do not deploy Debezium

Debezium is the industry default and would be the right answer at scale.

**Why not here.** It is a ~1 GB JVM service (Kafka Connect) on a 7.4 GB development
host, and it would also require Kafka to exist before Stage 9. More importantly,
configuring Debezium teaches configuration; implementing the consumer forces the slot
lifecycle, LSN feedback and restart semantics to be understood — which is what a viva
will probe.

**What it costs.** No schema-registry integration, no snapshot-then-stream mode, no
multi-table transaction grouping, single-consumer only. All acceptable for one
replicated table; none of it would be acceptable in production, and the report says so.

`wal2json` is used as the output plugin rather than decoding `pgoutput`'s binary
protocol by hand. `pgoutput` is built in but would mean several hundred lines of
protocol decoding for no additional insight.

## Decision 3 — Acknowledge only after the batch is durable

The replication connection is deliberately held open across the write, and
`send_feedback` is called only once the landing files exist.

The obvious implementation — and the one in most `psycopg2` examples — acknowledges
inside the consumer callback, per message. That is wrong: a crash between receipt and
write loses those changes **permanently**, because Postgres considers them consumed
and frees the WAL. Holding the stream open costs one connection; acknowledging early
costs data.

This yields **at-least-once** delivery. Combined with the content-addressed batch
registry, the effective end-to-end property is **exactly-once ingestion**: a replayed
batch is recognised and skipped.

## Decision 4 — A batch is identified by content, not by filename

`batch_digest` is SHA-256 over the ordered list of per-file digests, and it is the
primary key of `platform.ingest_batch`.

Source systems redeliver, rename and replay. Keying on filename means a redelivered
file loads twice. Redeliveries are **counted** (`seen_count`) rather than silently
discarded, because a rising redelivery rate is a real signal about the upstream
system.

Verified: re-running the file collector over unchanged input ingested 0 records across
all 7 healthcare datasets.

## Decision 5 — Retryable and terminal errors are different things

| Classification | Examples | Behaviour |
|---|---|---|
| `RetryableError` | 408, 425, 429, 5xx, timeouts, connection resets | Exponential backoff with full jitter |
| `TerminalError` | checksum mismatch, size mismatch, 4xx, malformed manifest, unsupported format | Fail immediately, no retry |

**A checksum mismatch must never be retried.** A retry that eventually "succeeds"
would present corrupted data as healthy — strictly worse than failing the run.

Full jitter (`sleep = uniform(0, backoff)`) rather than fixed backoff: when a source
recovers, unjittered clients reconverge on the same instant and knock it over again.

`Retry-After` is honoured when the server sets it. Guessing a backoff when the server
has already given the answer is how clients get banned.

Verified: injecting a 30% 503 rate produced 3 retries across 13 pages, all recovered,
zero records lost.

## Decision 6 — Land first, then Bronze

Ingestion writes to `lake/landing/`, not directly to Bronze.

*Landing* is exactly what arrived — byte-verifiable against the source's own
checksums, nothing cast or cleaned — plus a provenance envelope. *Bronze* (Stage 4) is
the typed, partitioned, encrypted Delta table.

One hop would be simpler. Two hops mean a schema-handling bug or an encryption defect
cannot destroy the only copy of what was received, and "what did the source actually
send us on Tuesday" stays answerable.

## Decision 7 — Watermarks are text with a kind discriminator

The four collectors resume on genuinely different things: a timestamp, a Postgres LSN,
a Kafka offset, a monotonic sequence. A `TIMESTAMPTZ` column would force three of the
four to lie about what they store.

`previous_value` is retained so a bad advance can be rolled back without
reconstructing it from logs.

The watermark advances **only after the batch is durably written**. Advancing per page
would mean a crash mid-batch skips the unwritten pages forever.

## Decision 8 — Plain SQL migrations, not Alembic

Forward-only `.sql` files applied in filename order, tracked with a checksum.

The schema is owned entirely by this project, its history is linear, and the ingestion
hot path stays free of an ORM. A reviewer can confirm what will run by reading the
`.sql` files. The checksum matters: editing an already-applied migration means
deployed databases no longer match the repository, and the runner refuses loudly
rather than diverging silently.

Alembic would be the right call once a second deployment target exists.

## What is not implemented

- **Kafka producer.** Deferred to Stage 9 with the rest of the streaming layer rather
  than standing up a broker for a component that has nothing to consume it yet.
- **Object-store collector.** The storage abstraction exists but MinIO is not
  installed; the local filesystem is the only backend so far.
- **Snapshot-then-stream.** The CDC collector reads whatever the slot holds; it does
  not take a consistent initial snapshot and then transition to streaming. For this
  project the OLTP seed load produces INSERT events in the WAL, which is equivalent
  in effect but not in mechanism.
