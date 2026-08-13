# ADR 0024 — A SEV-SNP reader, written but never executed on hardware

- **Status:** Accepted
- **Date:** 2026-08-14
- **Follows:** [ADR 0021](0021-cloud-target.md), and the defect recorded in `docs/deploy.md`

## Context

Preparing the Azure deployment found that `AttestationBackend.produce_measurement()`
raised for every backend that was not `sim`. Selecting `TEE_BACKEND=snp-cvm` flipped
`is_measured` to `True` and every label to MEASURED — those key off the enum alone —
and then failed every attestation and therefore every key release. The
infrastructure would come up and the platform would refuse to run on it.

The judgement recorded at the time was to *not* write the reader, on the grounds that
it can only be tested on the hardware and that shipping an untested hardware path
would be the failure this project is organised against. That judgement was put to the
author, who directed that it be written and marked untested. This ADR records what
was built and — more importantly — how the "untested" part is enforced rather than
promised.

## Decision

`crypto/snp.py` implements the reader. `crypto/attestation.py` uses it for the
`snp-cvm` backend, obtains a Microsoft Azure Attestation token for the report, and
verifies that token before trusting the measurement.

### The marker is a value, not a comment

```python
VALIDATED_ON_HARDWARE = False
```

It is stamped into every piece of evidence as the `snp_implementation_validated`
claim, so it reaches `platform.key_release_audit` and an audit trail written by this
code says on its face that the code producing it has never run on hardware. A unit
test asserts it is `False`, so flipping it is a deliberate act with a diff attached,
and the conditions for flipping it are written next to it.

This follows the discipline the rest of the project uses for MEASURED and MODELLED.
A third state existed in reality — *implemented, unproven* — and had no label, so it
got one.

### What is provisional, precisely

The parts written from specifications rather than from a running system:

| | Risk |
|---|---|
| `SNP_GET_REPORT` ioctl number | Computes to `0xC0205300`, which matches the documented value — the one thing here that could be checked from a laptop, and was |
| `struct snp_guest_request_ioctl` layout | Kernel-version dependent; newer kernels changed a field to a union without changing the size |
| Report field offsets | `LAUNCH_MEASUREMENT` at `0x90`, `REPORT_DATA` at `0x50` |
| Azure HCL envelope in the vTPM | Header size and NV index |
| MAA request shape | Endpoint, api-version, and the `runtimeData` field |

**The dangerous failure is not a crash.** A wrong offset returns 48 bytes that look
exactly like a launch measurement, and no test on a machine without SEV-SNP can tell
the difference.

Two defences, both cheap:

1. An all-zero measurement is rejected. That is what reading the wrong offset of a
   mostly-zero struct produces.
2. **MAA parses the same report independently, and the two answers are compared on
   every attestation.** If MAA's `x-ms-sevsnpvm-launchmeasurement` disagrees with
   what this module parsed, it raises and says which offset to fix. This is the
   check that turns "it looked plausible" into a hard failure, and it is why the MAA
   exchange is not optional.

### What is not provisional

**The failure behaviour, and the verification.** Both were written to be correct
regardless of whether the reader is, and both are tested here:

- Every path that cannot obtain a genuine report raises. Nothing falls back to the
  self-measured value.
- The MAA token is verified against MAA's JWKS with the algorithm pinned to RS256 —
  letting the token name its own algorithm is the confusion attack the API layer
  already documents — and the issuer taken from configuration, not from the token.
- The guest must be `sevsnpvm`, `azure-compliant-uvm`, and not debuggable. A
  debuggable confidential VM's memory is readable by the host, so attestation
  succeeding there would mean nothing.
- **The measurement in the evidence must equal the one MAA signed.** Without this a
  guest could present a genuine token from any confidential VM beside whatever
  measurement it liked, and the broker's allowlist — which is checked against the
  evidence — would approve key release for a measurement nothing attested to. This
  is the sharpest test in `tests/unit/test_snp.py` and the one worth reading first.

### The local signature stays

The ECDSA signature over the evidence is kept. It is no longer the root of trust,
but it binds the nonce and the claims, and removing it would mean rewriting the
verifier's freshness and replay logic for one backend. The MAA token is what a
compromised guest cannot forge; the local signature is what ties it to *this*
challenge.

## Consequences

**`confidentia` can now be deployed to Azure and attempted.** It could not before.
Whether it works is unknown, and the first run will answer it in one of three ways:
a raised `SnpError` naming the source that failed; a measurement disagreement naming
the offset to fix; or a working attestation, at which point `VALIDATED_ON_HARDWARE`
can be flipped and every number the platform produces there becomes usable.

**No number from this path may enter the dissertation until that third outcome.**
The report says so, `docs/deploy.md` says so, and the audit trail records it per
release rather than relying on either being read.

**`crypto` is a measured package, so this changed the measurement.** Every existing
approval is void until `confidentia keys approve` runs. That is the mechanism
working, and it is the third time this project has been reminded of it by being
locked out of its own keys.
