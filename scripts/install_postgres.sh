#!/usr/bin/env bash
# Install and configure PostgreSQL for CONFIDENTIA.
#
# Two databases with different jobs:
#   confidentia  -- platform metadata: ingestion runs, watermarks, batch registry,
#                   data quality results, the lineage ledger, users and audit.
#   ehr_oltp     -- a stand-in for the hospital's source-of-record system. The CDC
#                   collector replicates from its write-ahead log, which is why
#                   wal_level must be `logical`.
#
# wal_level=logical is the whole point: it lets the platform consume real change
# events rather than polling an updated_at column, which cannot see deletes and
# misses updates that land inside the polling interval.
#
# Idempotent: safe to re-run.
set -euo pipefail

PG_USER="${POSTGRES_USER:-confidentia}"
PG_PASSWORD="${POSTGRES_PASSWORD:-confidentia}"
PG_DATABASE="${POSTGRES_DATABASE:-confidentia}"
PG_OLTP="${POSTGRES_OLTP_DATABASE:-ehr_oltp}"

log() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }

if ! command -v psql >/dev/null 2>&1; then
    log "Installing PostgreSQL"
    sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        postgresql postgresql-contrib libpq-dev
fi

PG_VERSION="$(ls /etc/postgresql | sort -V | tail -1)"
PG_CONF="/etc/postgresql/${PG_VERSION}/main/postgresql.conf"
log "PostgreSQL ${PG_VERSION} at ${PG_CONF}"

# WSL has no systemd by default; pg_ctlcluster is the portable way to start it.
sudo pg_ctlcluster "${PG_VERSION}" main start 2>/dev/null || true

log "Configuring for logical replication and a small memory footprint"
sudo sed -i \
    -e "s/^#*wal_level *=.*/wal_level = logical/" \
    -e "s/^#*max_replication_slots *=.*/max_replication_slots = 8/" \
    -e "s/^#*max_wal_senders *=.*/max_wal_senders = 8/" \
    -e "s/^#*shared_buffers *=.*/shared_buffers = 128MB/" \
    -e "s/^#*max_connections *=.*/max_connections = 60/" \
    "${PG_CONF}"

sudo pg_ctlcluster "${PG_VERSION}" main restart
sleep 2

log "Creating role and databases"
sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${PG_USER}') THEN
        -- REPLICATION is required for the CDC collector to open a logical
        -- replication slot; LOGIN and CREATEDB cover everything else.
        CREATE ROLE ${PG_USER} WITH LOGIN CREATEDB REPLICATION PASSWORD '${PG_PASSWORD}';
    END IF;
END
\$\$;
SQL

for db in "${PG_DATABASE}" "${PG_OLTP}" airflow; do
    if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${db}'" | grep -q 1; then
        sudo -u postgres createdb -O "${PG_USER}" "${db}"
        log "created database ${db}"
    else
        log "database ${db} already exists"
    fi
done

log "Verifying"
export PGPASSWORD="${PG_PASSWORD}"
psql -h localhost -U "${PG_USER}" -d "${PG_DATABASE}" -tAc "SELECT version();" | head -1
psql -h localhost -U "${PG_USER}" -d "${PG_OLTP}" -tAc "SHOW wal_level;" \
    | xargs -I{} echo "  wal_level = {}"

log "PostgreSQL ready."
