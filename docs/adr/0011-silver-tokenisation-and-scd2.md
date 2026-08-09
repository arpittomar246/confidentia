# ADR 0011 — Silver: tokenisation, deduplication, and SCD2

- **Status:** Accepted
- **Date:** 2026-08-09
- **Stage:** 5 (Silver)

## Decision 1 — Tokenisation is deterministic, and that is a trade

The same SSN must produce the same token in every dataset, or the patient in
`encounter` cannot be joined to the patient in `claim_line` and the warehouse stops
working. Random tokens would be more private and useless.

**The cost, stated rather than hidden.** Deterministic tokenisation is vulnerable to
a dictionary attack whenever the input domain is small. Anyone who can compute the
tokenisation function can enumerate 36,500 plausible dates of birth or 50 state
codes and match by brute force.

Three mitigations, and one of them is a refusal:

1. Tokens are HMAC-SHA256 under a key derived from the MEK, so an attacker without
   the key cannot compute candidates at all.
2. The token key is derived separately from both the column keys and the vault key:
   compromising one does not yield the others.
3. **Low-cardinality fields are refused, not tokenised.** `date_of_birth`, `sex`,
   `state`, `postal_code` and `currency` raise `TokenisationError`. They are
   protected by column encryption instead. A test asserts no shipped contract marks
   any of them for tokenisation.

The field name is part of the HMAC input, so a value appearing in two fields does
not collapse into one token — an email and a phone number that happen to match must
not merge two entities.

## Decision 2 — A UDF, not `sha2(concat(key, value))`

The Spark-native approach puts the key into the query plan as a literal, and the
physical plan is rendered in the Spark UI's SQL tab. That is the same leak class as
ADR 0010's SparkConf finding. A UDF carries the key in the closure sent to
executors, which the UI does not render.

The cost is speed, mitigated with a vectorised pandas UDF rather than row-at-a-time.

**Implementation trap worth recording:** the UDF must be defined at module scope.
PySpark resolves a pandas UDF's signature with `typing.get_type_hints`, which looks
names up in the function's module globals. Because this package uses
`from __future__ import annotations`, the hints are strings, and a locally-imported
`pd` is not resolvable — producing an opaque `UNSUPPORTED_SIGNATURE` error at call
time rather than anything that points at the cause.

## Decision 3 — The vault stores ciphertext, never plaintext

Reversible tokenisation is supported for the narrow cases where an authorised role
must re-identify. The vault stores the original **encrypted** under a dedicated
vault key, with the token bound in as AEAD additional data so a ciphertext cannot be
moved to another token's row undetected.

A vault holding plaintext would merely relocate the PII and would become the most
attractive table in the platform. Every detokenisation is audited — who, what,
how many, why — because it is the one operation that undoes the privacy guarantee.

## Decision 4 — Only critical failures are quarantined

`critical` (missing business key, missing required field) is quarantined.
`major` (negative amount, discharge before admission) stays in Silver **carrying its
failure list**.

Removing `major` records would silently change every aggregate. The quality layer
reports them and the analyst decides. Quarantined records are persisted **redacted**
— sensitive columns dropped before anything reaches Postgres — because quarantine is
the obvious place to accidentally create a plaintext side channel around the
encrypted lakehouse.

A circuit breaker fails the run if the quarantine rate exceeds 10%: a batch where
most records fail has a systemic problem, and publishing the survivors would present
a heavily biased subset as complete.

## Decision 5 — Deterministic survivor selection in deduplication

Survivors are chosen by: most recently ingested, then most complete (fewest nulls),
then lowest row hash.

The third criterion exists solely to make the result reproducible. Without it the
survivor depends on Spark's partitioning, and rerunning the pipeline on identical
input could produce a different Silver table — which would make experiment E3
(incremental vs full reprocessing) meaningless.

## Decision 6 — SCD2 orders by LSN, not timestamp

Two changes committed in the same millisecond have a well-defined order in the
write-ahead log and no well-defined order by timestamp. Sorting by commit time
would occasionally invert them and produce a dimension whose history is subtly
wrong.

Postgres LSNs are `hex/hex` and must be converted to a number first: sorted as
strings, `0/A` comes after `0/10` but happened earlier.

Versions are created only when a **tracked attribute** actually changed, detected by
hashing tracked attributes and comparing against the previous event. An UPDATE that
touched only `updated_at` produces no new version.

Surrogate keys are derived from `(natural_key, version_number)` rather than a
monotonically increasing counter, so they are stable across reruns.

Intervals are half-open `[valid_from, valid_to)`, so a point-in-time predicate never
matches two versions at a boundary. Open-ended versions use a far-future sentinel
rather than NULL, keeping the predicate a plain `BETWEEN` without an `OR IS NULL`
branch that would defeat pushdown.

## Decision 7 — This is a transaction-time dimension, not valid-time

`valid_from` is the WAL commit timestamp: when the platform *learned* of the change,
not when it became true in the business.

Log-based CDC cannot supply the latter. The WAL records when a row was written, and
a source system back-dating a correction looks identical to one making it live.
Answering "which plan was in force when this claim was adjudicated in March" needs
the source to carry an effective date, which this one does not.

Consequence, visible in the data: every version starts at the moment the mutation
replay ran, so a point-in-time query for 2026-01-01 returns nothing. A bi-temporal
dimension is the correct target and is recorded as future work rather than being
silently approximated.

## Bug found: conformed values compared against raw before-images

`changed_attributes` reported `[state, plan_id]` for a change that only touched the
plan. The current value had been through `conform` (trimmed, categoricals
lower-cased) while the before-image came raw from the WAL, so `NY` → `ny` registered
as a change and every conformed column looked modified.

Both sides are now normalised before comparison. After the fix the distribution
matches the generator's design exactly: 304 changes touching 3 attributes
(address + city + state), 549 touching 1 (plan, PCP, phone, surname).

This mattered beyond cosmetics — `changed_attributes` is intended as audit evidence
of what a change actually did, and it was systematically overstating.
