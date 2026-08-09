# ADR 0012 — How a detection is matched to an injected defect

- **Status:** Accepted
- **Date:** 2026-08-09
- **Stage:** 7b (Evaluation)

Stage 7a produces detections. Stage 2 produces an answer key. Turning those two
into precision and recall requires deciding when a detection *counts* as having
found a defect, and that decision determines the headline numbers far more than
the quality rules do. It is written down here because a scoring harness with an
undocumented matching rule is not a measurement, it is a claim.

## Decision 1 — A true positive requires `(record_key, field, family)`

The alternative was matching on `record_key` alone.

Under record-only matching, a null check that fired on the wrong column would be
credited with whatever defect that record happened to carry. On `encounter`, where
3,597 defects are spread over seven fields and most corrupted records carry a
defect *somewhere*, precision would approach 1.0 for reasons that have nothing to
do with the rules being correct. The measurement would be unfalsifiable.

Stage 7a records every detection with the column it fired on precisely so this
could be strict. Relaxing it here would waste that and produce a flattering number,
which is the worst possible outcome because nobody questions a good result.

**The cost, stated plainly.** Strict matching penalises a detection that is *right
about the record and wrong about the reason*. Bronze `encounter` shows this
clearly: 639 diagnosis codes were whitespace-padded, and the padded value also
fails the ICD-10 format regex, so `format_violation` picks up 634 detections that
the manifest attributes to `whitespace_padding`. Under this criterion those are
false positives. They are real detections of real corruption, counted as errors.

That cost is accepted. The alternative — crediting a rule for finding a defect it
did not describe — makes per-family precision meaningless, and per-family precision
is the entire deliverable. The overlap is reported in the stage report rather than
scored away.

## Decision 2 — The unit is a claim, not a row

Both sides are deduplicated to distinct `(record_key, field, family)` triples
before matching.

Two facts about this platform make row counting produce recall above 1.0:

- `duplicate_record` appends a near-copy of an existing row. The manifest records
  one corruption; the uniqueness rule flags *both* copies.
- Bronze `encounter` holds a double delivery — the file and REST collectors each
  delivered all 60,000 rows — so every record key appears twice.

Counting claims answers the question actually being asked: *did the platform
identify this defect, on this field, of this record*. That is answered once,
regardless of how many physical rows carry it.

## Decision 3 — Undefined is reported as NULL, never as zero

A family with no ground truth in a dataset has no recall — 0/0 — and a family with
no detections has no precision. Both are stored as SQL `NULL`.

Reporting them as 0.0 would assert a failure that did not happen.
`whitespace_padding` is deliberately absent from the transaction defect plan
(machine-generated data has no human text to pad); a recall of 0.0 there would read
as a detection failure rather than as an absence of anything to detect. The
confusion counts are always stored, so nothing is hidden by the missing ratio.

## Decision 4 — Platform-only families are counted, not scored

`future_event` and `late_arrival` are detected by the platform and never injected
by the generator. Scoring them would make every one of their 1,676 detections a
false positive and drag overall precision down for rules that may be working
perfectly.

They are excluded from the confusion matrix and reported separately as *detected,
unscoreable*, with their counts intact. Dropping them silently would be the same
error in the other direction.

## Decision 5 — Recall of zero is disambiguated by rule coverage

A recall of 0.0 has two entirely different causes that look identical in the
number:

1. a rule ran against that column and failed to catch the corruption;
2. no rule was ever registered for that column.

Every row records `rule_exists`, read from `platform.dq_rule` — the catalogue the
runner actually registered, not a rebuild from the contract, because the uniqueness
and referential rules are appended at run time and would otherwise appear as
phantom gaps.

This turned out to carry most of the diagnostic value in Stage 7b. Of the nine
family/field pairs scoring zero recall, five had **no rule at all**, which is a
coverage finding for the rule catalogue, not an accuracy finding about the rules.

## Decision 6 — Bronze only, enforced rather than documented

The answer key describes what the *source* sent. Silver has already quarantined
those records, so recall measured on Silver is the platform grading its own
cleaning.

Documenting this was not enough. The detections table is overwritten per run, so a
later Silver run silently replaces a Bronze one in place — which is exactly what
had happened to `encounter` and `transaction` before this stage ran. A `layer`
column was therefore added to the detections table, and the evaluator **refuses**
when the detections on disk describe a layer other than the one requested.

The alternative — trusting the operator to remember which run happened last —
fails silently and produces a plausible number, which is the failure mode this
project cannot afford.

## Consequences

- Per-family precision on `encounter` is low for a structural reason (double
  delivery, overlapping corruptions) that must be restated every time the number
  is quoted. See the stage report.
- The criterion is stored on every row of `platform.dq_evaluation` as
  `matching_criterion`, so a future change of criterion cannot be mistaken for a
  change in the platform's accuracy.
- Changing the criterion later requires a new ADR and a re-run, not an edit.
