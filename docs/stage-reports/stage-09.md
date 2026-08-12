# Stage 9 — Streaming: Kafka into the same Bronze table

**Date:** 2026-08-12

A Structured Streaming consumer writes the financial tenant's transactions into
**the same** `lake/bronze/financial/transaction` the file and REST collectors
write (ADR 0014). It is a fourth source, not a second pipeline.

Bronze now holds 120,409 rows from two delivery mechanisms in one table:

| `_source_system` | rows | delivered by |
|---|---|---|
| `financial` | 60,409 | file drop (batch) |
| `kafka` | 60,000 | Structured Streaming |

## What was built

| Component | Purpose |
|---|---|
| `streaming/producer.py` | Replays landing files into Kafka, rate-limited, keyed on the business key |
| `streaming/consumer.py` | Structured Streaming → shared Bronze, via `foreachBatch` |
| `streaming/watermark.py` | Event-time policy, in plain Python so it is testable |
| `orchestration/dags/confidentia_streaming.py` | Scheduled bounded drain |
| `scripts/install_kafka.sh`, `kafka.sh` | Native KRaft broker, 512 MB heap |
| `scripts/verify_streaming.sh` | Proves the three claims below |
| `cli.py` | `stream produce | consume | status` |
| `tests/unit/test_streaming.py` | 13 tests, no broker required |

## The three claims, and how they are checked

`verify_streaming.sh` asserts all three. The third is the one worth having — the
first two would survive a consumer that quietly appended the same rows every run.

**1. One Bronze table, not two.** Verified by grouping the table on
`_source_system` and requiring both values to be present. A platform where
streaming writes somewhere else is two pipelines sharing a repository, and Silver
would have to union them.

**2. Streamed files are encrypted.** The `PARE` magic is asserted after every
micro-batch, the same check the batch writer makes. This is repeated rather than
assumed because a streaming writer is a fresh place to make ADR 0007's mistake,
where Delta writes plaintext with no error and no warning.

```
part-00000-6cf2744e-...c000.snappy.parquet: PARE (encrypted footer)
```

**3. Re-consuming does not duplicate.** Row count before and after a second
`stream consume`: **80,409 → 80,409**. At-least-once delivery plus a retry is a
duplicated record, and the only thing that would notice is a row count nobody
checks.

Exactly-once here is Delta's idempotent write, not a hope. Each micro-batch is
written with `txnAppId`/`txnVersion`, so a batch replayed after a crash — the
checkpoint says "in progress" but the commit already landed — is refused by the
transaction log rather than appended twice. That is what makes the scheduled DAG
safe to kill at any point.

## Measurements

| | |
|---|---|
| Producer, rate-limited | 4,694 rec/s (capped at 5,000) |
| Producer, unthrottled | **21,392 rec/s** — 40,000 records, 19.4 MB, 1.9 s |
| Consumer, end to end | 1,226 rec/s — 40,000 in 5 micro-batches, 32.6 s |
| Consumer, steady state | **~3,000 rec/s** per micro-batch |

**The two consumer numbers differ for a reason worth stating.** The end-to-end
figure includes roughly 15 seconds of SparkSession construction and Kafka
connector resolution, which is paid once per invocation and dominates a short run.
Micro-batch timestamps give the steady-state rate: batches of ~9,997 records
committed 2.9–5.7 s apart. Quoting 1,226 rec/s as the platform's streaming
throughput would understate it by more than half; quoting 3,000 without the
startup cost would ignore what a scheduled drain actually pays.

`maxOffsetsPerTrigger = 10,000` bounded the batches as intended — 9,997 / 9,997 /
9,996 / 9,998 / 12 across six partitions.

## Design decisions

**Kafka runs natively in WSL, in KRaft mode.** ADR 0001 rules out Docker as the
development path, and KRaft removes the second JVM ZooKeeper would need. The heap
is capped at 512 MB, matching the limit `docker-compose.yml` already declares, and
the version matches the pinned image — the local and portable paths should not run
different brokers.

**`foreachBatch`, not `writeStream.format("delta")`.** The batch path already
knows how to reconcile a frame against the contract, derive the column-to-key
mapping and assert encryption. `foreachBatch` hands each micro-batch over as an
ordinary DataFrame so all of that is reused rather than reimplemented — and
reimplementing the PME wiring is exactly how a streaming writer ends up writing
plaintext.

**The producer replays landing files rather than generating data.** The streaming
and batch paths therefore see *identical records*, which is what makes them
comparable at all. A producer with its own generator would make every difference
between the two ambiguous.

**JSON on the wire, not Avro.** A schema registry would be the production answer
and would add a service to a 3.9 GB box to demonstrate schema evolution, which
this project already tests at the Bronze contract. The payload schema is derived
from the contract and deliberately excludes the ingest envelope: the producer is a
source, and a topic carrying `_ingest_run_id` would be fiction.

**The DAG is a bounded drain, not a long-running consumer.** A continuous query is
not a DAG task — Airflow would show one task running for days, its retries would
restart an already-checkpointed query, and `execution_timeout` would kill a healthy
stream. The scheduled drain uses `availableNow`: process what is there, commit,
exit. Latency is floored at the 15-minute interval, which is the honest trade for a
platform whose other stages are batch. `stream consume --follow` remains available
outside the orchestrator.

## The watermark, and what it deliberately does not solve

`WatermarkPolicy` lives in plain Python rather than only as a `withWatermark`
literal, because Structured Streaming drops records beyond the watermark from
stateful operations **silently** — a boundary set too tight loses real records and
reports nothing, surfacing much later as a row count that does not match the topic.

The tests pin the financial tenant's central problem as an assertion: a chargeback
confirming fraud 1 to 45 days after the transaction is `DROPPED` by any watermark
that keeps state bounded. **That is not a watermark to widen.** It is the case that
belongs in the batch restatement path, which Stage 6 already built and Stage 8
already schedules. Streaming handles the fresh path; late confirmation stays batch.

## Known limitations

- **Only the financial tenant streams.** Healthcare arrives by file and CDC, which
  is realistic — an EHR is not a message bus — but it means the streaming path is
  demonstrated on one dataset.
- **No Silver streaming.** The consumer stops at Bronze; Silver remains batch, so
  end-to-end streaming latency is not yet measurable. Streaming Silver would need
  stateful deduplication across sources, which is a materially harder problem than
  the append this stage performs.
- **`acks=1`, not `acks=all`.** On a single broker they are the same guarantee at
  different latencies, and claiming otherwise would be a replication claim this
  topology cannot make.
- **Throughput is measured on one machine with a 2 GB driver and a 512 MB broker.**
  The numbers characterise this configuration, not Kafka or Spark.
- **The Kafka log lives under the data root**, so `reset_data.sh` clears it. That is
  deliberate: resetting the lake without the topic would leave the consumer's
  checkpoint holding offsets into data that no longer corresponds to Bronze.

## Reproducing

```bash
bash scripts/install_kafka.sh          # once
bash scripts/kafka.sh start
export CONFIDENTIA_KEYSTORE_PASSPHRASE="dev-passphrase-not-for-real-use"

python -m confidentia.cli stream produce financial transaction --limit 40000 --rate 0
python -m confidentia.cli stream consume financial transaction
python -m confidentia.cli stream status

bash scripts/verify_streaming.sh
```
