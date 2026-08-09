-- Gold serving layer and its governance records.
--
-- Published marts live in Postgres rather than in the encrypted lakehouse for a
-- reason established in ADR 0007: delta-rs cannot read Parquet Modular Encryption,
-- so the JVM-free API layer cannot query encrypted Delta at all. Rather than
-- weaken Gold's encryption to make it readable, only *disclosure-controlled
-- aggregates* cross into Postgres. The star schema stays encrypted in Delta for
-- Spark-based analysis.

-- What each publish released, and what it withheld. A disclosure control that is
-- applied but not recorded cannot be audited, and "we suppress small cells" is a
-- claim until there are numbers behind it.
CREATE TABLE IF NOT EXISTS platform.disclosure_log (
    disclosure_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    published_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id             UUID REFERENCES platform.run (run_id) ON DELETE SET NULL,
    tenant             TEXT NOT NULL,
    mart               TEXT NOT NULL,
    min_cell_count     INTEGER NOT NULL,
    total_cells        BIGINT NOT NULL,
    primary_suppressed BIGINT NOT NULL,
    complementary_suppressed BIGINT NOT NULL,
    differential_privacy BOOLEAN NOT NULL DEFAULT FALSE,
    epsilon            DOUBLE PRECISION,
    rows_published     BIGINT NOT NULL
);

CREATE INDEX IF NOT EXISTS disclosure_log_mart_idx
    ON platform.disclosure_log (tenant, mart, published_at DESC);

-- Restatement: which event dates a mart currently reflects, and when a late
-- arrival invalidated them.
--
-- Chargebacks land 1-45 days after the transaction they dispute, so an aggregate
-- for March closed in April becomes wrong in May. Recomputing every partition on
-- every run would be correct and unaffordable; recomputing only the dates a late
-- arrival actually touched is the point of this table.
CREATE TABLE IF NOT EXISTS platform.mart_partition_state (
    tenant          TEXT NOT NULL,
    mart            TEXT NOT NULL,
    event_date      DATE NOT NULL,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Source rows the partition was computed from, so a change in the count is a
    -- cheap signal that recomputation is due.
    source_rows     BIGINT NOT NULL DEFAULT 0,
    is_dirty        BOOLEAN NOT NULL DEFAULT FALSE,
    dirty_reason    TEXT,
    dirtied_at      TIMESTAMPTZ,
    restated_count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant, mart, event_date)
);

CREATE INDEX IF NOT EXISTS mart_partition_dirty_idx
    ON platform.mart_partition_state (tenant, mart) WHERE is_dirty;

CREATE TABLE IF NOT EXISTS platform.restatement_log (
    restatement_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    restated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id          UUID REFERENCES platform.run (run_id) ON DELETE SET NULL,
    tenant          TEXT NOT NULL,
    mart            TEXT NOT NULL,
    partitions_restated INTEGER NOT NULL,
    trigger_reason  TEXT NOT NULL,
    oldest_partition DATE,
    newest_partition DATE,
    lag_days        INTEGER
);

-- Published marts. Deliberately generic: one table holding narrow key/value rows
-- would be unqueryable, and a table per mart would need a migration per KPI. Each
-- mart is a real table created by the publisher from the aggregate's own schema.
CREATE SCHEMA IF NOT EXISTS gold;

COMMENT ON SCHEMA gold IS
    'Disclosure-controlled aggregates only. Never row-level data, never identifiers.';
COMMENT ON TABLE platform.mart_partition_state IS
    'Which event dates each mart reflects. Late arrivals mark partitions dirty for targeted restatement.';
