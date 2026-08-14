# CONFIDENTIA

**A confidential lakehouse platform — attested, privacy-preserving data engineering on
untrusted cloud infrastructure.**

M.Tech major project · Arpit Tomar (M25DE2005) · IIT Jodhpur

---

## The problem

Encryption protects data at rest and in transit. It does not protect **data in use**:
during computation, data must be decrypted in host memory on infrastructure the data
owner does not control — exposing it to malicious administrators, compromised
hypervisors, and co-tenant attacks.

Trusted Execution Environments close that gap for a single process. This project asks
the harder, unanswered question:

> **What does it cost, architecturally and quantitatively, to run a full
> medallion-architecture data platform — ingestion, CDC, streaming, transformation, data
> quality, lineage, serving — under a hardware-enforced confidentiality boundary, and
> where in that pipeline should the boundary be drawn?**

## Contributions

| | Contribution | Verified by |
|---|---|---|
| **C1** | Crypto-partitioned medallion storage — per-column, per-sensitivity, per-tenant keys | Byte-level inspection; keyless reads refused in a separate OS process |
| **C2** | Attestation-gated key release — no key without an approved code measurement | 19 recorded refusals across 367 requests |
| **C3** | Cryptographically verifiable lineage — signed, Merkle-chained provenance | Four tamper attacks, each detected *for the correct reason* |
| **C4** | Disclosure control on Gold aggregates — k-anonymity with complementary suppression | Suppression counts audited per publish |
| **C5** | Enclave placement planner — boundary derived from the schema contracts | Placement is a derivation; every cost beside it labelled MODELLED |

All five are implemented. The one caveat that matters is below.

## Honesty policy

This project has **no confidential-computing hardware**. The TEE layer is an abstraction
with three backends (`sim`, `gramine-sgx`, `snp-cvm`) and defaults to `sim`.

- **Real measurements:** cryptography, storage, Spark, streaming, pipeline throughput,
  data quality, recovery — everything measurable on commodity hardware.
- **Modelled:** enclave-transition and EPC-paging costs only.

`TeeSettings.is_measured` is False under `sim`, and every chart, table and report line
derived from it is labelled `MODELLED`. No number in this repository is presented as a
measurement unless it was measured.

## Getting started

**New to the project?** [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) explains what
this is in plain language and walks through a first run end to end, assuming no
background in confidential computing, Spark or data engineering. The rest of this
section is the terse version.

Requires WSL2 with Ubuntu (see [ADR 0001](docs/adr/0001-runtime-environment.md) for why
this is not optional on Windows).

```bash
bash scripts/bootstrap_wsl.sh
```

Then verify the encryption guarantee end to end:

```bash
source .venv/bin/activate && python spikes/pme/build_kms_jar.py && bash scripts/_wsl_probes.sh
```

Expected: encrypted tables show `PARE` magic with no plaintext in the bytes, keyless
reads are refused with `KeyAccessDeniedException`, and `delta-rs` cannot open them.

## Layout

```
src/confidentia/
  config/        settings, profiles, sensitivity classification
  common/        logging, hashing, Merkle chaining
  crypto/        key hierarchy, envelope encryption, KMS client
  tee/           TEE backend abstraction (sim / gramine-sgx / snp-cvm)
  ingestion/     REST, file, CDC and stream collectors
  streaming/     Kafka producers and Structured Streaming consumers
  pipelines/     bronze / silver / gold transformations
  quality/       data quality rules, scoring, quarantine
  lineage/       signed provenance records and the ledger
  catalog/       dataset metadata and schema contracts
  benchmarks/    experiments E1-E8
  generators/    synthetic healthcare and financial data
orchestration/   Airflow DAGs
backend/         FastAPI service
frontend/        React + TypeScript dashboard
spikes/          feasibility experiments (the PME spike lives here)
infrastructure/  Terraform for the Azure deployment architecture
docs/adr/        architecture decision records
```

## Documentation

**Start here**

- [Getting started](docs/GETTING_STARTED.md) — what this is, in plain language, and a
  first run end to end
- [The dissertation](docs/report/final-report.md) — abstract through conclusion
- [Demo script](docs/report/demo-script.md) — a 12-minute guided tour

**The decisions that constrain everything else** — 20 ADRs in [`docs/adr/`](docs/adr/):

- [0001 — Local runtime environment](docs/adr/0001-runtime-environment.md), and why WSL
  is not optional
- [0007 — Column encryption strategy](docs/adr/0007-column-encryption-strategy.md), and
  the KMS seam the whole design turns on
- [0017 — Where the confidentiality boundary is drawn](docs/adr/0017-enclave-placement.md)
- [0022 — Encrypting the landing zone](docs/adr/0022-landing-zone-encryption.md), and why
  the violation detector now reads the disk rather than repeating a conclusion
- [0024 — A SEV-SNP reader, written and marked unproven](docs/adr/0024-snp-reader-unvalidated.md)

**Operations**

- [Deploying to Azure](docs/deploy.md) — including what stops it working today
- [Experiments](docs/experiments.md) — E1–E8, what each measured and what is unmeasured
- [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md) — the full engineering handover

## Licence

MIT.
