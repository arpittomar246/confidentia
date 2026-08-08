# ADR 0008 — Synthetic data: ground truth, latent labels, and reproducibility

- **Status:** Accepted
- **Date:** 2026-08-08
- **Stage:** 2 (data generation)

## Context

The platform has no real data and cannot have any: the whole point is handling PHI
and cardholder data, which cannot be sourced for a student project. So the generators
are not a convenience — they are the substrate every later stage is evaluated on. Three
decisions determine whether that evaluation means anything.

## Decision 1 — Every injected defect is recorded in a ground-truth manifest

Defects are written to `_defects.parquet` beside the data, keyed by record: dataset,
tenant, chunk, record key, defect type, severity, field, original value, corrupted value.

**Why.** Most academic data quality layers are *demonstrated*, not *evaluated*: rules
run, rows get flagged, and nobody can say whether the flagged rows were the right ones.
With an answer key, Stage 7 produces precision and recall **per check family**. That
converts a claim into a measurement, and makes the weak families visible — a check that
catches 70% of unit inconsistencies is a more interesting result than one that "works".

**Consequence.** The record key must never be corrupted, or the manifest cannot be
joined back and nothing is measurable. This is enforced in `DefectPlan.fields_for`, which
excludes the key field, and asserted by test.

**Constraint.** The pipeline must never read `_defects.parquet`. Only the evaluation
harness may. A pipeline that consulted the answer key would score perfectly and mean
nothing.

## Decision 2 — Fraud is latent; transactions carry no label

Transactions have no `is_fraud` column. A subset is *generated* with fraud-like
characteristics (high-risk merchant category, card-not-present, foreign terminal,
elevated amount, small-hours clustering), and confirmation arrives 1–45 days later as
separate `chargeback` events. True labels go to the manifest only.

**Why.** A payment system does not know at authorisation time whether a transaction is
fraudulent. An `is_fraud` column in the raw stream is a label leak that makes every
downstream analysis trivially easy and indefensible in a viva.

**Consequence, and this is the valuable part.** Chargebacks are genuinely late-arriving
data referencing facts whose daily aggregates closed weeks ago. Gold cannot assume
append-only facts; it must support **restatement**. That is a real data engineering
problem the project now has to solve rather than assert.

Fraud rate is 0.21%, chargeback confirmation 82%, plus a 0.035% "friendly fraud" rate
on legitimate transactions. Rare on purpose: a 20% fraud rate would make class balance
trivial and any detection result meaningless.

## Decision 3 — Hierarchical seeding, not a single global RNG

Seeds derive through `numpy.random.SeedSequence`: `derive_rng(seed, "encounter", 7)`
always produces the same generator and is independent of chunk 8.

**Why.** Experiment E3 compares incremental against full reprocessing of *the same*
data; E8 replays a dataset under a drifted schema. Neither is valid if the generator
wanders between runs. Per-chunk derivation additionally means a failed run resumes at
chunk 40 without regenerating 0–39, and the writer could parallelise later without
changing the output.

The same mechanism solves a coordination problem: the fraud population is drawn once
over the whole transaction space, so the transaction generator and the chargeback
generator each compute an identical set independently, with no shared state and without
materialising the stream.

## Decision 4 — Identifiers are pure functions of an integer index

`PAT-000000123`, `ENC-000000000007`, `TXN-000000000007`.

**Why.** A downstream generator can reference an entity without the upstream dataset
existing. Claim lines are produced for 5M encounters without holding 5M encounters in
memory. The format is therefore a **contract**, not cosmetics, and is asserted by test.

## Decision 5 — Type drift only where the format permits it

`TYPE_DRIFT` is disabled for Parquet and enabled for CSV/JSON.

**Why.** A Parquet column has a fixed physical type; a string cannot be smuggled into a
numeric column — the write would fail or silently coerce. Recording a defect that cannot
exist in the output would depress Stage 7's measured recall for a reason unrelated to
the quality layer.

## Distributions, and why they are not uniform

| Aspect | Distribution | What breaks if it were uniform |
|---|---|---|
| Transaction / billed amounts | Log-normal, per merchant category | Outlier detection becomes trivial; the anomaly experiments say nothing |
| Encounters per patient | Zipf (a≈1.25) | No partition skew, so experiment E2 measures nothing real |
| Activity over the day | Two-peak diurnal curve, weekend damping | Time-series dashboards show a flat line no analyst would recognise |
| Diagnosis codes | Weighted by real prevalence | Every condition equally common; Gold aggregates are featureless |
| Patient age | Bimodal (general + older high-utilisation cohort) | Every age-band aggregate identical |
| Account risk score | Beta(2,8) | No thin high-risk tail to segment on |

## Known limitation

The transaction defect plan omits `whitespace_padding` and `encoding_corruption`.
This is deliberate — transactions are machine-generated and have no human-entered text
fields — but it means those two families are exercised only on the `account`, `patient`
and `claim_line` datasets. Stage 7 recall for them must be reported per dataset, not
pooled, or the absence will look like a detection failure.

## Measured results

Generation, single WSL2 instance capped at 4 GB, Python 3.11.15:

| Run | Rows | Wall time | Throughput | Peak RSS | On disk |
|---|---|---|---|---|---|
| Financial, 5M transactions | 5,143,526 | 55.0 s | 94,734 rows/s | **583 MB** | 252 MB |
| Healthcare, 2M encounters | 7,049,152 | 43.7 s | 163,448 rows/s | **492 MB** | 295 MB |

These are real measurements on the development host, not projections. Peak resident
memory stays under 600 MB at 5M rows, which is the claim the chunked design exists to
support.
