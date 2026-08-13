# CONFIDENTIA on Azure: what would replace each simulated component

This document answers the question the rest of the project cannot: **if the enclave
were real, what exactly would change?**

It is the closing argument of the whole thesis. Every confidentiality claim
CONFIDENTIA makes is currently conditional on `TEE_BACKEND=sim`, where the process
measures itself and could lie. This is the map from there to hardware.

**Nothing here has been deployed.** See `infrastructure/terraform/README.md` for
exactly what was and was not verified.

## The mapping, component by component

| CONFIDENTIA today | Azure equivalent | What changes |
|---|---|---|
| `TeeSettings.backend = "sim"` | DCasv5 VM, AMD SEV-SNP | `is_measured` becomes True; every MODELLED label becomes MEASURED |
| `crypto/attestation.py` — SHA-256 of the source tree | SEV-SNP report signed by AMD firmware | The measurement is produced by hardware the process cannot forge |
| the verifier in `attestation.py` | Microsoft Azure Attestation | A third party decides whether the report is genuine |
| `crypto/broker.py` — four refusal paths | Managed HSM secure key release policy | Refusal stops being a policy the app honours and becomes one it cannot bypass |
| `crypto/keys.py` — scrypt-wrapped local keystore | Managed HSM, FIPS 140-3 Level 3 | The master key never exists in host memory |
| `LocalKmsClient.java` | A KMS client calling Managed HSM | Parquet's `KmsClient` interface is the seam; this was why it exists |
| `lake/` on local disk | ADLS Gen2, `is_hns_enabled` | Delta needs atomic rename, which hierarchical namespace provides |
| local PostgreSQL | Flexible Server, VNet-injected, `wal_level=logical` | CDC still works; the metadata store holds no tenant rows |
| `docker compose --profile core` | Container Apps / AKS | Unchanged in shape; the API stays outside the boundary |

## What actually becomes true, and what does not

### Becomes true

**The attestation stops being self-issued.** Today `measure_code()` hashes
`MEASURED_PACKAGES` and the same process presents that hash to the broker. Under
SEV-SNP the launch measurement comes from firmware, is signed by AMD's key, and is
verified by MAA before the HSM will release anything. The four refusal paths in
`broker.py` remain exactly as they are — they become enforceable rather than
voluntary.

**C5's modelled costs become measurable.** The placement planner reports a modelled
×1.206 for running the pipeline inside the enclave, built from published SGX
microbenchmarks. Run the same pipeline on a DCasv5 and that number is a
measurement. The *placement* does not change — it is derived from the schema
contracts and would give the same answer on any hardware.

**`hardware_rooted` becomes true.** Every row in `platform.attestation_policy` and
`platform.key_release_audit` carries it, and it is false everywhere today.

### Does not become true

**The policy proves the hardware, not the code.** The claims in
`policies/skr-policy.json` — `sevsnpvm`, `azure-compliant-uvm`,
`is-debuggable=false` — attest that a genuine, non-debuggable Azure confidential VM
is asking. They do **not** attest that CONFIDENTIA is what is running on it. Any
Azure CVM meeting those claims gets the key.

Closing that needs the `x-ms-sevsnpvm-hostdata` claim carrying the hash of a
confidential container group's security policy, which means Confidential Containers
on ACI or AKS rather than a plain confidential VM. That is a materially different
deployment and is not what the Terraform builds.

So the honest ladder is:

```
sim              proves nothing (the process measures itself)
   ↓
this deployment  proves the hardware and the platform image
   ↓
confidential     proves the workload as well
containers
```

This deployment is strictly stronger than `sim` and strictly weaker than the claim
"attestation-gated key release" implies in full. Both halves of that need saying.

**The landing zone was plaintext when this was written, and no longer is.** C5 found
it, ADR 0017 recorded it, and ADR 0022 closed it: landing is encrypted under the same
attestation-released, per-sensitivity keys as Bronze, at a measured ×1.014 storage
cost. The separate container and one-day lifecycle rule in the Terraform were sized
as mitigation for the plaintext and are now defence in depth rather than the only
defence.

**Managed HSM is expensive.** It bills per hour whether or not a key is used, and it
is the dominant cost of this architecture by a wide margin. Key Vault Premium
supports secure key release too, at lower assurance (FIPS 140-2 Level 2, shared
tenancy). For a dissertation deployment that trade is worth making explicitly rather
than by default.

## What would have to change in the code

Less than one might expect, which is a consequence of decisions taken earlier for
other reasons.

1. **`TEE_BACKEND=snp-cvm`.** `TeeSettings` already has the enum; `is_measured` and
   `measurement_label` already key off it. Nothing that consumes them branches on
   the backend, so the labels flip and every report follows.

2. **A new `TeeBackend` implementation** producing an MAA token instead of a
   self-signed ECDSA report. `crypto/attestation.py` already separates producing
   evidence from verifying it.

3. **A Managed-HSM-backed `KmsClient`** replacing `LocalKmsClient.java`. Parquet's
   `KmsClient` interface is the seam, and ADR 0007 chose
   `PropertiesDrivenCryptoFactory` specifically so the KMS could be swapped without
   touching the writers. This is the one piece of real Java work.

4. **`broker.py` calls the HSM's release endpoint** rather than deciding locally.
   The audit table and the four refusal reasons stay; what changes is who decides.

5. **Storage paths become `abfss://`.** `settings.lake_root` is already a single
   property.

Nothing in `pipelines/`, `quality/`, `evaluation/` or `lineage/` changes. That is
the argument for the abstraction: the confidentiality boundary was a design
parameter from Stage 1, so moving it touches the seams built for it rather than
requiring a rewrite.

**This section used to end "configuration and one Java class", which contradicted
the five items above it.** Item 2 is Python, in a measured package, and item 4
changes who decides a key release. Preparing the deployment is what caught it:
`produce_measurement()` raises for every non-`sim` backend, so today
`TEE_BACKEND=snp-cvm` flips every label to MEASURED and then fails every
attestation. The infrastructure comes up; the platform refuses to run on it.

**Item 2 has since been written** — `crypto/snp.py`, ADR 0024 — and has never run on
confidential computing hardware. `VALIDATED_ON_HARDWARE` is `False`, stamped into
every piece of evidence and asserted by a test, because "implemented" and "known to
work" are different claims and this project labels the difference rather than
letting a reader assume it. The MAA cross-check on the launch measurement is what
will catch a wrong struct offset on first run.

Item 4 is now the open one: `broker.py` still decides locally.

## What this would cost to prove

The smallest experiment that would turn the modelled numbers into measurements:

1. One DCasv5 VM, one Managed HSM, one attestation provider.
2. `keys init` against the HSM; create the tenant keys with the SKR policy.
3. `rebuild_pipeline.sh` — the same script used locally.
4. `confidentia plan show` — the placement is unchanged, the costs are now measured.
5. `verify_encryption.sh`, `verify_lineage.sh` — the same claims, now hardware-rooted.

Roughly a day of work against a subscription, most of it the KMS client. The result
would replace the single largest caveat in the dissertation.
