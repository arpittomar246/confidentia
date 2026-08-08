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

| | Contribution | Status |
|---|---|---|
| **C1** | Crypto-partitioned medallion storage — per-column encryption keys across Bronze/Silver | Feasibility proven (ADR 0007) |
| **C2** | Attestation-gated key release — no key without a verified enclave measurement | Seam in place (`LocalKmsClient`) |
| **C3** | Cryptographically verifiable lineage — signed, Merkle-chained provenance | Primitives in place (`common/hashing.py`) |
| **C4** | Disclosure control on Gold aggregates and DQ metrics | Designed |
| **C5** | Enclave placement planner and EPC-aware partition sizing | Designed |

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

- [ADR 0001 — Local runtime environment](docs/adr/0001-runtime-environment.md)
- [ADR 0007 — Column encryption strategy](docs/adr/0007-column-encryption-strategy.md)
- [Stage 1 report](docs/stage-reports/stage-01.md)

## Licence

MIT.
