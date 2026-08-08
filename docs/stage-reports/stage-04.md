# Stage 4 — Bronze: encrypted lakehouse layer

**Date:** 2026-08-08

This is the stage where the Stage 1 encryption spike stops being a spike.
Contributions **C1** (crypto-partitioned storage) and **C2** (attestation-gated key
release) are both implemented and verified end to end.

## What was implemented

| Component | File | Purpose |
|---|---|---|
| Key hierarchy | `crypto/keys.py` | MEK → tenant KEK → per-class DEK, all derived; sealed keystore |
| Attestation | `crypto/attestation.py` | Code measurement, ECDSA P-256 signed evidence, nonce freshness |
| Key broker | `crypto/broker.py` | Policy allowlist, four refusal paths, full audit trail |
| KMS client | `crypto/java/LocalKmsClient.java` | Promoted from `spikes/`; now production code |
| Schema contracts | `catalog/contracts.py` | 6 datasets, executable sensitivity classification |
| Spark session | `spark/session.py` | Memory-capped, Delta, PME wired without leaking keys |
| Bronze writer | `pipelines/bronze/writer.py` | Landing → encrypted Delta, verified at byte level |
| Migration | `store/sql/004_attestation.sql` | Attestation policy, key release audit |

## Measured results

All six Bronze tables built and **verified encrypted**:

| Table | Files | Size | public / internal / pii / phi columns |
|---|---|---|---|
| healthcare/patient | 1 | 0.4 MB | 8 / 6 / 7 / 3 |
| healthcare/encounter | 4 | 6.4 MB | 12 / 6 / 0 / 5 |
| healthcare/claim_line | 4 | 7.5 MB | 10 / 10 / 0 / 2 |
| financial/account | 1 | 0.1 MB | 11 / 6 / 4 / 0 |
| financial/transaction | 3 | 3.5 MB | 17 / 5 / 4 / 0 |
| financial/chargeback | 1 | 0.0 MB | 10 / 5 / 0 / 0 |

Throughput: 60,501 transactions in 4.2 s; 145,138 claim lines in 4.8 s. Idempotent
re-runs promote 0 batches.

## The two claims, and how each was proven

### C1 — column-level key separation

Not by a byte search. Parquet dictionary-encodes and Snappy-compresses, so a value's
absence from the raw bytes can be compression rather than encryption — observed
directly, since the PUBLIC column `encounter_type` is written in plaintext yet the
literal `outpatient` did not appear in the file.

The definitive test withholds one key at a time, each arm in its own OS process
because Parquet caches unwrapped keys per JVM:

| Keys held | `primary_diagnosis_code` (PHI) | `encounter_id` (INTERNAL) | PUBLIC columns |
|---|---|---|---|
| all | OK | OK | OK |
| all except PHI | **DENIED** | OK | OK |
| all except INTERNAL | — | **DENIED** | OK |

Denial comes from `KeyAccessDeniedException` raised by our own KMS client.

Tenant isolation is cryptographic: healthcare and financial key material has zero
overlap, so cross-tenant reads fail on key derivation, not on a policy check.

### C2 — attestation-gated key release

Four refusal paths, all exercised and audited:

| Condition | Outcome |
|---|---|
| Replayed evidence | denied — nonce mismatch |
| Forged measurement | denied — signature does not verify |
| Pipeline code changed | denied — measurement not approved |
| Right measurement, wrong tenant | denied — tenant not permitted |

**The gate blocked my own development within minutes of being built.** Adding
`bronze/writer.py` changed the measurement from `10de202e…` to `9baf7e8e…` and key
release stopped. Every subsequent change to a measured package required
`confidentia keys approve`. That is the feature working — "which code touched this
data" is now enforceable — but it is a genuine development tax and the report says
so rather than presenting it as free.

## A security fix found by reasoning, not by a test

Wiring PME the obvious way — `.config("spark.hadoop.parquet.encryption.key.list", …)`
— **displays base64 master keys in the Spark UI's Environment tab**. Spark's default
redaction regex covers only `secret|password|token`, so `key.list` is rendered in
full.

Key material is now set on the Hadoop Configuration after session creation, which
the UI does not display, and the redaction regex is widened as defence in depth.
Nothing would have failed; the keys would simply have been on a web page.

## Bugs found and fixed

**Bronze silently doubled.** `_promoted_batches` filtered on `status = 'ingested'`,
but a batch left `pending` by the Stage 3 corruption test was therefore invisible to
the promotion check and re-promoted on every run — the encounter table reached
241,596 rows instead of 120,798. Promotion and ingest status are independent facts;
coupling them was wrong. The resolver now also raises if a landing batch has no
registry row rather than leaving it permanently un-promotable.

**Falsy-zero in the freshness window.** `AttestationVerifier(max_age_seconds=0)` hit
`max_age_seconds or default` and silently became 300 seconds — a caller asking for
the strictest possible window got a lax one. Caught by a test that failed for the
right reason. Now `is None`.

**Cross-source schema conflict.** The same dataset arriving from two collectors had
different physical types (`admit_ts` as INT64 from files, as string from the REST
API via JSON), and reading them as one path failed. Batches are now reconciled
against the contract individually before union — which also absorbs additive schema
drift.

## Known limitations

- **Single partition.** All ingestion happened on one day, so `ingest_date` has one
  value and partition pruning is not yet demonstrated.
- **`sim` backend only.** The measurement is self-reported; under `sim` this is a
  mechanism, not a security boundary. Every policy and audit row records
  `hardware_rooted = false`.
- **Passphrase, not hardware sealing.** The keystore is scrypt-wrapped and
  owner-only. That is not sealing to an enclave measurement, and the metadata says
  so explicitly.
- **MinIO / object storage still not wired.** Deferred again; local filesystem only.
  This is now the second deferral and should land before the cloud-architecture work.

## How to verify

```bash
wsl -d Ubuntu -- bash -lc "cd ~/confidentia && bash scripts/verify_bronze_isolation.sh"
```

## State

84 unit tests, 12 integration tests, lint clean. 6 encrypted Bronze tables,
17.8 MB. Nothing claimed as deployed.

## Next: Stage 5 — Silver

Conform, deduplicate, tokenise PII against a sealed vault, and build SCD2 dimensions
from the CDC before-images captured in Stage 3. This is where the 798 injected
duplicate records and the `patient_cdc` UPDATE stream finally get consumed.
