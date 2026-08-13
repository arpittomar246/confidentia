# Stages 15–16 — Infrastructure and the cloud architecture

The last two stages, and the ones with the hardest honesty problem: writing a
deployment for hardware that does not exist, without letting it read as though it
had been deployed.

## What was verified, and what was not

| | |
|---|---|
| `terraform fmt -check -recursive` | passes |
| `terraform init -backend=false` | passes |
| `terraform validate` | **Success! The configuration is valid.** |
| `checkov --config-file .checkov.yml` | **36 passed, 0 failed, 8 skipped with reasons** |
| `terraform plan` | **never run** — requires subscription credentials |
| `terraform apply` | **never run** |

Every resource carries `provenance = "never-applied"` as a tag, so the label travels
into the portal rather than living in a comment. Same rule as
`TeeSettings.measurement_label`.

## Stage 15 — the Terraform

```
infrastructure/terraform/
├── versions.tf, variables.tf, main.tf, outputs.tf
├── .checkov.yml                    8 exceptions, each with a reason
├── policies/skr-policy.json        the real version of broker.py
├── policies/README.md              what it proves, and what it does not
├── environments/dev.tfvars
└── modules/
    ├── network/                    vnet, two subnets, two NSGs
    ├── data/                       PostgreSQL (wal_level=logical), ADLS Gen2
    ├── key_management/             Azure Attestation + Managed HSM
    └── confidential_compute/       DCasv5, SEV-SNP, vTPM, secure boot
```

Modules keep variables and outputs in `main.tf` rather than in separate files. The
convention exists to keep large modules navigable; these are forty lines each, and
three files of six lines is harder to read than one with headings.

### Validation caught two real errors

Both would have failed at apply time, and both are the reason `validate` is worth
running rather than assuming HCL is correct:

1. **`file("~/.ssh/id_rsa.pub")`** — Terraform does not expand `~` and requires the
   path to exist at validation time, so the configuration would only have validated
   on a machine that happened to have that key. Now a variable with no default, and
   a validation rule that rejects anything not shaped like an OpenSSH public key —
   because the failure mode of getting it wrong is pasting a *private* key into
   state.

2. **`delete_after_creation_greater_than_days`** — the azurerm 3.x name for the blob
   lifecycle argument. In 4.x it is
   `delete_after_days_since_creation_greater_than`.

### Checkov found three things worth fixing

`allow_nested_items_to_be_public = false`, `local_user_enabled = false`, and an NSG
on the delegated data subnet. All three were correct findings: a delegated subnet is
not exempt from needing a security group, and SFTP local users are a second
credential system beside Entra.

Eight are skipped with written reasons. The one worth naming is **`CKV_AZURE_50`,
"Ensure Virtual Machine Extensions are not Installed"** — the extension is
Microsoft's GuestAttestation agent, which turns an SEV-SNP hardware report into an
MAA token. Deleting it would give a clean score and remove the feature the
deployment exists for.

## Stage 16 — the architecture

`docs/architecture/azure.md` maps every simulated component onto its Azure
equivalent. The short version:

| Today | Azure |
|---|---|
| `attestation.py` — SHA-256 of the source tree | SEV-SNP report signed by AMD firmware |
| the verifier in `attestation.py` | Microsoft Azure Attestation |
| `broker.py` — four refusal paths | Managed HSM secure key release policy |
| `LocalKmsClient.java` | a KMS client calling Managed HSM |

### The claim that becomes true

**Attestation stops being self-issued.** Today `measure_code()` hashes
`MEASURED_PACKAGES` and the same process presents that hash to the broker — the
process measures itself and could lie, which is why every audit row carries
`hardware_rooted = false`. Under SEV-SNP the measurement comes from firmware, is
signed by AMD, and is verified by MAA before the HSM releases anything. The four
refusal paths stay exactly as they are; they become enforceable rather than
voluntary.

**C5's modelled ×1.206 becomes a measurement.** The placement itself does not change
— it is derived from the schema contracts and would be identical on any hardware.

### The claim that does not

**The policy proves the hardware, not the code.** `sevsnpvm`,
`azure-compliant-uvm` and `is-debuggable=false` attest that a genuine,
non-debuggable Azure confidential VM is asking. They do not attest that CONFIDENTIA
is running on it. Any Azure CVM meeting those claims gets the key.

Closing it needs `x-ms-sevsnpvm-hostdata` — the hash of a confidential container
group's security policy — which means Confidential Containers on ACI or AKS, a
materially different deployment. So:

```
sim              proves nothing (the process measures itself)
   ↓
this deployment  proves the hardware and the platform image
   ↓
confidential     proves the workload as well
containers
```

Strictly stronger than `sim`, strictly weaker than "attestation-gated key release"
implies in full. Both halves need saying.

### `is-debuggable = false` is the claim people leave out

A debuggable confidential VM attests successfully and offers no confidentiality at
all, because the hypervisor can read guest memory — the entire property being paid
for. Requiring it costs nothing and closes the hole.

## What moving to hardware would actually cost in code

Less than expected, and that is the payoff for treating the boundary as a design
parameter from Stage 1:

1. `TEE_BACKEND=snp-cvm` — the enum already exists and nothing branches on it
2. A backend producing an MAA token instead of a self-signed ECDSA report
3. **A Managed-HSM-backed `KmsClient`** — the one piece of real work. ADR 0007 chose
   `PropertiesDrivenCryptoFactory` precisely so this could be swapped
4. `broker.py` calls the HSM's release endpoint rather than deciding locally
5. `abfss://` paths — one property

Nothing in `pipelines/`, `quality/`, `evaluation/` or `lineage/` changes.

## Two manual steps that are deliberately not automated

**Activating the Managed HSM** needs a quorum of RSA key pairs held by humans. That
is the moment an organisation takes custody of the root of trust; automating it
would mean a machine holding the quorum. `apply` stops before it.

**`admin_object_ids` has no default.** An HSM whose administrators are whoever ran
`terraform apply` is an HSM with an accidental administrator.

## Known gaps

1. **Never applied.** The largest one. Whether these resources deploy, whether the
   SKUs are available under a given quota, and whether the attestation extension
   binds correctly to the HSM policy are all unknown.
2. **The landing zone is still plaintext.** Moving to Azure relocates it into a
   storage account; the Terraform bounds it with a separate container and a one-day
   lifecycle rule, which is mitigation, not a fix. ADR 0017 has the remedies.
3. **No cost estimate.** Managed HSM bills per hour regardless of use and dominates.
   Quoting figures I have not been billed would be the kind of unverified number
   this project otherwise refuses to print.
4. **No API or frontend deployment.** The Terraform builds the data plane and the
   confidential compute. Container Apps for the serving layer is a straightforward
   addition and is not there.
5. **The region list is a guess about someone else's roadmap.** `location` validates
   against regions that offered both confidential VMs and attestation at the time of
   writing; the README says how to re-check.

## Reproducing

```bash
cd infrastructure/terraform
terraform fmt -check -recursive && terraform init -backend=false && terraform validate
docker run --rm -v "$PWD:/tf" bridgecrew/checkov:latest \
  -d /tf --config-file /tf/.checkov.yml --compact --quiet
```
