#!/usr/bin/env bash
# Run the integration-tests CI job's exact steps against throwaway databases.
#
# The job cannot be run locally as GitHub Actions, but its steps can: create the
# OLTP database, apply migrations, run `pytest -m integration` against a database
# that has never seen this project. That last part is what matters — the tests
# passed for six stages against a database with real data in it, so "works
# locally" never established that they work from empty.
#
# Databases are dropped at the end. The platform database is never touched.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
# shellcheck disable=SC1091
source .venv/bin/activate

PLATFORM_DB="${PLATFORM_DB:-confidentia_cisim}"
OLTP_DB="${OLTP_DB:-ehr_oltp_cisim}"
export PGPASSWORD="${POSTGRES_PASSWORD:-confidentia}"
PGHOST="${POSTGRES_HOST:-localhost}"
PGUSER="${POSTGRES_USER:-confidentia}"

cleanup() {
    psql -h "$PGHOST" -U "$PGUSER" -d postgres -q \
        -c "DROP DATABASE IF EXISTS $PLATFORM_DB" \
        -c "DROP DATABASE IF EXISTS $OLTP_DB" >/dev/null 2>&1
    rm -rf /tmp/confidentia-cisim-data
}
trap cleanup EXIT

echo "==> creating throwaway databases"
cleanup
psql -h "$PGHOST" -U "$PGUSER" -d postgres -q \
    -c "CREATE DATABASE $PLATFORM_DB" -c "CREATE DATABASE $OLTP_DB" || exit 1

export POSTGRES_DATABASE="$PLATFORM_DB"
export POSTGRES_OLTP_DATABASE="$OLTP_DB"
export CONFIDENTIA_DATA_ROOT=/tmp/confidentia-cisim-data
export CONFIDENTIA_KEYSTORE_PASSPHRASE=ci-only-throwaway-passphrase

echo "==> applying migrations to an empty database"
python -m confidentia.cli db migrate || { echo "FAILED: db migrate"; exit 1; }

echo "==> pytest -m integration"
if pytest -m integration -q; then
    echo
    echo "integration job simulated successfully against empty databases."
else
    echo
    echo "FAILED: integration tests do not pass from empty."
    exit 1
fi
