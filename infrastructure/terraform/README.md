# Infrastructure

Terraform for deploying CONFIDENTIA to Azure, where the simulated TEE would become
a real one.

## This has never been applied

There is no Azure subscription behind this project. What has been verified:

| | |
|---|---|
| `terraform fmt -check -recursive` | passes |
| `terraform init -backend=false` | passes |
| `terraform validate` | **Success! The configuration is valid.** |
| `checkov -d . --config-file .checkov.yml` | **36 passed, 0 failed** |

What has **not**:

- `terraform plan` — requires subscription credentials, so it has never run.
  Validation checks the configuration against the provider's schema; plan checks it
  against reality, and reality has not been consulted.
- `terraform apply` — never run. Whether these resources deploy, whether the SKUs
  are available in the chosen region under a given quota, and whether the
  attestation extension binds correctly to the HSM policy are all **unknown**.

This is the same distinction the platform makes everywhere else. `TeeSettings`
labels a modelled number MODELLED; every resource here carries
`provenance = "never-applied"` as a tag, so the label survives into the portal
rather than living only in a comment.

## What it builds

```
rg-confidentia-dev
├── vnet 10.42.0.0/16
│   ├── snet-compute 10.42.1.0/24   confidential VM, NSG, no inbound
│   └── snet-data    10.42.2.0/24   delegated to PostgreSQL, NSG
├── Microsoft Azure Attestation      verifies SEV-SNP reports
├── Managed HSM                      holds the per-tenant wrapping keys
├── PostgreSQL Flexible Server       platform metadata, wal_level=logical
├── ADLS Gen2                        the lake + a separate landing container
└── DCasv5 confidential VM           AMD SEV-SNP, vTPM, secure boot
```

The interesting part is `modules/key_management` and `policies/skr-policy.json`.
Everything else is ordinary cloud plumbing; those two are the real version of C2,
and `policies/README.md` compares them against what `broker.py` does today.

## Two manual steps that are deliberately not automated

**Activating the Managed HSM.** A new HSM holds no keys until it is activated with
a security domain, which requires a quorum of RSA key pairs held by humans. That is
the moment an organisation takes custody of the root of trust, and automating it in
Terraform would defeat the reason it exists. `apply` stops before it; the tenant
keys are created afterwards with `az keyvault key create --policy @skr-policy.json`.

**Supplying `admin_object_ids`.** No default, and deliberately not the current
principal — an HSM whose administrators are whoever ran `terraform apply` is an HSM
with an accidental administrator.

## Running the checks

```bash
cd infrastructure/terraform
terraform fmt -check -recursive
terraform init -backend=false
terraform validate

docker run --rm -v "$PWD:/tf" bridgecrew/checkov:latest \
  -d /tf --config-file /tf/.checkov.yml --compact --quiet
```

CI runs all four on every push (`terraform` job in `.github/workflows/ci.yml`).

## Before you plan against a real subscription

Region availability for confidential VMs changes, and a missing SKU surfaces as a
quota error deep in an apply rather than a clear message:

```bash
az vm list-skus --location northeurope --size Standard_DC --output table
az provider show --namespace Microsoft.Attestation --query registrationState
```

The `location` variable validates against a hard-coded list of regions that offered
both at the time of writing. That list is a guess about someone else's roadmap and
should be re-checked rather than trusted.

## Cost

Not estimated here, and that is deliberate. Managed HSM bills per hour whether or
not a key is used and is the dominant line item by a wide margin; confidential VM
SKUs carry a premium over their general-purpose equivalents. Quoting figures I have
not been billed would be exactly the kind of unverified number this project
otherwise refuses to print. Use the Azure pricing calculator against the SKUs in
`environments/dev.tfvars`.
