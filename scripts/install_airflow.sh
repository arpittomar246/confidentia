#!/usr/bin/env bash
# Install Airflow into its OWN virtualenv, deliberately not the project's.
#
# Airflow pins its transitive dependencies hard, and resolving it into
# ~/confidentia/.venv would be free to move pandas, numpy or pyarrow underneath a
# pipeline that has just been verified end to end. This project has already lost
# three CI runs to environment drift (PROJECT_CONTEXT §14), and the Stage 7c
# measurements are only reproducible if the environment that produced them stays
# put.
#
# The separation costs nothing at runtime because the DAGs shell out to the
# project CLI rather than importing it: Airflow needs to know how to *start* the
# pipeline, not how it works. See ADR 0013.
#
#   Airflow venv   ~/airflow-venv        python 3.11, apache-airflow[postgres]
#   Project venv   ~/confidentia/.venv   python 3.11, pyspark + delta + the CLI
#   Metadata DB    postgres/airflow      separate database, never `confidentia`
set -euo pipefail

AIRFLOW_VERSION="${AIRFLOW_VERSION:-2.11.2}"
PYTHON_VERSION="3.11"
AIRFLOW_VENV="${AIRFLOW_VENV:-$HOME/airflow-venv}"
export AIRFLOW_HOME="${AIRFLOW_HOME:-$HOME/airflow}"

PGHOST="${POSTGRES_HOST:-localhost}"
PGUSER="${POSTGRES_USER:-confidentia}"
export PGPASSWORD="${POSTGRES_PASSWORD:-confidentia}"
AIRFLOW_DB="${AIRFLOW_DB:-airflow}"

echo "==> creating $AIRFLOW_VENV (python $PYTHON_VERSION)"
# --clear so a re-run is a clean reinstall rather than a resolve on top of
# whatever a previous attempt left behind.
uv venv --clear --python "$PYTHON_VERSION" "$AIRFLOW_VENV"

# The constraints file is the only supported way to install Airflow. Without it
# pip resolves a combination upstream has never tested.
CONSTRAINTS="https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt"
echo "==> installing apache-airflow $AIRFLOW_VERSION with upstream constraints"
uv pip install --python "$AIRFLOW_VENV/bin/python" \
    "apache-airflow[postgres]==${AIRFLOW_VERSION}" --constraint "$CONSTRAINTS"

echo "==> creating the $AIRFLOW_DB database (separate from confidentia)"
# Keeping Airflow's ~30 tables out of the platform database matters for more than
# tidiness: `db migrate` tracks migrations by checksum in platform.schema_migration,
# and a second tool creating tables in the same database makes it far harder to say
# what the platform's schema actually is.
psql -h "$PGHOST" -U "$PGUSER" -d postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname = '$AIRFLOW_DB'" | grep -q 1 \
    || psql -h "$PGHOST" -U "$PGUSER" -d postgres -c "CREATE DATABASE $AIRFLOW_DB"

echo "==> initialising the metadata database at $AIRFLOW_HOME"
mkdir -p "$AIRFLOW_HOME"

# Source the shared environment BEFORE `db migrate`, or Airflow initialises
# against its default SQLite file and silently ignores the Postgres database just
# created. SQLite also forces SequentialExecutor, so LocalExecutor would appear
# to be configured while never actually being used.
# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/airflow_env.sh"

# A leftover SQLite file from an earlier run is not harmless: whichever database
# Airflow opens first wins, and a stale one produces "DAG not found" against a
# metadata store nobody is looking at.
rm -f "$AIRFLOW_HOME/airflow.db"

"$AIRFLOW_VENV/bin/airflow" db migrate 2>&1 | grep -vi "RemovedInAirflow3Warning" | tail -3

echo
echo "installed:"
"$AIRFLOW_VENV/bin/airflow" version 2>/dev/null | tail -1
"$AIRFLOW_VENV/bin/airflow" config get-value database sql_alchemy_conn 2>/dev/null \
    | tail -1 | sed 's#://[^@]*@#://***@#'
"$AIRFLOW_VENV/bin/airflow" config get-value core executor 2>/dev/null | tail -1
echo
echo "AIRFLOW_HOME = $AIRFLOW_HOME"
echo "next: source scripts/airflow_env.sh, then airflow dags list"
