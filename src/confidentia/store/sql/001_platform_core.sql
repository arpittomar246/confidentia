-- Platform core: run tracking shared by every stage.
--
-- One `run` table rather than per-subsystem tables. Ingestion, transformation,
-- quality and benchmark executions all answer the same questions -- what ran, over
-- what, how long, how many records, did it fail -- and the dashboard's pipeline
-- monitoring view needs them in one place to render a single timeline. Splitting
-- them would push a five-way UNION into the hot path of every screen.

CREATE SCHEMA IF NOT EXISTS platform;

CREATE TABLE IF NOT EXISTS platform.schema_migration (
    version     TEXT PRIMARY KEY,
    checksum    TEXT        NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$ BEGIN
    CREATE TYPE platform.run_status AS ENUM
        ('running', 'succeeded', 'failed', 'cancelled', 'skipped');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE platform.run_type AS ENUM
        ('ingest', 'transform', 'quality', 'stream', 'benchmark');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS platform.run (
    run_id            UUID PRIMARY KEY,
    run_type          platform.run_type   NOT NULL,
    source            TEXT,
    tenant            TEXT                NOT NULL,
    dataset           TEXT                NOT NULL,
    status            platform.run_status NOT NULL DEFAULT 'running',
    attempt           INTEGER             NOT NULL DEFAULT 1,
    started_at        TIMESTAMPTZ         NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,
    records_read      BIGINT              NOT NULL DEFAULT 0,
    records_written   BIGINT              NOT NULL DEFAULT 0,
    records_rejected  BIGINT              NOT NULL DEFAULT 0,
    bytes_read        BIGINT              NOT NULL DEFAULT 0,
    error_type        TEXT,
    error_message     TEXT,
    -- Free-form per-run context: watermark values, collector settings, the TEE
    -- backend in force. JSONB because the useful keys differ per run_type and
    -- pinning them into columns would mean a migration per collector.
    metadata          JSONB               NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT run_finished_after_started
        CHECK (finished_at IS NULL OR finished_at >= started_at),
    -- A terminal run must record when it ended, or duration and SLA reporting
    -- silently produce nulls that look like zero.
    CONSTRAINT run_terminal_has_finished_at
        CHECK (status = 'running' OR finished_at IS NOT NULL)
);

-- The runs table is append-heavy and almost always queried by recency, which is
-- exactly the access pattern BRIN is built for: a fraction of a btree's size.
CREATE INDEX IF NOT EXISTS run_started_at_brin
    ON platform.run USING brin (started_at);

CREATE INDEX IF NOT EXISTS run_dataset_status_idx
    ON platform.run (tenant, dataset, status, started_at DESC);

-- Partial index: "what is running right now" is the dashboard's most frequent
-- query and matches a tiny fraction of rows.
CREATE INDEX IF NOT EXISTS run_active_idx
    ON platform.run (started_at DESC) WHERE status = 'running';

CREATE OR REPLACE VIEW platform.run_summary AS
SELECT
    run_id,
    run_type,
    source,
    tenant,
    dataset,
    status,
    attempt,
    started_at,
    finished_at,
    EXTRACT(EPOCH FROM (COALESCE(finished_at, now()) - started_at)) * 1000
        AS duration_ms,
    records_read,
    records_written,
    records_rejected,
    CASE WHEN records_read > 0
         THEN records_rejected::numeric / records_read
         ELSE 0 END AS rejection_rate,
    error_type,
    error_message
FROM platform.run;

COMMENT ON TABLE platform.run IS
    'One row per execution of any pipeline stage. Joined to logs by run_id.';
