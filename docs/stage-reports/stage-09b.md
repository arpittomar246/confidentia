# Stage 9b — C3: cryptographically verifiable lineage

**Date:** 2026-08-12

`common/hashing.py` has had canonical JSON, Merkle roots and chain verification
since Stage 1, and until now nothing called them. This stage is the contribution
those primitives were put there for.

## The claim

> Given a Gold row, prove which code, which keys and which input records produced
> it — and detect any tampering with that record of provenance.

The second half is the load-bearing one. A lineage **table** records what happened;
a lineage **chain** makes altering that record detectable. Everything below is
built around not blurring those two.

## What was built

| Component | Purpose |
|---|---|
| `lineage/merkle.py` | Trees over record digests, with O(log n) inclusion proofs |
| `lineage/chain.py` | What an entry says, how it is linked and signed. Pure Python |
| `lineage/recorder.py` | Digest a table, bind it to code and keys, append under a lock |
| `lineage/verify.py` | Recompute the chain, the columns, and — on demand — the data |
| `store/sql/010_lineage.sql` | `platform.lineage_entry`, plus `lineage_latest` / `lineage_head` |
| `cli.py` | `lineage record \| verify \| show \| prove` |
| `scripts/verify_lineage.sh` | Breaks four things and requires the verifier to notice |
| `tests/unit/test_lineage.py` | 50 tests, no Spark and no database |

Wired into `confidentia_bronze_silver` and `confidentia_gold` so provenance is
recorded immediately after each write.

**31 entries** now chain 2 tenants across bronze, silver and gold.

## What the chain records

Each entry commits to a Merkle root over **every record** in the table, bound to:

- the code measurement in force (the same value that gates key release),
- the key **IDs** released to read it — never key material,
- the Delta table version,
- the run id,
- and the **entry digests** of the upstream datasets.

That last one is what makes it a graph rather than a list. A Gold mart cites every
current Silver entry digest; a Silver entry cites its Bronze entry digest; a Bronze
entry cites the landing batch digests the ingest registry content-addressed at
Stage 3. Naming the upstream *dataset* would give a lineage graph. Naming the
upstream *commitment* gives one whose edges cannot be silently redirected.

## Proving one row

```
$ confidentia lineage prove healthcare encounter ENC-000000000000 --layer bronze

PROVEN: row is in the set committed by entry #3 (120,800 records, 17 sibling digests)
    signed root    dae86b0dd4350c6aa741974f520539e34476cf148f60f36edcb7c917b2d8589c
    recomputed     dae86b0dd4350c6aa741974f520539e34476cf148f60f36edcb7c917b2d8589c
    [ok ] leaf 9145ac23ac66cbaa.. at index 68662 via 17 siblings
    [ok ] leaf c4909d4d1fd1ba3e.. at index 92783 via 17 siblings
```

Seventeen digests instead of 120,800 records. **Two** matches, which is correct and
worth noticing: this encounter was delivered twice — once by the file collector and
once by the REST collector — and Bronze holds both. The proof reports what is
actually in the table rather than what the business key suggests should be.

**Leaves are never stored.** The tree is rebuilt from the table every time a proof
is needed, so a proof can only be produced if the data still hashes to the signed
root. Proof generation *is* the tamper check rather than something layered on top.

## The four tamper claims

`verify_lineage.sh` breaks something real and requires the verifier to notice, and
to notice for the right reason — a verifier that reports "broken link" when the
problem is a forged signature sends an operator to the wrong place.

| # | Attack | Detected as |
|---|---|---|
| 1 | Edit a signed payload in Postgres | `entry #2: payload altered — recomputed b9de4134895f…, stored 8ebbec190a9d…` |
| 2 | Edit a denormalised column | `entry #2: column diverges from signed payload — merkle_root` |
| 3 | Delete a mid-chain entry | `entry #3: broken link — stored previous 8ebbec190a9d…, expected 9215055334e8…` |
| 4 | Edit one row in the encrypted Bronze table | `DOES NOT MATCH entry #31 (signed f0fb3d7e15f0…, recomputed 8c2aefc41317…, row count unchanged)` |

Claim 2 is the one that is easy to leave out. Only `payload` is covered by the
signature, so the columns beside it — including `merkle_root` — are the one place an
edit is invisible to the chain, and an operator reading that column would be reading
unprotected data. Verification compares the columns against the payload for exactly
this reason.

Claim 4 edits an actual encrypted Delta table (`healthcare/provider`, one row, the
plaintext `specialty` column — the column an attacker with filesystem access but no
keys could actually change), then undoes it with Delta time travel. `row count
unchanged` in that output is the point: the table is the same size and one field
differs, and the root still moves.

## Orchestration

`confidentia_bronze_silver` and `confidentia_gold` both run end to end with the new
tasks, and the gate still stops Silver behind an unreachable threshold. Two notes
from running `verify_dags.sh`:

- **Bronze lineage runs before the gate, not after.** The commitment must cover what
  the source actually sent — including a batch the gate is about to reject, which is
  exactly the provenance someone asks for afterwards.
- The script now starts the mock source API itself. `confidentia_ingest` failed
  during this stage's verification with five connection-refused retries, which reads
  as a broken DAG; it was a missing precondition, and the DAG passes with the API up.
  Fixed in the script rather than in a note nobody reads.

DAG verification is materially slower now, because each lineage task starts a Spark
session. That is the cost of closing the write-to-commitment window in the
orchestrator rather than inside the writers.

## What it costs

Per-record digesting, measured across the whole lake: **~37 µs per record, plus a
~1.1 s fixed cost per table** (a Spark job launch, which is why per-record figures
for small tables are meaningless).

| Layer | Pipeline run | Provenance | Overhead |
|---|---|---|---|
| Silver `claim_line` (141,499 rows) | 21.2 s | 6.9 s | **32%** |
| Silver `encounter` (59,120 rows) | 17.2 s | 3.5 s | **20%** |
| Gold healthcare (7 marts, 207,702 rows) | 47.4 s | 12.3 s | **26%** |
| Bronze `claim_line` (145,136 rows) | 3.2 s | 6.5 s | **201%** |

The Bronze row is not buried. A Bronze write is a shaped Parquet append, so
digesting the result costs twice what producing it did. Provenance at this
granularity is a real and visible cost.

### Why per-record anyway

The three granularities, measured on the same table (145,136 records, 2 files,
7.1 MB):

| Granularity | Cost | Can prove one row? |
|---|---|---|
| Per run — one digest over the directory | 27 ms | no |
| Per file — Merkle over file digests | 8 ms | no |
| Per record — Merkle over row digests | 7,330 ms | **yes** |

Per-record costs about **900×** per-file and is the only one of the three that
answers the claim. A per-file root proves a *file* was committed to, and answering
"was this row in it" from that means handing over the file — 120,800 other people's
records. ADR 0015 records this as a measured decision, including where it stops
being reasonable: at 37 µs a record, a 10-million-row table is six minutes of
digesting, and beyond roughly that scale the answer changes.

## What this does not establish

Sharper here than anywhere else in the project, and stated in the code, the schema,
the CLI output and the ADR rather than only here.

**Under `sim` the platform measures itself and holds its own signing key**, in a
file it can read. The chain detects an edit to a stored entry, a deleted entry, a
rewritten tail, a swapped signing key, and an edit to the data that never touched
the ledger. It **cannot detect a dishonest platform**: one that signs a false record
at the moment of writing has produced a perfectly valid chain. Every entry carries
`hardware_rooted = false` and `lineage record` prints that on every run. What would
change the answer is the signing key living in an enclave, which is hardware this
project does not have.

Three narrower limits, each found while building rather than assumed:

- **Failures are local, not a cascade.** A tampered entry produces a failure at
  itself and at the link immediately after it — not at every entry to the end of the
  chain, which is how hash chains are usually described. This surfaced when a unit
  test asserting the cascade failed; the test and two docstrings were corrected to
  the true claim rather than the appealing one. Localised detection is more useful
  anyway: it points at where the tampering happened.
- **Truncation at the tip is undetectable.** Deleting the newest entries leaves a
  shorter chain that verifies perfectly. Closing that needs the head digest
  published somewhere the platform cannot rewrite — an external anchor, out of scope
  here. `verify_lineage.sh` prints this rather than quietly omitting it.
- **Tampering localises to a table, not a row.** One edited row invalidates the
  proof for *every* row in that table, because the root commits to the whole set.
  That is what a Merkle commitment does; it is not a per-row blame mechanism.

## Design decisions

**The chain lives in PostgreSQL, not the lake.** Appending a link requires reading
the tip and being the only writer to extend it — a transaction. A chain in a Delta
table with concurrent writers forks under exactly the conditions it exists to
survive. `previous_digest` is `UNIQUE`, so the database refuses a fork structurally
rather than relying on the advisory lock alone.

**`run_id` is `ON DELETE SET NULL`, not `CASCADE`.** Every other child of
`platform.run` cascades. Here that would make `DELETE FROM platform.run` a way to
remove chain entries without breaking anything — a tamper path through referential
integrity.

**Leaves are computed by a pandas UDF calling the same `leaf_digest` the driver
uses**, not by `sha2(to_json(...))` in Spark SQL. Two canonicalisations of the same
record is the one failure this design cannot tolerate, and Spark's `to_json` omits
null fields — so a column nulled out by an attacker would hash identically to one
that was never there. There is a unit test pinning exactly that.

**The signature covers the entry digest, not the payload.** So it covers the chain
position too: an entry lifted from one point and replayed at another has a different
predecessor, so a different digest, so a signature that no longer verifies.

**Recording is a step after the write, not inside the writers.** Rewriting four
working writers to emit provenance inline was ruled out; the cost is a window in
which a table has changed and no entry says so, and what closes it is the
orchestrator running `lineage record` as the next task. The DAG edges are the
mechanism, not decoration.

## Reproducing

```bash
export CONFIDENTIA_KEYSTORE_PASSPHRASE="dev-passphrase-not-for-real-use"
python -m confidentia.cli db migrate

bash scripts/record_all_lineage.sh
python -m confidentia.cli lineage show --limit 10
python -m confidentia.cli lineage verify --deep
python -m confidentia.cli lineage prove healthcare encounter ENC-000000000000

bash scripts/verify_lineage.sh
python scripts/lineage_granularity_bench.py healthcare claim_line bronze
```
