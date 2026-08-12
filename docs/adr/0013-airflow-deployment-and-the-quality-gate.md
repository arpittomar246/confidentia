# ADR 0013 — How Airflow is deployed, and what the quality gate is allowed to do

- **Status:** Accepted
- **Date:** 2026-08-10
- **Stage:** 8 (Orchestration)

## Decision 1 — Airflow lives in its own virtualenv

Airflow is installed into `~/airflow-venv`, not into `~/confidentia/.venv`.

Airflow pins its transitive dependencies hard and ships a constraints file because
resolving it any other way produces combinations upstream has never tested.
Resolving it *into* the project venv would be free to move `pandas`, `numpy` or
`pyarrow` underneath a pipeline that had just been verified end to end. This
project has already lost three CI runs to environment drift, and the Stage 7c
measurements are only reproducible if the environment that produced them stays put.

The separation costs nothing at runtime because **the DAGs shell out to the project
CLI rather than importing it**. Every stage already has a tested entry point;
orchestration's job is to decide *when* things run and what happens when one fails,
not to know how Bronze is written. A DAG that imported `confidentia` would also be
a DAG that could drift from it.

The consequence to accept: a DAG cannot pass a Python object to a task, only
strings. That has not been a constraint in practice, and if a DAG ever needs to
know something about the data, that is a signal the knowledge belongs in the CLI.

## Decision 2 — LocalExecutor on Postgres, in a separate database

`docker-compose.yml` had already committed to `LocalExecutor` and a database named
`airflow` before this stage began. The local WSL setup mirrors it rather than
inventing a second design, and both are configured by the same `AIRFLOW__*`
environment variables so they cannot drift.

Two things this rules out:

- **SQLite.** Airflow's default silently forces `SequentialExecutor`, so
  `LocalExecutor` would appear in the configuration while never being used. The
  install script deletes any stray `airflow.db` for that reason.
- **Airflow's tables in the `confidentia` database.** Migrations there are tracked
  by checksum in `platform.schema_migration`, and a second tool creating thirty
  tables in the same database makes it materially harder to say what the platform's
  schema actually is.

**Airflow 2.11.2, not 3.x.** Airflow 3 splits into an api-server, scheduler,
dag-processor and triggerer — roughly 1.2 GB, which overruns the 1280 MB the
compose file already declares for this service, on a box with 3.9 GB total and a
2 GB Spark driver to accommodate. Two processes fit; four do not. The cost is that
2.x is on an end-of-life track, and that is the right trade for a project whose
measurements have to be reproducible on this hardware.

## Decision 3 — Everything runs one task at a time

`parallelism`, `max_active_tasks_per_dag` and `max_active_runs_per_dag` are all 1.

This is a correctness constraint, not tuning. A Spark task takes a 2 GB driver, and
two concurrent ones on a 3.9 GB box would swap or be OOM-killed mid-write. For an
encrypted Delta table that means a partial commit to diagnose rather than a clean
failure — the worst possible outcome, because the table still reads.

## Decision 4 — The gate fails; it does not skip

`platform.dq_score.promotion_blocked` has been computed since Stage 7a and consumed
by nothing. A score nobody acts on is a dashboard, not a control.

The gate is a `BashOperator` running `confidentia quality gate`, which **exits
non-zero** when quality is below threshold. The obvious alternative,
`ShortCircuitOperator`, is wrong in a way that is hard to see: a short-circuited
task is *green*, and its downstream tasks are marked skipped rather than failed. A
gate built that way would let a DAG look healthy while Silver was built from a
Bronze layer that had just been judged unfit.

`scripts/verify_gate.sh` asserts the exit statuses, and `scripts/verify_dags.sh`
asserts that an unreachable threshold fails the whole DAG run and that Silver does
not execute behind it. Those assertions exist because every property here would
survive a gate that printed `BLOCKED` and returned 0.

Three sub-decisions follow:

**A missing score blocks.** The natural reading of "no score" is "nothing wrong
yet"; it actually means the quality run never happened, so nothing has been
checked. A gate that opens when its input is absent protects nothing at exactly the
moment the pipeline is already broken.

**The gate re-decides against the current threshold** rather than trusting the
`promotion_blocked` flag stored at scoring time. The score is a measurement, the
threshold is policy, and baking the second into the first means raising the
threshold has no effect until everything is re-scored. Each decision stores the
threshold it used, so historical decisions remain interpretable after the policy
moves.

**Overrides are recorded, never silent.** `--override` requires `--reason` and
writes `overridden = true` to `platform.promotion_gate`. Operators genuinely do
need to push data through a known-bad gate; the alternative to supporting it
explicitly is that people learn to comment the gate out, and then nobody can count
how often it happens.

Passing decisions are recorded too, for the same reason
`platform.key_release_audit` records grants as well as denials: "was this gate even
running when that batch went through" is the question asked afterwards, and a table
of refusals cannot answer it.

## Decision 5 — `keys approve` stays a manual release step

Changing any measured package revokes key release, so the first pipeline run after
a deploy fails. That tax is the feature — it is what makes "which code touched this
data" enforceable rather than merely recorded — and automating it inside a DAG
would mean the orchestrator approving whatever code it happened to find, which is
not attestation at all.

It failed *badly*, though: sixty seconds into starting a Spark session, once per
task, in a traceback. So the DAG opens with a `preflight_attestation` task running
`confidentia keys check`, which asks the same question in about two seconds and
prints the one command that fixes it. This weakens nothing; it moves the answer
earlier and makes it legible.

## Decision 6 — Data-aware scheduling between stages, not cron offsets

Stages are chained with Airflow Datasets: `ingest` produces `landing`,
`bronze_silver` consumes it and produces `bronze` and `silver`, and so on. A
medallion architecture *is* a chain of datasets, so "Silver is stale until Bronze is
rewritten" is the dependency that exists; a cron offset chosen to be safely after
the previous job is a guess about runtime, and it is the guess that produces 03:00
jobs reading half-written tables.

Restatement is the exception and is a **sensor**, because it is driven by a
condition in the data — a chargeback arriving 1 to 45 days late — rather than by an
upstream task completing. `gold dirty` exits 0 when there is work, which inverts
the usual convention deliberately: a sensor succeeds when its condition is met.

## Decision 7 — The mock source API is not started by a DAG

The REST collector reads from `source_api.py`, which represents an external system.
A DAG that started its own upstream would be modelling something false — you do not
boot your vendor's API before reading from it — so the task fails if nothing is
listening, which is the correct behaviour for a source that is down.

## Consequences

- The scheduler must be started with `CONFIDENTIA_KEYSTORE_PASSPHRASE` in its
  environment; `BashOperator` inherits it. There is deliberately no default,
  consistent with the rest of the platform.
- `orchestration/dags/` is on `PYTHONPATH` for DAG parsing, so `confidentia_common`
  imports as a top-level module. That is Airflow's convention, not an accident.
- The ordering that Bronze quality must be the last quality run before `evaluate`
  is now a dependency edge in `confidentia_quality` rather than tribal knowledge.
