"""Shared wiring for the CONFIDENTIA DAGs.

**The DAGs shell out to the project CLI; they never import it.** Two reasons, and
the second is the load-bearing one:

* Airflow lives in its own virtualenv (ADR 0013). It cannot import
  ``confidentia`` at all, because pyspark, delta-spark and the pinned
  pandas/numpy are deliberately not installed beside it.
* Even if it could, a DAG that reimplements pipeline logic drifts from the
  pipeline. Every stage already has a CLI entry point that is tested, measured and
  attested; orchestration's job is to decide *when* things run and what happens
  when one fails, not to know how Bronze is written.

So a task here is a one-line ``BashOperator``. If a DAG file ever needs to know
something about the data, that is a signal the knowledge belongs in the CLI.
"""

from __future__ import annotations

import os
import shlex
from datetime import timedelta

import pendulum
from airflow.datasets import Dataset

# Resolved from the environment so the same DAG file works in WSL and in the
# docker-compose service, where the paths differ. `airflow_env.sh` exports both.
REPO = os.environ.get("CONFIDENTIA_REPO", "/opt/confidentia")
VENV = os.environ.get("CONFIDENTIA_VENV", f"{REPO}/.venv")
PYTHON = f"{VENV}/bin/python"

TENANTS = ("healthcare", "financial")

# Data-aware scheduling. A medallion architecture is a chain of datasets, so the
# dependency between stages is expressed as "Silver is stale until Bronze is
# rewritten" rather than as a cron offset chosen to be safely after the previous
# job. Cron chaining is what produces 03:00 jobs reading half-written tables.
LANDING = Dataset("confidentia://lake/landing")
BRONZE = Dataset("confidentia://lake/bronze")
SILVER = Dataset("confidentia://lake/silver")
GOLD = Dataset("confidentia://lake/gold")

DEFAULT_ARGS = {
    "owner": "confidentia",
    "depends_on_past": False,
    # One retry, and only one. Most failures here are deterministic -- a schema
    # contract violation or a revoked attestation will fail identically forever --
    # so retrying is only worth it for the genuinely transient case of a Spark
    # session losing its JVM. Ten retries would just delay the alert by an hour.
    "retries": 1,
    "retry_delay": timedelta(minutes=2),
    "execution_timeout": timedelta(minutes=45),
    "email_on_failure": False,
}

# Every DAG in this project starts here. A fixed past date with catchup disabled:
# a start_date of "now" makes runs non-reproducible, and catchup would queue six
# months of daily Spark jobs the first time the scheduler starts.
START = pendulum.datetime(2026, 8, 1, tz="UTC")

DAG_KWARGS = {
    "default_args": DEFAULT_ARGS,
    "start_date": START,
    "catchup": False,
    # One run at a time. Two concurrent runs of the same DAG would put two Spark
    # drivers on a 3.9 GB box and both would be OOM-killed mid-write, which for an
    # encrypted Delta table leaves a partial commit rather than a clean failure.
    "max_active_runs": 1,
    "max_active_tasks": 1,
}


def cli(*args: str) -> str:
    """A bash command that invokes the project CLI in the project's virtualenv.

    The keystore passphrase is **not** set here. It is inherited from the
    environment the scheduler was started in, which is the same contract the rest
    of the platform has: there is no fallback for it anywhere, and inventing a
    default in the orchestration layer would make this the one place in the
    codebase where key material has one.
    """
    joined = " ".join(shlex.quote(a) for a in args)
    return f"cd {shlex.quote(REPO)} && {shlex.quote(PYTHON)} -m confidentia.cli {joined}"


def doc(text: str) -> str:
    """Markdown shown on the task in the Airflow UI."""
    return "\n".join(line.strip() for line in text.strip().splitlines())


def preflight_attestation(task_id: str = "preflight_attestation"):
    """Fail fast if the running code is not approved for key release.

    Every DAG whose tasks touch the lake starts with this. Changing any measured
    package revokes key release, so the first run after a deploy *should* fail —
    that tax is what makes "which code touched this data" enforceable rather than
    merely recorded.

    Without it the failure still happens, but sixty seconds into starting a Spark
    session, once per task, inside a `KeyReleaseDeniedError` traceback. This asks
    the same question in about two seconds and prints the one command that fixes
    it. It weakens nothing.

    `retries=0` because the answer is deterministic: an unapproved measurement will
    still be unapproved in two minutes.
    """
    from airflow.operators.bash import BashOperator

    return BashOperator(
        task_id=task_id,
        bash_command=cli("keys", "check"),
        retries=0,
        doc_md=doc(
            """
            Is the running code approved for key release?

            If this fails, a measured package changed and every task that touches
            the lake will be refused its keys. Fix with:

                python -m confidentia.cli keys approve
            """
        ),
    )
