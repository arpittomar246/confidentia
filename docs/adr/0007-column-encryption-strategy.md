# ADR 0007 — Column encryption strategy for the lakehouse

- **Status:** Accepted
- **Date:** 2026-08-08
- **Stage:** 1 (setup / feasibility spike)
- **Decides:** `CryptoSettings.column_encryption`

## Context

Contribution **C1 (crypto-partitioned medallion storage)** assumes that different
columns of the same table can be encrypted under *different* keys, so that a principal
holding the `clinical` key cannot decrypt `identity` columns. This is what makes
"multi-tenant key isolation" and "sensitivity-tiered access" real rather than
policy-only.

Two candidate mechanisms:

| Option | Mechanism | Cost if wrong |
|---|---|---|
| **PME** — Parquet Modular Encryption | Per-column AES-GCM keys, encrypted footer, KMS-managed | Storage layer is rewritten |
| **Envelope columns** | Sensitive columns replaced by encrypted `BINARY`, crypto UDFs on read/write | Loses column statistics, pushdown, and adds per-row UDF cost |

Discovering the answer in Stage 5 would mean rewriting Bronze and Silver, so it was
resolved before any pipeline code was written.

## Experiment

Environment: WSL2 Ubuntu 26.04, JDK 17.0.19, Python 3.11.15, PySpark 3.5.3,
delta-spark 3.2.1, `local[2]`, 1 GB driver, 50 000 rows.

Parquet ships a mock KMS only in its test artifact, so a `KmsClient` implementation
was written for this project (`spikes/pme/java/LocalKmsClient.java`) doing AES-256-GCM
key wrapping with the master key identifier bound in as AAD. It compiles against the
Parquet 1.13 jars that PySpark bundles.

**Verification method matters here.** The first run asserted only that data
round-tripped, which an unencrypted write satisfies equally well — it produced a false
pass. Verification was changed to inspect the bytes on disk directly: Parquet magic
(`PARE` = encrypted footer vs `PAR1`) plus a search for known sensitive values in the
raw file. A second unsound result came from running the keyless-read test in the same
process as the write; `SparkSession.stop()` does not restart the JVM under PySpark, and
Parquet's `KeyToolkit` caches unwrapped keys statically, so the cached key made a
keyless read appear to succeed. Every access-control probe now runs in its own OS
process.

## Results

| Probe | Question | Result |
|---|---|---|
| `write-parquet` | Does PME work through Spark? | **`PARE/PARE`, no plaintext found** |
| `delta-hadoopconf` | Does it work under Delta Lake? | **`PARE/PARE`, no plaintext found** |
| `keyless-read` (parquet) | Does a keyless read fail? | **Denied** |
| `keyless-read` (delta) | Does a keyless read fail? | **Denied** |
| `delta-rs` | Can the JVM-free reader open it? | **Cannot** |

The denial's root cause chain, which is the evidence that access control is enforced by
cryptography rather than by convention:

```
org.apache.parquet.crypto.KeyAccessDeniedException: Column [patient_name]: key access denied
  Caused by: KeyAccessDeniedException: Master key not available to this principal: k_identity
    at org.confidentia.crypto.LocalKmsClient.requireMasterKey(LocalKmsClient.java:129)
```

A principal holding only the footer key can read the schema and the plaintext columns
and is refused the identity columns — which is exactly the intended property.

### The Delta finding

Writing a Delta table with the encryption properties passed as **DataFrameWriter
options** produced `PAR1` magic with `diagnosis_code` and `note` readable in the clear.
Delta routes writes through its own commit protocol and does not forward writer options
into the Parquet crypto factory. **It failed silently** — no error, no warning, just
unencrypted output.

Setting the same properties on the **Hadoop Configuration** produced correctly
encrypted output.

This is a security-relevant trap: the naive integration looks like it works, and only a
byte-level check reveals that it does not. It is worth reporting as a finding in its own
right.

## Decision

Use **Parquet Modular Encryption**. Set `column_encryption = "pme"`.

Encryption properties are applied to the **Hadoop Configuration**, never as
DataFrameWriter options (`encryption_via_hadoop_conf = True`).

Envelope-column encryption is retained as the `"envelope"` mode. It is no longer the
fallback but the **baseline comparison arm for experiment E4**, since the spike already
shows it inflates storage substantially (12.3 MB vs 1.5 MB at 50 k rows, ~8×, because
ciphertext does not compress and column statistics are lost).

## Consequences

1. **The API layer cannot read Bronze or Silver directly.** `delta-rs` — the JVM-free
   reader behind the FastAPI service — cannot open encrypted Parquet at all:
   *"Could not read encrypted metadata, no decryption found in reader's properties."*
   Serving must go through Spark, or through Gold. This reinforces the existing design
   in which Postgres holds the Gold marts and the Data Explorer queries Gold rather
   than raw layers.
2. **The Gold storage decision is now open and deferred to Stage 6.** If Gold is
   PME-encrypted, DuckDB cannot read it either. Gold is disclosure-controlled
   (k-suppressed, aggregated) so it is a candidate for footer-only or no column
   encryption — a genuine confidentiality-versus-accessibility trade-off to argue
   explicitly rather than settle by default.
3. **A write-time verification gate is now mandatory.** Because Delta can silently
   write plaintext, the Bronze and Silver writers must assert `PARE` magic on a sample
   output file after every write, and fail the run otherwise. This becomes a data
   quality check (`encryption_enforced`) in Stage 7, not merely a unit test.
4. `LocalKmsClient` is the integration seam for **C2**: `wrapKey`/`unwrapKey` will
   later call the Key Broker Service with an attestation report instead of reading
   master keys from configuration.
5. Encryption configuration is session-scoped rather than per-write. Writing two tables
   with different key sets in one Spark session requires mutating the Hadoop
   Configuration between writes — the pipeline framework must own this, not callers.

## Numbers

Indicative only — single machine, cold JVM, 50 k rows, one run each. Not experiment E4;
these are a feasibility signal, not a benchmark.

| Arm | Write | Read | Size |
|---|---|---|---|
| Plaintext Parquet | 1.34 s | 0.42 s | 1 512 996 B |
| PME Parquet | 0.44 s | 0.43 s | 1 527 744 B |
| Envelope columns | 1.93 s | 0.13 s | 12 265 657 B |

The PME size overhead over plaintext is ~1%. The write-time ordering (PME faster than
plaintext) is an artifact of JVM warm-up across arms, not a real effect — which is
precisely why E4 will run under the isolated `bench` profile with warm-up iterations and
repeats.
