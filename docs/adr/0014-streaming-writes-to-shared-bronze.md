# ADR 0014 — Streaming writes into the same Bronze tables as batch

- **Status:** Accepted (decided ahead of Stage 9; revisit if the first consumer disagrees)
- **Date:** 2026-08-10
- **Stage:** 9 (Streaming) — decided before implementation

## Context

Stage 9 streams the financial tenant through Kafka. The question that has to be
settled before a consumer is written, because retrofitting it means rewriting the
checkpointing, is whether the streaming consumer writes into the **same**
`lake/bronze/financial/transaction` table that the batch collectors write, or into
a separate `bronze_stream/` table.

Separate tables are the easy answer. They avoid concurrent writers to one Delta
table, avoid a micro-batch small-file problem in a table batch also uses, and make
the throughput comparison in experiment E1 trivially clean.

## Decision

**Shared Bronze.** One `transaction` table, written by both paths.

The research question is what it costs to run a *full medallion platform* under a
confidentiality boundary. A platform where the streaming path writes somewhere
else is two pipelines that happen to share a repository: Silver would have to union
two tables, the deduplication that Stage 5 exists to demonstrate would not span
them, and "where should the boundary be drawn" becomes a question about two
separate systems rather than one.

The problems separate tables avoid are also the problems worth having:

**Concurrent writers.** Delta's optimistic concurrency is a real property of the
table format this project chose, and a streaming append racing a batch append is
the case that exercises it. If it does not hold, that is a finding about Delta
under this configuration, which is more interesting than not testing it.

**Small files.** Stage 5 already hit and fixed a small-file explosion in Silver, so
the platform has the compaction machinery and the scar tissue. A 10-second trigger
writing 8,640 files a day per partition is the same problem arriving from a new
direction, and handling it from the first commit rather than after the table has
100k files is cheap.

**Bronze already tolerates multiple sources.** `_source_system` distinguishes
`file_drop`, `rest_api` and `EHR-OLTP-WAL`, and Stage 7c made Bronze uniqueness
*per source system* precisely so that one source redelivering is not confused with
a duplicate. A fourth source called `kafka` needs no new concept.

## Consequences

- The consumer must set `_source_system` and go through the same ingest envelope as
  the collectors. It is a source, not a special case.
- **Encryption applies identically.** The streaming writer needs
  `column_keys_for_frame` and the `PARE` assertion after write. Delta silently
  writes plaintext when the properties are set as DataFrameWriter options rather
  than on the Hadoop Configuration (ADR 0007), and a streaming writer is a fresh
  place to make that mistake.
- **Exactly-once means the checkpoint and the Delta commit must agree.** Stage 3's
  CDC collector already establishes the pattern — acknowledge only after a durable
  write (ADR 0009) — and it must not be weakened for throughput.
- Experiment E1 has to separate batch from streaming rows by `_source_system`
  rather than by table. That is a `WHERE` clause, not a design problem.
- `streaming` is already in `MEASURED_PACKAGES`, so editing a consumer revokes key
  release until `keys approve` runs.

## What would overturn this

If Delta's optimistic concurrency turns out to make a shared table unworkable at
the trigger interval the experiments need — repeated commit conflicts rather than
occasional retries — then separate tables become the honest answer and this ADR
should be superseded rather than worked around. That is a measurement, so it will
be visible; it should be reported either way, because "we had to split the tables"
is itself a result about running a streaming medallion under this configuration.
