# Experiments E1–E8

## Status

| | Experiment | State |
|---|---|---|
| **E1** | Streaming against batch throughput on the shared Bronze table | **designed, not run** — needs a Kafka broker up |
| **E2** | Partition sizing: write time, file size, the EPC budget | **MEASURED** |
| **E3** | Incremental against full reprocessing | **blocked** — the incremental arm does not exist |
| **E4** | What Parquet Modular Encryption costs | **MEASURED** |
| **E5** | — | **never defined** |
| **E6** | — | **never defined** |
| **E7** | — | **never defined** |
| **E8** | Replay under a drifted schema | **designed, not run** |

E1–E4 and E8 are recovered from ADRs 0007, 0008, 0011 and 0014, which reference
them by number. **E5, E6 and E7 appear nowhere in the repository** — not in an ADR,
a stage report, a docstring or a comment. They were numbered in the original plan
and never specified, and inventing three experiments now and presenting them as
part of that plan would be dishonest. Candidates are proposed at the end.

Every result below is recorded in `platform.benchmark_result` with the machine
configuration that produced it. `confidentia bench results` prints them.

---

## E4 — what Parquet Modular Encryption costs **[MEASURED]**

3,000,000 rows, six columns, Delta, one warm-up and five interleaved repeats.

| Arm | p50 | p90 | rows/s | vs plaintext | footer |
|---|---|---|---|---|---|
| plaintext | 3.45 s | 3.51 s | 869,913 | — | `PAR1` |
| pme | 3.59 s | 4.77 s | 835,234 | **×1.042** | `PARE` |

**Write overhead ×1.042. Storage overhead ×1.001** (249.0 MB → 249.2 MB).

The storage figure confirms ADR 0007's estimate of about 1%. Encryption costs
roughly 4% of write time on this data — which is the number the project has been
asserting was "small" without measuring since Stage 1.

### The measurement inverted at small scale, and that is the finding

Run first at 200,000 rows, E4 reported **×0.967 — encryption 3.4% *faster* than
plaintext.** That is impossible, and it is the identical artefact the Stage 1 spike
produced and ADR 0007 flagged as "an artifact of JVM warm-up across arms".

It is not warm-up. This harness warms up, repeats five times and interleaves the
arms, and it still inverted. The cause is scale: at 200,000 rows the write takes
0.85 s, which is dominated by Spark's fixed per-job cost, and the encryption signal
is smaller than the noise on that fixed cost.

At 3,000,000 rows the write takes 3.45 s and the ordering becomes physically
plausible. **Any encryption benchmark on this stack below roughly a million rows is
measuring Spark's job overhead, not encryption**, and `confidentia bench e4` says so
in its help text.

### The arms are verified, not assumed

Each arm's Parquet footer magic is read after the run and must be `PAR1` for
plaintext and `PARE` for encrypted, or the experiment raises. Stage 1 shipped a
false pass where an "encrypted" arm was silently writing plaintext; a benchmark that
cannot tell its arms apart measures nothing.

### Only two arms

ADR 0007 names `envelope` as "the baseline comparison arm for experiment E4". **That
mode does not exist.** `crypto.column_encryption` is declared in settings with
values `pme | envelope | none`, and nothing in `pipelines/` reads it — the Bronze
writer always encrypts and asserts `PARE` afterwards. Reporting a third arm would
mean implementing envelope encryption first.

---

## E2 — partition sizing **[MEASURED]**

3,000,000 rows with a Zipf-skewed key, one warm-up and three interleaved repeats.

| Partitions | p50 | Files | Mean file | Rows/partition | Total on disk |
|---|---|---|---|---|---|
| 4 | **2.76 s** | 4 | 9,279 KB | 750,000 | **37.1 MB** |
| 16 | 3.09 s | 16 | 2,366 KB | 187,500 | 37.9 MB |
| 90 | 4.70 s | 90 | 468 KB | 33,333 | 42.1 MB |
| 360 | 10.43 s | 360 | 138 KB | 8,333 | 49.5 MB |
| 720 | **17.36 s** | 720 | 72 KB | 4,166 | **51.8 MB** |

**A 6.3× write-time penalty and 40% storage inflation** across the range. The same
rows, the same schema, the same content — only the output partition count differs.

This quantifies the Stage 5 bug rather than merely recording that it was fixed:
Silver was writing 1,980 files averaging 37 KB, which is past the right-hand end of
this table. The `repartition("event_date")` that fixed it moved it several rows up.

### It sweeps partition count because the setting it was supposed to sweep does nothing

`spark/session.py` sets `spark.databricks.delta.optimize.maxFileSize` from
`settings.spark.target_file_size_mb`, with the comment *"Experiment E2 sweeps this
against the enclave memory budget"*.

Sweeping it produced a **completely flat** result: 90 files of 384 KB at every value
from 8 MB to 512 MB. That property governs `OPTIMIZE`, the compaction command, and
**this pipeline never runs OPTIMIZE**. Write-time file count is decided by the
number of Spark output partitions and nothing else.

So a documented tuning knob has no effect on the path it is applied to. The setting
is not wrong — it would matter if compaction were run — but the comment promising E2
would sweep it was.

### Against C5's EPC threshold **[MODELLED]**

C5 computes 867,787 rows per partition for a 128 MB EPC at 75% utilisation and the
measured 116 B/row. Every arm above fits, including 750,000 rows at 4 partitions —
consistent with C5's conclusion that at this project's volume the EPC is not the
constraint.

The EPC column is MODELLED. There is no enclave on this machine.

---

## E3 — incremental against full reprocessing **[BLOCKED]**

ADR 0008 and ADR 0011 both describe E3 as comparing incremental against full
reprocessing of the same data, and ADR 0011's tie-breaking rule for deduplication
exists specifically so that E3 would be reproducible.

**The incremental arm does not exist.** Stage 5's report says "Silver is
full-refresh. Incremental MERGE is what experiment E3 compares against, and it is
not built yet." Stage 6's says the same of Gold: "incremental recomputation of only
dirty partitions is the obvious next step and is what experiment E3 will measure."

Both are still true. `platform.mart_partition_state` tracks which partitions are
dirty and `gold/writer.py` marks them, but the rebuild is whole-table.

Running E3 means building incremental Silver (a Delta `MERGE` on the business key)
and incremental Gold (recompute dirty partitions only) first. That is pipeline work,
not experiment work, and it is the single largest remaining engineering item in the
project.

---

## E1 — streaming against batch throughput **[DESIGNED, NOT RUN]**

ADR 0014 chose to have the Kafka consumer write the **same** Bronze table as batch,
and notes the consequence: "Experiment E1 has to separate batch from streaming rows
by `_source_system` rather than by table. That is a `WHERE` clause, not a design
problem."

Kafka is installed (`~/kafka`). What E1 needs beyond what exists: a broker running,
a producer driving a known rate, and a consumer run long enough for the steady state
to dominate the startup cost. The Stage 9 report has a measured consumer run of
31,231 ms for 60,000 rows, which is a data point rather than a controlled comparison
— it was not warmed up, repeated or interleaved against a batch arm.

---

## E8 — replay under a drifted schema **[DESIGNED, NOT RUN]**

ADR 0008 gives the generator per-chunk deterministic seeding specifically so that
"E8 replays a dataset under a drifted schema", and the `generator-determinism` CI
job protects that property on every push.

What E8 needs: a drifted variant of a contract (a column added, a type widened, a
column dropped), the same seed replayed through it, and a measurement of what the
pipeline does — how many rows quarantine, whether Bronze accepts the write, what the
quality score does. The determinism it depends on is already verified in CI; the
drift harness is not written.

---

## E5, E6, E7 — never defined

No specification exists anywhere in the repository. Rather than invent three and
imply they were planned, here are the gaps a reader would notice, for whoever
decides:

- **Tokenisation cost and cardinality.** `LOW_CARDINALITY_FIELDS` is refused by
  design, and the HMAC cost per row is unmeasured. Cheap to run; ties to C4.
- **Disclosure control: k against utility.** Stage 6 measured the grain-vs-k
  trade-off once. A sweep of `min_cell_count` against suppression rate and rows
  published would make C4's cost a curve rather than a point.
- **Lineage granularity.** Stage 9b measured per-run, per-file and per-record
  digesting (27 ms / 8 ms / 7,330 ms). That is arguably E7 already, done and
  reported under a different name.

The third is worth noting for the write-up: **one of the three missing experiments
has effectively already been run**, and reporting the Stage 9b measurement as E7
would be accurate rather than retrofitted.

---

## Reproducing

```bash
export CONFIDENTIA_KEYSTORE_PASSPHRASE=...     # E4 needs key release
confidentia bench e4 --rows 3000000 --repeats 5
confidentia bench e2 --rows 3000000 --repeats 3
confidentia bench results
```

Both write to `platform.benchmark_result` with the machine configuration attached.
