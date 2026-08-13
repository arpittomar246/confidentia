# Deploying to Azure, once

The goal is narrow: **replace the single largest caveat in the dissertation with a
measurement.** Section 4 labels every enclave figure MODELLED because the TEE
backend is simulated. A deployment of the confidential VM should produce a real
SEV-SNP attestation report signed by AMD and let `confidentia bench` record the
enclave overhead currently modelled at ×1.206.

---

## Read this first: the reader has never run on hardware

The SEV-SNP reader now exists (`crypto/snp.py`, ADR 0024), so the deployment can be
attempted end to end. **It has never been executed on confidential computing
hardware**, and that is a load-bearing caveat rather than a formality.

`snp.VALIDATED_ON_HARDWARE` is `False`. It is stamped into every piece of evidence,
lands in the key-release audit trail, and a test asserts it. The first successful
run on Azure is what earns the right to flip it.

**The failure to watch for is not a crash.** A wrong struct offset returns 48 bytes
that look exactly like a launch measurement. Nothing on a laptop can tell the
difference — so the code asks MAA to parse the same report and compares the two
answers on every attestation. If they disagree it raises and names the offset. That
check is why the deployment is worth attempting rather than guessing.

**No number from this path goes into the dissertation until the reader has produced
a measurement that MAA agreed with.** Three outcomes are possible on first run, and
all three are useful:

| Outcome | What it means |
|---|---|
| `SnpError` naming a source | The ioctl or the vTPM path is wrong. It says which. |
| Measurement disagreement | The offsets are wrong. MAA is right; fix `MEASUREMENT_OFFSET`. |
| A verified token | It works. Flip the marker, and every figure from that VM is real. |

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

## 4 · Validate the reader

The moment of truth, and the one thing this deployment exists to settle.

```bash
export TEE_BACKEND=snp-cvm
export CRYPTO_ATTESTATION_URI="$(terraform output -raw attestation_uri)"
confidentia keys check          # exercises the whole path: report, MAA, verify
```

Three outcomes, all informative:

**`SnpError: no SEV-SNP attestation report could be obtained`** — neither source
worked, and the message names what each returned. Usually one of: the `sev-guest`
driver is not loaded (`modprobe sev-guest`), the device node is not readable by this
user, or `tpm2-tools` is not installed for the vTPM fallback.

**`SnpError: MAA and this module disagree about the launch measurement`** — the
reader ran and parsed the wrong bytes. MAA is authoritative; adjust
`MEASUREMENT_OFFSET` in `crypto/snp.py` to match. This is the failure the whole
cross-check exists to catch, and finding it here rather than in the dissertation is
the point.

**A measurement, and a token that verifies** — it works. Now:

```bash
# Only after MAA has agreed with the parsed measurement.
sed -i 's/VALIDATED_ON_HARDWARE = False/VALIDATED_ON_HARDWARE = True/' \
  src/confidentia/crypto/snp.py
pytest tests/unit/test_snp.py   # the marker test now fails; update it deliberately
```

**`crypto` is a measured package**, so every edit above changes the measurement and
key release is refused until the new build is approved. That is the mechanism
working, not a problem. `confidentia keys approve` after each change.

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

**Settles once the reader is validated:** whether attestation is real. Under `sim`
the process measures itself and could lie; on SEV-SNP the measurement comes from
firmware, is signed by AMD, and is verified by MAA. Audit rows written after that
carry `hardware_rooted = true` and mean it.

**Settles once the reader is validated:** the enclave overhead, replacing a modelled
×1.206 with a measurement.

**Settles nothing on its own:** the infrastructure. Standing up the resource group
proves the Terraform applies and the quota exists. That is worth knowing and is not
a result.

**Does not settle, whatever happens:** whether `crypto/snp.py` is correct in the
places this deployment does not exercise. It reads two fields of a forty-field
structure. A validated launch measurement says those two offsets are right; it says
nothing about the rest, and nothing else is parsed — which is why nothing else is
parsed.

**Does not settle:** the placement. That is derived from the schema contracts and
would be identical on any hardware — which is the argument section 5.5 makes, and
one deployment neither strengthens nor weakens it.

**Does not settle, with `deploy_managed_hsm = false`:** whether a refusal is
enforceable. The four refusal paths still run, still audit, and are still a policy
the application chooses to honour. Say so plainly rather than letting a deployment
imply more than it demonstrated.
