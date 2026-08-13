# Deploying to Azure, once

The goal is narrow: **replace the single largest caveat in the dissertation with a
measurement.** Section 4 labels every enclave figure MODELLED because the TEE
backend is simulated. A deployment of the confidential VM should produce a real
SEV-SNP attestation report signed by AMD and let `confidentia bench` record the
enclave overhead currently modelled at ×1.206.

---

## Stop: this cannot be completed today

Preparing this runbook exposed a defect, and it is better to state it at the top
than to let you find it after an apply.

**Nothing in this codebase can read a SEV-SNP attestation report.**
`AttestationBackend.produce_measurement()` raises for every backend that is not
`sim`. So the sequence on a deployed VM is:

1. `TEE_BACKEND=snp-cvm` — settings accept it, `is_measured` flips to `True`, every
   label reads MEASURED. **It looks like it worked.**
2. The first attestation raises `AttestationError`.
3. Every key release fails. The pipeline cannot run at all.

**The infrastructure would come up and the platform would refuse to run on it.**
Verified locally, not inferred:

```
$ TEE_BACKEND=snp-cvm confidentia info
backend=snp-cvm is_measured=True label=MEASURED
AttestationError: backend snp-cvm is selected, and this host has no hardware
attestation source...
```

What is missing is `docs/architecture/azure.md` step 2: read the report from
`/dev/sev-guest` or the vTPM NV index and exchange it for an MAA token. That is
perhaps a day's work, and **it can only be written and tested on the hardware** —
which is why it is not written here. Shipping an untested hardware path and calling
the deployment ready would be precisely the failure this project spent sixteen
stages avoiding.

So the honest sequence is: **deploy the infrastructure, write step 2 on the VM,
then measure.** Sections 1–3 below stand as written. Section 4 is blocked until
step 2 exists.

---

## Read this before you run anything

**The Managed HSM cannot be undone in an afternoon.** Purge protection is required
for it and cannot be disabled after creation, so `terraform destroy` moves the HSM
into a soft-deleted state that persists for the retention period rather than
removing it. It is also the most expensive resource in the configuration by a wide
margin, billed per hour whether or not a key is ever released.

Check the current figure on the [Azure Key Vault pricing
page](https://azure.microsoft.com/pricing/details/key-vault/) before you decide —
it is the number that determines whether this is a student experiment or a bill.
Nothing in this repository should be taken as a price quote.

So the configuration now has a flag, defaulting to off:

| `deploy_managed_hsm` | What you get | What it costs |
|---|---|---|
| `false` *(default)* | A genuine confidential VM and an attestation provider — everything step 2 needs to be written and tested against. Key release stays a policy the application honours. | VM + Postgres + storage, hourly, destroyable |
| `true` | The full C2 architecture: key release the hardware enforces | The above plus an HSM you cannot delete for the retention period |

**Deploy with `false` first**, and note it does not answer the dissertation's
question by itself — step 2 does, and this is the environment in which step 2 can be
written. Set `true` only when the money and the retention window are both acceptable
to you, and only once step 2 works.

---

## What you need that this machine does not have

1. **An Azure subscription** with quota for `Standard_DC4as_v5` in the target
   region. Confidential VM quota is often zero by default on new and student
   subscriptions and has to be requested.
2. **The Azure CLI**, which is not installed here:
   ```bash
   curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
   ```
3. **Your Entra object ID**, for `admin_object_ids`.
4. **An SSH key pair**, for the operator account on the VM.

I have not run any of this. Everything below has been validated as far as it can be
without credentials — `terraform validate` passes, `fmt -check` passes, Checkov
reports 36 passed and 0 failed, and `terraform plan` gets as far as building the
provider and stops on authentication, which is the only thing missing.

---

## 1 · Sign in and check quota

```bash
az login
az account set --subscription "<subscription-id>"
```

Confidential VM availability is the thing most likely to stop you, so check it
before planning rather than after:

```bash
az vm list-skus --location northeurope --size Standard_DC4 --output table
```

If `Standard_DC4as_v5` is absent or restricted, try `westeurope` or `eastus` and
change `location` in `environments/measure.tfvars`. The attestation provider must
exist in the same region; both are listed per-region in Azure's product
availability pages.

```bash
az vm list-usage --location northeurope --output table | grep -i confidential
```

A quota of 0 means the plan will succeed and the apply will fail. Request an
increase from the portal before continuing.

---

## 2 · Plan

```bash
cd infrastructure/terraform
terraform init
terraform plan -var-file=environments/measure.tfvars \
  -var="admin_ssh_public_key=$(cat ~/.ssh/id_ed25519.pub)" \
  -var='admin_object_ids=["<your-entra-object-id>"]' \
  -out=measure.tfplan
```

**Read the plan.** Two things to confirm before applying:

- `azurerm_key_vault_managed_hardware_security_module` appears **nowhere**. If it
  does, you are on the wrong var file and about to make a commitment this runbook
  is written to stop you making by accident.
- The resource count is around twenty, all inside one resource group named
  `rg-confidentia-dev`. Everything is in that group, which is what makes teardown
  a single command.

```bash
terraform apply measure.tfplan
```

Roughly ten minutes, most of it Postgres.

---

## 3 · Confirm the attestation is real

This is the whole point of the exercise, so check it before anything else.

```bash
terraform output -json platform_environment
terraform output key_release_enforced     # false, on this var file
```

SSH to the VM through Bastion — the compute NSG allows nothing from the internet,
by design — then:

```bash
sudo systemctl status confidentia-attestation
curl -s http://169.254.169.254/metadata/instance?api-version=2021-02-01 \
  -H "Metadata: true" | jq .compute.securityProfile
```

`securityType` should read `ConfidentialVM`. **This is the check worth doing on day
one** — it proves the VM really is a confidential VM, independently of any code in
this repository, and it is the one thing the deployment settles on its own.

Note the environment variable is `TEE_BACKEND`, not `CONFIDENTIA_TEE_BACKEND`:
`TeeSettings` uses the `TEE_` prefix. The first draft of this runbook had it wrong,
which would have left the backend silently on `sim` and produced numbers
indistinguishable from a laptop's.

---

## 4 · Write step 2, on the VM

Blocked until this exists — see the warning at the top. Roughly:

- Read the SEV-SNP report. On an Azure CVM: `/dev/sev-guest` (`SNP_GET_REPORT`
  ioctl) with the nonce as report data, or the vTPM NV index the Azure guest
  attestation library uses.
- POST it to the MAA endpoint from `terraform output attestation_uri` and take the
  JWT back.
- Return `(launch_measurement, True)` from `produce_measurement()`, and have
  `AttestationVerifier` validate the MAA token's signature against MAA's JWKS
  instead of the self-signed ECDSA path.

`crypto/attestation.py` already separates producing evidence from verifying it, so
this is an implementation behind an existing seam rather than a redesign.

**`crypto` is a measured package.** Changing it changes the measurement, so key
release is refused until the new build is approved — which is the mechanism working,
not a problem. `confidentia keys approve` after each change.

---

## 5 · Take the measurement

```bash
confidentia keys approve --approved-by azure-deployment
confidentia info                          # measured must read True *and* attest must succeed
bash scripts/rebuild_pipeline.sh          # ~10 minutes
confidentia bench e4                      # encryption cost, now on real hardware
confidentia plan show                     # the modelled figures, beside the real ones
confidentia bench results
```

The number that matters is the pipeline overhead. `plan show` models it at ×1.206
from a cost model with no hardware behind it; this run produces the measured
equivalent. **Record both.** If they disagree — and they may, substantially — the
disagreement is a result, not a failure, and it makes a better section 9 than the
one currently there.

---

## 6 · Tear it down the same day

```bash
terraform destroy -var-file=environments/measure.tfvars \
  -var="admin_ssh_public_key=$(cat ~/.ssh/id_ed25519.pub)" \
  -var='admin_object_ids=["<your-entra-object-id>"]'
```

Then confirm, because a destroy that half-failed is the expensive kind:

```bash
az group show --name rg-confidentia-dev   # should be: ResourceGroupNotFound
az keyvault list-deleted --output table    # should list nothing for this project
```

The second command is not paranoia. It is the one that catches a soft-deleted
resource still billing.

---

## If you do deploy the HSM

Use `environments/dev.tfvars`, which sets `deploy_managed_hsm = true`, and know
these three things first:

1. **An HSM is not usable when Terraform finishes.** It must be *activated* by
   downloading a security domain, quorum-encrypted to keys you provide. Lose the
   security domain and the HSM is unrecoverable — and so is every DEK derived from
   it. `terraform output planned_tenant_keys` lists what to create afterwards.
2. **`policies/skr-policy.json` needs the real authority.** Paste
   `terraform output attestation_uri` into it before creating any exportable key.
   The policy is attached at key creation and cannot be loosened afterwards without
   recreating the key.
3. **The policy proves the hardware, not the code.** This is already recorded as a
   limitation in the report, and deploying does not change it: any Azure
   confidential VM meeting `sevsnpvm`, `azure-compliant-uvm` and
   `is-debuggable=false` obtains the key. Pinning the workload needs the `hostdata`
   claim and confidential containers.

---

## What deployment does and does not settle

**Settles immediately:** that the VM is genuinely a confidential VM. The instance
metadata `securityType` field is Azure's answer, not this repository's, and it needs
no code from here.

**Settles once step 2 is written:** whether attestation is real. Under `sim` the
process measures itself and could lie; on SEV-SNP the measurement comes from
firmware, is signed by AMD, and is verified by MAA. Audit rows written after that
carry `hardware_rooted = true` and mean it.

**Settles once step 2 is written:** the enclave overhead, replacing a modelled
×1.206 with a measurement.

**Settles nothing on its own:** the infrastructure. Standing up the resource group
without step 2 proves the Terraform applies and the quota exists. That is worth
knowing and is not a result.

**Does not settle:** the placement. That is derived from the schema contracts and
would be identical on any hardware — which is the argument section 5.5 makes, and
one deployment neither strengthens nor weakens it.

**Does not settle, with `deploy_managed_hsm = false`:** whether a refusal is
enforceable. The four refusal paths still run, still audit, and are still a policy
the application chooses to honour. Say so plainly rather than letting a deployment
imply more than it demonstrated.
