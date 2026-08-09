#!/usr/bin/env bash
# Return the platform to a clean, pre-ingest state so a pipeline run starts from
# nothing. Written for Stage 7c, where the generator's defect logic changed and
# every landing batch therefore has a new content digest: without a reset the old
# and new batches both sit in the landing zone and a full-refresh Bronze write
# would promote both.
#
# WHAT IS DESTROYED  the lake (landing/bronze/silver/gold/quality), the generated
#                    raw files, Spark scratch, the ingestion and quality tracking
#                    tables, the gold marts, and the replication slot.
# WHAT IS KEPT       the keystore -- lose it and any surviving encrypted table is
#                    unreadable forever -- the applied migrations, and the schema.
#
# Everything destroyed is regenerable by design (PROJECT_CONTEXT §11). Anything
# that is not, such as a stage's measured results, must be exported first.
set -euo pipefail

DATA_ROOT="${CONFIDENTIA_DATA_ROOT:-$HOME/confidentia-data}"
PGHOST="${POSTGRES_HOST:-localhost}"
PGUSER="${POSTGRES_USER:-confidentia}"
PGDATABASE="${POSTGRES_DATABASE:-confidentia}"
export PGPASSWORD="${POSTGRES_PASSWORD:-confidentia}"

if [[ "${1:-}" != "--yes" ]]; then
    echo "This deletes the lake, the raw files and all pipeline tracking state."
    echo "Keystore and migrations are preserved. Re-run with --yes to proceed."
    exit 1
fi

echo "==> dropping the replication slot (it pins WAL on the source)"
psql -h "$PGHOST" -U "$PGUSER" -d "${POSTGRES_OLTP_DATABASE:-ehr_oltp}" -q -c \
    "SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots
      WHERE slot_name = 'confidentia_patient_slot'" >/dev/null 2>&1 || true

echo "==> removing generated data under $DATA_ROOT"
for directory in lake raw checkpoints spark-warehouse spark-tmp; do
    rm -rf "${DATA_ROOT:?}/$directory"
done

echo "==> truncating pipeline state (schema and migrations preserved)"
# `run` cascades to dq_result, dq_score and dq_evaluation via ON DELETE CASCADE;
# they are listed anyway so the intent is readable rather than inferred.
psql -h "$PGHOST" -U "$PGUSER" -d "$PGDATABASE" -q <<'SQL'
TRUNCATE TABLE
    platform.run,
    platform.dq_result,
    platform.dq_score,
    platform.dq_evaluation,
    platform.dq_rule,
    platform.dq_freshness,
    platform.ingest_batch,
    platform.ingest_file,
    platform.ingest_reject,
    platform.watermark,
    platform.cdc_slot,
    platform.quarantine_record,
    platform.disclosure_log,
    platform.mart_partition_state,
    platform.restatement_log,
    platform.token_vault,
    platform.detokenisation_audit,
    platform.key_release_audit,
    -- Approved measurements are dropped too: the measured packages changed, so
    -- every stored measurement is stale and `keys approve` must run again anyway.
    platform.attestation_policy
RESTART IDENTITY CASCADE;

-- The marts are created dynamically by the publisher, one table per mart, so
-- they are dropped rather than truncated.
DO $$
DECLARE t record;
BEGIN
    FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'gold'
    LOOP
        EXECUTE format('DROP TABLE IF EXISTS gold.%I CASCADE', t.tablename);
    END LOOP;
END $$;
SQL

echo
echo "reset complete. Next:"
echo "  python -m confidentia.cli keys init      # keystore survived, policy did not"
echo "  python -m confidentia.cli keys approve"
echo "  ... then the full pipeline from PROJECT_CONTEXT §9"
