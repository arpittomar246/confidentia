# Stage 6 — Gold: star schema, disclosure control, restatement

**Date:** 2026-08-09

Implements **C4 (disclosure control)** and the chargeback restatement problem left
open by Stage 2.

## What was built

| Component | Purpose |
|---|---|
| `gold/star.py` | Conformed dimensions and facts, hash surrogate keys |
| `gold/disclosure.py` | Primary + complementary suppression, optional Laplace noise |
| `gold/marts.py` | 6 KPI marts, each declaring its own disclosure policy |
| `gold/writer.py` | Star build, Gold column classification, publish, restatement |
| `store/sql/006_gold.sql` | Disclosure log, partition state, restatement log, `gold` schema |

11 star tables in encrypted Delta; 6 disclosure-controlled marts published to
Postgres.

| Tenant | Star tables | Marts |
|---|---|---|
| healthcare | dim_date, dim_patient, dim_facility, dim_provider, dim_diagnosis, fact_encounter, fact_claim_line | encounters_daily, diagnosis_prevalence, claims_monthly |
| financial | dim_date, dim_account, dim_merchant, fact_transaction | transactions_daily, merchant_risk, fraud_by_hour |

## Disclosure control is doing real work

| Mart | Cells | Primary suppressed | Complementary | Rate |
|---|---|---|---|---|
| mart_transactions_daily | 11,406 | 8,676 | 272 | **78.4%** |
| mart_encounters_daily | 4,542 | 1,297 | 417 | 37.7% |
| mart_merchant_risk | 82 | 36 | 4 | 48.8% |
| mart_diagnosis_prevalence | 247 | 78 | 0 | 31.6% |
| mart_claims_monthly | 128 | 7 | 3 | 7.8% |
| mart_fraud_by_hour | 120 | 0 | 0 | 0% |

The rows suppressed in `mart_diagnosis_prevalence` are sepsis, breast neoplasm and
anaemia — the rare diagnoses, which is exactly what k-anonymity is for. A suppressed
cell arrives in Postgres as `NULL` measures with `_suppressed = true` and
`_suppression_reason = below_k_5`, so the dashboard can render "withheld" rather
than silently showing a gap.

**Complementary suppression is not decoration.** 417 cells in `encounters_daily`
were suppressed *only* because they were the last unsuppressed cell in a row —
without them, the primary-suppressed value would be recoverable by subtracting from
the published row total. Primary suppression alone would have looked like protection
while providing none.

## The finding: 78% suppression is the mart's fault, not the control's

`mart_transactions_daily` losing 78% of its cells is not a bug. It means the mart is
too finely grained for the data volume. Measured directly on `fact_transaction`
(58,899 rows):

| Grain | Cells | k=5 | k=10 | k=20 |
|---|---|---|---|---|
| day × category × channel | 14,606 | **66%** | 97% | 100% |
| day × category | 3,600 | **0%** | 13% | 97% |
| week × category × channel | 2,680 | 13% | 50% | 63% |
| week × category | 540 | 0% | 0% | 4% |
| month × category | 120 | 0% | 0% | 0% |

Two things fall out of this. At k=5, **dropping a single dimension (`channel`) takes
suppression from 66% to zero** — the grain matters more than the threshold. And at
k=20, daily granularity is unusable at any dimensionality, so a stricter privacy
requirement is really a requirement to coarsen the mart.

The correct response is to coarsen the grain, not lower k. `mart_transactions_daily`
is left at its current grain deliberately, as the worked example.

(These figures are measured on the fact table directly and differ slightly from the
published mart's 78.4%, which additionally joins `dim_merchant` and includes
complementary suppression.)

## Restatement

54 partitions spanning 2026-01-01 → 2026-06-27 (a 177-day span) were invalidated by
chargebacks arriving after their aggregates had closed. Only those partitions are
marked dirty; the rest are untouched.

## Bugs found and fixed

**Restatement was recorded before the marts published.** A failed publish therefore
marked partitions clean that had never been recomputed — the same
acknowledge-before-durable-write mistake the CDC collector deliberately avoids
(ADR 0009). Recording now happens after the publish loop, and only for marts that
actually published. The duplicate entry in the restatement log that revealed this
came from a run that failed at publish *after* recording.

**Parquet cannot encrypt a nested column by name.** `dim_patient.changed_attributes`
is `array<string>`, and Parquet Modular Encryption addresses leaf paths
(`col.list.element`), so naming the column failed the write. The dangerous fix would
be to skip complex columns silently — which writes them in **plaintext** while the
code appears to encrypt them. Instead, any complex column not explicitly
allow-listed as plaintext now raises `ComplexColumnEncryptionError`.
`changed_attributes` is allow-listed because it holds attribute *names*
("state", "plan_id"), never values.

**Gold reused Silver's column-key mapping.** It named `mrn` and `ssn`, which a fact
table does not have, and Parquet failed the write. Gold now derives its mapping from
the columns actually present.

**Suppression broke the Postgres COPY.** Blanked cells become pandas `NaN`/`NaT`/
`pd.NA` depending on dtype, none of which psycopg can adapt
(`cannot adapt type 'NAType'`). All three are normalised to Python `None` after
column types are inferred — a suppressed cell must arrive as a genuine SQL `NULL`.

**A regex edit corrupted the writer.** Patching call sites with a regular expression
injected an argument into a function *definition* and into nested calls. Reverted and
fixed by hand; `ruff format` used for the cosmetic cleanup, because unlike a regex it
parses the code first.

## Known limitations

- **Deterministic suppression is vulnerable to differencing.** An attacker who can
  request an aggregate twice with slightly different filters recovers the difference.
  Defending against that needs a query budget the platform does not have.
- **Laplace noise is implemented but off.** Its epsilon accounting is per-release,
  not per-analyst, so it raises the cost of a differencing attack without bounding
  it. Calling it differential privacy would overstate it.
- **Reference dimensions are Type-1**, read straight from the raw drop. A facility
  rename overwrites history. Acceptable for these attributes; anything needing
  history must become an SCD2 dimension in Silver.
- **Gold is full-refresh.** Targeted restatement marks partitions dirty but the
  rebuild is still whole-table; incremental recomputation of only dirty partitions
  is the obvious next step and is what experiment E3 will measure.

## How to verify

```bash
wsl -d Ubuntu -- bash -lc "cd ~/confidentia && source .venv/bin/activate && python -m confidentia.cli gold disclosure"
```

## Next: Stage 7 — Data quality

The full check families, scored against the Stage 2 ground-truth manifest, giving
precision and recall per family rather than "the rules found some rows".
