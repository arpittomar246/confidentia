# Stage 3 — Ingestion

**Date:** 2026-08-08

## What was implemented

PostgreSQL 18.4 with `wal_level = logical`, a platform metadata store, and three
working collectors.

| Component | File | What it does |
|---|---|---|
| Metadata schema | `store/sql/001–003` | Run ledger, batch registry, watermarks, CDC slot state, reject log, OLTP source |
| Migration runner | `store/migrations.py` | Forward-only SQL with checksum drift detection |
| DB access | `store/db.py` | psycopg 3 pool, no ORM on the hot path |
| Repositories | `store/repositories.py` | `track_run`, `BatchRegistry`, `WatermarkStore`, reject log |
| Framework | `ingestion/base.py` | Envelope, retry policy, landing-zone writer |
| File collector | `ingestion/file_collector.py` | Checksum-verified, idempotent batch drops |
| REST collector | `ingestion/rest_collector.py` | Paged, retrying, watermark-resumed |
| Source API | `ingestion/source_api.py` | Real HTTP source with fault injection |
| CDC collector | `ingestion/cdc_collector.py` | Logical replication via `wal2json` |
| OLTP loader | `ingestion/oltp_loader.py` | Seeds and mutates the source-of-record database |

## Measured results

All figures observed on the development host, not projections.

**File collector**, 7 healthcare datasets:

| Behaviour | Result |
|---|---|
| First ingest | All datasets landed, checksums verified |
| Re-run over unchanged input | **0 records** — every batch recognised as a duplicate |
| One byte flipped mid-file | **Run failed**, batch refused, reject recorded with expected vs computed digest |

**REST collector**, 60,399 encounters with a 30% injected 503 rate:

| Metric | Value |
|---|---|
| Pages fetched | 13 |
| Requests retried | 3 (all recovered) |
| Records lost | 0 |
| Throughput | 20,513 records/s |
| Watermark stored | `2026-06-29T23:59:04+00:00` |
| Second run (incremental) | 1 page, **0 records** — nothing new |

**CDC collector**:

| Phase | Events | Result |
|---|---|---|
| Initial seed (5,000 patients) | 5,000 INSERT | Captured, 1,529 events/s |
| 900 mutations replayed | 900 UPDATE | Captured |
| Before-image | 17 columns | Fields that moved correctly identified as `last_name`, `updated_at` |

That last row is the point of the whole CDC decision: `REPLICA IDENTITY FULL` gives
Silver's SCD2 logic the previous row, so it can version only the attributes that
actually changed rather than on every touch.

## Guarantees, and how each is enforced

| Guarantee | Mechanism | Verified by |
|---|---|---|
| Same content never loads twice | `batch_digest` = SHA-256 over ordered file digests, primary key of the registry | Re-run ingested 0 records; integration test |
| Corruption is never silently accepted | Every file hashed before any record is read; mismatch is terminal | Bit-flip test failed the run |
| Corruption is never retried away | `TerminalError` bypasses the retry loop entirely | Unit test asserts exactly one attempt |
| No data lost on crash | Watermark and LSN advance only after durable write | Design; see ADR 0009 |
| Nothing dropped silently | `platform.ingest_reject` records everything refused pre-Bronze | Integration test |
| Failed runs are visible | `track_run` closes on exception with error type and message | Integration test |

## Bugs found and fixed

**`CREATE_REPLICATION_SLOT` failed inside a transaction.** The cause was not the
obvious one. `ReplicationConnection.autocommit` is read-only — psycopg2 already forces
it — and my first fix attempt tried to set it. The actual problem was that I ran a
plain `SELECT` against `pg_replication_slots` on the *replication* connection before
creating the slot, which opened a transaction snapshot. Slot introspection now goes
through the normal pooled connection; the replication connection is used only for
replication commands.

**Acknowledging too early.** My first CDC draft opened a second replication connection
just to send feedback, which would have restarted streaming. Restructured so the
stream stays open across the durable write, preserving the ack-after-write ordering
that makes crash recovery lossless.

**Control flow through an exception.** The drain loop signalled "batch full" by raising
a private exception. Replaced with a boolean return — the linter flagged it, and it was
right.

## Deferred, with reasons

- **Kafka producer** → Stage 9. I said in the Stage 2 summary this would land here.
  It has not, and the reason is that standing up a broker now buys nothing: no consumer
  exists until the streaming layer does. The transaction stream is ingested through the
  file collector in the meantime, so the financial tenant is not blocked.
- **Object-store collector** → Stage 4, with MinIO. The storage abstraction exists;
  only the local filesystem backend is wired.
- **Snapshot-then-stream CDC.** The collector reads whatever the slot holds rather than
  taking a consistent snapshot and transitioning to streaming. Equivalent in effect
  here because the seed load produces INSERT events, but not equivalent in mechanism.

## How to verify

```bash
wsl -d Ubuntu -- bash -lc "cd ~/confidentia && source .venv/bin/activate && python -m confidentia.cli ingest file healthcare && python -m confidentia.cli runs"
```

## State

55 unit tests, 12 integration tests, lint clean. Nothing claimed as deployed; TEE
backend remains `sim`.

## Next: Stage 4 — Bronze

Landing → Delta with Parquet Modular Encryption applied through the Hadoop
Configuration (ADR 0007), partitioned by ingest date, plus the write-time `PARE`
assertion that ADR 0007 made mandatory after Delta was caught silently writing
plaintext. This is where Spark re-enters and where the encryption work from Stage 1
becomes part of the pipeline rather than a spike.
