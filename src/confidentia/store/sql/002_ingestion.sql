-- Ingestion state: idempotency, integrity, and resumability.
--
-- The design commitment here is that **a batch is identified by its content, not
-- its name**. Source systems redeliver, rename, and replay. Keying on filename
-- means a redelivered file loads twice; keying on the digest of the file digests
-- means it loads once, whatever it is called.

-- What happened to a batch we were offered.
DO $$ BEGIN
    CREATE TYPE platform.batch_status AS ENUM
        ('pending', 'ingested', 'failed', 'duplicate', 'corrupt');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS platform.ingest_batch (
    -- SHA-256 over the ordered list of per-file digests. This is the idempotency
    -- key for the entire platform.
    batch_digest   TEXT PRIMARY KEY,
    source         TEXT   NOT NULL,
    tenant         TEXT   NOT NULL,
    dataset        TEXT   NOT NULL,
    file_count     INTEGER NOT NULL,
    row_count      BIGINT NOT NULL,
    bytes          BIGINT NOT NULL,
    status         platform.batch_status NOT NULL DEFAULT 'pending',
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ingested_at    TIMESTAMPTZ,
    -- How many times this exact content was offered again. A redelivery is not an
    -- error, but a redelivery *rate* that climbs is a real upstream signal, so it
    -- is counted rather than discarded.
    seen_count     INTEGER NOT NULL DEFAULT 1,
    run_id         UUID REFERENCES platform.run (run_id) ON DELETE SET NULL,
    metadata       JSONB  NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS ingest_batch_dataset_idx
    ON platform.ingest_batch (tenant, dataset, first_seen_at DESC);

CREATE INDEX IF NOT EXISTS ingest_batch_status_idx
    ON platform.ingest_batch (status) WHERE status <> 'ingested';

-- Per-file record. Kept separate from the batch so that partial ingestion is
-- expressible: a batch of 20 files where file 13 fails checksum verification must
-- be able to say exactly which one, rather than failing opaquely.
CREATE TABLE IF NOT EXISTS platform.ingest_file (
    batch_digest   TEXT NOT NULL
        REFERENCES platform.ingest_batch (batch_digest) ON DELETE CASCADE,
    relative_path  TEXT   NOT NULL,
    file_digest    TEXT   NOT NULL,
    row_count      BIGINT NOT NULL,
    bytes          BIGINT NOT NULL,
    -- False until the digest computed from the bytes on disk matches the digest
    -- the source manifest declared.
    verified       BOOLEAN NOT NULL DEFAULT FALSE,
    ingested_at    TIMESTAMPTZ,
    PRIMARY KEY (batch_digest, relative_path)
);

CREATE INDEX IF NOT EXISTS ingest_file_digest_idx
    ON platform.ingest_file (file_digest);

-- Resumption point per source stream.
--
-- Deliberately stores the watermark as TEXT with a `kind` discriminator rather
-- than as a timestamp column. The four collectors resume on fundamentally
-- different things: a timestamp, a Postgres LSN, a Kafka offset, a monotonic id.
-- Forcing them into a timestamp column would mean lying about at least three.
DO $$ BEGIN
    CREATE TYPE platform.watermark_kind AS ENUM
        ('timestamp', 'lsn', 'offset', 'sequence', 'digest');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS platform.watermark (
    source           TEXT NOT NULL,
    tenant           TEXT NOT NULL,
    dataset          TEXT NOT NULL,
    watermark_kind   platform.watermark_kind NOT NULL,
    watermark_value  TEXT NOT NULL,
    -- Retained so a bad advance can be rolled back to the previous known-good
    -- position without reconstructing it from logs.
    previous_value   TEXT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id           UUID REFERENCES platform.run (run_id) ON DELETE SET NULL,
    PRIMARY KEY (source, tenant, dataset)
);

-- Logical replication slot state for the CDC collector. The slot itself lives in
-- Postgres; this records what the platform has confirmed, which is what allows a
-- restart to resume rather than replay from the slot's beginning.
CREATE TABLE IF NOT EXISTS platform.cdc_slot (
    slot_name        TEXT PRIMARY KEY,
    publication      TEXT NOT NULL,
    source_database  TEXT NOT NULL,
    tenant           TEXT NOT NULL,
    last_lsn         TEXT,
    last_commit_ts   TIMESTAMPTZ,
    events_consumed  BIGINT NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Records rejected before they ever reached Bronze -- unparseable, failed
-- checksum, malformed envelope. Data quality quarantine (Stage 7) handles records
-- that parsed but failed a rule; this is for the ones that never got that far.
-- Nothing is dropped silently at any layer.
CREATE TABLE IF NOT EXISTS platform.ingest_reject (
    reject_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id         UUID REFERENCES platform.run (run_id) ON DELETE CASCADE,
    source         TEXT NOT NULL,
    tenant         TEXT NOT NULL,
    dataset        TEXT NOT NULL,
    relative_path  TEXT,
    reason_code    TEXT NOT NULL,
    detail         TEXT,
    raw_excerpt    TEXT,
    rejected_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ingest_reject_run_idx
    ON platform.ingest_reject (run_id, reason_code);

COMMENT ON TABLE platform.ingest_batch IS
    'Content-addressed batch registry. batch_digest is the platform idempotency key.';
COMMENT ON TABLE platform.watermark IS
    'Resumption points. Value is text with a kind discriminator because collectors resume on timestamps, LSNs, offsets or sequences.';
