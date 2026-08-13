# ADR 0017 — Where the confidentiality boundary is drawn

- **Status:** Accepted
- **Date:** 2026-08-13
- **Stage:** C5 (enclave placement planner)

## Context

The project's research question is *where in a medallion pipeline should the
confidentiality boundary be drawn*. Stages 4 through 11 built the pipeline and drew
the boundary implicitly, by encrypting Bronze onward and releasing keys against an
attestation. C5 makes the rule explicit and checks what it implies.

## Decision — one rule, applied to the contracts

> A stage must run inside the enclave if and only if it observes the plaintext of a
> column classified PII or PHI.

`PUBLIC` and `INTERNAL` do not oblige placement: those columns are encrypted at
rest and in transit, and seeing one does not disclose a person.

**This is a derivation, not a model.** It is decided by `catalog/contracts.py` and
would give the same answer on real SGX. Keeping it in a different module from the
cost model (ADR 0006) is what lets a plan say "this stage must be inside" without
qualification while every millisecond beside it is labelled MODELLED.

## What the rule implies for this platform

Applied to the eleven stages, **every stage that touches tenant data is inside the
boundary**, and only `quality.gate` and `api.serve` are outside.

That is a flat answer, and the useful part is *why*, which the planner reports per
stage:

- `ingest.collect`, `bronze.write` — read raw source records. Nothing is tokenised
  yet, so all 42 protected columns are visible.
- `silver.tokenise` — performs tokenisation, so it sees the pre-token values by
  definition. **No placement decision can move it out.**
- `gold.star`, `gold.marts`, `quality.run`, `lineage.record` — read Silver or Gold,
  where the 17 direct identifiers are tokenised but **25 quasi-identifiers remain**:
  dates of birth, diagnosis codes, geography.

So tokenisation does shrink the boundary — it removes 17 of 42 columns — and it does
not shrink it enough to move any stage out. That is the finding, and it is more
interesting than a placement that happened to work: **the direct identifiers were
never the binding constraint.**

## Why the quasi-identifiers cannot simply be tokenised too

Tokenising a diagnosis code destroys the aggregation it exists for; tokenising a
date of birth destroys the age banding. The platform's answer to those columns is
not tokenisation but **disclosure control** (C4) — k-anonymity with complementary
suppression, applied when the aggregate is published.

Which reframes the result: what makes Gold's *output* publishable is C4, and what
keeps Gold's *computation* inside the enclave is that C4 has not been applied yet at
that point. Suppression is a property of the published mart, not of the data being
aggregated.

## The boundary violation this exposed

The rule is about stages. Applying it made the gap obvious: **the landing zone is
outside the boundary and holds plaintext.**

Confirmed by inspection, not inferred — landing files carry the `PAR1` footer magic
while Bronze files carry `PARE`, and grepping one landing `patient` file's raw bytes
turns up 19 SSN-shaped and 7 email-shaped strings.

The planner reports it as a violation with two remedies:

1. Bring landing inside the boundary and encrypt it with the same per-sensitivity
   keys. Preserves replay; costs an encrypted write of data that is about to be
   rewritten.
2. Delete each batch as soon as Bronze has durably committed it. Cheaper, and loses
   the ability to replay a batch without re-fetching it from the source.

**Neither is implemented.** This ADR records the finding and the options; choosing
between them changes ingestion's recovery story and is not a decision to make as a
side effect of writing a planner.

## Partition sizing

The second half of C5. One partition's working set must fit the EPC budget, or it
pages — and paging is the only term in the cost model that grows non-linearly.

At the measured row width (116 bytes, row-weighted across 12 datasets) and a 128 MB
EPC at 75% utilisation, a partition holds **868k rows**. This platform's Bronze
layer is 422k rows, so **it already fits and partition sizing buys nothing today**.

The modelled sweep says sizing starts to matter at roughly 5× this volume and saves
about 260 seconds at 100×. At a 32 MB EPC it matters immediately.

Reported as a threshold rather than a recommendation to repartition, because the
honest statement is "you do not need this yet, and here is when you will".

## Consequences

- `confidentia plan placement` re-derives the boundary from the contracts. A
  contract change that alters it shows up there and in a unit test that pins the
  current answer — the test failing is the signal that the boundary moved.
- The landing-zone violation is now a reported defect rather than an unexamined
  assumption. It should be closed before any claim that the platform protects data
  at rest end to end.
- Nothing in the pipeline changed. C5 is an analysis of the architecture, and
  finding that the architecture already places the boundary correctly is a result;
  moving something to make the planner look useful would not be.
