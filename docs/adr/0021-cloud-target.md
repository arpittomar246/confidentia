# ADR 0021 — Azure as the cloud target, and what stays simulated

- **Status:** Accepted
- **Date:** 2026-08-13
- **Stage:** 15–16 (infrastructure and cloud architecture)

## Context

The project's thesis is that a data platform can run under a hardware
confidentiality boundary, and its largest caveat is that no such hardware was
available. Stages 15–16 write the deployment that would remove the caveat.

## Decision 1 — Azure, because of secure key release

The choice is not about the compute. AWS Nitro Enclaves and GCP Confidential VMs
both offer AMD SEV-SNP, and the pipeline would run on any of them.

It is about **what the key manager will do with an attestation**. Azure Managed HSM
supports *secure key release*: a key marked exportable carries a release policy, and
the HSM will hand it to a workload only if an attestation service says the
workload's claims satisfy that policy. That is `crypto/broker.py`, implemented by a
service instead of by the application asking for the key.

AWS KMS can condition a decrypt on a Nitro attestation document through a key-policy
condition, which is close. GCP has no equivalent at the key-manager level today.
Azure's is the one where the platform's existing shape — measurement, policy,
refusal, audit — maps onto a product feature rather than onto glue code.

The secondary reason is Intel SGX: Azure DCsv3 offers it, which is the other backend
in `TeeBackend`. Nothing in this deployment uses it, but the abstraction was written
for two backends and Azure is where both exist.

## Decision 2 — A confidential VM, not confidential containers

The Terraform builds a DCasv5 VM. This is the weaker of the two options and the
choice is deliberate.

Confidential Containers on ACI or AKS would let the release policy pin the
*workload* through `x-ms-sevsnpvm-hostdata` — the hash of a container group security
policy. That is what "attestation-gated key release" means in full, and it is what
the platform claims.

A plain confidential VM can only prove the hardware and the platform image. Any
Azure CVM meeting the same claims obtains the key.

It is the starting point because the pipeline is a Spark job driven by a CLI, not a
long-lived service, and confidential containers impose a container-image and policy
generation workflow that would need Stage 13's images rebuilt against a different
runtime. **The gap is real and stated in `docs/architecture/azure.md` rather than
glossed** — it is the difference between "the hardware is genuine" and "the code is
what we think it is".

## Decision 3 — Validate, never apply, and label it

`terraform plan` requires credentials and has never run. `apply` has never run.
What runs in CI is `fmt`, `init -backend=false`, `validate`, and Checkov.

Every resource carries `provenance = "never-applied"` as a tag. The same rule as
`TeeSettings.measurement_label`: an unverified thing is labelled where the label
travels with it, not in a footnote.

The temptation was to write a `plan` against a free-tier subscription to produce
output for the report. That would be a different claim — "this plans against an
empty subscription" is not "this deploys" — and the honest version is to say plan
has not run.

## Decision 4 — A policy scan with documented exceptions, not a clean score

Checkov reports 36 passed, 0 failed, **8 skipped with written reasons** in
`.checkov.yml`. Three findings were fixed because the checks were right
(`allow_nested_items_to_be_public`, `local_user_enabled`, an NSG on the delegated
data subnet).

The rest are skipped, and each reason is either "the check is looking for a
different topology" — private endpoint checks cannot see that
`public_network_access_enabled = false` already removes the public endpoint — or "a
real gap, and here is why it is open", which is the customer-managed key waiting on
HSM activation.

One is worth naming because deleting it would be the obvious way to get a clean
score: `CKV_AZURE_50`, *"Ensure Virtual Machine Extensions are not Installed"*. The
extension is Microsoft's GuestAttestation agent — the component that turns an
SEV-SNP report into an MAA token. Removing it would satisfy the scanner and delete
the feature the deployment exists for.

This is the same policy as the Trivy gate (ADR 0020) and the alert rules (ADR 0019):
act on what can be acted on, write down the rest, never suppress for quiet.

## Decision 5 — HSM activation stays manual

A Managed HSM holds no keys until it is activated with a security domain, requiring
a quorum of RSA key pairs held by people. Terraform stops before it.

This is the moment an organisation takes custody of the root of trust. Automating it
would mean a machine holding the quorum, which defeats the reason the ceremony
exists. The tenant keys are created afterwards with `az keyvault key create
--policy @skr-policy.json`, and `--exportable` is only safe because the policy is
attached at creation and cannot be loosened without recreating the key.

## Consequences

- The dissertation can state precisely what would change under real hardware, and
  what would still not be proven. Both halves are in
  `docs/architecture/azure.md`.
- Moving to hardware is configuration plus one Java class — a Managed-HSM-backed
  `KmsClient`. Nothing in `pipelines/`, `quality/`, `evaluation/` or `lineage/`
  changes, which is the payoff for treating the boundary as a design parameter from
  Stage 1.
- **The landing-zone plaintext is not fixed by any of this.** It moves into a
  storage account, bounded by a one-day lifecycle rule. It should be closed before
  real data is deployed.
