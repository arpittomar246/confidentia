# PROJECT_CONTEXT

Handoff document for **CONFIDENTIA**. Written so development can continue without
re-deriving anything. Current as of **Stage 7c** (119 tracked files, 8 CI jobs
green, 174 unit + 12 integration tests).

---

## 1. What this project is

**CONFIDENTIA — a confidential lakehouse platform: attested, privacy-preserving
data engineering on untrusted cloud infrastructure.**

M.Tech major project, Arpit Tomar (M25DE2005), IIT Jodhpur. It elevates the MTP1
presentation *"Secure Data Analytics Using Trusted Execution Environments"* from a
single-process TEE benchmark into a full medallion data platform where the
confidentiality boundary is a first-class design parameter.

**Research question.** What does it cost, architecturally and quantitatively, to
run a full medallion-architecture data platform — ingestion, CDC, streaming,
transformation, data quality, lineage, serving — under a hardware-enforced
confidentiality boundary, and where in that pipeline should the boundary be drawn?

### The five contributions

| | Contribution | Status |
|---|---|---|
| **C1** | Crypto-partitioned medallion storage (per-column encryption keys) | **Done** (Stage 4) |
| **C2** | Attestation-gated key release | **Done** (Stage 4) |
| **C3** | Cryptographically verifiable lineage (signed, Merkle-chained provenance) | **Not started** — primitives exist in `common/hashing.py` |
| **C4** | Disclosure control on Gold aggregates and DQ metrics | **Done** (Stage 6) |
| **C5** | Enclave placement planner / EPC-aware partition sizing | **Not started** |

### Honesty policy (non-negotiable)

There is **no confidential-computing hardware**. The TEE layer is an abstraction
with three backends (`sim`, `gramine-sgx`, `snp-cvm`) and defaults to `sim`.

- **Real measurements:** cryptography, storage, Spark, pipeline throughput, data
  quality, deduplication, disclosure suppression.
- **Modelled:** enclave-transition and EPC-paging costs only.

`TeeSettings.is_measured` is `False` under `sim`. Anything derived from it must be
labelled `MODELLED`. Under `sim` the process measures itself and could lie, so
attestation is a **mechanism, not a security boundary** — every policy and audit
row carries `hardware_rooted = false`.

---

## 2. Environment (critical — read before running anything)

| | |
|---|---|
| **Canonical repo** | `~/confidentia` **inside WSL2 Ubuntu 26.04** |
| From Windows | `\\wsl.localhost\Ubuntu\home\arpit\confidentia` |
| **Data root** | `~/confidentia-data` (never inside OneDrive — `Settings` refuses to start if it is) |
| Git remote | `https://github.com/arpittomar246/confidentia` (**private**), branch `main` |
| Python | 3.11.15 (provisioned by `uv`; Ubuntu 26.04 ships only 3.14, which PySpark rejects) |
| Java | OpenJDK 17.0.19 (Spark 3.5 supports 8/11/17 only) |
| PostgreSQL | 18.4, `wal_level = logical` |
| Host | Windows 11, **7.4 GB RAM**, WSL capped to 4 GB via `~/.wslconfig` |

**No Docker.** Everything runs as native Linux processes in WSL. `docker-compose.yml`
is maintained as the portable/cloud deliverable and validated in CI, but is not the
development path (ADR 0001).

### Required environment variables

```bash
export CONFIDENTIA_KEYSTORE_PASSPHRASE="dev-passphrase-not-for-real-use"
```

Without it the keystore cannot be opened. There is deliberately no fallback.

### Activating

```bash
cd ~/confidentia && source .venv/bin/activate
```

Install **from pyproject**, never package-by-package (this caused three CI failures):

```bash
pip install -e ".[dev,generators,storage,ingestion,spark,lake,api,observability]"
```

---

## 3. Architecture

```
SOURCES                    INGESTION                LANDING          BRONZE
 generated files    ──▶  FileCollector      ──▶  lake/landing/  ──▶  encrypted
 mock REST API      ──▶  RestCollector           (raw + envelope)    Delta
 Postgres WAL       ──▶  CdcCollector                                (PME)
                              │                                        │
                              ▼                                        ▼
                    platform.ingest_batch                         SILVER
                    (content-addressed                    conform → validate →
                     idempotency)                         quarantine → dedupe →
                                                          tokenise → SCD2
                                                                │
     KEY BROKER ◀── attestation evidence ──┐                    ▼
     (measurement allowlist,               │                 GOLD
      audited release)                     │        star schema (encrypted Delta)
            │                              │                    │
            ▼                              │        disclosure control (k=5 +
     KEY HIERARCHY                         │        complementary suppression)
     MEK → tenant KEK → per-class DEK      │                    │
                                           │                    ▼
                                           │        gold.* marts in PostgreSQL
     QUALITY ───────────────────────────────┘        (aggregates only)
     237 rules → detections (Delta) → scores (Postgres)
```

### Layer semantics

| Layer | Location | Partition | Meaning |
|---|---|---|---|
| landing | `lake/landing/<tenant>/<dataset>/batch=<digest16>/` | none | Exactly what arrived, checksum-verified |
| bronze | `lake/bronze/<tenant>/<dataset>/` | `ingest_date` | Typed, encrypted, still raw values |
| silver | `lake/silver/<tenant>/<dataset>/` | `event_date` | Conformed, deduped, tokenised, SCD2 |
| gold | `lake/gold/<tenant>/<table>/` | `event_date` (facts) | Star schema, encrypted |
| quality | `lake/quality/<tenant>/<dataset>/detections/` | none | Field-level detections |
| serving | PostgreSQL `gold` schema | — | Disclosure-controlled aggregates only |

**Bronze partitions on ingest date, Silver on event date.** Bronze answers "what did
we receive on Tuesday"; Silver answers "what happened on Tuesday".

---

## 4. Technology stack

| Layer | Choice | Version |
|---|---|---|
| Processing | PySpark (`local[2]`, 2 GB driver) | 3.5.3 |
| Table format | Delta Lake | delta-spark 3.2.1 |
| Column encryption | Parquet Modular Encryption (AES-256-GCM) | parquet-mr 1.13 |
| KMS client | `org.confidentia.crypto.LocalKmsClient` (own Java) | JDK 17 |
| Metadata store | PostgreSQL | 18.4 |
| DB driver | psycopg 3 + psycopg-pool (no ORM) | ≥3.2 |
| CDC | psycopg2 logical replication + `wal2json` | 2.6 |
| HTTP | httpx (client), FastAPI (mock source only) | — |
| CLI | Typer | ≥0.12 |
| Crypto | `cryptography` (AES-GCM, HKDF, scrypt, ECDSA P-256) | ≥43 |
| Lint / types | ruff, mypy | — |
| Data | pandas 3.0.5, numpy 2.1.3, pyarrow | — |

**Not used:** Kafka (Stage 9), Airflow (Stage 8), MinIO, Docker locally, Debezium
(consumer implemented instead), ORM, Great Expectations (own rule engine).

---

## 5. Folder structure

```
~/confidentia/
├── src/confidentia/
│   ├── cli.py                    790  Typer CLI, all commands
│   ├── catalog/contracts.py      395  Schema contracts + sensitivity classification
│   ├── common/
│   │   ├── hashing.py            118  Canonical JSON, Merkle root, chain verify (C3 primitives)
│   │   └── logging.py            106  structlog, secret redaction, TEE label binding
│   ├── config/settings.py        374  All settings; refuses OneDrive data_root
│   ├── crypto/
│   │   ├── keys.py               249  MEK→KEK→DEK hierarchy, sealed keystore
│   │   ├── attestation.py        285  Measurement, ECDSA evidence, verifier
│   │   ├── broker.py             309  Key Broker: policy, 4 refusal paths, audit
│   │   ├── tokenisation.py       270  Deterministic HMAC tokens, vault
│   │   └── java/LocalKmsClient.java   Parquet KmsClient (C2 integration seam)
│   ├── generators/               1,671  Two tenants, ground-truth manifest
│   ├── ingestion/                1,436  file/REST/CDC collectors + mock source API
│   ├── pipelines/
│   │   ├── bronze/writer.py      319
│   │   ├── silver/{transforms,scd2,writer}.py  839
│   │   └── gold/{disclosure,star,marts,writer}.py  1,338
│   ├── quality/{rules,scoring,runner}.py  1,032
│   ├── evaluation/{ground_truth,detections,scoring,runner}.py  665  Stage 7b harness
│   ├── spark/session.py          272  Session builder, PME wiring, column_keys_for_frame
│   └── store/{db,migrations,repositories}.py + sql/  612 + 8 migrations
├── tests/  (conftest + 8 unit + 1 integration)  1,890 lines
├── scripts/  (13 operational scripts, see §9)
├── docs/adr/ (7 ADRs)  docs/stage-reports/ (9 reports)
├── spikes/pme/                   Stage 1 feasibility spike (still runnable, used by CI)
├── .github/workflows/ci.yml      8 jobs (7 before Stage 7c)
├── docker-compose.yml            5 profiles (CI-validated, not used locally)
└── pyproject.toml

EMPTY (not yet built): backend/ frontend/ orchestration/ monitoring/ infrastructure/
                       src/confidentia/{streaming,benchmarks,tee,lineage}/

`evaluation/` is NOT a pipeline package. It is the only place besides
generators/writers.py allowed near the ground-truth answer key, and it reaches it
only through `iter_defects`. Do not add it to the pipeline list in
check_ground_truth_isolation.sh, and do not name the manifest file in it — the
guard greps the filename across all of src/confidentia.
```

---

## 6. Database schema

Two databases: `confidentia` (platform metadata + `gold` serving) and `ehr_oltp`
(CDC source). Migrations are forward-only SQL, applied in filename order, tracked
with checksums in `platform.schema_migration`. **Editing an applied migration is
refused** — add a new one.

### `platform` schema

Row counts are from the Stage 7c rebuild, which started from an empty database —
they are smaller than the 7b figures because they describe **one** clean pipeline
run rather than an accumulation of development runs.

| Table | Rows now | Purpose |
|---|---|---|
| `run` | 52 | One row per execution of any stage. `run_summary` view adds duration/rejection rate |
| `ingest_batch` | 19 | **Content-addressed idempotency.** PK is SHA-256 over ordered file digests. `seen_count` counts redeliveries. `metadata->>'bronze_promoted_at'` marks Bronze promotion |
| `ingest_file` | 33 | Per-file digest + `verified` flag |
| `watermark` | 100 | Resume points. TEXT value + `watermark_kind` (timestamp/lsn/offset/sequence/digest); `previous_value` allows rollback |
| `cdc_slot` | 1 | Replication slot state, `last_lsn`, `events_consumed` |
| `ingest_reject` | 15 | Records refused *before* Bronze (checksum/size/parse) |
| `attestation_policy` | 21 | Approved measurements, `tenants[]`, `hardware_rooted` |
| `key_release_audit` | 90 | Every release decision **including refusals**. Key IDs only, never material |
| `token_vault` | 0 | Token → **encrypted** original. Unpopulated (see §10) |
| `detokenisation_audit` | 0 | Every re-identification |
| `quarantine_record` | 5,312 | Silver failures, **redacted** (sensitive columns stripped) |
| `disclosure_log` | 6 | Per-publish suppression counts |
| `mart_partition_state` | 54 | Which event dates a mart reflects; `is_dirty` drives restatement |
| `restatement_log` | 1 | Late-arrival-triggered recomputations |
| `dq_rule` | 282 | Rule catalogue (dimension, family, field, severity). Up from 214: the four reference contracts added in 7c each derive their own rules |
| `dq_result` | — | Per-rule outcome per run (`rows_evaluated` kept, not just rate) |
| `dq_score` | 10 | Overall + six dimension scores + `promotion_blocked` |
| `dq_freshness` | — | Dataset-level lag vs SLA |
| `dq_evaluation` | 83 | **Stage 7b.** TP/FP/FN + P/R/F1 per family and per field vs the answer key. `scope` is one of `overall`/`family`/`field`/`unscoreable`. `rule_exists` separates "the rule missed" from "no rule existed". View: `dq_evaluation_latest` |
| `schema_migration` | 7 | Applied migrations with checksum |

### `gold` schema (Postgres serving)

Created dynamically by the publisher, one real table per mart:
`mart_encounters_daily` (4,542), `mart_diagnosis_prevalence` (247),
`mart_claims_monthly` (128), `mart_transactions_daily` (11,406),
`mart_merchant_risk` (82), `mart_fraud_by_hour` (120).

Every mart carries `_suppressed` (bool) and `_suppression_reason`. Suppressed cells
have `NULL` measures.

### `ehr_oltp` database

`patient` table with `REPLICA IDENTITY FULL` (so the WAL carries before-images) and
publication `confidentia_patient_pub`. Slot: `confidentia_patient_slot`
(`wal2json`, format-version 2).

### Lakehouse tables

Counts are from the Stage 7c rebuild. They differ slightly from the 7b figures
because the defect logic changed, not because the pipeline did.

| Delta table | Rows |
|---|---|
| bronze/healthcare/{patient, encounter, claim_line, patient_cdc} | 5,044 / 120,800 / 145,136 / — |
| bronze/healthcare/{facility, provider, insurance_plan} | 5 / 41 / 8 — **reference, added 7c** |
| bronze/financial/{account, transaction, chargeback} | 1,097 / 60,409 / 121 |
| bronze/financial/merchant | 136 — **reference, added 7c** |
| silver/healthcare/{patient, encounter, claim_line, dim_patient} | — / — / — / — |
| silver/financial/{account, transaction, chargeback} | — / — / — |

**Reference tables stop at Bronze.** They exist so the referential rules have a
parent to check against; `is_reference` on the contract keeps them out of Silver,
and Gold builds its own dimensions from the star schema.

`encounter` is ~2× because the file drop and the REST API each delivered every
record. That double delivery is deliberate and load-bearing: it is what Silver
deduplicates, and it is why Bronze uniqueness is evaluated **per source system**
(§11).
| gold/healthcare/{dim_date, dim_patient, dim_facility, dim_provider, dim_diagnosis, fact_encounter, fact_claim_line} | — |
| gold/financial/{dim_date, dim_account, dim_merchant, fact_transaction} | — |

---

## 7. APIs

**There is no backend API yet.** `backend/` is empty. Stage 10.

The only HTTP service is **`src/confidentia/ingestion/source_api.py`** — a *mock
source system*, not part of the platform:

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness |
| `GET /api/v1/{tenant}` | List datasets |
| `GET /api/v1/{tenant}/{dataset}?page&page_size&since` | Paged read, `since` is an **exclusive** lower bound |

Fault injection via env: `SOURCE_API_FAIL_RATE` (503s), `SOURCE_API_SLOW_RATE`,
`SOURCE_API_RATE_LIMIT` (429 + `Retry-After`).

```bash
uvicorn confidentia.ingestion.source_api:app --port 8081
```

---

## 8. Implemented features (Stages 1–7a)

### Generators (Stage 2)
Two tenants, chunked, deterministically seeded via `SeedSequence`. **12.2M rows in
99 s, peak RSS 583 MB.** 11 defect families injected at a configured rate, every one
recorded in a **ground-truth manifest** (`_defects.parquet`). Transactions carry
**no fraud label** — fraud is latent, confirmed 1–45 days later by chargebacks.
Patient mutations are a replayable script driving CDC/SCD2.

### Ingestion (Stage 3)
- **File**: SHA-256 verified per file; checksum mismatch is `TerminalError`, never retried
- **REST**: paged, retry with full jitter, honours `Retry-After`, watermark-resumed
- **CDC**: logical replication via `wal2json`; **acknowledges only after durable write**
- Content-addressed idempotency: re-ingesting unchanged input landed **0 records**

### Bronze (Stage 4) — C1 + C2
Per-column encryption keys by sensitivity class. **Verified by withholding keys**:
without the PHI key `primary_diagnosis_code` is DENIED while public columns still
read. Key release gated on attestation with four verified refusal paths (replayed
nonce, forged measurement, changed code, wrong tenant).

### Silver (Stage 5)
Conform → validate → quarantine → dedupe → tokenise. **Cross-source deduplication**:
encounter 120,798 → 59,121 because two collectors each delivered all 60,000.
SCD2 patient dimension: 5,877 versions, all three structural checks pass (0 keys
without exactly one current, 0 gaps/overlaps, 0 inverted intervals).

### Gold (Stage 6) — C4
Star schema + 6 marts. Disclosure control with **primary and complementary**
suppression. Restatement: 54 partitions over a 177-day span invalidated by late
chargebacks.

### Quality (Stage 7a)
237 rules across 6 dimensions, mostly **derived from the contract**. Field-level
detections. Bronze and Silver scored separately.

### Evaluation (Stage 7b)
The quality layer scored against the Stage 2 answer key on **Bronze**, strict
`(record_key, field, family)` matching, per family and per field. Overall recall
0.39–0.94 by dataset; see `docs/stage-reports/stage-07b.md` for the full table and
[ADR 0012](docs/adr/0012-evaluation-matching-criterion.md) for the criterion.

The harness found more about the *generator* and the *rule catalogue* than about
the rules:

- **`_break_format` is a no-op on all-digit fields.** 45/45 `postal_code`,
  1,342/1,361 `procedure_code`, 400/400 `mcc` corruptions left the value
  byte-identical. `format_violation` recall is depressed by defects that were never
  actually injected, not by rules that missed them.
- **`case_inconsistency` upper-cases**, so 400 injections into `currency` (already
  `USD`) changed nothing.
- **Five family/field pairs have no rule at all** — including nulls injected into
  `patient.first_name`, which the *contract declares nullable*. Recall 0.0 there is
  a coverage gap, not a miss.
- **`unit_inconsistency` recall 0.085** on transaction, as predicted in 7a. Its
  precision is 1.000 on transaction but 0.093 on claim_line: a fixed threshold is
  not portable across datasets.
- **`encounter/duplicate_record` precision 0.007** — the Bronze double delivery
  means every key is genuinely duplicated. The rule is right; uniqueness is not
  meaningful at a layer whose contract permits redelivery.

**None of these were fixed in 7b** — fixing the generator and then reporting the
improved numbers in the same stage would mean the harness was tuned until the
result looked good. They were fixed in 7c, separately and afterwards.

### Correction (Stage 7c)
All eight defects closed, then the pipeline rebuilt from nothing and re-measured.
Full write-up in `docs/stage-reports/stage-07c.md`.

| Dataset | P (7b → 7c) | R (7b → 7c) | F1 (7b → 7c) |
|---|---|---|---|
| healthcare/patient | 1.000 → 1.000 | 0.390 → **0.997** | 0.561 → **0.998** |
| healthcare/encounter | 0.053 → **0.850** | 0.943 → **1.000** | 0.101 → **0.919** |
| healthcare/claim_line | 0.814 → 0.818 | 0.798 → **0.961** | 0.806 → **0.884** |
| financial/account | 1.000 → 1.000 | 0.818 → **0.985** | 0.900 → **0.992** |
| financial/transaction | 0.998 → 0.998 | 0.712 → **0.959** | 0.831 → **0.978** |

Family/field pairs with no rule at all: 5 → **0**.

**These are not evidence that the rules improved.** Almost nothing changed about
the rules' method; what changed is that the answer key now describes defects that
exist and the catalogue covers the columns being corrupted. 7b was measuring the
measurement apparatus. 7c measures the rules.

The 7b baseline is kept at `~/confidentia-baselines/stage-07b-evaluation.csv`
(92 rows), exported before the rebuild — **outside the data root**, so a reset does
not take it.

---

## 9. Commands

### CLI (`python -m confidentia.cli ...`)

```
info                                    Resolved config + MODELLED/MEASURED label
db migrate | status
keys init | status | approve | audit
generate healthcare|financial|profile
ingest file|rest|cdc|slot|oltp
bronze write|verify
silver write|dimension|quarantine
gold build|disclosure|restatements
quality run|rules|scores|failures
evaluate quality|report               Stage 7b: P/R/F1 vs the ground-truth answer key
runs
```

### Full pipeline from scratch

```bash
cd ~/confidentia && source .venv/bin/activate
export CONFIDENTIA_KEYSTORE_PASSPHRASE="dev-passphrase-not-for-real-use"

python -m confidentia.cli db migrate
python -m confidentia.cli keys init          # once
python -m confidentia.cli keys approve       # after ANY change to a measured package

python -m confidentia.cli generate healthcare --rows 60000
python -m confidentia.cli generate financial  --rows 60000
python -m confidentia.cli ingest file healthcare
python -m confidentia.cli ingest file financial
python -m confidentia.cli ingest slot create
python -m confidentia.cli ingest oltp seed
python -m confidentia.cli ingest cdc
python -m confidentia.cli ingest oltp mutate --limit 2000
python -m confidentia.cli ingest cdc
python -m confidentia.cli bronze write healthcare
python -m confidentia.cli bronze write financial
python -m confidentia.cli silver write healthcare
python -m confidentia.cli silver write financial
python -m confidentia.cli silver dimension healthcare
python -m confidentia.cli gold build healthcare
python -m confidentia.cli gold build financial
python -m confidentia.cli quality run healthcare --layer bronze
python -m confidentia.cli quality run financial  --layer bronze
python -m confidentia.cli evaluate quality healthcare
python -m confidentia.cli evaluate quality financial
```

**Bronze quality must be the last quality run before `evaluate`.** The detections
table is overwritten per run, so a Silver run replaces the Bronze detections in
place. `evaluate` refuses rather than scoring the wrong layer, but the fix is
always to re-run `quality run <tenant> --layer bronze`.

**`keys approve` must be the last step before running anything** after editing
`crypto/`, `pipelines/`, `quality/` or `lineage/` — those are the measured packages,
and any change to them revokes key release.

### Scripts

| Script | Purpose |
|---|---|
| `ci_local.sh` | **Run before every push.** Runs CI's exact commands |
| `bootstrap_wsl.sh` | Fresh environment setup |
| `install_postgres.sh` | Postgres + `wal_level=logical` + databases |
| `build_kms_jar.sh` | Compile `LocalKmsClient` against PySpark's Parquet jars |
| `verify_encryption.sh` | Byte-level PME proof (PARE magic, keyless read denied) |
| `verify_bronze_isolation.sh` | Column-level key separation (C1 proof) |
| `verify_silver.sh` | Tokenisation + SCD2 point-in-time |
| `check_ground_truth_isolation.sh` | No pipeline package reads the answer key |
| `check_key_material_handling.py` | AST guard: keys never in SparkConf |
| `check_declared_dependencies.py` | AST guard: every import declared in pyproject |
| `check_plan_rule_coverage.py` | **7c.** Every injected family/field pair has a rule that *could* catch it. Checks existence, not accuracy — `unit_inconsistency` has a rule and finds a quarter of what is injected, and that passes |
| `reset_data.sh --yes` | **7c.** Destroys the lake, the raw files and all pipeline tracking state. **Keeps the keystore** — the one thing in the data root that is not regenerable |
| `rebuild_pipeline.sh` | **7c.** The full pipeline from nothing, in the one order that works (see below) |

`rebuild_pipeline.sh` encodes three orderings that are easy to get wrong by hand:
`keys approve` after any measured-package edit; the **REST redelivery of
`encounter`** before Bronze, because that is what creates the double delivery
Silver deduplicates; and **Bronze quality last**, because the detections table is
overwritten per run and `evaluate` refuses to score Silver detections against the
answer key.

### CI — 8 jobs, all green

`lint` (ruff + mypy) · `unit-tests` · `ground-truth-isolation` ·
`plan-rule-coverage` · `declared-dependencies` · `encryption-guarantee` ·
`generator-determinism` · `compose-validate`

Integration tests are **not** a CI job — they need PostgreSQL, which the workflow
does not provision. They run locally via `ci_local.sh` only. Adding a Postgres
service container is a small, worthwhile task.

---

## 10. Important design decisions

Full rationale in `docs/adr/`. The ones that constrain future work:

1. **WSL2 native processes, no Docker** (ADR 0001). Hadoop cannot initialise on
   Windows without unofficial `winutils` binaries.
2. **Parquet Modular Encryption, configured on the Hadoop Configuration — never as
   DataFrameWriter options** (ADR 0007). Delta **silently writes plaintext**
   otherwise: no error, no warning. Hence the mandatory `PARE` assertion after every
   write.
3. **`delta-rs` cannot read encrypted Parquet.** The JVM-free API layer therefore
   cannot query Bronze/Silver/Gold Delta at all. This is why marts are published to
   Postgres. **Any future API work must go through Postgres or Spark.**
4. **Key material never in SparkConf** (ADR 0010). `spark.*` properties render in
   the Spark UI Environment tab and Spark's default redaction regex misses
   `key.list`. Enforced by AST guard.
5. **Keys are derived, not stored**: MEK → tenant KEK → per-class DEK via HKDF.
   Tenant isolation is cryptographic — healthcare and financial share **zero** key
   material.
6. **Attestation gates key release**, and changing measured code revokes it. This is
   a real development tax and is intentional.
7. **Log-based CDC, acknowledged after durable write** (ADR 0009). Polling cannot see
   deletes, misses intra-interval updates, and provides no before-image.
8. **Deterministic tokenisation** so joins survive de-identification —
   **low-cardinality fields are refused** (`date_of_birth`, `sex`, `state`,
   `postal_code`, `currency`) because they could be brute-forced.
9. **Complementary suppression is mandatory.** Primary suppression alone leaves the
   value recoverable by subtraction from a published row total.
10. **`column_keys_for_frame`** (in `spark/session.py`) derives the column-key
    mapping from the frame's actual columns. Reusing a source contract's mapping on a
    derived table fails the write; this bit three separate writers before being
    centralised. Complex types **raise** rather than being silently skipped.
11. **Quality is layer-dependent.** Bronze = what the source sent (score against
    this); Silver = what we publish (looks better because Silver cleaned it).
12. **Severity-weighted scoring** (critical 10, major 3, minor 1) so the score cannot
    be improved by adding cosmetic checks.
13. **Ground-truth isolation.** No pipeline package may read the answer key. The
    evaluation harness therefore lives in `src/confidentia/evaluation/`, outside
    `quality/`, and reaches the manifest only through `iter_defects`. The guard also
    greps the manifest's *filename* across all of `src/confidentia`, so do not name
    it even in a docstring there.
14. **Evaluation matches strictly on `(record_key, field, family)`** (ADR 0012).
    Record-only matching would credit a rule that fired on the wrong column and push
    precision towards 1.0 for reasons unrelated to correctness. The cost — a
    detection that is right about the record and wrong about the reason counts as a
    false positive — is accepted and reported, not scored away.
15. **Undefined metrics are stored as NULL, never 0.0.** A family with no injected
    defects has no recall; writing zero would assert a failure that did not happen.
16. **Detections carry their `layer`, and the evaluator refuses the wrong one.**
    The detections table is overwritten per run, so a Silver run replaces Bronze
    detections in place — which had already happened before Stage 7b ran. Scoring
    those would measure the platform's own cleaning and produce a good number.
17. **The generator never injects a defect the platform has no rule for** (7c,
    enforced by `check_plan_rule_coverage.py`). An uncatchable defect is a permanent
    false negative, and it makes recall measure rule *coverage* rather than rule
    *quality* — the two are indistinguishable in the number. The check asserts a
    rule **exists**, never that it succeeds: `unit_inconsistency` has a rule and
    finds a quarter of what is injected, and that is a legitimate result.
18. **The corruption functions are deliberately blind to the detector's regexes.**
    Importing `quality.rules.PATTERNS` into the generator to guarantee a corruption
    fails them would make recall 1.0 by construction and measure nothing. The
    generator breaks structure; whether the rules notice is the question being asked.
19. **A recorded defect must have changed the data** (7c). `_record` drops any
    corruption whose value is unchanged, comparing nullness properly. This is what
    stops a no-op corruption entering the answer key, whatever a future corruption
    function does.
20. **Bronze uniqueness is per source system.** Bronze is "exactly what arrived",
    and two collectors legitimately delivering the same records is not a duplicate.
    Cross-source deduplication is Silver's job and Silver already does it.

---

## 11. Known issues and limitations

### Correctness / completeness
- **Gold and Silver are full-refresh.** Restatement marks partitions dirty but
  rebuilds whole tables. Incremental recompute is what experiment E3 measures.
- **SCD2 is transaction-time, not valid-time.** `valid_from` is when the platform
  *learned* of a change. A point-in-time query for January returns nothing because
  every version starts when the mutation replay ran. Bi-temporal is future work.
- **Token vault is implemented but unpopulated** — `vault_store`/`detokenise` work
  and are audited, but no pipeline writes to it. Tokenisation is one-way in practice.
- **Detections are overwritten per run** — no history, so no DQ trend analysis. They
  now carry a `layer` column, so `evaluate` refuses the wrong layer instead of
  silently scoring Silver detections against the answer key. Adding a run key on top
  of that would give trends and is small.
- **`promotion_blocked` is computed but nothing consumes it.** Wire into Stage 8.
- **Integration tests do not run in CI** — they need PostgreSQL and the workflow
  provisions none. They pass locally. Adding a `services: postgres` block to the
  workflow would close this and is a small task.
- **Only one `ingest_date` partition exists** (all ingestion happened on one day), so
  partition pruning is not yet demonstrated.

### Quality specifics
- **177 of 237 rules are cosmetic** (whitespace/encoding, one pair per string
  column). Rule count overstates coverage.
- **`unit_inconsistency` is still a fixed threshold**, not distribution-aware. 7c
  made the threshold per column (`UNIT_THRESHOLDS`) because one value was wrong in
  two directions at once — a clinical bill legitimately exceeds 50,000 and a retail
  card transaction does not — but the **method is unchanged**. Current recall
  **0.104** on transaction, **0.246** on claim_line. This is the family 7a predicted
  would score worst and it does. A distribution-aware check (median absolute
  deviation, or reconciling `billed`/`allowed`/`paid`) is the real fix and is future
  work.
- **Transaction defect plan omits `whitespace_padding` and `encoding_corruption`**
  (machine-generated, no human text). Recall is reported **per dataset**, never
  pooled, or their absence would look like a detection failure.

### Generator and rule-coverage defects — found by 7b, **all fixed in 7c**

The eight defects the harness exposed are closed and the pipeline was rebuilt from
nothing to re-measure. Details in `docs/stage-reports/stage-07c.md`; the durable
protection is `scripts/check_plan_rule_coverage.py`, a CI job asserting that every
family/field pair the generator injects has a rule that *could* catch it.

Do not re-open these without reading that report — several look like plausible
"improvements" that would actually re-introduce the bug (e.g. making the injector
consult `quality.rules.PATTERNS`, which would guarantee recall 1.0 by construction).

### What is still imperfect after 7c, and why

All of these are real findings or accepted costs. **None is an artefact.**

- **`format_violation` precision 0.430** on encounter and claim_line. 635 and 1,801
  whitespace-padded codes also fail the ICD-10 / CPT pattern — real detections of
  real corruption, counted as false positives because the manifest attributes them
  to `whitespace_padding`. The documented, accepted cost of strict matching (ADR
  0012, Decision 1). Recall on that family is **1.000**. This is the largest
  remaining number and the one most likely to be misread.
- **`unit_inconsistency` recall 0.104–0.246.** See above; method unchanged by design.
- **`range_violation` precision 0.958–0.981.** Legitimately extreme clinical amounts
  and transaction values. Working as intended.
- **`patient/null_violation` recall 0.990.** One record was nulled *and then*
  format-corrupted on the same field, and `_break_format` turns a null into `"??"`,
  so the value is no longer null. An overlapping-defect artefact, correctly recorded
  twice in the manifest.
- **`account/unit_inconsistency` 2 of 3.** Sample of three; not a signal.

### Security caveats
- **`sim` backend only.** Measurement is self-reported; not a security boundary.
- **Keystore is scrypt-wrapped and owner-only — passphrase protection, not hardware
  sealing.** Metadata records this.
- **Deterministic suppression is vulnerable to differencing attacks.** No query
  budget exists.
- **Laplace noise implemented but OFF.** Per-release epsilon is not the guarantee DP
  is taken to mean.

### Environment
- **Data is not backed up** — only code is. `~/confidentia-data` (111 MB + keystore +
  Postgres) is regenerable *except the keystore*: lose it and existing encrypted
  tables are unreadable forever. Back it up manually, never to the repo.
- pandas 3.0.5 / numpy 2.1.3 installed; `pyproject` pins `numpy<2.2`, `pandas>=2.2`.
  Tests pass but the pin is looser than the installed reality.

---

## 12. Completed vs pending

### Completed

| Stage | Deliverable | Commit |
|---|---|---|
| 1 | Setup, config, crypto primitives, **PME feasibility spike** | `1c9f2ec` |
| 2 | Generators with ground truth and latent labels | `8d54282` |
| 3 | Ingestion: file, REST, CDC, exactly-once | `0aed7c1` |
| 4 | Bronze: C1 + C2 | `de8d973` |
| 5 | Silver: dedupe, tokenise, SCD2 | `ce9f3f4` |
| 6 | Gold: star schema, C4, restatement | `cc54677` |
| 7a | Quality: 237 rules, scoring, detections | `a05bb4e` |
| 7b | Evaluation harness: P/R/F1 per family vs ground truth | `b3fc540` |
| 7c | Fixed the 8 defects 7b found; reference contracts; rebuilt and re-measured | *this session* |

### Pending

| Stage | Deliverable | Est. sessions |
|---|---|---|
| **8** | **Airflow orchestration, 5 DAGs, quality gate wiring** | **1–2** |
| 9 | Kafka streaming, Structured Streaming consumers | 1–2 |
| 9b | **C3 — verifiable signed lineage** | 1–2 |
| 10 | FastAPI backend, native OIDC auth, RBAC, ~40 endpoints | 2 |
| 11 | React/TS dashboard, 9 modules | 3–4 |
| 12 | Prometheus/Grafana/OTel | 1 |
| 13–16 | Docker, CI/CD, Terraform + Azure architecture | 1–2 |
| — | **C5** — enclave placement planner | 1 |
| — | Experiments E1–E8 with graphs | 2 |
| — | Final report, PPT, demo script, viva Q&A | 2 |

**Total remaining: ~12–17 sessions.**

---

## 13. Exact next steps — Stage 8

**Goal:** orchestrate the pipeline as Airflow DAGs and make the quality gate
actually gate something. `promotion_blocked` has been computed since Stage 7a and
nothing consumes it.

### Before starting, decide one thing

**Where Airflow runs.** ADR 0001 rules out Docker locally, and the host has 7.4 GB
RAM with WSL capped at 4 GB. Airflow's scheduler plus webserver plus a Spark driver
will not fit comfortably alongside PostgreSQL. Two viable options:

- `LocalExecutor` against the existing PostgreSQL instance, webserver started only
  when a screenshot is needed. Cheapest, and the DAGs are the deliverable, not the UI.
- `airflow dags test` only — no scheduler at all, DAGs exercised as code. Honest,
  but weaker as a demo.

Pick one and record it in an ADR. Do not install Airflow before deciding; a failed
install with a half-initialised metadata DB in the platform database is a mess.

### Where the code goes

`orchestration/dags/` and `orchestration/plugins/` already exist and are empty.
Airflow's metadata database must **not** be the `confidentia` database — put it in
its own, or the `platform` schema and Airflow's tables end up in one backup unit and
`db migrate`'s checksum tracking gets confusing.

DAGs should call the CLI or the Python entry points, **not** reimplement pipeline
logic. Every stage already has a callable: `run_quality`, `evaluate_dataset`,
`write_bronze`, and so on. A DAG that duplicates transformation logic will drift.

### The five DAGs

| DAG | Trigger | Shape |
|---|---|---|
| `ingest` | schedule | file → REST → CDC per tenant |
| `bronze_silver` | after ingest | bronze write → quality run (bronze) → **gate** → silver write |
| `gold` | after silver | silver dimension → gold build → disclosure |
| `quality` | schedule | quality run + `evaluate quality` |
| `restatement` | sensor on `mart_partition_state.is_dirty` | rebuild dirty marts |

### The gate is the point

`platform.dq_score.promotion_blocked` is already written per run. Stage 8 must make
a blocked score **stop the DAG** before Silver, and record why. Two properties worth
getting right:

- The gate must fail the task, not skip it. A skipped task looks like success in
  the Airflow UI and the next scheduled run proceeds as though nothing happened.
- A blocked promotion must be visible in `platform.run` with a reason, so
  `confidentia runs` explains the stall without opening Airflow.

### Watch out for

1. **`keys approve` is a deployment step.** Any DAG that runs after a code change
   fails key release until the measurement is re-approved. Either add an approval
   task guarded on measurement drift, or document it as a manual release step. Do
   not weaken the attestation check to make Airflow convenient.
2. **One Spark session per tenant.** `session_for_tenant` scopes PME configuration
   to one tenant; two tenants in one task risks writing one tenant's data under the
   other's key. Keep tenants in separate tasks.
3. **Bronze quality must run before `evaluate`**, and nothing may run Silver quality
   in between — the detections table is overwritten per run. The evaluator refuses
   rather than scoring the wrong layer, so a mis-ordered DAG fails loudly, but the
   ordering is still a real constraint.
4. **Memory.** `local[2]` with a 2 GB driver is already most of the WSL budget. Do
   not run two Spark tasks concurrently; set `max_active_tasks=1` on the Spark DAGs.

### Deliverables

- `orchestration/dags/*.py` — five DAGs
- `orchestration/plugins/` — the quality-gate operator or sensor
- ADR for the Airflow deployment decision above
- `store/sql/009_*.sql` if the gate needs its own audit table
- `docs/stage-reports/stage-08.md`
- CI: DAG import test (`python -c "import dagbag"`) is cheap and catches most breaks

---

## 14. Working practices that matter

1. **Run `scripts/ci_local.sh` before every push.** Three CI failures came from
   local verification that merely resembled CI.
2. **Install from `pyproject`, never by hand.** Hand-assembled environments drift.
3. **`keys approve` after editing any measured package**, and *after* lint-fix, not
   before — a formatter changes the measurement.
4. **Never edit source with PowerShell text cmdlets** (UTF-8 → cp1252 corruption) or
   with regex over code (it once injected an argument into a function definition).
   Use the editor or `ruff format`.
5. **Verify security properties at the byte level.** Round-trip success is not
   evidence of encryption; a keyless read must be attempted in a **separate OS
   process** because Parquet caches unwrapped keys per JVM.
6. **Push after every stage:** `git push origin main`.
