# Stage 5 — Silver: conform, deduplicate, tokenise, SCD2

**Date:** 2026-08-09

## Measured results

| Dataset | In | Out | Duplicates removed | Quarantined | Flagged |
|---|---|---|---|---|---|
| healthcare/patient | 5,038 | 5,000 | 38 | 0 (0.00%) | 0 |
| healthcare/encounter | 120,798 | 59,121 | 59,911 | 1,766 (1.46%) | 472 |
| healthcare/claim_line | 145,138 | 141,525 | 1,118 | 2,495 (1.72%) | 1,000 |
| financial/account | 1,097 | 1,074 | 7 | 16 (1.46%) | 6 |
| financial/transaction | 60,501 | 58,899 | 497 | 1,105 (1.83%) | 445 |
| financial/chargeback | 121 | 121 | 0 | 0 (0.00%) | 0 |

**The encounter number is the interesting one.** 120,798 → 59,121 is not a data
problem: the same 60,000 encounters were delivered twice, once by the file collector
and once by the REST collector, and Silver correctly recognised them as the same
records by business key. Cross-source deduplication is a real requirement that fell
out of having two collectors rather than being contrived.

Quarantine rates (1.46–1.83%) track the null-violation rate injected in Stage 2
closely. That correspondence is suggestive, not proof — a proper precision/recall
measurement against the ground-truth manifest is Stage 7's job, and this report does
not claim it here.

## SCD2 patient dimension

| Metric | Value |
|---|---|
| Change events consumed | 6,400 |
| Versions produced | 5,877 |
| Distinct patients | 5,000 |
| Versions per patient | 1.175 |
| Patients with history | 877 |
| Keys without exactly one current version | **0** |
| Interval gaps or overlaps | **0** |
| Inverted intervals | **0** |

Attribute-change distribution matches the generator's mutation script exactly:

| Attributes changed per version | Count | Corresponds to |
|---|---|---|
| 3 | 304 | address change (address_line1 + city + state) |
| 1 | 549 | plan, PCP, phone or surname change |
| 2 / 4 | 24 | address changes where the new city or state happened to match the old |

## Verified claims

| Claim | Evidence |
|---|---|
| Direct identifiers are tokenised | `mrn` → `tk_mrn_a172fa7391a25123b07f79ae` |
| No plaintext identifier survives | 0 rows match `^MRN` or contain `@` |
| Tokens preserve cardinality (joins work) | 5,000 rows → 5,000 distinct MRN tokens |
| Tokens are cosmetic-variant stable | `"  mrn000123 "` and `"MRN000123"` agree |
| Tokens do not correlate across tenants | Same email, different tokens |
| Silver stays encrypted | `PARE` magic verified on every write |

## Small-file explosion — found, measured, fixed

Silver was 163 MB for ~260k rows while Bronze was 19 MB for more. The cause was a
textbook small-file explosion: partitioning by `event_date` (180 days) meant every
Spark task wrote into every date partition.

| Table | Files | Size | Bytes/file |
|---|---|---|---|
| encounter *before* | 1,980 | 73 MB | ~37 KB |
| encounter *after* | **180** | **13 MB** | ~74 KB |
| transaction *before* | 1,260 | 45 MB | ~37 KB |
| transaction *after* | **180** | **14 MB** | ~80 KB |

Fix: `repartition("event_date")` before the write, so each date lands in one task
and becomes one file.

**The 5.6× size reduction is the striking part** — the same rows, same encoding.
Roughly 60 MB was per-file overhead: each Parquet file carries a footer, and here
every footer is *encrypted*, so small files cost a key unwrap as well as a seek.
Encryption makes the small-file problem materially worse than it is in a plaintext
lakehouse, which is a genuine finding for the performance work in Stage 9.

**A measurement I am not reporting as a result.** A full `count()` returned in 0.44 s
while a single-partition filtered count took 1.96 s. That does not mean pruning is
slow — Delta answers an unfiltered `count()` from transaction-log statistics without
reading any data files, so the two are not comparable. Proper query benchmarks need
methodology that defeats metadata shortcuts, and belong to experiments E2/E4 rather
than to an incidental check here.

## Bugs found and fixed

**`changed_attributes` was systematically overstating.** A plan-only change reported
`[state, plan_id]`, because the current value had been conformed (categoricals
lower-cased) while the before-image came raw from the WAL — so `NY` → `ny` looked
like a change. Both sides are now normalised before comparison. This mattered beyond
cosmetics: `changed_attributes` is audit evidence of what a change actually did.

**Quarantine was silently empty.** The first Silver run quarantined 0 records across
every dataset, because almost every contract column was declared nullable — so the
injected null violations passed straight through and the quarantine path was
untested. Genuinely required business fields are now marked `NOT NULL`, and
quarantine immediately started catching ~1.5% of records, matching the injection
rate.

**Opaque PySpark UDF failure.** `pandas_udf` raised `UNSUPPORTED_SIGNATURE`. Cause:
`from __future__ import annotations` makes the type hints strings, and PySpark
resolves them with `typing.get_type_hints` against the function's *module* globals —
so a locally-imported `pd` is invisible. The UDF factory now lives at module scope.

**Workflow consequence of the attestation gate.** My first Stage 5 script ran
`keys approve` before `ruff --fix`, so the lint fix changed a measured package and
invalidated the approval that had just been made. Approval must be the last step
before execution. This is a permanent property of the design, not a one-off.

## Known limitations

- **Transaction-time, not valid-time.** `valid_from` is when the platform learned of
  a change, not when it became true. Log-based CDC cannot supply the latter. A
  point-in-time query for 2026-01-01 therefore returns nothing, since every version
  begins at the moment the mutation replay ran. Bi-temporal modelling is future work
  and is not approximated silently.
- **Validation is thin.** Silver checks nullability, negative amounts, temporal
  ordering and future timestamps. The full check families (format, referential
  integrity, freshness, uniqueness, anomaly) arrive in Stage 7.
- **The token vault is implemented but unpopulated.** `vault_store` and
  `detokenise` work and are audited, but no pipeline writes to the vault yet;
  tokenisation is currently one-way in practice.
- **Silver is full-refresh.** Incremental MERGE is what experiment E3 compares
  against, and it is not built yet.

## How to verify

```bash
wsl -d Ubuntu -- bash -lc "cd ~/confidentia && bash scripts/verify_silver.sh"
```

## State

Six Silver tables plus one SCD2 dimension, all encrypted. Lint clean. Nothing
claimed as deployed; TEE backend remains `sim`.

## Next: Stage 6 — Gold

Star-schema facts and dimensions, KPI marts, and the disclosure controls (C4):
small-cell suppression before any aggregate leaves the boundary. Gold is also where
the chargeback restatement problem from Stage 2 has to be solved — late-arriving
disputes invalidate daily aggregates closed weeks earlier.
