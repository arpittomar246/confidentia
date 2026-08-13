# PROJECT_CONTEXT

Handoff document for **CONFIDENTIA**. Written so development can continue without
re-deriving anything. Current as of **Stage 16 — all sixteen stages complete** (14 CI jobs green plus
security and release workflows,
286 unit + 74 integration tests,
**all five contributions delivered**, API and dashboard live, containers building,
Prometheus and Grafana wired to the pipeline's own tables).

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
| **C3** | Cryptographically verifiable lineage (signed, Merkle-chained provenance) | **Done** (Stage 9b) — per-record Merkle roots, ECDSA-signed, hash-chained in Postgres |
| **C4** | Disclosure control on Gold aggregates and DQ metrics | **Done** (Stage 6) |
| **C5** | Enclave placement planner / EPC-aware partition sizing | **Done** (C5) — derived placement, measured widths, modelled cost; found plaintext in the landing zone |

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

**Docker Engine is installed** (29.7.2, in the distro, started with `sudo dockerd`
because there is no systemd) but development still runs as native Linux processes.
ADR 0001 rejected Docker *Desktop* and its VM; Engine is a daemon and costs nothing
idle. Containers are the portable/cloud path and are now built and verified in CI
(ADR 0018), not the development path.

**Published container ports are overridable** -- the native Postgres, API and Vite
own 5432/8000/5173, so `docker compose up` against the defaults fails to bind.
`scripts/verify_docker.sh` publishes on 15432/18000/15173.

### Required environment variables

```bash
export CONFIDENTIA_KEYSTORE_PASSPHRASE="dev-passphrase-not-for-real-use"
```

Without it the keystore cannot be opened. There is deliberately no fallback.

### Activating

```bash
cd ~/confidentia && source .venv/bin/activate
```

Install **from pyproject**, never package-by-package (this caused three CI failures).

**The venv was created by `uv` and has no `pip` in it** — `pip install` fails with
`command not found`, which is not obvious when it happens mid-stage. Use:

```bash
uv pip install --python .venv/bin/python \
  -e ".[dev,generators,storage,ingestion,spark,lake,api,observability,stream]"
```

CI is a different environment and does use `pip` (`setup-python` provides it), so
the workflow files are correct as written and should not be changed to match this.

---

## 3. Architecture

```
SOURCES                    INGESTION                LANDING          BRONZE
 generated files    ──▶  FileCollector      ──▶  lake/landing/  ──▶  encrypted
 mock REST API      ──▶  RestCollector           (raw + envelope)    Delta
 Postgres WAL       ──▶  CdcCollector                                (PME)
 Kafka topic        ──▶  Structured Streaming ─────────────────────────▲
                         (bypasses landing; writes the SAME table)     │
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

**Streaming has no landing zone.** The batch collectors land files first because a
file drop is content-addressable and re-readable; a Kafka topic is neither, and the
offset *is* the durable record. The consumer therefore writes Bronze directly, and
`_source_file` carries `topic:partition:offset` — the streaming analogue of the
file+row a batch record traces back to.

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
| Orchestration | Apache Airflow, LocalExecutor, **its own virtualenv** | 2.11.2 |
| Streaming | Kafka (KRaft, native WSL, 512 MB heap) + Spark Structured Streaming | 3.8.0 |
| Kafka client | `confluent-kafka` (producer only; Spark reads via the Maven connector) | 2.15.0 |

**Not used:** MinIO, Docker locally, Debezium (consumer implemented instead), ORM,
Great Expectations (own rule engine), Avro/schema registry (JSON on the wire — see
`stage-09.md`).

**Airflow is installed in `~/airflow-venv`, never in `.venv`** (ADR 0013). It pins
its dependencies hard and would be free to move pandas/numpy/pyarrow underneath the
pipeline. The DAGs shell out to the project CLI; they cannot import `confidentia`
and are not meant to.

---

## 5. Folder structure

```
~/confidentia/
├── src/confidentia/
│   ├── cli.py                  1,278  Typer CLI, all commands
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
│   ├── quality/{rules,scoring,runner,gate}.py  1,250  gate.py is Stage 8
│   ├── evaluation/{ground_truth,detections,scoring,runner}.py  665  Stage 7b harness
│   ├── streaming/{producer,consumer,watermark}.py  588  Stage 9 Kafka path
│   ├── lineage/{merkle,chain,recorder,verify}.py  1,421  Stage 9b, C3
│   ├── api/{app,security,deps,errors}.py + routers/  1,832  Stage 10 serving layer
│   ├── tee/{placement,profile,model,planner}.py      C5 enclave placement planner
│   ├── spark/session.py          272  Session builder, PME wiring, column_keys_for_frame
│   └── store/{db,migrations,repositories}.py + sql/  612 + 10 migrations
├── orchestration/dags/           6 DAGs + confidentia_common.py   Stages 8-9b
├── tests/  (conftest + 12 unit + 1 integration + 2 api)  333 tests
├── scripts/  (operational scripts, see §9)
├── docs/adr/ (17 ADRs)  docs/stage-reports/ (18 reports)
├── frontend/                     Vite + React + TS, 9 modules (Stage 11)
├── docker/{backend,frontend,spark,airflow}/  four images, all built (Stage 13)
├── docs/api/openapi.json         Committed schema; CI fails if stale
├── spikes/pme/                   Stage 1 feasibility spike (still runnable, used by CI)
├── .github/workflows/          ci.yml (13 jobs), security.yml, release.yml
├── docker-compose.yml            5 profiles; core verified up, all images build
└── pyproject.toml

├── monitoring/{prometheus,grafana}/  scrape config, 8 alerts, dashboard as code

├── infrastructure/terraform/     Azure IaC: validated + Checkov clean, never applied
├── docs/architecture/azure.md    what would replace each simulated component

EMPTY (not yet built): backend/
                       src/confidentia/benchmarks/
                       orchestration/plugins/
                       src/confidentia/{benchmarks,tee}/

`backend/` stays empty: the serving layer lives in `src/confidentia/api/` so it is
covered by mypy's `files = ["src"]` and importable in tests without path handling.
Only `docker/backend/Dockerfile` keeps the old name.

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
| `promotion_gate` | — | **Stage 8.** Every promotion decision, passes included. `blocked` stops the DAG; `overridden` records a deliberate bypass (which requires a reason). Stores the `threshold` used, since policy changes and old decisions must stay interpretable. View: `promotion_blocked_now` |
| `dq_evaluation` | 83 | **Stage 7b.** TP/FP/FN + P/R/F1 per family and per field vs the answer key. `scope` is one of `overall`/`family`/`field`/`unscoreable`. `rule_exists` separates "the rule missed" from "no rule existed". View: `dq_evaluation_latest` |
| `schema_migration` | 8 | Applied migrations with checksum |

Airflow's ~48 metadata tables live in a **separate `airflow` database**, never in
`confidentia`. Migrations here are tracked by checksum, and a second tool creating
tables in the same database makes it far harder to say what the platform's schema
actually is.

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

### The serving layer (Stage 10) — `src/confidentia/api/`

**36 read-only endpoints** over `platform.*` and `gold.*`. Reads PostgreSQL, never
the lake (`delta-rs` cannot decrypt PME). Computes no metrics of its own, holds no
key material, is not in `MEASURED_PACKAGES`, and cannot trigger a pipeline run —
ADR 0016 for all four boundaries.

Placed under `src/`, **not** in top-level `backend/`, which remains empty. It
imports `store.db` and `config.settings` so it cannot ship separately; inside `src/`
it gets mypy, coverage and clean test imports for free.

| Router | Endpoints | Role |
|---|---|---|
| `/health`, `/ready`, `/meta` | 3 | public |
| `/runs` | 3 | viewer |
| `/quality` | 8 | viewer / operator / auditor |
| `/evaluation` | 3 | viewer |
| `/gate` | 3 | viewer / operator |
| `/ingestion` | 3 | operator |
| `/lineage` | 5 | viewer / operator / auditor |
| `/audit` | 5 | operator / auditor |
| `/marts` | 3 | viewer |

Auth: bearer token. `AUTH_MODE=oidc` verifies RS256/ES256 against a JWKS with `iss`
and `aud` checked and the algorithm list **asymmetric-only** (algorithm confusion);
`AUTH_MODE=local` verifies HS256 tokens from `confidentia api token` and is refused
outside `CONFIDENTIA_ENV=local`. Roles form a disclosure ladder
`viewer < operator < auditor`. No user table, no passwords.

OpenAPI schema committed at `docs/api/openapi.json`; CI job `openapi-schema` fails
if it is stale.

```bash
confidentia api serve              # http://127.0.0.1:8000/docs
confidentia api token me --role auditor
confidentia api routes             # every route and the role it needs
bash scripts/verify_api.sh         # live verification, needs a populated DB
```

### The mock source system (Stage 3)

**`src/confidentia/ingestion/source_api.py`** — a *source to ingest from*, not part
of the platform:

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

### Orchestration (Stage 8)
Five Airflow DAGs, and the change that matters more than any of them: the quality
score now **stops** something. `promotion_blocked` had been computed since 7a and
consumed by nothing. Full write-up in `docs/stage-reports/stage-08.md`,
rationale in [ADR 0013](docs/adr/0013-airflow-deployment-and-the-quality-gate.md).

```
ingest ──▶ landing ──▶ bronze_silver ──▶ bronze ──▶ quality
                             │
                             └──▶ silver ──▶ gold
restatement: @hourly sensor on dirty partitions ──▶ gold
```

Stages are chained by Airflow **Datasets**, not cron offsets: a medallion
architecture is a chain of datasets, and a cron offset is a guess about runtime.

**The gate exits non-zero and the task goes red.** `ShortCircuitOperator` was
rejected: a short-circuited task is *green* and its downstream tasks are skipped, so
Silver would build from a Bronze layer just judged unfit while the DAG looked
healthy. `scripts/verify_gate.sh` asserts the exit statuses because every other
property would survive a gate that printed `BLOCKED` and returned 0.

Three properties of the gate worth knowing before touching it:

- **A missing score blocks.** "No score" means the quality run never happened, so
  nothing was checked — not that nothing is wrong.
- **The threshold is policy, re-evaluated at gate time**, not the
  `promotion_blocked` flag frozen at scoring time. Otherwise raising the threshold
  does nothing until everything is re-scored.
- **Overrides require a reason** and are recorded as overrides. The alternative to
  supporting bypass explicitly is that people comment the gate out.

**Orderings that used to be tribal knowledge are now dependency edges**: Bronze
quality before `evaluate` (detections are overwritten per run); the CDC slot before
the OLTP mutation (a slot created afterwards has no WAL to replay).

### Streaming (Stage 9)
A Structured Streaming consumer writes the financial tenant's transactions into
**the same Bronze table** the collectors write (ADR 0014) — a fourth source, not a
second pipeline. Full write-up in `docs/stage-reports/stage-09.md`.

Bronze holds both, distinguished only by `_source_system`:

| `_source_system` | rows | delivered by |
|---|---|---|
| `financial` | 60,409 | file drop (batch) |
| `kafka` | 60,000 | Structured Streaming |

Measured on this box: producer **21,392 rec/s** unthrottled; consumer **~3,000
rec/s** steady state per micro-batch, **1,226 rec/s** end to end including ~15 s of
SparkSession and Kafka-connector startup. Quote both or neither — the first alone
overstates a scheduled drain, the second alone understates the engine by half.

Three properties, each asserted by `verify_streaming.sh` rather than assumed:

- **One table.** Grouping Bronze on `_source_system` must show both values.
- **Encrypted.** The `PARE` assertion runs after every micro-batch. A streaming
  writer is a fresh place to make ADR 0007's mistake.
- **Idempotent.** Re-consuming left the count at 80,409 → 80,409. Exactly-once is
  Delta's `txnAppId`/`txnVersion`, so a micro-batch replayed after a crash is
  refused by the transaction log rather than appended twice. This is what makes the
  scheduled DAG safe to kill at any point.

**The watermark deliberately does not solve late chargebacks.** A chargeback
confirming fraud 1–45 days later is `DROPPED` by any watermark that keeps state
bounded — pinned as a test. That case belongs to the batch restatement path Stage 6
built and Stage 8 schedules. Streaming handles the fresh path only.

### Lineage (Stage 9b) — C3

`platform.lineage_entry` holds one signed, hash-chained entry per table state. Each
commits to a **Merkle root over every record**, bound to the code measurement, the
released key IDs, the Delta version, the run id, and the **entry digests** of its
upstream datasets — so a Gold mart's provenance is followed backwards through Silver
and Bronze entries to the landing batch digests Stage 3 content-addressed.

- **Per record, and it was measured.** Per-run digesting costs 27 ms, per-file 8 ms,
  per-record 7,330 ms on the same 145k-row table — roughly 900×. Per-record is the
  only one that can prove *one row* belongs to the committed set, which is the
  claim. Overhead is ~37 µs/record + ~1.1 s/table: **20–32% of a Silver or Gold run,
  and 201% of a Bronze append** (ADR 0015).
- **Leaves are never stored.** The tree is rebuilt from the table for every proof,
  so a proof can only be produced if the data still hashes to the signed root.
  Proof generation *is* the tamper check.
- **The chain is in Postgres, not the lake.** Appending needs the tip read and
  extended in one transaction; `previous_digest` is `UNIQUE`, so the database
  refuses a fork structurally. `run_id` is `ON DELETE SET NULL`, not `CASCADE` —
  cascading would make `DELETE FROM platform.run` a tamper path.
- **Only `payload` is signed.** The columns beside it are projections, and
  verification compares them against the payload; an edit there is reported as
  tampering rather than drift.

`verify_lineage.sh` breaks four things and requires detection: an edited payload, an
edited column, a deleted mid-chain entry, and one row changed in the real encrypted
Bronze table (undone with Delta time travel).

**What it does not establish.** Under `sim` the platform measures itself and holds
its own signing key. The chain detects external tampering; it cannot detect a
dishonest platform, which signs a false record and produces a valid chain. Every
entry carries `hardware_rooted = false` and the CLI prints it on every recording.
Two narrower limits: failures are **local**, not a cascade to the end of the chain,
and **truncation at the tip is undetectable** without an external anchor.

---

## 9. Commands

### CLI (`python -m confidentia.cli ...`)

```
info                                    Resolved config + MODELLED/MEASURED label
db migrate | status
keys init | status | approve | audit | check
generate healthcare|financial|profile
ingest file|rest|cdc|slot|oltp
bronze write|verify
silver write|dimension|quarantine
gold build|disclosure|restatements|dirty
quality run|rules|scores|failures|gate
evaluate quality|report               Stage 7b: P/R/F1 vs the ground-truth answer key
stream produce|consume|status         Stage 9: Kafka -> the shared Bronze table
lineage record|verify|show|prove      Stage 9b: signed Merkle-chained provenance
runs
```

Three of these exist for the orchestrator and **signal through their exit status**,
which is easy to break by "tidying" them:

| Command | Exit 0 | Exit non-zero |
|---|---|---|
| `quality gate` | promotion allowed | **blocked** — this is what stops the DAG |
| `keys check` | measurement approved | needs `keys approve` after a measured-package edit |
| `gold dirty` | **there is work to do** | nothing to restate |

`gold dirty` inverts the usual convention on purpose: a sensor succeeds when its
condition is met, so "there is work" must be the success case.

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

**`keys approve` must be the last step before running anything** after editing a
measured package: `crypto/`, `pipelines/`, `quality/`, `lineage/`, `evaluation/`
and `streaming/`. Any change to them revokes key release.

The test for membership is **does this code see plaintext under released keys**,
not "is it part of the pipeline". `evaluation/` was added after Stage 8 on exactly
that basis: it is deliberately outside the pipeline, but it decrypts `record_key`
to match detections against the answer key, and the measurement answers "which code
touched this data". `streaming/` is listed before it exists, because the failure
mode of remembering later is a consumer writing encrypted Delta from unattested
code, which nothing would report.

The API is deliberately **not** in that list. It reads Postgres and never touches
plaintext under released keys, so adding it would mean editing a route revokes
every attestation policy entry — coupling presentation to the confidentiality
mechanism for nothing (ADR 0016).

### The API (Stage 10)

```bash
confidentia api serve                     # http://127.0.0.1:8000/docs
confidentia api serve --port 8077 --reload
confidentia api token me --role auditor   # local mode only; refuses under OIDC
confidentia api routes                    # every route + the role it enforces
python scripts/export_openapi.py          # rewrite docs/api/openapi.json
python scripts/export_openapi.py --check  # what CI runs
bash scripts/verify_api.sh                # live HTTP verification, needs data
```

`api serve` binds `127.0.0.1` by default. The compose service sets
`API_HOST=0.0.0.0` because it is inside a container; do not set that on the host.

### The dashboard (Stage 11)

```bash
bash scripts/serve_dashboard.sh          # API + built dashboard + a token, one command
bash scripts/serve_dashboard.sh --dev    # Vite dev server instead (needs port 5173 reachable)
bash scripts/serve_dashboard.sh --role viewer --no-build
```

Starts the API on 8000, builds the frontend against it, serves `frontend/dist` on
8001 with SPA history fallback and `no-store` on `index.html`, and prints a token.
Ctrl-C stops everything.

**Use this rather than `python -m http.server`.** Two things go wrong by hand: the
default build calls `/api` and expects Vite's proxy, so a plain static server 404s
every request; and a plain static server 404s on a reload of `/evaluation`, because
React Router owns that path and the server does not know it.

`npm run dev` is the better loop once port 5173 is reachable from Windows — it is
not on this machine (see the environment note in §11).

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
| `install_airflow.sh` | **8.** Airflow 2.11.2 into `~/airflow-venv` with upstream constraints, plus the separate `airflow` database. Never touches `.venv` |
| `airflow_env.sh` | **8.** `source` it, do not run it. Every `AIRFLOW__*` key has a counterpart in docker-compose.yml so local and portable cannot drift |
| `verify_gate.sh` | **8.** The gate's four exit statuses. Everything else about the gate would survive it printing `BLOCKED` and returning 0 |
| `verify_dags.sh` | **8.** All five DAGs run, and an unreachable threshold fails the DAG run with Silver never executing. Starts the mock source API itself — without it `confidentia_ingest` fails on five connection-refused retries and reads as a broken DAG rather than a missing precondition. Slower since 9b: the lineage tasks start Spark |
| `check_dag_cli_contract.py` | **8.** AST guard: every `cli(...)` a DAG invokes resolves against the real Typer tree. No Airflow needed |
| `backup_keystore.sh` | **Run this.** Copies the keystore outside the data root, keeping the last 3. The keystore is the only thing in the data root that is not regenerable, and `reset_data.sh` is the realistic threat to it |
| `simulate_ci_integration.sh` | Runs the `integration-tests` CI job's exact steps against throwaway databases, so the workflow can be verified without pushing |
| `services.sh status\|start` | **Run first.** Checks/starts Postgres and Kafka. Exists because Postgres has twice died mid-session under sustained Spark load |
| `install_kafka.sh` | **9.** Kafka 3.8.0, KRaft, native. Version matches the image docker-compose.yml pins |
| `kafka.sh start\|stop\|status\|topics\|reset` | **9.** 512 MB heap, matching the compose limit. `reset` also reformats storage |
| `verify_streaming.sh` | **9.** One Bronze table, encrypted, and idempotent on replay — the third is the one worth having |
| `record_all_lineage.sh` | **9b.** A chain entry per table, bottom layer first — Silver cites Bronze's entry digest, so the order is a correctness constraint |
| `verify_lineage.sh` | **9b.** Breaks four things — a signed payload, a denormalised column, a mid-chain entry, and a row in the real encrypted table — and requires the verifier to catch each for the right reason |
| `lineage_tamper.py` | **9b.** Used by the above. Edits a real Delta table with the column keys set, and restores it by time travel. Not underscore-prefixed because `verify_lineage.sh` depends on it — a gitignored dependency would break on a fresh clone |
| `lineage_granularity_bench.py` | **9b.** Reproduces ADR 0015's per-run / per-file / per-record measurement |
| `lineage_overhead.sql` | **9b.** Digest cost per record, from `platform.run` metadata |
| `pick_business_key.py` | Prints one business-key value from a landing file. No keys, no Spark — so a demo value is independent of the machinery being demonstrated |

`rebuild_pipeline.sh` encodes three orderings that are easy to get wrong by hand:
`keys approve` after any measured-package edit; the **REST redelivery of
`encounter`** before Bronze, because that is what creates the double delivery
Silver deduplicates; and **Bronze quality last**, because the detections table is
overwritten per run and `evaluate` refuses to score Silver detections against the
answer key.

### Airflow

```bash
bash scripts/install_airflow.sh            # once
source scripts/airflow_env.sh
export CONFIDENTIA_KEYSTORE_PASSPHRASE="dev-passphrase-not-for-real-use"

airflow dags test confidentia_bronze_silver 2026-08-09   # no scheduler needed
bash scripts/verify_dags.sh
```

For the UI: `airflow scheduler` and `airflow webserver --port 8080` in separate
shells, **both with the passphrase in their environment** — `BashOperator`
inherits it, and the platform has no fallback for it anywhere.

### CI — 13 jobs, all green

`lint` (ruff + mypy) · `unit-tests` · **`integration-tests`** ·
`ground-truth-isolation` · `plan-rule-coverage` · `dag-cli-contract` ·
`declared-dependencies` · **`openapi-schema`** · **`frontend`** · `encryption-guarantee` ·
`generator-determinism` · `compose-validate` · **`container-build`**

`compose-validate` used to assert only that the YAML parsed, and passed for eleven
stages against four Dockerfiles that did not exist. It now runs
`scripts/verify_docker.sh --static`, which also checks that every referenced
dockerfile and bind mount is really there. `container-build` does the expensive
half: builds all four images and brings the `core` profile up.

`verify_docker.sh` also brings up Prometheus and Grafana and asserts the scrape
target is `up`, that a metric is queryable through Prometheus, and that Grafana's
datasource and dashboard provisioned -- not merely that the containers started.

`integration-tests` runs against a `postgres:18` service container and applies all
migrations to an **empty** database first. That last part is the point: the tests
passed from Stage 3 onwards against a database that already had this project's data
in it, so "works locally" never established that they work from nothing.
`scripts/simulate_ci_integration.sh` runs the job's exact steps against throwaway
databases, which is how the workflow was verified without pushing to find out.

`ruff` covers `orchestration` too, but **`mypy` does not**: the DAGs import
`airflow`, which is deliberately absent from the project venv, so type-checking
them there would only report the missing library. `check_dag_cli_contract.py` is
what covers that gap — it verifies the one thing static typing would have caught,
which is that the commands the DAGs invoke exist.

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
21. **The promotion gate fails; it never skips** (ADR 0013). A skipped Airflow task
    is *green* and its downstream tasks still run, so a `ShortCircuitOperator` gate
    would let Silver build from a Bronze layer just judged unfit while the DAG
    looked healthy. The exit status is the mechanism, and `verify_gate.sh` asserts
    it because nothing else would notice a gate that printed `BLOCKED` and
    returned 0.
22. **A missing quality score blocks promotion.** No score means the quality run
    never happened, so nothing has been checked. A gate that opens when its input is
    absent protects nothing at exactly the moment the pipeline is already broken.
23. **Overrides require a reason and are recorded as overrides.** Bypassing a
    known-bad gate is legitimate; doing it silently is not, and the alternative to
    supporting it explicitly is that people comment the gate out.
24. **Airflow never imports `confidentia`** (ADR 0013). It runs in its own
    virtualenv and shells out to the CLI, so orchestration knows *when* to run
    things and never *how* they work. `check_dag_cli_contract.py` covers the seam
    that costs.

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
- ~~`promotion_blocked` is computed but nothing consumes it.~~ **Closed in Stage 8**
  — the gate sits between Bronze and Silver and fails the task. Note the gate now
  re-decides against the *current* threshold rather than reading this flag, so
  `dq_score.promotion_blocked` is a historical record, not the control.
- ~~Integration tests do not run in CI.~~ **Closed after Stage 8** — the
  `integration-tests` job runs against a `postgres:18` service container, applying
  every migration to an empty database first.
- **Only one `ingest_date` partition exists** (all ingestion happened on one day), so
  partition pruning is not yet demonstrated.

### Environment flakiness — read this before debugging a mystery
- **PostgreSQL has twice been found dead mid-session**, both times during sustained
  Spark work, both times abruptly: the log shows normal checkpoints and then an
  end-of-recovery restart, with no shutdown request between. Kafka survived both, so
  it is not the WSL VM stopping, and no OOM kill was recorded. **The cause is not
  established.** The symptom is `PoolTimeout: couldn't get a connection` from the
  middle of a long run. `scripts/services.sh start` checks first and is now the
  opening step of `rebuild_pipeline.sh`; the fix if it happens is
  `sudo service postgresql start`.

### Streaming (Stage 9)
- **Only the financial tenant streams.** Healthcare arrives by file and CDC, which
  is realistic — an EHR is not a message bus — so the streaming path is
  demonstrated on one dataset.
- **No Silver streaming.** The consumer stops at Bronze, so end-to-end streaming
  latency is not measurable. Streaming Silver needs stateful cross-source
  deduplication, which is materially harder than the append this stage does.
- **`acks=1`, not `acks=all`.** On a single broker they are the same guarantee at
  different latencies; claiming otherwise would be a replication claim this
  topology cannot make.
- **The Kafka log lives under the data root**, so `reset_data.sh` clears it. That is
  deliberate — resetting the lake without the topic would leave the consumer's
  checkpoint holding offsets into data that no longer corresponds to Bronze. Reset
  both together, or neither.

### Lineage (Stage 9b)
- **`sim` means self-measured and self-signed.** The chain detects external
  tampering and cannot detect a dishonest platform. Every entry says so.
- **Failures are local, not a cascade.** A tampered entry fails at itself and at
  the link after it, not at every entry to the end of the chain — which is how hash
  chains are usually described. A unit test asserting the cascade failed, and the
  claim was narrowed rather than the test loosened.
- **Truncating the chain at the tip is undetectable.** A shorter chain verifies
  perfectly. Closing it needs the head digest published somewhere the platform
  cannot rewrite.
- **Tampering localises to a table, not a row.** One edited row invalidates the
  proof for every row in that table, because the root commits to the whole set.
- **Recording is a step after the write, not inside the writers**, so there is a
  window where a table has changed and no entry says so. The DAG edges close it;
  running a write by hand and forgetting `lineage record` reopens it.
- **`lineage verify --deep` reads and decrypts every table.** It is minutes, not
  seconds, and is opt-in for that reason.

### Orchestration (Stage 8)
- **The scheduler is not run persistently.** DAGs are verified with
  `airflow dags test`, which executes the same tasks in-process. On 3.9 GB a
  scheduler plus webserver competing with a 2 GB Spark driver is what the memory
  budget cannot absorb. Start them manually for a UI screenshot.
- **No SLA or alerting.** `email_on_failure` is off and no callback is wired. The
  gate's audit trail is the notification channel — enough for one operator, not for
  a team.
- **`max_active_tasks=1` everywhere**, so the two tenants run serially despite
  sharing nothing and being cryptographically isolated. A memory constraint, not a
  design one; the first thing to relax on larger hardware.
- **Backfill is untested.** `catchup=False` everywhere and every DAG is triggered or
  `@once`, so the historical-reprocessing path has never run.
- **`orchestration/plugins/` is empty.** The gate is a `BashOperator` calling the
  CLI, which is deliberate (ADR 0013) — there was nothing a custom operator would
  have added except a second place for the logic to live.

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
- **Only the keystore is irreplaceable.** Everything else in `~/confidentia-data`
  regenerates in about ten minutes via `reset_data.sh` + `rebuild_pipeline.sh`; lose
  the keystore and every existing encrypted table is unreadable forever, with no
  recovery path and no escrow by design. `scripts/backup_keystore.sh` copies it to
  `~/confidentia-backups/` (outside the repo *and* outside the data root, keeping
  the last 3) — **run it after `keys init`, and never commit it.**
  That defends against `reset_data.sh`, which is the realistic threat. It does not
  defend against losing the disk; copying it off-machine is a decision about where
  key material may live and is deliberately left to a human.
- pandas 3.0.5 / numpy 2.1.3 installed; `pyproject` pins `numpy<2.2`, `pandas>=2.2`.
  Tests pass but the pin is looser than the installed reality.

### Serving layer (Stage 10)
- **No Dockerfile exists for any compose service.** `docker/{backend,frontend,spark,
  airflow}/Dockerfile` are all missing, so no profile can be built. The
  `compose-validate` CI job passes anyway because `docker compose config` validates
  syntax and interpolation only — it is a weaker check than its name implies.
  Stage 13.
- **No identity provider has been integrated.** The OIDC path is tested against a
  locally generated RSA keypair with an injected JWKS lookup, which does exercise
  signature verification, the `iss`/`aud` checks and the asymmetric-only algorithm
  restriction. It has never spoken to Entra, Keycloak or Auth0.
- **No rate limiting.** `/health` and `/ready` are unauthenticated, and `/ready`
  takes a pooled connection on every call.
- **`/marts/{name}` has no `ORDER BY`.** Mart schemas are inferred at publish time
  and differ, so no column is guaranteed to exist. Pages can shift under a client if
  a mart is republished mid-scroll. The fix is a stable sort key in `MartSpec`,
  which is a Gold change.
- **`argon2-cffi` is declared and unused.** There is no password path anywhere and
  there must not be; the dependency predates the decision.
- `/quality/quarantine` is the slowest endpoint at ~22 ms p50 against 30,951 rows.
  No index added — the plan has not been read, and guessing at one is how useless
  indexes get committed.

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
| 7c | Fixed the 8 defects 7b found; reference contracts; rebuilt and re-measured | `f027465` |
| 8 | Airflow: 5 DAGs, the promotion gate, attestation preflight | `ff95d1e` |
| 8+ | Attested boundary widened, integration tests in CI, keystore backup | `f63b5c3` |
| 9 | Streaming: Kafka -> the shared Bronze table, exactly-once | `317c7e3` |
| 9b | **C3** — signed, Merkle-chained provenance; 4 tamper claims verified | `a7b48b1` |
| 10 | Serving layer: 36 read-only endpoints, JWT auth, 3-role ladder, OpenAPI | `ab52185` |
| 11 | Dashboard: 9 modules, generated API types, 3 defects found by running it | `3733292` |
| C5 | Placement planner: the boundary derived, costed and swept | `b91c3e0` |
| 13 | Containers: four images built and verified; 5 defects only building found | `07a9938` |
| 12 | Observability: 8 platform gauges from pipeline tables, 8 alerts, Grafana as code | `52c3491` |
| 14 | Supply chain: scanning with a policy, SBOMs, Sigstore provenance | `f541117` |
| 15–16 | Azure IaC + the cloud architecture: validated, Checkov clean, never applied | *this session* |

### Pending

| Stage | Deliverable | Est. sessions |
|---|---|---|
| — | Experiments E1–E8 with graphs | 2 |
| — | Final report, PPT, demo script, viva Q&A | 2 |

**Total remaining: ~2–4 sessions. All sixteen stages and all five contributions
are delivered.** What is left is not build work: experiments E1–E8 with graphs,
and the report, PPT, demo script and viva preparation.

---

## 13. Exact next steps — Stage 12/13 (packaging), then the writing

**All five contributions are delivered.** What remains is packaging, experiments and
the report — none of it research, all of it necessary.

### Next: Stage 13 first, not Stage 12

`compose-validate` is a green CI job asserting a compose file that cannot be built:
`docker/{backend,spark,airflow}/Dockerfile` do not exist. `docker/frontend/` landed
at Stage 11 and is the template — multi-stage, a real healthcheck, and an nginx
config whose two rules both came from observed failures.

Do that before Stage 12, because a monitoring stack is the one thing that genuinely
wants containers, and because a job that asserts less than its name says is worse
than no job.

The API image is the easy one: `pip install -e ".[api,storage]"`, `confidentia api
serve`, `API_HOST=0.0.0.0`. Spark and Airflow are harder — both need the JDK, the
KMS jar built by `scripts/build_kms_jar.sh`, and the keystore passphrase, which must
arrive as a secret and not a build arg.

### Then Stage 12 — Prometheus/Grafana/OTel

`monitoring/` is empty and compose already mounts `./monitoring/prometheus` and
`./monitoring/grafana`. `prometheus-client` is a declared dependency and
`api/deps.py` already times every query and logs the slow ones, so the metric is one
registry away.

Watch for the trap this project keeps hitting: a Grafana dashboard that renders is
not evidence that anything is measured correctly. Whatever it displays should be
traceable to a table the pipeline writes, the same rule as ADR 0016.

### Experiments E1–E8, and what C5 changed about them

C5 produced numbers that belong in the report and are cheap to regenerate:
`confidentia plan show` and `plan sweep`. Two of them are worth a figure — the
placement derivation, and the volume sweep showing where partition sizing starts to
pay.

**The landing-zone violation should be closed or explicitly accepted before the
report claims data is protected at rest.** ADR 0017 records both remedies. It is a
real hole: landing files are `PAR1`, and one patient file's raw bytes contain 19
SSN-shaped strings.

### Optional, and genuinely useful

- **A Playwright smoke test for the dashboard.** The three Stage 11 defects were
  found by a human driving a browser, which is not repeatable. All three would have
  been caught by signing in and asserting each module renders.
- **Persist C5 plans.** They print or write JSON; there is no `platform.enclave_plan`
  table and the API does not serve them, so the dashboard cannot show the boundary.
- **Allow Node through Windows Firewall** so `npm run dev` is reachable. Until then,
  `bash scripts/serve_dashboard.sh`.

### Still true, and still the rules

1. **Never present a modelled number as a measurement.** `TeeSettings.is_measured`
   is the switch, `MODELLED` is the label, and C5 is the largest consumer: its
   placement is derived and its costs are modelled, and the two are kept in
   different modules so the distinction is visible from the import list.
2. **No second source of truth.** Every number comes from a table or view the
   pipeline writes. Why `/gate/blocked` reads a view, why the API micro-averages in
   SQL, why the dashboard computes nothing.
3. **`keys approve` after editing a measured package** — `crypto/`, `pipelines/`,
   `quality/`, `lineage/`, `evaluation/`, `streaming/`. The API, the frontend and
   `tee/` are deliberately outside that boundary: the planner reads contracts and
   run history, never plaintext.
4. **Run `scripts/ci_local.sh` before every push.** 12 jobs.

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
