# Stage 1 — Project setup and feasibility spike

**Date:** 2026-08-08 · **Commit:** `204790d`

## What this stage was for

Stage 1 exists to answer the question whose wrong answer would be most expensive later:
**can the medallion layers encrypt individual columns under separate keys?** Contribution
C1 assumes yes. Learning otherwise in Stage 5 would mean rewriting Bronze and Silver.

Everything else in Stage 1 is scaffolding built around that question.

## What was implemented

| Component | File | Purpose |
|---|---|---|
| Settings model | `src/confidentia/config/settings.py` | Typed configuration for every subsystem; refuses a `data_root` inside OneDrive/Dropbox |
| Structured logging | `src/confidentia/common/logging.py` | JSON logs, automatic secret redaction, TEE-backend binding on every line |
| Hashing primitives | `src/confidentia/common/hashing.py` | Canonical JSON, content hashing, Merkle root, provenance chain verification |
| KMS client | `spikes/pme/java/LocalKmsClient.java` | `KmsClient` implementation; AES-256-GCM key wrapping with the key id bound as AAD |
| Feasibility spike | `spikes/pme/` | Six arms plus four follow-up probes |
| Unit tests | `tests/unit/test_hashing.py` | 13 tests, all passing |
| Container stack | `docker-compose.yml` | Five profiles with hard memory limits |
| CI | `.github/workflows/ci.yml` | Lint, types, unit tests, **encryption-guarantee regression job**, compose validation |

## Environment

Three blockers surfaced and were resolved (ADR 0001):

1. Hadoop's `Shell` class cannot initialise on Windows without `winutils.exe`, available
   only from unofficial mirrors. Unsetting `HADOOP_HOME` does not help.
2. Ubuntu 26.04 ships Python 3.14 only; PySpark 3.5 does not support it.
3. Docker Desktop would take 1.5–2 GB of RAM on a 7.4 GB host.

**Resolution:** WSL2 Ubuntu 26.04, capped at 4 GB, running native Linux processes.
Python 3.11.15 provisioned by `uv`. JDK 17. Repository at `~/confidentia`, data at
`~/confidentia-data`, both in the Linux filesystem.

## The spike result

**Verdict: per-column encryption is viable. `column_encryption = "pme"`.**

### Method, and a correction to it

The first run reported 5/6 arms passing. **Two of those results were unsound**, and the
method was changed:

- Arm B asserted only that data round-tripped — which an *unencrypted* write satisfies
  equally well. Verification now inspects the bytes on disk: Parquet magic (`PARE` vs
  `PAR1`) plus a search for known sensitive values in the raw file.
- The keyless-read test ran in the same process as the write. `SparkSession.stop()`
  does not restart the JVM under PySpark, and Parquet's `KeyToolkit` caches unwrapped
  keys statically, so a cached key made the keyless read appear to succeed. Every
  access-control probe now runs in its own OS process.

Both corrections made the evidence stronger, not weaker.

### Findings

| Probe | Result |
|---|---|
| PME through Spark to Parquet | `PARE/PARE`, no plaintext in bytes |
| PME through Spark to Delta, via Hadoop Configuration | `PARE/PARE`, no plaintext in bytes |
| Keyless read of encrypted Parquet | **Refused** |
| Keyless read of encrypted Delta | **Refused** |
| `delta-rs` reading an encrypted table | **Cannot open it** |

The refusal chain, which is the actual evidence of enforcement:

```
KeyAccessDeniedException: Column [patient_name]: key access denied
  Caused by: KeyAccessDeniedException: Master key not available to this principal: k_identity
    at org.confidentia.crypto.LocalKmsClient.requireMasterKey(LocalKmsClient.java:129)
```

A principal holding only the footer key reads the schema and the plaintext columns, and
is refused the identity columns. That is precisely the sensitivity-tiered access C1
requires.

### Two consequences worth flagging

**Delta silently writes plaintext when encryption is passed as DataFrameWriter
options.** No error, no warning — `PAR1` magic and clinical notes readable in the clear.
The properties must be set on the Hadoop Configuration instead. Because the naive
integration *looks* like it works, a write-time verification gate is now mandatory:
Bronze and Silver writers must assert `PARE` magic on sample output and fail the run
otherwise. This becomes a data quality check in Stage 7, and CI re-proves it on every
push.

**`delta-rs` cannot read encrypted Parquet**, so the FastAPI layer cannot read Bronze or
Silver directly. Serving goes through Spark or through Gold. This confirms rather than
disrupts the planned design (Postgres holds Gold marts), but it makes the Gold storage
decision a real trade-off to argue in Stage 6 rather than a default.

### Indicative timings

Not benchmarks — single machine, cold JVM, 50 000 rows, one run each. Experiment E4
will measure this properly under the isolated `bench` profile.

| Arm | Write | Read | Size |
|---|---|---|---|
| Plaintext Parquet | 1.34 s | 0.42 s | 1 512 996 B |
| PME Parquet | 0.44 s | 0.43 s | 1 527 744 B |
| Envelope columns | 1.93 s | 0.13 s | 12 265 657 B |

PME costs ~1% in storage. Envelope encryption costs ~8×, because ciphertext does not
compress and column statistics are lost — which is why it is now the E4 baseline
comparison arm rather than the fallback.

The write ordering (PME apparently faster than plaintext) is a JVM warm-up artifact
across arms, not a real effect. It is stated rather than quietly omitted because it
illustrates exactly why E4 needs warm-up iterations and repeats.

## How to verify

```bash
source .venv/bin/activate && python spikes/pme/build_kms_jar.py && bash scripts/_wsl_probes.sh
```

## State

- 13 unit tests passing, lint clean.
- Nothing is claimed as deployed. No cloud resources exist. The TEE backend is `sim`,
  and `TeeSettings.is_measured` is `False`.

## Next: Stage 2 — data generation

Synthetic healthcare and financial generators capable of 5M+ rows, with realistic
distributions and deliberately injected dirty records, schema drift and late-arriving
events, so that the data quality layer in Stage 7 has something real to catch.
