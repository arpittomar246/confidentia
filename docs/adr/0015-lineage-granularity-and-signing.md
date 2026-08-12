# ADR 0015 — Lineage granularity, and what signs the chain

- **Status:** Accepted
- **Date:** 2026-08-12
- **Stage:** 9b (C3 — verifiable lineage) — decided against measurements, not ahead of them

## Context

C3 claims: *given a Gold row, prove which code, which keys and which input records
produced it, and detect any tampering with that record of provenance.*

Three questions had to be settled before writing a recorder, and one of them is the
difference between a contribution and a hash column.

## 1. Granularity: per run, per file, or per record?

The project brief said to measure this rather than assume it, on the grounds that
"we measured the cost and chose per-file" is a result while "we chose per-file" is
an assertion. So it was measured, on `healthcare/claim_line@bronze` — 145,136
records, 2 Parquet files, 7.1 MB:

| Granularity | What it commits to | Cost |
|---|---|---|
| Per run | one SHA-256 over the directory | **27 ms** |
| Per file | Merkle root over file digests | **8 ms** |
| Per record | Merkle root over row digests | **7,330 ms** |

Per-record costs roughly **900×** per-file. It is also the only one of the three
that can answer the claim: a per-file root proves a *file* was committed to, and
answering "was this row in it" from that means handing over the file, which is
120,800 other people's records.

### Decision: per record.

Measured across the whole lake, per-record digesting costs **~37 µs per record with
a ~1.1 s fixed cost per table** (the Spark job launch). As a share of the run that
produced the data:

| Layer | Pipeline run | Provenance | Overhead |
|---|---|---|---|
| Silver (`claim_line`, 141,499 rows) | 21.2 s | 6.9 s | **32%** |
| Silver (`encounter`, 59,120 rows) | 17.2 s | 3.5 s | **20%** |
| Gold (healthcare, 7 marts, 207,702 rows) | 47.4 s | 12.3 s | **26%** |
| Bronze (`claim_line`, 145,136 rows) | 3.2 s | 6.5 s | **201%** |

The Bronze row is the honest one and it is not buried. A Bronze write is a shaped
Parquet append, so digesting the result costs twice what producing it did.
Provenance at this granularity is not a rounding error; it is a real and visible
cost, accepted because the alternative does not support the claim.

**Where this stops being reasonable:** at 37 µs a record, a 10-million-row table is
six minutes of digesting. Beyond roughly that scale the answer changes to per-file
roots with per-record roots only on the datasets that need row-level proof. The
crossover is stated here so that a future reader does not have to rediscover it.

## 2. What goes in the leaf, and in the entry

**Leaf:** the whole record, canonicalised as JSON and hashed. Including the ingest
envelope, so the commitment is to the row *as stored* rather than to an idealised
version of it.

Leaves are computed by a pandas UDF calling the same `leaf_digest` the driver uses,
not by `sha2(to_json(...))` in Spark SQL. Two canonicalisations of the same record
is the failure this design cannot tolerate — and Spark's `to_json` omits null
fields, so a column nulled out by an attacker would hash identically to one that was
never there.

**Entry:** the Merkle root, the code measurement, the released key **IDs**, the TEE
backend, `hardware_rooted`, the Delta table version, the run id, and the entry
digests of the upstream datasets. Key material never — the same rule
`platform.key_release_audit` already follows. A record naming which DEKs were
released is auditable; one carrying them is a keystore with extra steps.

Upstream is cited by **entry digest, not dataset name**. Naming the dataset gives a
lineage graph; naming the upstream commitment gives a graph whose edges cannot be
silently redirected.

**Leaves are not stored.** Storing 145,000 leaf digests beside the root would let
anyone who can edit the data edit the leaves to match. Rebuilding the tree from the
table each time a proof is needed makes proof generation *itself* the tamper check.

## 3. What anchors and signs the chain

**Anchor:** `H(previous_digest || H(payload))`, one global chain, in PostgreSQL.
Not in the lake: appending a link requires reading the tip and being the only writer
to extend it, which is a transaction. A chain in a Delta table with concurrent
writers forks under exactly the conditions it exists to survive. The
`previous_digest` column is `UNIQUE`, so the database refuses a fork structurally
rather than relying on the advisory lock alone.

**Signature:** ECDSA with the existing attestation signing key, over the *entry*
digest rather than the payload — so the signature covers the chain position too,
and an entry lifted from one point and replayed at another no longer verifies.

### What this establishes, and what it does not

Under the `sim` backend the process measures itself and holds its own signing key in
a file it can read. A chain signed that way:

- **detects** an edit to a stored entry, a deleted entry, a rewritten tail, a
  swapped signing key, and an edit to the data that never touched the ledger;
- **cannot detect** a dishonest platform. A platform that signs a false record at
  the moment of writing has produced a perfectly valid chain.

Every entry carries `hardware_rooted = false` and the CLI prints that on every
recording. What would change the answer is the signing key living somewhere the
platform cannot read it — an enclave — and that is hardware this project does not
have. This is the contribution most likely to be overclaimed and the easiest for a
viva panel to probe, so the limitation is in the code, the schema, the CLI output
and the report, not only here.

Two further limits, stated because they are not obvious:

- **Failures are local, not a cascade.** Verification walks the digests as stored,
  so one tampered entry produces a failure at itself and at the link after it —
  not at every entry to the end of the chain, which is what hash chains are usually
  described as doing. Localised detection is more useful, and it is the true claim.
- **Truncation at the tip is undetectable.** Deleting the newest entries leaves a
  shorter chain that verifies perfectly. Closing that needs the head digest
  published somewhere the platform cannot rewrite, which is an external anchor and
  therefore out of scope here.

## Consequences

- Recording is a **separate step after a write**, not inline in the four writers.
  This leaves a window in which a table has changed and no entry says so; the
  orchestrator closes it by running `lineage record` as the task immediately after
  each write. The alternative was rewriting four working writers, which the project
  brief rules out and which would have put provenance in the path of every write.
- `lineage` joins the measured packages, so editing it revokes key release until
  `keys approve` runs — correctly, since it now reads plaintext under released keys.
- A tampered row invalidates the proof for **every** row in that table, not just
  itself. The root is a commitment to the whole set. This is the expected behaviour
  of a Merkle commitment and it means the chain localises tampering to a *table*,
  not to a row.
