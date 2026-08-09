# PROJECT_CONTEXT

Handoff document for **CONFIDENTIA**. Written so development can continue without
re-deriving anything. Current as of commit `69a3bd8` (16 commits, 105 tracked
files).

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
│   ├── quality/{rules,scoring,runner}.py  1,027
│   ├── spark/session.py          272  Session builder, PME wiring, column_keys_for_frame
│   └── store/{db,migrations,repositories}.py + sql/  608 + 7 migrations
├── tests/  (conftest + 7 unit + 1 integration)  1,510 lines
├── scripts/  (10 operational scripts, see §9)
├── docs/adr/ (6 ADRs)  docs/stage-reports/ (7 reports)
├── spikes/pme/                   Stage 1 feasibility spike (still runnable, used by CI)
├── .github/workflows/ci.yml      8 jobs
├── docker-compose.yml            5 profiles (CI-validated, not used locally)
└── pyproject.toml

EMPTY (not yet built): backend/ frontend/ orchestration/ monitoring/ infrastructure/
                       src/confidentia/{streaming,benchmarks,tee,lineage}/
```

---

## 6. Database schema

Two databases: `confidentia` (platform metadata + `gold` serving) and `ehr_oltp`
(CDC source). Migrations are forward-only SQL, applied in filename order, tracked
with checksums in `platform.schema_migration`. **Editing an applied migration is
refused** — add a new one.

### `platform` schema

| Table | Rows now | Purpose |
|---|---|---|
| `run` | 138 | One row per execution of any stage. `run_summary` view adds duration/rejection rate |
| `ingest_batch` | 85 | **Content-addressed idempotency.** PK is SHA-256 over ordered file digests. `seen_count` counts redeliveries. `metadata->>'bronze_promoted_at'` marks Bronze promotion |
| `ingest_file` | 33 | Per-file digest + `verified` flag |
| `watermark` | 100 | Resume points. TEXT value + `watermark_kind` (timestamp/lsn/offset/sequence/digest); `previous_value` allows rollback |
| `cdc_slot` | 1 | Replication slot state, `last_lsn`, `events_consumed` |
| `ingest_reject` | 15 | Records refused *before* Bronze (checksum/size/parse) |
| `attestation_policy` | 21 | Approved measurements, `tenants[]`, `hardware_rooted` |
| `key_release_audit` | 90 | Every release decision **including refusals**. Key IDs only, never material |
| `token_vault` | 0 | Token → **encrypted** original. Unpopulated (see §10) |
| `detokenisation_audit` | 0 | Every re-identification |
| `quarantine_record` | 11,124 | Silver failures, **redacted** (sensitive columns stripped) |
| `disclosure_log` | 6 | Per-publish suppression counts |
| `mart_partition_state` | 54 | Which event dates a mart reflects; `is_dirty` drives restatement |
| `restatement_log` | 1 | Late-arrival-triggered recomputations |
| `dq_rule` | 214 | Rule catalogue (dimension, family, field, severity) |
| `dq_result` | 302 | Per-rule outcome per run (`rows_evaluated` kept, not just rate) |
| `dq_score` | 8 | Overall + six dimension scores + `promotion_blocked` |
| `dq_freshness` | 12 | Dataset-level lag vs SLA |
| `schema_migration` | 6 | Applied migrations with checksum |

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

| Delta table | Rows |
|---|---|
| bronze/healthcare/{patient, encounter, claim_line, patient_cdc} | 5,038 / 120,798 / 145,138 / 6,400 |
| bronze/financial/{account, transaction, chargeback} | 1,097 / 60,501 / 121 |
| silver/healthcare/{patient, encounter, claim_line, dim_patient} | 5,000 / 59,121 / 141,525 / 5,877 |
| silver/financial/{account, transaction, chargeback} | 1,074 / 58,899 / 121 |
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
```

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

### CI — 8 jobs, all green

`lint` (ruff + mypy) · `unit-tests` · `integration-tests`(local only) ·
`ground-truth-isolation` · `declared-dependencies` · `encryption-guarantee` ·
`generator-determinism` · `compose-validate`

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
13. **Ground-truth isolation.** No pipeline package may read `_defects.parquet`.
    Stage 7b's evaluation harness must therefore live **outside** `quality/` —
    put it in `src/confidentia/benchmarks/` or a new `evaluation/` package.

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
- **Detections are overwritten per run** — no history, so no DQ trend analysis.
- **`promotion_blocked` is computed but nothing consumes it.** Wire into Stage 8.
- **Only one `ingest_date` partition exists** (all ingestion happened on one day), so
  partition pruning is not yet demonstrated.

### Quality specifics
- **177 of 237 rules are cosmetic** (whitespace/encoding, one pair per string
  column). Rule count overstates coverage.
- **`unit_inconsistency` uses a fixed threshold** (>50,000), not distribution-aware.
  It caught only 17 of an expected ~190. Deliberate — it is the family expected to
  score worst, and 7b will quantify the miss.
- **Transaction defect plan omits `whitespace_padding` and `encoding_corruption`**
  (machine-generated, no human text). 7b must report recall **per dataset**, not
  pooled, or their absence looks like a detection failure.

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

### Pending

| Stage | Deliverable | Est. sessions |
|---|---|---|
| **7b** | **Evaluation harness — precision/recall/F1 per family vs ground truth** | **1** |
| 8 | Airflow orchestration, 5 DAGs, quality gate wiring | 1–2 |
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

## 13. Exact next steps — Stage 7b

**Goal:** score the Stage 7a detections against the Stage 2 ground-truth manifest,
producing precision, recall and F1 **per defect family**.

### Where the code goes

**`src/confidentia/evaluation/`** — a new package, *not* `quality/`.
`check_ground_truth_isolation.sh` forbids any pipeline package from referencing
`_defects.parquet`, `iter_defects` or `latent_fraud`. Add `evaluation` to neither the
pipeline list nor the guard's forbidden set; it is evaluation tooling, like
`cli.py profile`.

### Inputs

| Side | Location | Key columns |
|---|---|---|
| Detections | `lake/quality/<tenant>/<dataset>/detections` (Delta) | `record_key`, `family`, `field`, `rule_id`, `severity` |
| Ground truth | `<data_root>/raw/<tenant>/<dataset>/_defects.parquet` | `record_key`, `defect_type`, `field`, `severity` |

Both use the **same family vocabulary** by design — no translation layer.

### The matching criterion (decide and document)

Use the **strict** criterion: a true positive requires
`(record_key, field, family)` to match. Loose (record-only) matching would inflate
precision and mean little. Expect some families to score badly — that is the result.

- **TP** — detection matches a ground-truth defect on all three
- **FP** — detection with no matching defect
- **FN** — defect with no matching detection

### Must score against **Bronze**, not Silver

Silver has already quarantined the defects, so recall there measures the platform's
own cleaning. `quality run <tenant> --layer bronze` first.

### Known complications

1. **Duplicate defects append rows.** `duplicate_record` entries in the manifest
   refer to appended near-duplicates; the detection side flags *both* copies. Decide
   whether that is 1 TP or 2 and state it.
2. **Overlapping defects.** One record can carry several defects on the same field
   (e.g. nulled then format-corrupted). Join on all three keys, not two.
3. **`latent_fraud` is a label, not a defect** — exclude it from DQ scoring; it
   belongs to the analytics evaluation.
4. **Bronze contains the double delivery**, so `encounter` record keys appear twice.
   Deduplicate the detection side on `(record_key, field, family)` before scoring, or
   recall will exceed 1.0.
5. **Platform-only families** (`future_event`, `late_arrival`) have no ground truth —
   report them separately as "detected, unscoreable".

### Deliverables

- `evaluation/ground_truth.py` — load and normalise the answer key
- `evaluation/scoring.py` — TP/FP/FN, precision, recall, F1 per family per dataset
- `store/sql/008_evaluation.sql` — `platform.dq_evaluation` table
- `cli.py`: `evaluate quality <tenant> [dataset]`
- `tests/unit/test_evaluation.py` — matching logic with synthetic fixtures
- `docs/stage-reports/stage-07b.md` — the headline table
- ADR if the matching criterion needs defending at length

### Expected shape of the result

`null_violation` and `referential_orphan` should score near 1.0 on both metrics.
`unit_inconsistency` should have **poor recall** (~10%) — a fixed threshold cannot
catch cents-as-dollars when the value stays plausible. `whitespace_padding` and
`encoding_corruption` should be near-perfect where they were injected and absent for
`transaction`. **The low numbers are the interesting ones and must not be hidden.**

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
