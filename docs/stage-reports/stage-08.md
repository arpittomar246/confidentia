# Stage 8 — Orchestration, and making the quality score mean something

**Date:** 2026-08-10

Five Airflow DAGs, and the change that matters more than any of them:
`platform.dq_score.promotion_blocked` has been computed since Stage 7a and consumed
by nothing. **A score nobody acts on is a dashboard, not a control.** This stage
puts it between Bronze and Silver, where a failing score stops the pipeline.

## What was built

| Component | Purpose |
|---|---|
| `orchestration/dags/confidentia_common.py` | Shared wiring: `cli()`, datasets, DAG defaults |
| `orchestration/dags/confidentia_ingest.py` | File, REST and CDC collectors |
| `orchestration/dags/confidentia_bronze_silver.py` | Bronze → quality → **gate** → Silver |
| `orchestration/dags/confidentia_gold.py` | Star schema, disclosure control, marts |
| `orchestration/dags/confidentia_quality.py` | Bronze scoring → evaluation → Silver scoring |
| `orchestration/dags/confidentia_restatement.py` | Sensor on dirty partitions → rebuild |
| `quality/gate.py` | The promotion decision, as a pure function plus persistence |
| `store/sql/009_promotion_gate.sql` | `platform.promotion_gate` + `promotion_blocked_now` |
| `cli.py` | `quality gate`, `keys check`, `gold dirty` |
| `scripts/install_airflow.sh`, `airflow_env.sh` | Isolated install, shared configuration |
| `scripts/verify_gate.sh`, `verify_dags.sh` | Proof the gate is a control, not a report |
| `scripts/check_dag_cli_contract.py` | CI guard: every command a DAG invokes exists |
| `tests/unit/test_gate.py` | 14 tests on the decision policy |

Rationale for every design choice is in
[ADR 0013](../adr/0013-airflow-deployment-and-the-quality-gate.md).

## The gate

`quality gate` compares the latest score against the current threshold and **exits
non-zero** when it is below. That exit status is the entire mechanism: the
`BashOperator` fails, and Silver does not run.

The obvious alternative is `ShortCircuitOperator`, and it is wrong in a way that is
easy to miss: a short-circuited task is **green**, and its downstream tasks are
marked skipped rather than failed. A gate built that way would let a DAG look
healthy while Silver was built from a Bronze layer just judged unfit. That is why
the exit statuses are asserted rather than assumed:

```
$ bash scripts/verify_gate.sh
  PASS  passing score exits 0 (exit 0)
  PASS  failing score exits non-zero (exit 1)
  PASS  audited override exits 0 (exit 0)
  PASS  override without a reason is refused (exit 2)
```

And end to end, with an unreachable threshold:

```
$ DQ_PROMOTION_THRESHOLD=0.999999 airflow dags test confidentia_bronze_silver ...
  Marking task as FAILED. task_id=gate_financial
  DagRun Finished: state=failed
```

Three sub-decisions carry most of the weight:

**A missing score blocks.** "No score" reads naturally as "nothing wrong yet"; it
actually means the quality run never happened, so nothing has been checked. A gate
that opens when its input is absent protects nothing at exactly the moment the
pipeline is already broken.

**The threshold is policy, not measurement.** The gate re-decides against the
current threshold rather than trusting the `promotion_blocked` flag frozen at
scoring time — otherwise raising the threshold changes nothing until every dataset
is re-scored. Each decision stores the threshold it used, so old decisions stay
interpretable after the policy moves.

**Overrides are loud.** `--override` requires `--reason`, and the row records
`overridden = true`. Operators genuinely need to push data through a known-bad
gate; the alternative to supporting it is that people learn to comment the gate
out, and then nobody can count how often it happens.

A blocked gate explains itself without opening Airflow:

```
healthcare/claim_line @bronze: BLOCKED  score=0.997 threshold=1.000
  score 0.997 < threshold 1.000; 9/36 rules failing, 8,095/145,136 rows.
  worst: format_violation/procedure_code (3,193), null_violation/billed_amount (2,524)
```

The same block appears in `platform.run` as a **failed** run with the reason
attached, so `confidentia runs` shows it. Passing decisions are recorded too, for
the same reason `key_release_audit` records grants as well as denials: "was this
gate even running when that batch went through" is the question asked afterwards,
and a table of refusals cannot answer it.

## Orderings that used to live in someone's head

The most valuable thing an orchestrator adds here is not scheduling — it is that
constraints stop being conventions.

**Bronze quality must be the last quality run before `evaluate`.** The detections
table is overwritten per run, so a Silver run in between makes the evaluation score
the platform's own cleaning against an answer key describing what the source sent.
Stage 7b found this had already happened by accident. It is now a dependency edge
in `confidentia_quality`: bronze scoring → evaluation → silver scoring.

**The CDC slot must exist before the source is mutated.** A slot created afterwards
has no write-ahead log to replay and the change stream comes back empty, with no
error anywhere. It is a task, not a step someone remembers.

**`keys approve` is a deployment step.** Changing a measured package revokes key
release, so the first run after a deploy fails — correctly; that tax is what makes
"which code touched this data" enforceable. It failed *badly* though: sixty seconds
into a Spark session, once per task, in a traceback. Every DAG that touches the lake
now opens with `preflight_attestation` running `keys check`, which asks the same
question in about two seconds and prints the one command that fixes it. This
weakens nothing — it moves the answer earlier and makes it legible.

This happened twice during the stage, for real. Adding `quality/gate.py` changed the
`quality` measurement and the first full DAG run failed with `KeyReleaseDeniedError`
on every Spark task. Later, refactoring `gate.py` to make its policy unit-testable
changed it again — and that time only `bronze_silver` had the preflight, so `gold`
and `quality` failed deep inside Spark exactly as described. That is what put the
preflight on all four.

## Data-aware scheduling

Stages are chained with Airflow Datasets rather than cron offsets:

```
ingest ──▶ landing ──▶ bronze_silver ──▶ bronze ──▶ quality
                              │
                              └──▶ silver ──▶ gold ──▶ gold
restatement: @hourly sensor on dirty partitions ──▶ gold
```

A medallion architecture *is* a chain of datasets, so "Silver is stale until Bronze
is rewritten" is the real dependency. A cron offset is a guess about runtime, and it
is the guess that produces 03:00 jobs reading half-written tables.

Restatement is the exception and is a sensor, because it is driven by a condition in
the data — a chargeback confirming fraud 1 to 45 days after the transaction — rather
than by an upstream task finishing. `gold dirty` exits **0 when there is work**,
inverting the usual convention deliberately: a sensor succeeds when its condition is
met.

## Two bugs the verification found

Neither would have shown up in a DAG that merely parsed.

**The gate retried.** With the project-wide `retries: 1`, a blocked gate went
`UP_FOR_RETRY` and only failed two minutes later. Retrying is right for the
transient case of a Spark session losing its JVM; a gate decision is deterministic,
so the retry only delayed the alert while the UI showed a task still trying. The
gate and the preflight now set `retries=0`.

**The restatement sensor could deadlock its own DAG.** Its timeout was two hours on
an `@hourly` schedule with `max_active_runs=1`, so a sensor that outlived its
interval blocked the run it was waiting to hand over to and the DAG would stop
firing — a deadlock that looks like "nothing to restate" from the outside. The
timeout is now shorter than the interval.

A third was found by the new unit tests rather than by running anything: a NULL
`overall_score` crashed the reason-string formatting, on the block path, which is
the rarest and worst moment to crash.

## Environment

Airflow **2.11.2** in its **own virtualenv** (`~/airflow-venv`), LocalExecutor, and
its own `airflow` Postgres database. The project venv is untouched: Airflow pins its
dependencies hard, and resolving it into `.venv` would be free to move
`pandas`/`numpy`/`pyarrow` under a pipeline whose Stage 7c measurements are only
reproducible if that environment stays put.

The DAGs shell out to the project CLI rather than importing it — they cannot import
it, by design — so orchestration knows *when* to run things and never *how* they
work. The cost is that a renamed CLI command would break a DAG silently at 03:00,
which is exactly what `check_dag_cli_contract.py` now prevents: it resolves all 21
`cli(...)` invocations against the real Typer tree in about a second, with no
Airflow needed.

Airflow 3.x was rejected on memory: four processes (api-server, scheduler,
dag-processor, triggerer) is roughly 1.2 GB, which overruns the 1280 MB
`docker-compose.yml` already declares for this service, on a 3.9 GB box that also
has to hold a 2 GB Spark driver.

## Running it

```bash
bash scripts/install_airflow.sh            # once
source scripts/airflow_env.sh
export CONFIDENTIA_KEYSTORE_PASSPHRASE="dev-passphrase-not-for-real-use"

airflow dags test confidentia_bronze_silver 2026-08-09    # no scheduler needed
bash scripts/verify_dags.sh                               # all five, plus the gate
```

For the UI, `airflow scheduler` and `airflow webserver --port 8080` in separate
shells, both with the passphrase in their environment — `BashOperator` inherits it,
and there is deliberately no default for it anywhere in the platform.

## Known limitations

- **The scheduler is not run persistently.** Everything here is verified with
  `airflow dags test`, which executes the same tasks in-process. On this hardware
  that is the honest choice: a scheduler plus webserver competing with a 2 GB Spark
  driver is what the memory budget cannot absorb.
- **No SLA or alerting.** `email_on_failure` is off and no callback is wired. The
  gate's audit trail is the notification channel, which is enough for a single
  operator and not enough for a team.
- **`max_active_tasks=1` throughout**, so the two tenants are processed serially
  even though they share nothing and are cryptographically isolated. That is a
  memory constraint, not a design one, and it is the first thing to relax on
  larger hardware.
- **Backfill is untested.** `catchup=False` everywhere and every DAG is `@once` or
  triggered, so the historical-reprocessing path has not been exercised.
- **The compose `airflow` service is still not buildable.** It references
  `docker/airflow/Dockerfile`, which does not exist — as is true of all four
  `docker/*/` directories. `compose-validate` parses the config and does not build,
  so CI is green and always has been. Containers are Stages 13–16; what this stage
  did was make the local path real and configure it from the same `AIRFLOW__*`
  keys the compose service already declares, so the two cannot drift when the
  image is eventually built.
