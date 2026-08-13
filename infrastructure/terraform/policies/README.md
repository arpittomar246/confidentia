# The secure key release policy

`skr-policy.json` is the real version of `confidentia/crypto/broker.py`, and the
comparison is the most useful thing in this directory.

## What the platform does today

`broker.py` receives an attestation report, checks the measurement against
`platform.attestation_policy`, and refuses if it is unknown, revoked, not permitted
for that tenant, or stale. Four refusal paths, all verified, all recorded in
`platform.key_release_audit`.

Under `TEE_BACKEND=sim` the measurement is a SHA-256 of this repository's source,
produced by the same process that then asks for the key. **The process measures
itself and could lie.** Every policy and audit row therefore carries
`hardware_rooted = false`. The mechanism is real; the guarantee is not.

## What this policy does instead

The three claims below are asserted by Microsoft Azure Attestation about a report
that AMD's firmware signed, and the Managed HSM will not release the key unless MAA
says they hold:

| Claim | Requires |
|---|---|
| `x-ms-attestation-type = sevsnpvm` | The report came from AMD SEV-SNP hardware, not a simulator |
| `x-ms-compliance-status = azure-compliant-uvm` | The guest is running a Microsoft-signed utility VM whose measurement Azure recognises |
| `x-ms-sevsnpvm-is-debuggable = false` | The guest was launched with debugging disabled, so its memory cannot be inspected from the host |

The third is the one people leave out. A debuggable confidential VM attests
successfully and offers no confidentiality at all, because the hypervisor can read
guest memory — the entire property being paid for. Requiring it to be false costs
nothing and closes the hole.

The refusal stops being a policy the application chooses to honour and becomes one
it cannot bypass: a workload that does not satisfy these claims does not receive the
key, whatever its code says.

## What this policy does *not* do

**It does not pin the application.** `azure-compliant-uvm` attests the Microsoft
utility VM, not CONFIDENTIA. Any Azure confidential VM meeting these claims can
obtain the key. Pinning the workload needs a container-launch measurement — the
`x-ms-sevsnpvm-hostdata` claim carrying the hash of a confidential container group's
security policy — which requires Confidential Containers on ACI or AKS rather than a
plain confidential VM.

That is a real gap between this policy and the property the platform claims, and it
is the honest state of the design: **the current policy proves the hardware, not the
code.** `broker.py` under `sim` proves neither, so this is still strictly stronger —
but "attestation-gated key release" in its full sense needs the container path.

## Applying it

The `authority` field must be replaced with the attestation provider's real URI,
which Terraform outputs as `attestation_uri`:

```bash
terraform -chdir=infrastructure/terraform output -raw attestation_uri
```

Then, once the HSM has been activated with its security domain:

```bash
az keyvault key create --hsm-name <hsm> --name confidentia-healthcare-kek \
  --kty RSA-HSM --size 3072 --ops wrapKey unwrapKey \
  --exportable true --policy @skr-policy.json
```

`--exportable` is what makes a key eligible for release at all, and it is only safe
because the policy is attached at creation. A key created without one, or with a
weaker one, cannot be tightened afterwards — it has to be recreated, which means
rewrapping everything beneath it.

**None of this has been run.** There is no Azure subscription behind this project.
The policy's structure is from the documented schema; that it is accepted by a real
Managed HSM is untested.
