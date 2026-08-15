# CONFIDENTIA — A Confidential Lakehouse Platform

**Attested, privacy-preserving data engineering on untrusted cloud infrastructure**

M.Tech Major Project (MTP-2) · Arpit Tomar · M25DE2005 · IIT Jodhpur

---

> **A note on citations.** This document contains no references section. Every
> factual claim in it is drawn from this project's own code, measurements and
> commit history, and each is traceable to a stage report or an ADR. The
> literature review and citations are the author's to add; nothing here should be
> attributed to a source it does not name.

---

## Abstract

Confidential computing promises that data can be processed on infrastructure the
data owner does not trust. Most demonstrations of it are single-process
benchmarks — an enclave doing one computation. This project asks what happens when
the same guarantee is asked of a *whole data platform*: ingestion, change data
capture, streaming, transformation, data quality, lineage and serving.

CONFIDENTIA is that platform. It implements a two-tenant medallion architecture
(healthcare and financial) in which the confidentiality boundary is a first-class
design parameter rather than a wrapper. Five contributions are delivered:
crypto-partitioned storage with per-column keys (C1), attestation-gated key release
(C2), cryptographically verifiable lineage (C3), disclosure control on published
aggregates (C4), and an enclave placement planner that derives where the boundary
must fall and models what it costs (C5).

The central finding is architectural rather than numerical. Applying the placement
rule — *a stage must run inside the enclave if and only if it observes plaintext of
a column classified PII or PHI* — shows that **tokenising direct identifiers is not
what shrinks the boundary**. Tokenisation removes 17 of 42 protected columns between
Bronze and Silver and moves *no stage out of the enclave*, because 25
quasi-identifiers remain. What makes Gold's output publishable is disclosure
control; what keeps Gold's computation inside the boundary is that disclosure
control has not been applied yet at that point.

Applying that rule also exposed a defect the platform had carried since Stage 3:
the landing zone wrote unencrypted Parquet, and a single landing file was found to
contain 19 SSN-shaped strings. It has since been closed — landing is encrypted
under the same attestation-released keys as Bronze, at a measured ×1.014 storage
cost — and the closing turned up a second, quieter problem: a dataset that had sat
in the landing zone with no schema contract, and so had never been promoted,
classified or protected, because nothing knew it was there. It was source-side
tooling that had never belonged in the lake, and has been withdrawn.

**There is no confidential-computing hardware behind this work.** The TEE layer is
an abstraction with a simulated backend. Every number derived from it is labelled
MODELLED in the code, the database and this report. What that means, and what it
does not, is stated in §4.

---

## 1. Introduction

### 1.1 Motivation

A data platform running on rented infrastructure asks its owner to trust the
operator of that infrastructure — with the hypervisor, the storage service, the
memory of the machine. For healthcare and financial data this trust is often the
blocking constraint rather than a preference.

Hardware-based confidential computing (Intel SGX, AMD SEV-SNP) narrows that trust
to the CPU vendor. The literature demonstrates it convincingly for isolated
computations. It demonstrates it much less often for a *pipeline*, where data moves
between layers, is joined, aggregated, quality-checked, and eventually published —
and where the interesting question is not "can this computation run in an enclave"
but "which of these computations has to".

### 1.2 Research question

> What does it cost, architecturally and quantitatively, to run a full
> medallion-architecture data platform under a hardware-enforced confidentiality
> boundary — and where in that pipeline should the boundary be drawn?

The second half is the contribution. The first half is measurable only in part
without hardware, and §4 is explicit about which part.

### 1.3 Relation to MTP-1

MTP-1 presented *Secure Data Analytics Using Trusted Execution Environments* — a
single-process TEE benchmark. This work elevates that into a platform where the
boundary is a parameter, and adds four contributions the benchmark could not
express: verifiable provenance, disclosure control, an evaluation harness that
scores the platform against ground truth, and the placement planner.

---

## 2. Contributions

| | Contribution | Verified by |
|---|---|---|
| **C1** | Crypto-partitioned medallion storage: per-column, per-sensitivity, per-tenant encryption keys | Byte-level inspection of stored files; keyless reads refused in a separate OS process |
| **C2** | Attestation-gated key release with four audited refusal paths | 15 recorded refusals across 347 requests |
| **C3** | Cryptographically verifiable lineage: per-record Merkle roots, ECDSA-signed, hash-chained | Four tamper attacks, each detected *for the correct reason* |
| **C4** | Disclosure control on Gold aggregates: k-anonymity with complementary suppression | 8,729 primary and 250 complementary suppressions on one mart |
| **C5** | Enclave placement planner: boundary derived from contracts, cost modelled | Placement is a derivation; costs labelled MODELLED |

---

## 3. Architecture

```
SOURCES                 INGESTION            LANDING          BRONZE
 generated files  ──▶  FileCollector   ──▶  lake/landing/ ──▶ encrypted Delta
 mock REST API    ──▶  RestCollector        (encrypted —      (Parquet Modular
 Postgres WAL     ──▶  CdcCollector          see §5.5.1)        Encryption)
 Kafka topic      ──▶  Structured Streaming ──────────────────────▲
                        (no landing; writes the SAME table)       │
                              │                                   ▼
                    platform.ingest_batch                      SILVER
                    (content-addressed                 conform → validate →
                     idempotency)                      quarantine → dedupe →
                                                       tokenise → SCD2
                                                            │
    KEY BROKER ◀── attestation evidence ──┐                 ▼
    (measurement allowlist,               │              GOLD
     audited release)                     │     star schema (encrypted Delta)
           │                               │                 │
           ▼                               │     disclosure control (k=5 +
    KEY HIERARCHY                          │     complementary suppression)
    MEK → tenant KEK → per-class DEK       │                 │
                                           │                 ▼
    QUALITY ────────────────────────────────┘     gold.* marts in PostgreSQL
    282 rules → detections → scores → gate         (aggregates only)
```

### 3.1 Layer semantics

| Layer | Partition | Meaning |
|---|---|---|
| landing | none | Exactly what arrived, checksum-verified, encrypted (§5.5.1) |
| bronze | `ingest_date` | Typed, encrypted, still raw values |
| silver | `event_date` | Conformed, deduplicated, tokenised, SCD2 |
| gold | `event_date` | Star schema, encrypted |
| serving | — | PostgreSQL `gold` schema: disclosure-controlled aggregates only |

Bronze partitions on ingest date and Silver on event date. Bronze answers "what did
we receive on Tuesday"; Silver answers "what happened on Tuesday".

### 3.2 Scale of the implementation

| | |
|---|---|
| Python | 21,399 lines across `src/` |
| Tests | 404 (309 unit, 80 API, 15 integration) |
| CI | 14 jobs, plus security and release workflows |
| Migrations | 11 forward-only SQL migrations |
| Design records | 20 ADRs, 19 stage reports |
| Data processed | 422,297 Bronze rows across 11 datasets, two tenants |

---

## 4. The honesty policy

This section exists because without it every number in §5 and §6 is ambiguous.

**There is no confidential-computing hardware.** The TEE layer is an abstraction
with three backends — `sim`, `gramine-sgx`, `snp-cvm` — and it runs on `sim`.

| Category | Status |
|---|---|
| Cryptography, storage, Spark, pipeline throughput | **MEASURED** |
| Data quality, deduplication, disclosure suppression | **MEASURED** |
| Enclave transition and EPC paging costs | **MODELLED** |

`TeeSettings.is_measured` returns `False` under `sim`, and `measurement_label`
returns `MODELLED`. Consuming code is required to print it. Every row in
`platform.attestation_policy` and `platform.key_release_audit` carries
`hardware_rooted = false`.

**The consequence for C2 must be stated plainly.** Under `sim`, the code
measurement is a SHA-256 of the source tree, produced by the same process that then
presents it to the broker. **The process measures itself and could lie.** The
mechanism — policy, allowlist, four refusal paths, full audit — is real and
exercised. The *guarantee* is not. §9 sets out precisely what would change on real
hardware.

---

## 5. The five contributions

### 5.1 C1 — Crypto-partitioned storage

Parquet Modular Encryption with a distinct AES-256-GCM key per (tenant,
sensitivity class). PUBLIC columns are left unencrypted so predicate pushdown
survives; PII and PHI columns are encrypted individually; the footer is encrypted,
which is what makes the file's own metadata unreadable without a key.

**Key hierarchy.** MEK → per-tenant KEK → per-sensitivity DEK, derived by
HKDF-SHA256 rather than stored. Tenant isolation is therefore cryptographic: two
tenants do not share a key, and one tenant's key cannot decrypt another's column.

**Verification, and why it is byte-level.** The Stage 1 feasibility spike produced
a **false pass**: the "encrypted" arm asserted only a successful round-trip, which
an unencrypted write also satisfies. The keyless-read test reused the JVM, so
Parquet's `KeyToolkit` cache served the key from memory and the read succeeded when
it should have failed.

The verification is now:

1. Read the Parquet footer magic directly from the bytes: `PAR1` is plaintext,
   `PARE` is an encrypted footer.
2. Grep the raw file for values that should not appear in cleartext.
3. Attempt a keyless read **in a separate OS process**, so no cache can serve it.

This runs in CI on every push (`encryption-guarantee`).

**A defect this design forced into the open.** Delta silently ignores encryption
properties passed as DataFrameWriter options and writes plaintext — no error, no
warning. Encryption must be set on the Hadoop Configuration. Every Bronze write now
asserts `PARE` afterwards and raises if it is absent.

### 5.2 C2 — Attestation-gated key release

A key is released only against attestation evidence whose measurement appears in
`platform.attestation_policy`, is unrevoked, permits that tenant, and whose nonce
is fresh.

**Four refusal paths, all exercised:** unknown measurement, revoked policy, tenant
not permitted, stale or replayed evidence. Of 347 recorded requests, **15 were
refused**, and every refusal is in `platform.key_release_audit` with its reason.

`MEASURED_PACKAGES` is `crypto`, `pipelines`, `quality`, `lineage`, `evaluation`,
`streaming`. Editing any of them changes the measurement and revokes key release
until `confidentia keys approve` runs — which is the mechanism working, and is the
most common cause of a refusal burst.

Two defects found by reasoning rather than testing, both security-relevant:

- The attestation verifier accepted **any** PEM key type, permitting key confusion.
  It now pins to elliptic-curve.
- `max_age_seconds or default` turned a caller's explicit `0` into 300 seconds. Now
  `is None`.

### 5.3 C3 — Verifiable lineage

Every recorded table is digested into a Merkle root over per-record hashes, bound
to the code measurement and the keys in force, ECDSA-signed, and appended to a hash
chain in PostgreSQL. 72 entries at the time of writing.

**Granularity was measured, not assumed** (`healthcare/claim_line`, 145,136 records):

| Granularity | Commits to | Cost |
|---|---|---|
| Per run | one SHA-256 over the directory | 27 ms |
| Per file | Merkle root over file digests | **8 ms** |
| Per record | Merkle root over row digests | **7,330 ms** |

Per-record costs ~900× per-file and is the only one that can answer "was this row
included", which is the claim. Per-file would require handing over the file to
prove membership.

**Four tamper attacks, each detected for the correct reason** — a verifier that
reports "broken link" when the problem is a forged signature sends an operator to
the wrong place:

| Attack | Detected as |
|---|---|
| Edit a signed payload | `payload altered — recomputed b9de…, stored 8ebb…` |
| Edit a denormalised column | `column diverges from signed payload — merkle_root` |
| Delete a mid-chain entry | `broken link — stored previous 8ebb…, expected 9215…` |
| Edit one row in the encrypted Bronze table | `DOES NOT MATCH entry #31 … row count unchanged` |

The fourth edits a real encrypted Delta table and undoes it with time travel.
"Row count unchanged" is the point: the table is the same size, one field differs,
and the root still moves.

### 5.4 C4 — Disclosure control

Gold publishes aggregates into PostgreSQL with k-anonymity at k=5, and — critically
— **complementary suppression**. Primary suppression alone leaves a small cell
recoverable by subtracting published cells from a published total.

Measured on the published marts:

| Mart | Rows | Primary suppressed | Complementary |
|---|---|---|---|
| Transactions daily | 11,479 | 8,729 | 250 |
| Encounters daily | 4,477 | 1,227 | 432 |
| Diagnosis prevalence | 263 | 86 | **0** |

The third row is a finding, not an omission: a mart with primary suppression and
*no* complementary suppression has usually not been protected. It is surfaced in
the API and flagged in the dashboard rather than hidden.

### 5.5 C5 — Enclave placement planner

The package separates a **derivation** from a **model**, and the separation is the
contribution:

- `placement.py` derives which stages must be inside from the schema contracts.
  The answer would be identical on real SGX.
- `profile.py` gathers measured inputs: row widths from Parquet footer metadata,
  durations from the recorded run history, row counts from signed lineage.
- `model.py` turns those into modelled enclave costs, labelled MODELLED.

**The rule:** a stage must run inside the enclave iff it observes the plaintext of a
column classified PII or PHI.

**The result.** Every stage touching tenant data is inside; only `quality.gate` and
`api.serve` are outside. That flat answer is uninteresting until you ask *why*, and
the planner reports it per stage:

- `ingest.collect`, `bronze.write` — read raw source records; **all 42** protected
  columns visible.
- `silver.tokenise` — performs tokenisation, so it sees pre-token values *by
  definition*. No placement decision can move it out.
- `gold.*`, `quality.run`, `lineage.record` — read Silver/Gold, where 17 direct
  identifiers are tokenised but **25 quasi-identifiers remain**.

**This is the central architectural finding.** Tokenisation removes 17 of 42
protected columns and moves nothing out of the boundary. The direct identifiers
were never the binding constraint. What makes Gold's *output* publishable is C4;
what keeps Gold's *computation* inside is that C4 has not been applied yet at that
point.

**Modelled cost** (MODELLED — there is no enclave):

| Group | Rows | Baseline | Overhead | Factor |
|---|---|---|---|---|
| ingest | 60,400 | 1,247 ms | 370 ms | ×1.296 |
| bronze | 422,297 | 3,240 ms | 769 ms | ×1.237 |
| silver | 422,297 | 28,170 ms | 5,755 ms | ×1.204 |
| gold | 279,299 | 70,092 ms | 14,139 ms | ×1.202 |
| quality | 279,299 | 15,764 ms | 3,273 ms | ×1.208 |
| lineage | 422,297 | 7,397 ms | 1,600 ms | ×1.216 |
| **pipeline** | | **125,909 ms** | **25,906 ms** | **×1.206** |

**EPC-aware partition sizing.** At the measured row width (116 B, row-weighted
across 12 datasets) and a 128 MB EPC at 75% utilisation, one partition holds
**867,787 rows**. Bronze is 422,297 rows, so **it already fits and partition sizing
buys nothing today**. The sweep says it starts to matter at ~5× this volume and
saves ~260 s at 100×, and immediately at a 32 MB EPC.

Reported as a threshold, not a recommendation to repartition — the honest statement
is "you do not need this yet, and here is when you will".

#### 5.5.1 The boundary violation the planner found, and closing it

Asking a tool where the boundary *should* fall invites the question of whether the
boundary actually *encloses* what it claims to. It did not. The planner reported one
violation: **the landing zone sat outside the boundary and held plaintext.** Landing
files carried the `PAR1` footer magic while Bronze carried `PARE`, and grepping one
landing `patient` file's raw bytes turned up 19 SSN-shaped and 7 email-shaped
strings. Every stage downstream encrypted per column under attestation-released
keys; the first thing written to disk was not encrypted at all.

ADR 0017 recorded two remedies and chose neither, because the choice changes
ingestion's recovery story. **ADR 0022 took the first**: landing is now written with
Parquet Modular Encryption under the same per-sensitivity keys, released by the same
broker, mapped to columns by the same schema contract. The alternative — deleting
each batch once Bronze commits — was rejected because it destroys the property the
landing zone exists for, and deleting evidence to avoid protecting it solves the
wrong problem.

**The obstacle was that the two halves speak different languages.** Bronze is
written by Spark through a Java crypto factory; ingestion is pandas, and standing up
a Spark session to encrypt a single 60,000-row landing part would cost more than the
ingest it protects. So landing is encrypted by pyarrow, and the engines agree
because `crypto/landing.py` implements the same wrapping envelope as
`LocalKmsClient.java` — AES-256-GCM, `base64(nonce ‖ ciphertext ‖ tag)`, key
identifier as additional authenticated data — over the published `PKMT1` key
material format. Spark therefore reads what pandas wrote, and Bronze promotes
through an unchanged `spark.read.parquet` call. That was proved by a spike before
any of it was built, and is now enforced by a CI test, because a drift in either
wrapping routine would be invisible until a promotion failed weeks later.

**Measured cost of the fix:** migrating 24 parts and 369,097 rows took storage from
21.13 MiB to 21.42 MiB, **×1.014**. The larger cost is not storage. Three call sites
— experiments E1 and E8, and a demo helper — could previously read raw source
records with no key, no attestation and no audit entry. All three now request keys.
That convenience *was* the vulnerability.

**Two further defects surfaced while closing it.**

First, an *undeclared* column would have landed in the clear. Bronze casts to the
contract before writing, so a column the contract does not declare never reaches its
writer; landing is deliberately pre-contract and does receive them — E8 showed
additive drift arriving unannounced. Under a plaintext-by-default rule a new
`ssn_alt` column would land readable and nothing would say so, so an undeclared
column now takes the strictest key the contract uses. This fired on the first real
run, on a `batch` column the collectors add and no contract declares.

Second, and worse: the migration found **`healthcare/patient_change`** — 900 rows of
patient address changes, landed once by the file collector, belonging to *no schema
contract at all*. It had never been promoted, because Bronze refuses a dataset it
cannot type. Never quality checked or classified, for the same reason. And never
encrypted — not because anything decided it should not be, but because nothing knew
it was there. It is a source-side mutation script the CDC path replays into
Postgres, never intended to be a lake dataset, and a collector pointed at a
directory tree landed it anyway. The migration encrypted it rather than skipping it,
which protected it without pretending to classify it.

**ADR 0023 resolved what it was: not a lake dataset, so it was deleted.** Its effects
already reach the lake as `patient_cdc` — contracted, encrypted, promoted, tokenised
— and giving it a contract would have committed the platform to a second route into
Bronze for data already there by a better one. Nothing is lost:
`raw/healthcare/patient_change/` is still the source of truth for the replay, and the
generator is deterministic, which CI proves on every push.

The `platform.ingest_batch` row was kept and marked `withdrawn_at`, not deleted.
Removing the files while the registry still said `ingested` would make it lie about
what is on disk; removing the row would erase the fact that this arrived at all,
which is the one question the landing zone exists to answer.

**The rule that stops it recurring is one line:** the catalog decides what the lake
ingests, not the shape of a directory tree. `available_datasets` now returns
manifested *and* contracted datasets.

That change also fixed a bug the previous one introduced, which is worth stating
plainly. Making an uncontracted write fail closed is right when someone names a
dataset and wrong during a sweep: `confidentia ingest file healthcare` would have
aborted the whole tenant's ingest on `patient_change`, taking `rebuild_pipeline.sh`
with it. **A guard that fails closed is only safe if every caller can survive the
refusal, and this one could not.** Filtering at discovery keeps the sweep working
while a targeted request for that dataset still fails loudly.

**The detector changed too, and this is the part worth defending.**
`boundary_violations()` returned a hard-coded finding. That was honest while the
hole was open, and would have become a lie the moment it closed — worse, a lie
printed by a tool nobody would re-check. It now scans the landing zone for the
`PAR1` magic, so the answer is re-derived from the bytes on every run: silent when
the lake is clean, and loud again the day a future collector reintroduces a
plaintext write. `scripts/verify_landing.sh` runs the same check in four separate OS
processes, because Parquet's `KeyToolkit` caches unwrapped keys statically per JVM
and an in-process retry would read from that cache and falsely pass — which is
exactly how the *first* Bronze verification reported a pass on unencrypted data.

**What it does not fix.** The keys protecting the landing zone are released against
a *simulated* attestation. What changed is that raw source records are no longer
readable from disk without presenting evidence to a gate; on real hardware the same
code path presents it to a gate that cannot be lied to. The mechanism moved to the
right place. The root of trust did not, and stays labelled MODELLED.

---

## 6. Data quality and evaluation

### 6.1 The rule engine

282 rules derived from the schema contracts across six dimensions — completeness,
validity, consistency, uniqueness, integrity, timeliness — with severity weighting
(critical 10 / major 3 / minor 1). Failing rows are **quarantined, never dropped**:
30,951 rows are held and recoverable.

### 6.2 Scoring the platform against ground truth

The generators inject 11 defect families and record every one in a ground-truth
manifest. **Isolation is enforced rather than trusted**: a CI job greps the whole
of `src/confidentia` to prove no pipeline package can read the answer key. A
pipeline that read it would score perfectly and mean nothing.

Measured precision, recall and F1, **excluding the contaminated Bronze
`duplicate_record` family** (see §6.3):

| Tenant | Dataset | Precision | Recall | F1 |
|---|---|---|---|---|
| healthcare | patient | 1.000 | 0.996 | **0.998** |
| financial | account | 1.000 | 0.983 | **0.991** |
| healthcare | encounter | 0.834 | 1.000 | **0.910** |
| financial | transaction | 0.837 | 0.954 | **0.891** |
| healthcare | claim_line | 0.795 | 0.955 | **0.868** |

Defect families with **no rule at all: 0**, down from 5, and held there by a CI job.

### 6.3 What the evaluation harness measured first was the harness

Stage 7b's initial run reported `patient` recall 0.390 and `encounter` precision
0.053. Investigation found eight defects — in the *generator* and the *rule
catalogue*, not in the scoring:

- `_break_format` did not break formats
- `case_inconsistency` upper-cased already-upper-case values
- four foreign keys were never declared
- `unit_inconsistency` covered two columns of four

After Stage 7c, recall moved 0.390 → 0.997 on patient and 0.712 → 0.959 on
transaction. **These numbers are not evidence that the rules improved.** Almost
nothing about the rules' method changed. What changed is that the answer key now
describes defects that exist and the catalogue covers the columns being corrupted.
7b measured the measurement apparatus; 7c measures the rules.

**The contamination that cannot be fixed.** At Bronze, a duplicate and a legitimate
redelivery are the same bytes. Every redelivered row counts as a false positive for
`duplicate_record` — 19,855 of `transaction`'s 20,450. Including it takes that
dataset's F1 from 0.891 to **0.251**. The API returns both figures and names which
to report; the dashboard shows the corrected one as the headline with the raw one
beside it.

---

## 7. Experiments

Six of eight measured. Full detail in `docs/experiments.md`.

### 7.1 E4 — cost of Parquet Modular Encryption **[MEASURED]**

3,000,000 rows, one warm-up, five interleaved repeats, arms verified by footer
magic.

| Arm | p50 | rows/s | vs plaintext |
|---|---|---|---|
| plaintext | 3.45 s | 869,913 | — |
| pme | 3.59 s | 835,234 | **×1.042** |

**Write overhead ×1.042; storage overhead ×1.001.**

Run first at 200,000 rows it reported **×0.967 — encryption *faster***, which is
impossible and is the same artefact the Stage 1 spike produced. It is not warm-up:
this harness warms up, repeats and interleaves. It is scale — at 200k the write
takes 0.85 s and Spark's fixed job cost swamps the signal. **Any encryption
benchmark on this stack below ~1M rows measures job overhead, not encryption.**

### 7.2 E2 — partition sizing **[MEASURED]**

| Partitions | p50 | Mean file | Total |
|---|---|---|---|
| 4 | **2.76 s** | 9,279 KB | **37.1 MB** |
| 90 | 4.70 s | 468 KB | 42.1 MB |
| 720 | **17.36 s** | 72 KB | **51.8 MB** |

**6.3× write penalty and 40% storage inflation** for identical rows. This quantifies
the Stage 5 small-file bug (1,980 files averaging 37 KB) rather than merely
recording that it was fixed.

It sweeps partition count because the setting it was *meant* to sweep does nothing:
`spark.databricks.delta.optimize.maxFileSize` governs `OPTIMIZE`, which this
pipeline never runs. Sweeping it was completely flat.

### 7.3 E1 — streaming against batch **[MEASURED]**

| Arm | p50 | rows/s | vs batch |
|---|---|---|---|
| batch_file | 2.00 s | 29,982 | — |
| streaming_kafka | 8.75 s | 6,859 | **×4.37** |

Same 60,000 records into the same format, and — since the fix recorded in §8 item 9
— both arms now *write encrypted*, as the real pipeline does. The earlier figures
(×4.27) were measured before the landing zone was encrypted and are no longer what
the harness reproduces; the ratio is unchanged in substance. The producer sustains
~22,000 records/s, so the cost is entirely on the consume side. The streaming arm
writes 24 files to the batch arm's one, which is most of the difference: it is the
price of a bounded micro-batch, not a defect.

It also exposed an asymmetry: the batch arm *failed outright* on a type mismatch
(`amount` declared string, generator writes DOUBLE) that the Kafka arm never sees,
because JSON `from_json` coerces silently. **The streaming path accepts a type the
batch path refuses.**

### 7.4 E8 — schema drift **[MEASURED]**

| Drift | Accepted | Rows nulled |
|---|---|---|
| baseline | yes | 0 |
| column_added | yes | 0 — but the field is **silently dropped** |
| column_dropped | yes | **60,000 (100%)** |
| type_violation | yes | **60,000 (100%)** |
| column_renamed | yes | **60,000 (100%)** |

**Every drift is accepted; three of four destroy 100% of a column silently.** That
is the design — Bronze must accept what arrived — but detection is *entirely
downstream*. Nothing at Bronze raises or records that a source changed shape.

### 7.5 E3 — blocked, and E5–E7 — never defined

**E3** compares incremental against full reprocessing. The incremental arm does not
exist: Silver and Gold are both full-refresh, as Stage 5 and Stage 6 each recorded.
Running E3 requires building incremental processing first — pipeline work, and the
largest remaining engineering item.

**E5, E6 and E7 appear nowhere in the repository.** They were numbered in the
original plan and never specified. Candidates are proposed in
`docs/experiments.md`; notably, Stage 9b's lineage-granularity measurement (§5.3)
would fit E7 accurately rather than retrospectively.

---

## 8. Limitations

Stated as findings, because most of them were found by this project's own checks.

1. **No confidential-computing hardware.** Every enclave number is MODELLED.
   Attestation is a mechanism, not a security boundary, under `sim`.

2. **Deleted data is unlinked, not shredded.** Both the re-encryption of the landing
   zone and the withdrawal of `patient_change` (§5.5.1) freed blocks that stay on the
   device until reused, and on a copy-on-write filesystem or an SSD doing wear
   levelling they may survive far longer. On a lake holding real data the answer is
   full-disk encryption, not trusting an unlink — and this project does not attempt
   the disk-level operation it would take to claim otherwise.
   *(The landing-zone plaintext defect this entry used to describe was closed by
   ADR 0022, and the unclassified dataset it exposed by ADR 0023; see §5.5.1.)*

3. **Schema drift is undetected at Bronze** (§7.4), and type drift is undetectable
   there for almost every column because the Bronze contract declares them `string`.

4. **Incremental processing does not exist.** Silver and Gold are full-refresh.

5. **`crypto.column_encryption` is declared and unread.** ADR 0007 names `envelope`
   as E4's baseline arm; the mode does not exist.

6. **The Azure deployment has never been applied, and the SEV-SNP reader has never
   run.** `terraform validate` passes, `fmt -check` passes and Checkov reports 36/0;
   `plan` builds the provider and stops on authentication. The reader exists
   (ADR 0024) and `snp.VALIDATED_ON_HARDWARE` is `False` — enforced by a test and
   carried in every audit row. **No figure in this report comes from that path**,
   and none may until a first run produces a measurement MAA agrees with. §9
   previously understated the work involved; that is corrected there.

7. **The Azure key release policy proves the hardware, not the code.** Any Azure
   confidential VM meeting the claims obtains the key; pinning the workload needs
   confidential containers.

8. **No frontend tests.** The three defects found in the dashboard were found by a
   human driving a browser, which is not repeatable.

9. **The controls fire; nothing obliges anyone to read them.** Three defects found
   in the final week share a shape, and it is not the one this project spent its
   effort defending against. In each case the platform behaved correctly and
   reported correctly, into somewhere no routine path forces a reader:

   - **`rebuild_pipeline.sh` never recorded lineage.** It built Bronze, Silver,
     Gold, quality and evaluation, then stopped. Every development machine already
     had a chain from earlier manual runs, so the omission was invisible; a clean
     checkout produced an empty Lineage page and gave `verify_lineage.sh` nothing
     to verify. The script an examiner is told to run in Appendix A did not
     demonstrate C3.
   - **E1 stopped working when the landing zone was encrypted** (ADR 0022). The
     benchmark fails outright with `No column keys configured`, and did so from the
     moment landing became encrypted until someone next ran it — which was weeks
     later. An experiment that only runs on demand is indistinguishable from a
     passing one while nobody demands it.
   - **The ADR 0022 migration added a column to 23 of 25 landing parts.** `pq.
     read_table` materialises the hive partition key, so a rewrite meant to change
     only encryption also changed the schema. The platform *caught this as it
     happened*: it logged `landing_undeclared_columns_encrypted` naming the column,
     and encrypted it under the strictest key rather than skipping it. Every
     control worked. The finding went to a log line.

   The generalisation is uncomfortable and worth stating plainly: this project
   invested in making controls *correct* — refusals that fail closed, verification
   from bytes rather than from the absence of an error — and comparatively little
   in making their output *unignorable*. A detection that reaches only a log, an
   on-demand script, or a page nobody opens has a failure mode the detection logic
   cannot fix. The quality gate (§5.4) is the one control in the platform that does
   not have this problem, because it blocks a pipeline rather than reporting to
   one; that contrast is the argument for where the remaining detections belong.

   None of the three caused data loss. That is the point. They were found by
   running things, not by the platform telling anyone — and a control whose
   discovery depends on someone happening to run the right script is not yet an
   operational control.

---

## 9. What would change on real hardware

| Today | On Azure |
|---|---|
| `attestation.py` — SHA-256 of the source tree | SEV-SNP report signed by AMD firmware |
| the verifier in `attestation.py` | Microsoft Azure Attestation |
| `broker.py` — four refusal paths | Managed HSM secure key release policy |
| `LocalKmsClient.java` | KMS client backed by Managed HSM |

**Attestation stops being self-issued.** The four refusal paths stay exactly as they
are and become enforceable rather than voluntary. C5's modelled ×1.206 becomes a
measurement; the *placement* does not change, because it is derived from contracts.

**An earlier draft of this section claimed the change was "configuration plus one
Java class".** That was wrong, and preparing the deployment exposed it:
`AttestationBackend.produce_measurement()` raised for every non-`sim` backend, so
selecting `TEE_BACKEND=snp-cvm` flipped every label to MEASURED and then failed every
attestation and therefore every key release. The infrastructure would have come up
and the platform would have refused to run on it.

**The reader has since been written (ADR 0024), and it has never run on confidential
computing hardware.** That is a third state — implemented, unproven — which the
project's MEASURED/MODELLED vocabulary had no word for, so it was given one:
`snp.VALIDATED_ON_HARDWARE` is `False`, it is stamped into every piece of evidence as
a claim, it reaches the key-release audit table, and a test asserts it. An audit
trail written by that code says on its face that the code producing it is unproven.

The risk is specific and worth naming, because it is not a crash. **A wrong struct
offset returns 48 bytes that look exactly like a launch measurement**, and no test on
a machine without SEV-SNP can distinguish one from the other. So MAA is asked to
parse the same report independently and the two answers are compared on every
attestation; a disagreement raises and names the offset to fix. That check is the
reason the deployment is worth attempting rather than guessing, and it is why the MAA
exchange is not optional.

What is *not* provisional is everything that would still be a vulnerability if the
reader were perfect, and it is tested: the MAA token is verified against MAA's JWKS
with the algorithm pinned and the issuer taken from configuration rather than from
the token; the guest must be `sevsnpvm`, `azure-compliant-uvm` and not debuggable;
and **the measurement in the evidence must be the one MAA signed** — without which a
guest could present a genuine token from any confidential VM beside whatever
measurement it liked, and the broker's allowlist would approve key release for a
measurement nothing attested to.

One item from that draft's omission remains open: `broker.py` still decides locally,
and on Azure must call the HSM's release endpoint — keeping the audit table and the
four refusal reasons, but not the decision.

What *is* true is the claim underneath: nothing in `pipelines/`, `quality/`,
`evaluation/` or `lineage/` changes, and the seams the swap needs — a `TeeBackend`
that produces evidence separately from verifying it, a `KmsClient` interface behind
`PropertiesDrivenCryptoFactory` (ADR 0007) — exist and were built for this. That is
the payoff for treating the boundary as a design parameter from Stage 1. It is a
smaller claim than the one this section used to make, and it is one the repository
supports.

The five remaining items are enumerated in `docs/architecture/azure.md`, and
`docs/deploy.md` is the runbook for the deployment itself.

---

## 10. Conclusion

The project set out to ask where a confidentiality boundary should fall in a
medallion data platform, and to measure what putting it there costs.

The answer to *where* is derived rather than asserted, and it is not the answer the
architecture implicitly assumed. Tokenising direct identifiers — the intervention
the platform spends most effort on — does not move a single stage out of the
enclave. Quasi-identifiers do that work, and they cannot be tokenised without
destroying the aggregations they exist for. The lever that actually publishes data
safely is disclosure control, applied at the boundary of publication rather than of
computation.

The answer to *what it costs* is partly measured and partly modelled, and the report
keeps them apart. Column encryption costs 4.2% of write time and 0.1% of storage —
measured. Running the pipeline inside an enclave is modelled at ×1.206 — and would
become a measurement with roughly a day's work against an Azure subscription.

What the project demonstrates beyond either answer is a method: **build the check
before the claim.** The encryption guarantee is re-proved on every push because it
once produced a false pass. The evaluation harness scores the platform against an
answer key it is structurally forbidden from reading. The placement planner reported
a violation in the architecture that wrote it — and closing that violation exposed a
second one the planner had not been looking for, a dataset nothing in the catalog
knew existed. The detector that found the first was then rewritten to read the disk
rather than repeat its conclusion, so it can go quiet honestly and speak up again
without being asked. Six of the eight experiments produced
a finding beyond their number — a benchmark that inverts below a million rows, a
tuning knob that does nothing, an asymmetry between two ingestion paths, and silent
data loss on drift.

---

## Appendix A — Reproducing every claim

```bash
bash scripts/rebuild_pipeline.sh        # regenerate the lake end to end
bash scripts/verify_encryption.sh       # C1: byte-level, keyless read refused
bash scripts/verify_landing.sh          # C1: the landing zone, on the real lake
confidentia keys audit                  # C2: grants and the four refusal paths
bash scripts/verify_lineage.sh          # C3: four tamper attacks
confidentia gold disclosure             # C4: suppression counts per mart
confidentia plan placement && confidentia plan sweep   # C5
confidentia evaluate report             # §6: P/R/F1 against ground truth
confidentia bench e4 && confidentia bench e2           # §7
confidentia bench e8
bash scripts/ci_local.sh                # the 14 CI jobs, locally
```

E1 needs a broker holding exactly the records it compares against, so it does not
belong in the sequence above:

```bash
bash scripts/kafka.sh reset --yes       # and clear the streaming checkpoint
bash scripts/kafka.sh start
confidentia stream produce financial transaction --limit 60000 --rate 0
confidentia bench e1                    # §7.3
```

Leftovers in the topic are counted by the streaming arm — it reads from
`earliest` — and the two arms stop being comparable. The reset is the reason this
sequence is separate rather than one more line above.

## Appendix B — Where the design decisions live

20 ADRs in `docs/adr/`. The ones that constrain everything else:

| ADR | Decision |
|---|---|
| 0006 | The TEE cost model, and what it may not claim |
| 0007 | Column encryption strategy; the KMS seam |
| 0008 | Synthetic data and ground-truth isolation |
| 0016 | Serving-layer boundaries: reads Postgres, never the lake |
| 0017 | Where the confidentiality boundary is drawn |
| 0022 | Encrypting the landing zone, and why the detector reads the disk |
| 0023 | Why `patient_change` was deleted rather than contracted |
| 0024 | A SEV-SNP reader, written and marked unproven |
| 0020 | Supply chain: scan policy and what is accepted |
| 0021 | Azure as the cloud target, and what stays simulated |
