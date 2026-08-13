# ADR 0023 — `patient_change` is not a lake dataset

- **Status:** Accepted
- **Date:** 2026-08-13
- **Follows:** [ADR 0022](0022-landing-zone-encryption.md)

## Context

Closing the landing zone's plaintext hole (ADR 0022) turned up
`healthcare/patient_change`: 900 rows of patient address changes, landed once by the
file collector on 9 August, belonging to **no schema contract at all**.

It had never been promoted, because Bronze refuses a dataset it cannot type. Never
quality-checked or classified, for the same reason. And never encrypted — not
because anything decided it should not be, but because nothing knew it was there.
The migration encrypted every column of it under the tenant's PHI key, which
protected it without classifying it, and ADR 0022 left the question open
deliberately: *what is this, and should it exist?*

## Decision — delete it, and record that it was deleted

**`patient_change` is source-side tooling, not lake data.** The generator writes it
to `raw/healthcare/patient_change/`; `oltp_loader.replay_mutations()` reads it from
there and applies each row as an `UPDATE` against the OLTP database, so the CDC
collector observes the resulting WAL records. Its effects reach the lake as
**`patient_cdc`** — which is contracted, encrypted, promoted to Bronze and tokenised
in Silver.

Giving it a contract was the alternative and is worse. A contract is a commitment:
to a Bronze table, to quality rules, to a Silver conformance path, to evaluation
ground truth. It would create a second route into the lake for data that is already
there by a better one, and the two would then have to be kept consistent forever.

Nothing is lost. `raw/healthcare/patient_change/` remains the source of truth for
the replay, and the generator is deterministic — `confidentia generate healthcare
--seed 20260808` reproduces it byte for byte, which CI already proves.

### The ledger keeps the record

Deleting the files while leaving `platform.ingest_batch` reporting `ingested` would
make the registry lie about what is on disk. Deleting the row instead would erase
the fact that this arrived at all — the one question the landing zone exists to
answer.

So the row stays and gains `withdrawn_at`, `withdrawn_by` and `withdrawn_reason`.
*What did the source send us on Tuesday* is still answerable: **this, and it was
withdrawn on this date, for this reason.**

`confidentia landing withdrawn` prints the trail.

## How it got there, and the rule that stops it recurring

The file collector offered any directory under `raw/<tenant>/` that had a manifest.
Everything the generator writes has a manifest, including things that were never
meant to leave the source side, so a directory-shaped rule swept one up.

> **The catalog decides what the lake ingests, not the shape of a directory tree.**

`available_datasets` now returns manifested **and** contracted datasets, logging what
it skipped and why.

**This also fixes a bug ADR 0022 introduced.** That ADR made an uncontracted landing
write fail closed, which is right when someone asks for a dataset by name and wrong
during a sweep: `confidentia ingest file healthcare` would have aborted the entire
tenant's ingest on `patient_change`, taking `rebuild_pipeline.sh` with it. Filtering
at discovery keeps the sweep working while a *targeted* `ingest file healthcare
patient_change` still fails loudly — which remains the correct answer to someone
asking for it specifically.

That bug is worth recording rather than quietly fixing: a guard that fails closed is
only safe if every caller can survive the refusal, and this one could not.

## Why the withdrawal is a command rather than an `rm -rf`

Uncontracted data reaching a landing zone is a recurring class of mistake, not a
one-off, and an audited repeatable withdrawal is worth more than a deletion recorded
only in a terminal scrollback.

`confidentia landing withdraw` therefore:

- **refuses a contracted dataset outright.** Removing one means re-ingesting or
  losing replay; one command should not be able to do that job and this one.
- **defaults to a dry run**, printing the files, rows and batches it would remove.
- **requires `--reason` with `--yes`.** A withdrawal with no recorded justification
  is indistinguishable from data loss.
- **deletes files before marking the ledger.** If the second step fails, the ledger
  under-reports a withdrawal and re-running fixes it. The other order can leave rows
  marked withdrawn beside data that is still on disk, and a registry that
  under-reports what *exists* is the worse failure for a platform whose argument is
  that its checks match its bytes.

## Consequences

The landing zone is 23 files, all `PARE`, all belonging to datasets the catalog
declares. `confidentia plan placement` reports no boundary violations, and it
reports that by looking rather than by asserting.

The limitation this closes was the last one carried over from the C5 finding. What
remains in its place is smaller and honest: the plaintext blocks freed by both the
re-encryption and this deletion are unlinked, not shredded, and on a lake holding
real data the answer to that is full-disk encryption rather than trusting an unlink.
