# ADR 0022 — Encrypting the landing zone

- **Status:** Accepted
- **Date:** 2026-08-13
- **Supersedes:** the open finding in [ADR 0017](0017-enclave-placement.md)

## Context

ADR 0017 built a placement planner and used it to derive where the confidentiality
boundary falls. Applying its rule exposed a hole the rest of the platform had
carefully worked around without ever naming:

> The landing zone is outside the boundary and holds plaintext.

Confirmed by inspection rather than inferred. Landing files carried the `PAR1`
footer magic while Bronze carried `PARE`, and grepping one landing `patient` file's
raw bytes turned up 19 SSN-shaped and 7 email-shaped strings. Everything downstream
was encrypted per-column under attestation-released keys; the first thing written
to disk was not encrypted at all.

ADR 0017 recorded two remedies and deliberately chose neither, because the choice
changes ingestion's recovery story and is not a decision to make as a side effect
of writing a planner.

## Decision — encrypt landing, keep replay

**Remedy 1: bring the landing zone inside the boundary.** Landing parts are written
with Parquet Modular Encryption under the same per-sensitivity keys Bronze uses,
released by the same attestation-gated broker, mapped to columns by the same schema
contract.

Remedy 2 — delete each batch once Bronze commits — was rejected. It is cheaper, and
it costs the property the landing zone exists for: *what did the source actually
send us on Tuesday*, answerable without re-fetching from a system that may no longer
hold the answer. Deleting the evidence to avoid protecting it solves the wrong
problem, and it would have taken the byte-verifiable replay path with it.

### The obstacle, and why it was not obvious this would work

Bronze is written by Spark, which encrypts through the Java
`PropertiesDrivenCryptoFactory` and this project's `LocalKmsClient`. Ingestion is
pandas: a landing part is one file written by one process, and standing up a Spark
session per batch to encrypt a 60,000-row frame would cost more than the ingest it
protects.

So landing is encrypted by **pyarrow**, and the two engines have to agree on bytes
neither of them was written to share. They do, for two reasons:

1. Both implement the Parquet key-tools **`PKMT1`** key material format with double
   wrapping — a published interchange format, not either engine's private one.
2. `crypto/landing.py` implements the *same wrapping envelope* as
   `crypto/java/LocalKmsClient.java`: AES-256-GCM, `base64(nonce || ciphertext ||
   tag)`, with the master key identifier bound in as additional authenticated data.

The result is the property everything else rests on: **Spark reads what pandas
wrote.** Bronze promotes encrypted landing batches through an unchanged
`spark.read.parquet` call, because the session already carries the crypto factory
and the same derived keys.

This was proved before it was built. A spike wrote a pyarrow-encrypted file and read
it back through the project's own Spark session; only once that returned rows was
any of the rest written.

## What the fix costs

| | |
|---|---|
| Storage | **×1.014** — 21.13 MiB plaintext → 21.42 MiB encrypted, measured over the migration of 24 parts and 369,097 rows |
| Read access | Anything reading landing now needs key material. E1, E8 and `scripts/pick_business_key.py` all had to request keys |
| Write path | One broker round trip per ingest run, not per part |

The read cost is the interesting one, and it is the intended one. Three call sites
could previously read raw source records with no key, no attestation and no audit
entry. That was convenient, and the convenience *was* the vulnerability.

## Columns the contract does not declare

Bronze casts to the contract before writing, so an undeclared column never reaches
its writer. Landing is deliberately pre-contract and does receive them — experiment
E8 showed additive drift arriving unannounced.

Under a plaintext-by-default rule a new `ssn_alt` column would land in the clear and
nothing would say so. So **an undeclared column is encrypted under the strictest key
the contract uses**, and the write logs a warning naming it. Over-encrypting costs a
key lookup; under-encrypting is the entire bug being fixed.

This fired on its first real run: `batch` in `patient_cdc` and `provider` is a column
the collectors add and no contract declares.

## The second hole, found by the migration

Re-encrypting what was already on disk turned up `healthcare/patient_change`: 900
rows of patient address changes, landed once by the file collector on 9 August,
belonging to **no schema contract at all**.

It had never been promoted, because Bronze refuses a dataset it cannot type. It had
never been quality checked or classified, for the same reason. And it had never been
encrypted — not because anything decided it should not be, but because nothing knew
it was there. `patient_change` is a *source-side* mutation script that `oltp_loader`
replays into Postgres for the CDC collector to observe; it was never meant to be a
lake dataset, and a collector pointed at a directory tree landed it anyway.

Two changes follow:

- **Ingestion refuses to land a dataset the catalog does not declare.** The refusal
  now happens before the write, not at promotion, where nothing was listening.
- **The migration encrypts unclassified data rather than skipping it.** Every
  non-envelope column goes under the tenant's PHI key. That protects it; it does not
  classify it, and the CLI says so.

## Consequences

**The violation report now reads the disk.** `boundary_violations()` returned a
hard-coded finding — honest while the hole was open, and a lie the moment it closed.
It now scans for the `PAR1` footer magic, so the answer is re-derived on every run:
silent when the lake is clean, and loud again if a future collector reintroduces a
plaintext write.

**The guarantee is re-checked, not asserted.** `scripts/verify_landing.sh` runs four
probes in separate OS processes — footer magic, raw-byte identifier patterns, a
keyless read, and a Spark read — against the real lake. Parquet's `KeyToolkit` caches
unwrapped keys statically per JVM, and an in-process retry would read from that cache
and falsely pass. That is not hypothetical: it is how the first Bronze verification
reported a pass on unencrypted data.

**CI carries the interop claim.** `pytest -m spark` runs in the `encryption-guarantee`
job, which provisions a JVM. The cross-language wrapping agreement is enforced by
nothing else, and a drift would be invisible until a promotion failed weeks later.

**The plaintext blocks were unlinked, not shredded.** `os.replace` drops the old
inode; the blocks stay on the device until reused, and on a copy-on-write filesystem
or an SSD doing wear levelling they may survive far longer. On a lake holding real
data the answer is full-disk encryption, not trusting an unlink — and claiming
otherwise would be the kind of unearned guarantee this project exists to avoid.

## What this does not fix

The keys protecting the landing zone are released by a **simulated** attestation.
Under the `sim` backend the process measures itself and could lie. What changed here
is that raw source records are no longer readable from disk without presenting *some*
evidence to *some* gate; on real hardware the same code path presents it to a gate
that cannot be lied to. The mechanism is now in the right place. The root of trust
still is not, and remains labelled MODELLED everywhere it appears.
