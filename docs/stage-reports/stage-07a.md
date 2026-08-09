# Stage 7a — Data quality: rules, scoring, detections

**Date:** 2026-08-09

Stage 7 was split. **7a** builds the rule engine, scoring and detection records.
**7b** scores those detections against the Stage 2 ground-truth answer key to
produce precision and recall per defect family.

## What was built

| Component | Purpose |
|---|---|
| `quality/rules.py` | 237 row-level rules across six dimensions, mostly derived from the schema contract |
| `quality/scoring.py` | Severity-weighted scores, per dimension and overall |
| `quality/runner.py` | Single-pass evaluation, uniqueness, referential integrity, freshness, detection records |
| `store/sql/007_quality.sql` | Rule catalogue, per-rule results, scores, freshness |

Rules are **derived from the contract** wherever possible: marking a column
`NOT NULL` creates a completeness check, so the rule set cannot drift from the
schema. Explicit rules add what the contract cannot express — regexes, plausible
ranges, temporal ordering.

## The layer distinction, which turned out to matter

Running the same rules on Bronze and Silver answers two different questions, and
conflating them would flatter the platform.

| Dataset | Bronze score | Silver score |
|---|---|---|
| healthcare/encounter | 0.973 | 0.999 |
| healthcare/claim_line | 0.997 | 0.998 |
| financial/transaction | 0.998 | 0.999 |
| financial/account | 0.997 | 1.000 |
| healthcare/patient | 0.999 | 1.000 |

**Bronze measures what the source sent. Silver measures what the platform is
prepared to publish**, and it looks better precisely because Silver quarantined the
failures. Reporting a Silver score as though it described incoming data quality
would be measuring the platform's own cleaning. Stage 7b will therefore score
against **Bronze**, where the injected defects still exist.

Two consequences worth stating:

- **Bronze uniqueness for `encounter` is 0.000** — every one of 120,798 rows is a
  duplicate business key, because the same 60,000 encounters were delivered twice
  by two collectors. That is correct and not a data defect: at Bronze, uniqueness
  measures *delivery* duplication; only at Silver does it measure data.
- **Bronze completeness is not 1.000** (0.997–0.998) while Silver is — the injected
  null violations are visible at Bronze and quarantined by Silver.

## Two bugs found, one of them by the quality layer itself

**Format rules applied to tokenised columns.** The first Silver run scored
`healthcare/patient` at 0.842 with validity 0.200 and all 5,000 rows failing.
Silver replaces `mrn` with `tk_mrn_a172fa…`, and the rule was checking it against
`^MRN\d{9}$`. That is a category error, not a quality finding. Format rules are now
skipped for tokenised columns outside Bronze, and format validity is explicitly a
Bronze-layer measurement.

**Silver was lower-casing ISO codes.** `financial/transaction` scored validity 0.664
at Silver with all 58,899 rows failing. Silver applied blanket lower-casing to
categorical columns, turning `USD` into `usd`, and ISO 4217 currency codes are
uppercase by standard. The fix belonged in Silver, not in the rule: free-text
categories are lower-cased, standardised codes (currency, ISO country, USPS state,
HL7 sex) are upper-cased.

Result: transaction Silver validity **0.664 → 0.997**, rows failing
**58,899 → 1,747**. This is the quality layer earning its place — it found a real
modelling error in an upstream stage on its first run.

**A recurring structural bug, fixed structurally.** Writing the detections table
failed with `Encrypted column [event_ts] not in file schema` — the session's
column-key mapping still named the encounter contract's columns. That is the third
time a derived table has hit this (Gold facts, Gold nested columns, now
detections), so the derivation moved into `spark/session.py` as
`column_keys_for_frame`, complete with the complex-type guard, rather than being
rewritten a third time.

## What remains failing on Silver, and why that is right

`financial/transaction`, most recent run:

| Family | Field | Severity | Failed | Pass rate |
|---|---|---|---|---|
| late_arrival | ingest_ts | minor | 851 | 0.9856 |
| referential_orphan | account_id | critical | 459 | 0.9922 |
| range_violation | amount | major | 447 | 0.9924 |
| unit_inconsistency | amount | major | 17 | 0.9997 |

Every one corresponds to a defect family the Stage 2 generator deliberately
injects. The `unit_inconsistency` count of 17 is conspicuously low against an
injected rate of ~0.33%, which is the number Stage 7b exists to quantify — a
distribution-blind threshold rule is expected to miss most cents-as-dollars errors.

## Scoring decisions

- **Weighted by severity, not by rule count** (critical 10, major 3, minor 1).
  Without this the score improves by *adding* cosmetic checks, which is backwards.
- **Overall is a weighted mean over rules, not over dimensions.** Averaging
  dimensions would give one referential rule the same influence as twenty
  completeness rules.
- **Vacuous rules are excluded.** A rule that evaluated zero rows scores 1.0 by
  convention but is counted separately: "100% valid across 0 rows" is not a signal.
- **Dimensions are always stored alongside the headline.** A dataset can score
  0.997 overall while its integrity dimension sits at 0.983.

## Known limitations

- **177 of 237 rules are cosmetic** (whitespace and encoding, one pair per string
  column). Severity weighting keeps them from dominating the score, but the rule
  count is misleading as a measure of coverage.
- **`unit_inconsistency` uses a fixed threshold** (>50,000), not a
  distribution-aware test. Deliberate: it is the family expected to score worst,
  and a weak rule that can be measured is more useful here than no rule.
- **Detections are overwritten per run**, so there is no history of what a rule
  found last week. Trend analysis needs run-scoped detection partitions.
- **Quality does not yet gate the pipeline.** `promotion_blocked` is computed and
  stored but nothing consumes it; wiring it into orchestration is Stage 8.

## Next: Stage 7b

Join the detection records to `_defects.parquet` on (record key, field, family) and
report precision, recall and F1 per family. The strict matching criterion — record
*and* field *and* family — is what makes the numbers informative rather than
flattering, and some families are expected to score badly.
