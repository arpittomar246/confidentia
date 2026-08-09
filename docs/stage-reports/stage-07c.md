# Stage 7c — Fixing what the evaluation harness found

**Date:** 2026-08-09

Stage 7b measured the quality layer against the answer key and produced some very
low numbers. Most of them turned out not to be about the quality layer at all:
the generator was recording defects it had not actually injected, and the rule
catalogue had columns nobody checked. This stage fixes both, re-runs the whole
pipeline from nothing, and reports the corrected numbers beside the originals.

The 7b baseline is preserved at `~/confidentia-baselines/stage-07b-evaluation.csv`
(92 rows), exported before the rebuild.

## Headline — 7b vs 7c

| Dataset | P (7b → 7c) | R (7b → 7c) | F1 (7b → 7c) |
|---|---|---|---|
| healthcare/patient | 1.000 → **1.000** | 0.390 → **0.997** | 0.561 → **0.998** |
| healthcare/encounter | 0.053 → **0.850** | 0.943 → **1.000** | 0.101 → **0.919** |
| healthcare/claim_line | 0.814 → **0.818** | 0.798 → **0.961** | 0.806 → **0.884** |
| financial/account | 1.000 → **1.000** | 0.818 → **0.985** | 0.900 → **0.992** |
| financial/transaction | 0.998 → **0.998** | 0.712 → **0.959** | 0.831 → **0.978** |

Family/field pairs with **no rule at all**: 5 → **0**.

**These numbers are not evidence that the quality rules improved.** Almost nothing
was changed about the rules' *method*. What changed is that the answer key now
describes defects that exist and the catalogue now covers the columns being
corrupted. 7b was measuring the measurement apparatus; 7c measures the rules.

## The eight fixes

Seven were on the list 7b produced. The eighth was found by the invariant test
written to stop the others recurring.

### 1. `_break_format` did not break formats

It stripped `@`, `-` and `.`, which does nothing to an all-digit value. Verified
against the manifest by comparing `original_value` to `corrupted_value`:

| Field | Recorded | Byte-identical |
|---|---|---|
| patient.postal_code | 45 | 45 (100%) |
| claim_line.procedure_code | 1,361 | 1,342 (98.6%) |
| transaction.mcc | 400 | 400 (100%) |
| transaction.ip_address | 200 | 0 |

There was a second, subtler case the no-op test would not have caught:
`account/phone` scored 0 of 9 because stripping the hyphen from `(555) 123-4567`
yields `(555) 1234567` — *still a validly formatted phone number*. The value
changed and was still not a defect.

Both are fixed by stripping **and then** inserting a separator near the front.
Stripping breaks compound values (an email without its `@`); inserting breaks
fixed-width ones. All ten field patterns now fail:

```
postal_code   90049          -> 90-049          still_valid=False
mcc           7011           -> 70-11           still_valid=False
phone         (555) 123-4567 -> (5-55) 1234567  still_valid=False
email         jane.doe@x.com -> ja-nedoe_at_xcom still_valid=False
```

The corruption is deliberately **blind to the detector's regexes**. Importing
`quality.rules.PATTERNS` and asserting the result fails them would guarantee
recall 1.0 by construction and measure nothing.

### 2. `case_inconsistency` upper-cased already-upper-case values

400 of transaction's 600 injected case defects were on `currency`, which is
already `USD`. Same for `terminal_country`, `state` and `home_state`.

### 3. No check that a corruption changed anything

(1) and (2) are the same root cause. `_record` now drops any defect whose value
did not change, comparing nullness properly so that nulling an already-null value
is also recognised as a no-op. `duplicate_record` opts out — its recorded value is
the business key and is identical by construction, because the corruption is the
appended row.

This guard is what makes the class of bug impossible rather than these two
instances of it fixed.

### 4. Four foreign keys were never declared

`referential_orphan` scored 1.000 wherever a relationship was declared and 0.000
wherever it was not, and only two of five were declared. The four missing ones all
pointed at reference tables — `facility`, `provider`, `insurance_plan`,
`merchant` — which the generator has always produced but which had **no schema
contract**, so they were never promoted past the landing zone and there was no
parent to check against.

Stage 7c adds those four contracts with a new `is_reference` flag: they are
promoted to Bronze so the referential rules resolve, but skipped by Silver, since
Gold builds its own dimensions from the star schema and running dedupe,
tokenisation and SCD2 over a five-row facility table adds machinery without
meaning.

`referential_orphan` now scores 1.000/1.000 on every dataset.

### 5. The generator injected nulls into nullable columns

`patient.first_name` was `nullable=True`, so no completeness rule was derived for
it — and 82 injected "null violations" sat in the answer key as permanent false
negatives. That single field was most of patient's 0.390 recall.

Checking all five plans found **six** such disagreements, not one. Which side to
fix is a judgement per column:

- `patient.first_name` became **NOT NULL** — a patient master requires a name, and
  `last_name` already was. This was a contract oversight.
- `patient.email`, `patient.phone`, `patient.ssn`, `account.phone`,
  `transaction.ip_address` are genuinely optional and **lost their
  `NULL_VIOLATION` role**. A card-present transaction has no IP address.

### 6. `unit_inconsistency` covered two columns of four

The generator injects it into `billed_amount`, `amount`, `allowed_amount` and
`credit_limit`; the rule existed only for the first two.

The thresholds are now per column rather than a flat 50,000, because 7b showed a
single threshold is wrong in two directions at once: precision 1.000 on
`transaction.amount` and 0.093 on `claim_line.billed_amount`, since a clinical
bill legitimately exceeds 50,000 and a retail card transaction does not.

**The method is unchanged** — still a fixed threshold, still the family expected to
score worst. 7c widened its coverage, not its intelligence.

### 7. Bronze uniqueness counted redeliveries as duplicates

`encounter/duplicate_record` scored precision **0.007**: 399 injected duplicates
against 59,601 rows that were merely delivered twice, once by the file collector
and once by the REST collector. That one family dragged encounter's overall
precision to 0.053.

The rule was not wrong; it was answering the wrong question for the layer. Bronze
is "exactly what arrived", and what arrived was two copies. The window is now
partitioned by `(business key, _source_system)` at Bronze, which asks the question
worth asking there — *did this source send me the same key twice* — while
cross-source deduplication stays Silver's job, which it already did (120,798 →
59,121).

Precision on that family: **0.007 → 1.000**.

### 8. `case_inconsistency` on columns with no casing rule

Found by the invariant test written for (5). The casing convention applies to
*categorical* columns that Silver lower-cases; the plans also targeted names,
state codes and currency codes, for which no rule exists and none should. Seven
field/family pairs, all removed from the plans.

## The durable fix: `check_plan_rule_coverage.py`

Fixes 4, 5, 6 and 8 are the same bug wearing different clothes — the generator
injecting a defect the platform has no rule for. A new CI job asserts the
invariant directly:

```
plan/rule coverage holds (57 injected family/field pairs, all covered).
```

It checks that a rule *exists* for that column and family, not that it *catches*
the corruption. `unit_inconsistency` has a rule and still finds a quarter of what
is injected — that passes, and correctly so. The distinction is the point: recall
should measure rule quality, and it cannot while it is contaminated by rule
coverage.

CI is now **8 jobs**.

## What is still imperfect, and why

All remaining gaps are real findings about the rules or accepted costs of the
matching criterion. None is an artefact.

| Where | P | R | Why |
|---|---|---|---|
| claim_line / unit_inconsistency | 1.000 | 0.246 | Fixed threshold cannot catch cents-as-dollars while the value stays plausible. **This is the honest result** — the family 7a predicted would score worst. |
| transaction / unit_inconsistency | 1.000 | 0.104 | Same. |
| encounter / format_violation | 0.430 | 1.000 | 635 whitespace-padded diagnosis codes also fail the ICD-10 pattern. Real detections of real corruption, counted as false positives under strict matching (ADR 0012, Decision 1). |
| claim_line / format_violation | 0.431 | 1.000 | Same overlap, 1,801 of them. |
| claim_line / range_violation | 0.958 | 1.000 | 45 legitimately extreme clinical amounts. |
| patient / null_violation | 1.000 | 0.990 | One record was nulled *and then* format-corrupted on the same field; `_break_format` turns a null into `"??"`, so the value is no longer null. An overlapping-defect artefact, correctly recorded twice in the manifest. |

The two `format_violation` precision figures are the largest remaining number and
the one most likely to be misread. They are the documented, accepted cost of the
strict criterion, not a detection problem: recall on that family is 1.000.

## Reproducing

Two new scripts make the rebuild a single auditable unit rather than a sequence
to be remembered:

```bash
export CONFIDENTIA_KEYSTORE_PASSPHRASE="dev-passphrase-not-for-real-use"
bash scripts/reset_data.sh --yes      # destroys the lake; keeps the keystore
bash scripts/rebuild_pipeline.sh      # generate -> ingest -> bronze -> ... -> evaluate
```

`rebuild_pipeline.sh` encodes three orderings that are easy to get wrong by hand:
`keys approve` after any measured-package edit; the REST redelivery of `encounter`
before Bronze, since that is what creates the double delivery; and Bronze quality
**last**, because the detections table is overwritten per run and the evaluator
refuses to score Silver detections against the answer key.

## What this stage deliberately did not do

- **The `unit_inconsistency` method is unchanged.** A distribution-aware check
  (median absolute deviation, or reconciling `billed`/`allowed`/`paid`) is the real
  fix and remains future work. Changing it here would have conflated "the answer
  key was wrong" with "the rule got smarter" in the same measurement.
- **The overlap cost was not scored away.** It would have been easy to credit
  `format_violation` for detecting whitespace padding. ADR 0012 argues why not.
- **`evaluation/` is still not a measured package.** Unchanged from 7b and still
  worth deciding before the C5 enclave-placement work.
