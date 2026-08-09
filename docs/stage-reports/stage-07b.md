# Stage 7b — Evaluation: scoring the quality layer against the answer key

**Date:** 2026-08-09

Stage 7a asserted that the quality framework catches bad data. This stage measures
it. Every corruption injected in Stage 2 was recorded — dataset, record key, field,
defect type — so the detections can be scored with precision, recall and F1 **per
defect family**, against a known answer key.

The low numbers are the result. Several are low for reasons that have nothing to do
with the rules, and saying which is which is the whole point of the exercise.

## What was built

| Component | Purpose |
|---|---|
| `evaluation/ground_truth.py` | Load and normalise the answer key through the one sanctioned accessor |
| `evaluation/detections.py` | Read the encrypted detections table; refuse the wrong layer |
| `evaluation/scoring.py` | Strict matching, confusion counts, per-family and per-field metrics |
| `evaluation/runner.py` | Orchestration, run tracking, persistence |
| `store/sql/008_evaluation.sql` | `platform.dq_evaluation` + `dq_evaluation_latest` view |
| `cli.py` | `evaluate quality <tenant> [dataset] [--fields]`, `evaluate report` |
| `tests/unit/test_evaluation.py` | 20 tests, no Spark, no Postgres |

The package lives **outside** `quality/`. `check_ground_truth_isolation.sh` forbids
any pipeline package from touching the answer key, and a quality layer that could
see the answers would score perfectly and mean nothing.

## Method

A true positive requires **`(record_key, field, family)`** to match on both sides.
Both sides are deduplicated to distinct triples first. Undefined metrics are stored
as `NULL`, never as zero. `future_event` and `late_arrival` are counted but not
scored — the generator never injects them. Full rationale and the costs of each
choice are in [ADR 0012](../adr/0012-evaluation-matching-criterion.md).

Scored on **Bronze**. Silver has already quarantined the defects, so recall
measured there is the platform grading its own cleaning.

## Headline — overall, per dataset

| Dataset | Precision | Recall | F1 | TP | FP | FN | Injected |
|---|---|---|---|---|---|---|---|
| financial/account | 1.000 | 0.818 | 0.900 | 54 | 0 | 12 | 66 |
| financial/transaction | 0.998 | 0.712 | 0.831 | 2,566 | 5 | 1,037 | 3,603 |
| healthcare/claim_line | 0.814 | 0.798 | 0.806 | 6,893 | 1,575 | 1,740 | 8,633 |
| healthcare/patient | 1.000 | 0.390 | 0.561 | 117 | 0 | 183 | 300 |
| healthcare/encounter | 0.053 | 0.943 | 0.101 | 3,391 | 60,235 | 206 | 3,597 |

Reported per dataset, never pooled. Pooling would hide that `whitespace_padding`
and `encoding_corruption` are absent from the transaction defect plan by design —
machine-generated payment data has no human-typed text to pad or mis-encode — and
their absence would read as a detection failure.

## Per-family scores

Sorted worst recall first, because that is where the findings are.

| Dataset | Family | P | R | F1 | TP | FP | FN | Rule? |
|---|---|---|---|---|---|---|---|---|
| patient | null_violation | — | 0.000 | — | 0 | 0 | 82 | **no** |
| patient | case_inconsistency | — | 0.000 | — | 0 | 0 | 45 | **no** |
| patient | referential_orphan | — | 0.000 | — | 0 | 0 | 11 | **no** |
| patient | format_violation | — | 0.000 | — | 0 | 0 | 45 | yes |
| account | unit_inconsistency | — | 0.000 | — | 0 | 0 | 3 | **no** |
| account | format_violation | — | 0.000 | — | 0 | 0 | 9 | yes |
| encounter | referential_orphan | — | 0.000 | — | 0 | 0 | 120 | partial |
| claim_line | format_violation | 0.031 | 0.019 | 0.024 | 26 | 803 | 1,335 | yes |
| transaction | unit_inconsistency | 1.000 | 0.085 | 0.156 | 17 | 0 | 184 | yes |
| claim_line | unit_inconsistency | 0.093 | 0.163 | 0.119 | 74 | 720 | 380 | partial |
| transaction | case_inconsistency | 1.000 | 0.333 | 0.500 | 200 | 0 | 400 | partial |
| transaction | format_violation | 1.000 | 0.333 | 0.500 | 200 | 0 | 400 | yes |
| transaction | referential_orphan | 1.000 | 0.667 | 0.800 | 100 | 0 | 50 | partial |
| encounter | format_violation | 0.385 | 0.827 | 0.525 | 397 | 634 | 83 | yes |
| encounter | case_inconsistency | 1.000 | 0.994 | 0.997 | 477 | 0 | 3 | yes |
| claim_line | null_violation | 1.000 | 0.991 | 0.995 | 2,475 | 0 | 23 | yes |
| transaction | range_violation | 0.989 | 0.993 | 0.991 | 447 | 5 | 3 | yes |
| claim_line | range_violation | 0.951 | 0.998 | 0.974 | 1,020 | 52 | 2 | yes |
| encounter | duplicate_record | 0.007 | 1.000 | 0.013 | 399 | 59,601 | 0 | yes |

Everything not listed scored **1.000 / 1.000**: `null_violation` on encounter,
transaction and account; `whitespace_padding` and `duplicate_record` everywhere
except encounter; `encoding_corruption`; `temporal_inconsistency`;
`range_violation` on encounter and account; `referential_orphan` on claim_line.

**Detected but unscoreable:** `late_arrival` — 814 on encounter, 862 on
transaction. Real detections of a real condition the generator never labels.

## What the low numbers actually mean

Five distinct causes, only two of which are the quality layer's fault.

### 1. The injector produced no defect (format_violation)

`_break_format` corrupts a value by stripping `@`, `-` and `.`. On a purely numeric
string that is a **no-op** — the "corrupted" value is byte-identical to the
original, and the manifest records a defect that does not exist in the data.

Measured directly from the manifest by comparing `original_value` to
`corrupted_value`:

| Dataset | Field | Recorded | Unchanged by injection |
|---|---|---|---|
| patient | postal_code | 45 | **45 (100%)** |
| claim_line | procedure_code | 1,361 | **1,342 (98.6%)** |
| transaction | mcc | 400 | **400 (100%)** |
| transaction | ip_address | 200 | 0 (0%) |

`ip_address` contains dots, so the corruption works and recall is 1.000. Everywhere
the field is all digits, recall is near zero — and the rules are behaving
correctly, because there is nothing wrong with the data.

This accounts for essentially all of `format_violation`'s missing recall: 1,335 of
claim_line's 1,335 false negatives, 45 of 45 on patient, 400 of 400 on transaction.
**It is a generator defect, not a detector defect, and it was invisible until the
harness existed.**

### 2. The corruption is a no-op for a second reason (case_inconsistency)

`case_inconsistency` corrupts by upper-casing. `currency` is already `USD`/`EUR`/
`GBP`, so 400 of transaction's 600 injected case defects changed nothing. The 200
on `channel` — genuinely lower-case values — scored 1.000/1.000.

Same class of finding as above: the defect plan does not check that its corruption
is expressible in the field it targets.

### 3. No rule was ever registered

Five family/field pairs have no rule at all. A recall of 0.0 here is a **coverage
gap in the rule catalogue**, not a miss:

| Dataset | Field | Family | Why no rule |
|---|---|---|---|
| patient | first_name | null_violation | `first_name` is `nullable=True` in the contract, so no completeness rule is derived |
| patient | state | case_inconsistency | Case rules cover a fixed list of categorical columns; `state` is not on it |
| patient | plan_id | referential_orphan | No parent relationship declared for `patient` |
| encounter | facility_id, provider_id | referential_orphan | Only `encounter → patient` is declared |
| transaction | merchant_id | referential_orphan | Only `transaction → account` is declared |
| claim_line / account | allowed_amount, credit_limit | unit_inconsistency | Rule is hard-coded to `billed_amount` and `amount` |

The `patient/first_name` case is the sharpest: the generator injects nulls into a
column the **contract explicitly permits to be null**. Stage 7a's headline design
property — "marking a column `NOT NULL` creates a check" — means the contract *is*
the specification, and here the generator and the contract disagree. Neither
component is wrong on its own terms; nothing before this stage could see the
disagreement.

`referential_orphan` was expected to score near 1.000. It scores 1.000 where a
relationship is declared and 0.000 where it is not, and only two of five foreign
keys are declared. That is the finding.

### 4. The rule is known to be weak (unit_inconsistency)

Recall **0.085** on transaction, **0.366** on `claim_line/billed_amount`. The rule
is a fixed threshold — flag amounts above 50,000 — and a cents-as-dollars error on
a small transaction stays plausible. Predicted in Stage 7a as the family expected
to score worst; it does, and now by how much.

Precision is 1.000 on transaction but 0.093 on claim_line, because clinical billed
amounts legitimately exceed 50,000 while retail transactions do not. A fixed
threshold is not merely weak, it is **not portable across datasets**. A
distribution-aware check (median absolute deviation, or reconciling
`billed`/`allowed`/`paid`) is the fix, and is future work.

### 5. Bronze cannot distinguish a duplicate from a redelivery

`encounter/duplicate_record` scores precision **0.007** — 399 true positives against
59,601 false positives — and single-handedly drags encounter's overall precision to
0.053.

The cause is Stage 3, not Stage 7. Bronze `encounter` holds 120,798 rows because
the file collector and the REST collector each delivered all ~60,000 records. Every
business key therefore appears at least twice, and a uniqueness rule at Bronze
correctly reports that every key is duplicated.

**This is a true statement about Bronze.** Bronze is defined as "exactly what
arrived", and what arrived was two copies. Cross-source deduplication is Silver's
job, and Silver does it — 120,798 → 59,121. The rule is not wrong; a uniqueness
rule is simply not meaningful at a layer whose contract permits redelivery.

Excluding `duplicate_record`, encounter scores **P=0.881, R=0.928**.

## Verification

The layer guard was tested by asking for the wrong one:

```
$ confidentia evaluate quality healthcare encounter --layer silver
  healthcare/encounter: REFUSED  detections at .../detections describe
  layer(s) ['bronze'], not 'silver'.
```

This mattered in practice. Before this stage, the detections on disk for
`encounter` and `transaction` were from **Silver** runs — the table is overwritten
per run, and the last run of the Stage 7a session had been Silver. Scoring those
would have measured the platform's own cleaning and produced a good number. The
`layer` column added to the detections table is what makes the refusal possible.

## Changes to existing code

Minimal and confined to one property:

- `quality/runner.py` — added `layer` to `DETECTION_COLUMNS` and to the detections
  table's plaintext column set. Two lines. Requires `keys approve`, since
  `quality/` is a measured package.
- `store/repositories.py` — added `RunType.EVALUATION`.

Nothing in the rule engine, the scoring, or the pipelines was altered. The numbers
above describe Stage 7a exactly as it was built.

## What this stage did not settle

- **`evaluation/` is not a measured package.** It decrypts `record_key` under
  released keys, which is an argument for measuring it; it is also offline tooling
  holding the answer key and deliberately outside the attested pipeline, which is
  an argument against. Left as-is rather than changed silently. Worth a decision
  before the enclave placement work (C5).
- **Detections have no history** — the table is still overwritten per run, so there
  is no trend. The `layer` column makes a run key possible; adding one is small.
- **Overlapping corruptions are scored as false positives.** 634 of encounter's
  format_violation FPs are whitespace-padded diagnosis codes that genuinely fail the
  ICD-10 pattern. Under the strict criterion they count against precision. Accepted
  and documented rather than scored away (ADR 0012, Decision 1).

## Follow-up work this stage identified

1. Fix `_break_format` so it corrupts numeric strings (insert a separator, or
   change the length). Until then `format_violation` recall is not measurable on
   `postal_code`, `procedure_code` or `mcc`.
2. Guard the defect plan against no-op corruptions — assert `original != corrupted`
   at injection time and drop the manifest row otherwise.
3. Declare the missing foreign keys: `encounter → facility`, `encounter → provider`,
   `transaction → merchant`, `patient → insurance_plan`.
4. Reconcile the generator's defect plan with the contract's nullability, or accept
   the divergence explicitly.
5. Replace the fixed `unit_inconsistency` threshold with a distribution-aware check.
6. Exclude `duplicate_record` from Bronze scoring, or evaluate uniqueness at Silver
   where the layer contract makes it meaningful.

These are deliberately **not** done in this stage. Fixing the generator and then
reporting the improved numbers would mean the harness was tuned until the result
looked good, and the first honest measurement is worth more than a flattering one.

## Reproducing

```bash
cd ~/confidentia && source .venv/bin/activate
export CONFIDENTIA_KEYSTORE_PASSPHRASE="dev-passphrase-not-for-real-use"

python -m confidentia.cli db migrate
python -m confidentia.cli keys approve
python -m confidentia.cli quality run healthcare --layer bronze
python -m confidentia.cli quality run financial  --layer bronze
python -m confidentia.cli evaluate quality healthcare
python -m confidentia.cli evaluate quality financial
python -m confidentia.cli evaluate report
```

Add `--fields` to break each family down by column; that is where the
"no rule registered" markers appear.
