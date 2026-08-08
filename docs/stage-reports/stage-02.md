# Stage 2 — Synthetic data generation

**Date:** 2026-08-08

## What this stage was for

Every later stage is evaluated against this data. If the generators are a thin Faker
wrapper, then the data quality layer can only be demonstrated, the anomaly work has
nothing to find, and the partitioning experiments measure an artefact of uniform
sampling. So Stage 2 builds ground truth, not just volume.

## What was implemented

| Module | Purpose |
|---|---|
| `generators/base.py` | Hierarchical seeding, chunked streaming, distribution helpers, late-arrival split |
| `generators/reference.py` | Real ICD-10 / CPT / MCC code sets with prevalence weights, US geography |
| `generators/defects.py` | 11 defect families, budgeted injection, **ground-truth manifest** |
| `generators/healthcare.py` | Facilities, plans, providers, patients, **patient mutations**, encounters, claim lines |
| `generators/financial.py` | Merchants, accounts, transactions, **chargebacks** |
| `generators/writers.py` | Landing-zone files, SHA-256 per file, ingest manifest with batch digest |
| `cli.py` | `confidentia generate healthcare \| financial \| profile` |
| `tests/unit/test_generators.py` | 30 tests covering determinism, defect accounting, distributions, semantics |

## Three decisions that shape the rest of the project

**Defects come with an answer key.** Every corruption is recorded — record key, field,
original value, corrupted value, type, severity. Stage 7 can therefore report precision
and recall *per check family* rather than "the rules found some bad rows". The families
that score badly will be the interesting result.

**Transactions carry no fraud label.** Fraud is generated as a latent characteristic and
confirmed later by chargebacks arriving 1–45 days after the event. An `is_fraud` column
in the raw stream would be a label leak. The side effect is valuable: chargebacks are
genuinely late-arriving facts that invalidate closed aggregates, so **Gold must support
restatement** rather than assuming append-only facts.

**Patients and accounts mutate.** `patient_change` is a replayable script of address,
plan, PCP, surname and phone changes. Without records that change over time, the SCD2
implementation in Silver could be asserted but never exercised.

Full rationale in [ADR 0008](../adr/0008-synthetic-data-ground-truth.md).

## Measured results

Development host, WSL2 capped at 4 GB, Python 3.11.15. Real measurements, not targets.

| Run | Rows | Wall time | Throughput | Peak RSS | On disk |
|---|---|---|---|---|---|
| Financial, 5M transactions | 5,143,526 | 55.0 s | 94,734 rows/s | **583 MB** | 252 MB |
| Healthcare, 2M encounters | 7,049,152 | 43.7 s | 163,448 rows/s | **492 MB** | 295 MB |

12.2M rows in 99 seconds with peak memory under 600 MB. The 5M+ requirement is met with
substantial headroom; the binding constraint is disk, not memory.

Defect injection at the requested 6% rate, transaction dataset (5,041,660 rows):

| Family | Count | Share |
|---|---|---|
| null_violation | 91,660 | 1.818% |
| case_inconsistency | 50,000 | 0.992% |
| format_violation | 50,000 | 0.992% |
| duplicate_record | 41,660 | 0.826% |
| range_violation | 37,500 | 0.744% |
| unit_inconsistency | 16,660 | 0.330% |
| referential_orphan | 12,500 | 0.248% |
| *latent_fraud (label, not a defect)* | 10,500 | 0.208% |

## Bugs found and fixed

**Overlapping defects crashed the injector.** A field nulled by `NULL_VIOLATION` could
then be selected by `FORMAT_VIOLATION`, which passed `NaN` into a string operation.
Overlapping defects on one record are realistic, so the handler now coerces rather than
assuming a clean input. Covered by a regression test.

**Two source files were corrupted by a PowerShell edit.** `Get-Content`/`Set-Content`
round-tripped UTF-8 through cp1252 and turned every em-dash into mojibake. Repaired in
WSL. Source files are edited through the Linux side or the editor from now on — not
through PowerShell text cmdlets.

## Known limitation

The transaction defect plan omits `whitespace_padding` and `encoding_corruption`,
because transactions have no human-entered text fields. Those families are exercised on
`account`, `patient` and `claim_line` instead. Stage 7 must report recall **per dataset**
rather than pooled, or their absence here will read as a detection failure.

## How to verify

```bash
wsl -d Ubuntu -- bash -lc "cd ~/confidentia && source .venv/bin/activate && python -m confidentia.cli generate healthcare --rows 50000 && python -m confidentia.cli generate profile healthcare encounter"
```

Determinism check — the same seed must reproduce byte-identical output:

```bash
wsl -d Ubuntu -- bash -lc "cd ~/confidentia && source .venv/bin/activate && python -m pytest tests/unit/test_generators.py -q"
```

## State

43 unit tests passing (13 hashing, 30 generators), lint clean. Nothing claimed as
deployed; TEE backend remains `sim`.

## Next: Stage 3 — ingestion

Five collectors over this data: REST API (paged, retrying), file watcher (manifest and
checksum verified, idempotent on content hash), Postgres logical-replication CDC client
consuming `patient_change`, object-storage batch reader, and the Kafka producer for the
transaction stream. Watermarks and idempotency keys land in Postgres so a failed run
resumes rather than restarts.
