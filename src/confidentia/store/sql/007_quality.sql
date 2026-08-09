-- Data quality: rule registry, per-run results, and dataset scores.
--
-- Aggregates live here; the individual detections do not. A run over 60k records
-- with 30 rules can produce hundreds of thousands of detection rows, and those are
-- written to a Delta table in the lake instead. Postgres holds what the dashboard
-- queries: counts, rates and scores.

CREATE TABLE IF NOT EXISTS platform.dq_rule (
    rule_id      TEXT PRIMARY KEY,
    tenant       TEXT NOT NULL,
    dataset      TEXT NOT NULL,
    -- One of the six quality dimensions. Kept as a column rather than inferred
    -- from the family so a dimension score is a GROUP BY, not a CASE expression.
    dimension    TEXT NOT NULL,
    -- Aligned one-to-one with the defect families the generator injects, so the
    -- Stage 7b evaluation join needs no translation layer.
    family       TEXT NOT NULL,
    field        TEXT,
    severity     TEXT NOT NULL,
    description  TEXT NOT NULL,
    -- Contract-derived rules are regenerated whenever the contract changes;
    -- explicit rules are hand-written and stable.
    is_derived   BOOLEAN NOT NULL DEFAULT FALSE,
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS dq_rule_dataset_idx
    ON platform.dq_rule (tenant, dataset) WHERE is_active;

CREATE TABLE IF NOT EXISTS platform.dq_result (
    result_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id         UUID REFERENCES platform.run (run_id) ON DELETE CASCADE,
    rule_id        TEXT NOT NULL REFERENCES platform.dq_rule (rule_id) ON DELETE CASCADE,
    tenant         TEXT NOT NULL,
    dataset        TEXT NOT NULL,
    layer          TEXT NOT NULL,
    -- Rows the rule could actually be evaluated against. A rule on a column that
    -- is entirely NULL evaluates zero rows, and a pass rate of 1.0 there would be
    -- a lie -- hence storing the denominator rather than only the rate.
    rows_evaluated BIGINT NOT NULL,
    rows_failed    BIGINT NOT NULL,
    pass_rate      DOUBLE PRECISION,
    evaluated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS dq_result_run_idx ON platform.dq_result (run_id);
CREATE INDEX IF NOT EXISTS dq_result_rule_time_idx
    ON platform.dq_result (rule_id, evaluated_at DESC);

-- Dataset-level scores, one row per run.
--
-- Both the overall score and the per-dimension scores are stored. A single number
-- is what a dashboard wants and is also a compression that hides which dimension
-- collapsed; keeping the components means the headline can always be decomposed.
CREATE TABLE IF NOT EXISTS platform.dq_score (
    score_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id          UUID REFERENCES platform.run (run_id) ON DELETE CASCADE,
    tenant          TEXT NOT NULL,
    dataset         TEXT NOT NULL,
    layer           TEXT NOT NULL,
    overall_score   DOUBLE PRECISION NOT NULL,
    completeness    DOUBLE PRECISION,
    validity        DOUBLE PRECISION,
    consistency     DOUBLE PRECISION,
    uniqueness      DOUBLE PRECISION,
    integrity       DOUBLE PRECISION,
    timeliness      DOUBLE PRECISION,
    rows_total      BIGINT NOT NULL,
    rows_failing    BIGINT NOT NULL,
    rules_evaluated INTEGER NOT NULL,
    rules_failed    INTEGER NOT NULL,
    promotion_blocked BOOLEAN NOT NULL DEFAULT FALSE,
    scored_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS dq_score_dataset_time_idx
    ON platform.dq_score (tenant, dataset, scored_at DESC);

-- Freshness is a property of a dataset, not of a row, so it cannot be expressed as
-- a row-level rule. Tracked separately and folded into the timeliness dimension.
CREATE TABLE IF NOT EXISTS platform.dq_freshness (
    tenant           TEXT NOT NULL,
    dataset          TEXT NOT NULL,
    layer            TEXT NOT NULL,
    max_event_ts     TIMESTAMPTZ,
    max_ingest_ts    TIMESTAMPTZ,
    observed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    lag_minutes      DOUBLE PRECISION,
    sla_minutes      INTEGER NOT NULL,
    breached         BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (tenant, dataset, layer)
);

COMMENT ON TABLE platform.dq_result IS
    'Per-rule outcome for one run. Individual detections live in the lake, not here.';
COMMENT ON TABLE platform.dq_score IS
    'Dataset scores. Per-dimension components are kept so the headline can be decomposed.';
