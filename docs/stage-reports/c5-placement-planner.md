# C5 — The enclave placement planner

The fifth and last contribution, and the one that answers the project's research
question directly: *where in a medallion pipeline should the confidentiality
boundary be drawn, and what does drawing it there cost?*

It found something, which was not guaranteed: **the landing zone holds plaintext
outside the boundary**, and the direct identifiers everyone worries about were never
the binding constraint on placement.

## The split that makes it defensible

The package is four modules, and the division is the point:

| Module | What it produces | Status |
|---|---|---|
| `placement.py` | Which stages must be inside | **Derivation** — same answer on real SGX |
| `profile.py` | Row widths, durations, row counts | **MEASURED** on this machine |
| `model.py` | Enclave cost | **MODELLED** — no hardware exists |
| `planner.py` | The plan | Modelled cost over a derived placement |

`StageCost.as_dict()` returns `"label": "MODELLED"` even when the baseline duration
was measured, because a measured number plus a modelled number is a modelled number.
`confidentia plan` prints a banner saying so before any figure.

## The rule

> A stage must run inside the enclave if and only if it observes the plaintext of a
> column classified PII or PHI.

Applied to the eleven pipeline stages:

```
stage                verdict  why
ingest.collect       inside   reads raw source records, nothing tokenised yet (42 columns / 10 datasets)
bronze.write         inside   reads raw source records, nothing tokenised yet (42 columns / 10 datasets)
silver.conform       inside   reads plaintext bronze columns classified PII/PHI (42 columns)
silver.tokenise      inside   performs tokenisation, so it sees the pre-token values
silver.scd2          inside   reads plaintext silver columns classified PII/PHI (25 columns / 9 datasets)
gold.star            inside   reads plaintext silver columns classified PII/PHI (25 columns)
gold.marts           inside   reads plaintext gold columns classified PII/PHI (25 columns)
quality.run          inside   reads plaintext silver columns classified PII/PHI (25 columns)
lineage.record       inside   reads plaintext bronze columns classified PII/PHI (42 columns)
quality.gate         outside  reads no tenant data
api.serve            outside  reads no tenant data
```

## The finding: direct identifiers were never the constraint

Tokenisation removes **17 of 42** protected columns between Bronze and Silver — the
count drops from 42 to 25 in the output above, and that drop is derived from the
contracts, not asserted. Delete `tokenise=True` from a contract and the number
moves.

And it moves *nothing out of the enclave*. What remains after tokenisation is 25
**quasi-identifiers** — dates of birth, diagnosis codes, geography — and those keep
Gold, quality and lineage inside.

So the planner's advice, per group, is not a placement change:

```
ingest     Cannot move out: reads raw source records... the only lever is to
           tokenise or suppress at the source system.
silver     Cannot move out at any price: performs tokenisation, so it must see
           the pre-token values by definition.
gold       Blocked by 25 quasi-identifier columns. Tokenising them would destroy
           the joins and aggregations they exist for.
```

This reframes what C4 is for. **What makes Gold's output publishable is disclosure
control; what keeps Gold's computation inside the enclave is that disclosure control
has not been applied yet at that point.** Suppression is a property of the published
mart, not of the data being aggregated.

## The boundary violation

The rule is about stages; applying it made the gap obvious.

**The landing zone is written as unencrypted Parquet before Bronze encrypts
anything.** Confirmed by inspection rather than inferred:

| | footer magic | |
|---|---|---|
| `lake/landing/.../part-00001.parquet` | `50415231` = `PAR1` | **unencrypted** |
| `lake/bronze/.../part-00001...parquet` | `50415245` = `PARE` | encrypted |

Grepping one landing `patient` file's raw bytes found **19 SSN-shaped and 7
email-shaped strings**. 42 protected columns are affected.

Two remedies, neither implemented — choosing between them changes ingestion's
recovery story and is not a decision to make while writing a planner:

1. Encrypt landing with the same per-sensitivity keys. Preserves replay.
2. Delete each batch once Bronze has durably committed it. Cheaper; loses replay
   without re-fetching from source.

## Measured inputs

**Row width**, from Parquet footer metadata (`total_uncompressed_size` per column
chunk). No Spark, no JVM, no key release — landing footers are unencrypted, which is
itself a consequence of the violation above.

| Dataset | Rows | B/row uncompressed | Compression |
|---|---|---|---|
| healthcare/patient_cdc | 35,400 | 254 | 2.4× |
| healthcare/patient | 5,044 | 163 | 1.9× |
| financial/account | 1,097 | 126 | 1.9× |
| healthcare/encounter | 120,800 | 113 | 2.1× |
| financial/transaction | 60,409 | 102 | 1.6× |
| healthcare/claim_line | 145,136 | 88 | 1.7× |

**Row-weighted mean: 116 B/row.** Weighted, not maximum — the widest dataset is a
*five-row* facility lookup at 412 B/row, and using it made the first version of the
model think the pipeline was four times its actual size.

**Stage durations** from `platform.run_summary`, median across recorded runs:

| Group | Runs | Median |
|---|---|---|
| ingest | 35 | 1,247 ms |
| bronze | 14 | 3,240 ms |
| silver | 38 | 28,170 ms |
| gold | 10 | 70,092 ms |
| quality | 150 | 15,764 ms |
| lineage | 72 | 7,397 ms |

## The modelled cost

```
group            rows  parts    baseline   overhead  factor
ingest         60,400      1     1,247ms      370ms x 1.296
bronze        422,297      1     3,240ms      769ms x 1.237
silver        422,297      1    28,170ms    5,755ms x 1.204
gold          279,299      1    70,092ms   14,139ms x 1.202
quality       279,299      1    15,764ms    3,273ms x 1.208
lineage       422,297      1     7,397ms    1,600ms x 1.216

pipeline: 125,909 ms measured -> 151,815 ms modelled inside the enclave (x1.206)
```

**MODELLED.** The backend is `sim`; there is no enclave.

## Partition sizing, and the honest answer that it does not matter yet

At 116 B/row and a 128 MB EPC at 75% utilisation, one partition holds **867,787
rows**. Bronze is 422,297 rows, so it already fits and partitioning buys nothing
today.

Rather than recommend a repartition nobody needs, the sweep says *when* it starts to
matter — costing the same pipeline partitioned and unpartitioned at increasing
volume:

| Scale | Rows | Partitions | Sized | Unsized | Sizing saves |
|---|---|---|---|---|---|
| 1× | 422,297 | 1 | ×1.206 | ×1.206 | **0 ms** |
| 2× | 844,594 | 1 | ×1.203 | ×1.203 | **0 ms** |
| 5× | 2,111,485 | 3 | ×1.201 | ×1.212 | 6,781 ms |
| 10× | 4,222,970 | 5 | ×1.201 | ×1.216 | 19,705 ms |
| 50× | 21,114,850 | 25 | ×1.200 | ×1.220 | 126,150 ms |
| 100× | 42,229,700 | 49 | ×1.200 | ×1.221 | 259,672 ms |

At a **32 MB** EPC it bites immediately — 1,049 ms even at 1×.

The sweep varies *volume*, not EPC size, because varying EPC at this project's volume
answers nothing: the working set fits every budget tried, so every row is identical.
That flatness is a real result and it is why the sweep is shaped this way.

## Two bugs the first version had, and what they were worth

Both produced confident, wrong numbers, and both came from the same source — using a
convenient aggregate instead of the right one.

1. **Charging every stage its layer's full duration.** Silver records one run
   covering conform, tokenise and SCD2; charging all three tripled Silver and
   doubled Gold, inflating the pipeline baseline from 126 s to 252 s. Cost is now
   charged per `cost_group` — the recorded unit — while placement stays per stage.
2. **Using the widest dataset's row width for everything.** That was the five-row
   facility table at 412 B/row, applied to 422k rows. Combined with a Bronze
   "baseline" of 5 ms — taken from promotion-bookkeeping runs rather than the actual
   write — it produced a modelled slowdown of **×25.25** for Bronze. Now
   row-weighted (116 B/row), and timings filter to runs that actually moved rows.

## Tests

17 unit tests in `tests/unit/test_placement.py`, split like the package.

The placement tests assert the *rule* and would hold on hardware: that tokenisation
moves a downstream stage out, that a quasi-identifier keeps it in, that the
tokenising stage is inside regardless of its input layer, that the API is outside.

The model tests assert internal consistency rather than any absolute number, since
no number here has been observed — including that partitioning never costs more
paging than not partitioning, which is the property the recommendation rests on.

One test pins the headline finding: every stage touching tenant data is inside. If a
future contract change moves one out, that test fails, and the failure is the signal
that the boundary shrank.

## What this did not do

- **Nothing in the pipeline changed.** C5 is an analysis. Finding that the
  architecture already places the boundary correctly is a result; moving something
  to make the planner look useful would not be.
- **The landing violation is reported, not fixed.**
- **No persistence.** Plans are printed or written to JSON with `--json`; there is
  no `platform.enclave_plan` table and no dashboard module. The API and the console
  do not surface C5.
- **No SEV-SNP model.** The cost structure differs materially — no EPC paging cliff —
  and modelling it from the same four parameters would be fitting a shape that is
  not there.

## Reproducing

```bash
confidentia plan placement            # the derivation, no costs
confidentia plan show --json p.json   # placement + measured inputs + modelled cost
confidentia plan sweep --epc 32       # where partition sizing starts to matter
pytest tests/unit/test_placement.py -q
```
