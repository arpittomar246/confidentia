#!/usr/bin/env bash
# Environment for every Airflow command in this project. Source it, do not run it.
#
#   source scripts/airflow_env.sh
#   airflow dags list
#
# Configuration is by environment variable rather than by editing airflow.cfg, so
# that the local WSL setup and the docker-compose service (which sets the same
# AIRFLOW__* keys) are configured the same way and cannot drift. Every value here
# has a counterpart in docker-compose.yml's `airflow` service.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export AIRFLOW_HOME="${AIRFLOW_HOME:-$HOME/airflow}"
export AIRFLOW_VENV="${AIRFLOW_VENV:-$HOME/airflow-venv}"

# Postgres, not the default SQLite. This is not a preference: SQLite forces
# SequentialExecutor, so the LocalExecutor the whole design assumes silently
# degrades to one task at a time and the parallelism settings below do nothing.
export AIRFLOW__DATABASE__SQL_ALCHEMY_CONN="postgresql+psycopg2://${POSTGRES_USER:-confidentia}:${POSTGRES_PASSWORD:-confidentia}@${POSTGRES_HOST:-localhost}:5432/${AIRFLOW_DB:-airflow}"
export AIRFLOW__CORE__EXECUTOR=LocalExecutor

export AIRFLOW__CORE__DAGS_FOLDER="$REPO_DIR/orchestration/dags"
export AIRFLOW__CORE__PLUGINS_FOLDER="$REPO_DIR/orchestration/plugins"
export AIRFLOW__CORE__LOAD_EXAMPLES=False

# One task at a time, everywhere. Not tuning -- a correctness constraint on a
# 3.9 GB box: a Spark task takes a 2 GB driver, and two concurrent ones would
# swap or be OOM-killed mid-write, which for an encrypted Delta table means a
# partial commit to debug rather than a clean failure.
export AIRFLOW__CORE__PARALLELISM=1
export AIRFLOW__CORE__MAX_ACTIVE_TASKS_PER_DAG=1
export AIRFLOW__CORE__MAX_ACTIVE_RUNS_PER_DAG=1

# Nothing here is scheduled to run on its own. Every DAG in this project is
# @once or manually triggered, and a scheduler that backfills six months of daily
# runs on first start would queue hundreds of Spark jobs.
export AIRFLOW__SCHEDULER__CATCHUP_BY_DEFAULT=False

# The DAGs shell out to the project CLI, so they need the project venv and the
# keystore passphrase. The passphrase is deliberately not defaulted: the platform
# has no fallback for it anywhere else either, and inventing one here would be the
# only place in the codebase where key material has a default.
export CONFIDENTIA_VENV="${CONFIDENTIA_VENV:-$REPO_DIR/.venv}"
export CONFIDENTIA_REPO="$REPO_DIR"

if [[ -z "${CONFIDENTIA_KEYSTORE_PASSPHRASE:-}" ]]; then
    echo "warning: CONFIDENTIA_KEYSTORE_PASSPHRASE is not set." >&2
    echo "         Any DAG task that touches the lake will fail at key release." >&2
fi

export PATH="$AIRFLOW_VENV/bin:$PATH"
