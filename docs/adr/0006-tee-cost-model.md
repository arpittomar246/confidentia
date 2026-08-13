# ADR 0006 — The TEE cost model, and what it is allowed to claim

- **Status:** Accepted
- **Date:** 2026-08-13
- **Stage:** written at C5; referenced by `config/settings.py` since Stage 1

## Context

`TeeSettings` has carried four parameters — `sim_ecall_latency_us`,
`sim_ocall_latency_us`, `sim_page_fault_us`, `sim_cold_start_ms` — with a comment
saying they are "inputs to a model, not measurements — see docs/adr/0006". That ADR
did not exist. This is it, written when the model that consumes them was built.

There is no confidential-computing hardware on this project. Everything the model
produces is therefore a projection, and the only question that matters is what a
projection is allowed to claim.

## Decision 1 — Four terms, each corresponding to a real cost

| Term | What it charges | Parameter |
|---|---|---|
| Cold start | Creating and attesting an enclave | `sim_cold_start_ms` = 120 ms |
| Transitions | `ecall`/`ocall` per batch crossing the boundary | 8 µs / 12 µs |
| Paging | Working set beyond the EPC, evicted and re-read | `sim_page_fault_us` = 50 µs |
| Compute | Memory encryption slowing ordinary work | `DEFAULT_COMPUTE_FACTOR` = 1.20 |

The parameters are the order of magnitude reported for SGX in published
microbenchmarks. They are **not** calibrated against this machine, because this
machine has no SGX, and calibrating them against a simulation would be calibrating
against the assumption being tested.

Two constants live in `model.py` rather than in settings because they describe the
model, not the hardware: `DEFAULT_EPC_UTILISATION` (0.75, because the enclave's own
code, stack and runtime occupy EPC alongside application data) and
`DEFAULT_COMPUTE_FACTOR`.

## Decision 2 — Transitions are charged per batch, not per row

A per-row crossing would be an absurd implementation, and modelling it would produce
an enormous number that no reader would believe. Spark's vectorised reader moves
10,000 rows at a time, and that is the default here.

This is a modelling choice with a real consequence: it makes the transition term
small — a few hundred milliseconds across the whole pipeline — and therefore makes
**paging the term that matters**. If the batch size were 100, the conclusion would
change. The parameter is exposed so that claim can be checked rather than assumed.

## Decision 3 — Every result carries its provenance in the type

`Modelled` and `StageCost` both expose a `label`, and `StageCost.as_dict()` returns
`"label": "MODELLED"` **even when the baseline duration was measured**, because a
measured number plus a modelled number is a modelled number. A bare `float` loses
that the moment it is put in a table or a JSON payload, and the whole failure mode
this project guards against is a projection being read as a result.

Under a real backend the same structures come back with `is_measured = True` and the
label `MEASURED`, so no call site branches on the backend.

## Decision 4 — Row width is measured, and measured uncompressed

The EPC budget has to be divided by a bytes-per-row figure, and which figure is not
a detail. On-disk Parquet here compresses between 1.0× and 2.4×, so using the
compressed size would understate the working set by up to a factor of two — enough
to turn a partition that pages into one the model says fits.

Width is read from **Parquet footer metadata** (`total_uncompressed_size` summed
across column chunks) in the landing zone, which needs no Spark, no JVM and no key
release, because landing footers are unencrypted. It is the decompressed *Parquet*
size, not the JVM heap footprint of the same rows, and that gap is stated wherever
the number is used rather than papered over with a guessed expansion factor.

That this measurement is possible at all is a consequence of the boundary violation
ADR 0017 reports: if landing were encrypted, widths would need a Spark job and a key
release.

## What this model must never be used for

- **A performance claim.** "The pipeline runs at 1.21× inside an enclave" is a
  model output. The honest form names the backend and the label, which is why
  `confidentia plan` prints the banner before any number.
- **A hardware comparison.** Nothing here distinguishes SGX from SEV-SNP beyond the
  EPC size, and SEV-SNP's cost structure is genuinely different — no EPC paging
  cliff, different transition costs.
- **Sizing a purchase.** The row-count threshold it produces (≈868k rows at 116
  B/row for a 128 MB EPC) is arithmetic on measured widths and would hold; the
  *time* attached to crossing it would not.

## Consequences

- The four settings finally have the document they cite.
- Adding a real backend means replacing the parameters and flipping `is_measured`;
  no consumer changes, because none of them branch on it today.
- The model's own inputs are printed by `confidentia plan show` next to its outputs,
  so a result cannot be quoted without the assumptions that produced it being one
  command away.
