-- Experiment results.
--
-- Recorded in the metadata store rather than in a notebook or a CSV, for the same
-- reason every other number in this platform is: a figure that appears in the
-- report must be traceable to the run that produced it, on a machine whose state
-- is recorded alongside it.
--
-- `label` is the honesty column. A measured wall-clock time under TEE_BACKEND=sim
-- is MEASURED for the pipeline and says nothing about an enclave; anything derived
-- from the cost model is MODELLED. The distinction is stored, not inferred at
-- render time.

CREATE TABLE IF NOT EXISTS platform.benchmark_result (
    result_id     BIGSERIAL PRIMARY KEY,
    experiment    TEXT NOT NULL,
    arm           TEXT NOT NULL,
    metric        TEXT NOT NULL,
    value         DOUBLE PRECISION NOT NULL,
    unit          TEXT NOT NULL,
    label         TEXT NOT NULL CHECK (label IN ('MEASURED', 'MODELLED')),

    -- What was measured over. Null where the experiment is not row-oriented.
    rows_processed BIGINT,
    bytes_processed BIGINT,

    -- Dispersion. A single number from a single run is not a measurement, so the
    -- harness always reports repeats and these are how many and how spread.
    repeats       INTEGER NOT NULL DEFAULT 1,
    p50           DOUBLE PRECISION,
    p90           DOUBLE PRECISION,
    minimum       DOUBLE PRECISION,
    maximum       DOUBLE PRECISION,

    -- The machine and the configuration. Without these a number six months from
    -- now is a number nobody can reproduce or argue with.
    environment   JSONB NOT NULL DEFAULT '{}'::jsonb,
    notes         TEXT,
    run_id        UUID,
    measured_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_benchmark_experiment
    ON platform.benchmark_result (experiment, arm, measured_at DESC);

-- The most recent result per experiment/arm/metric, which is what a report renders.
CREATE OR REPLACE VIEW platform.benchmark_latest AS
SELECT DISTINCT ON (experiment, arm, metric)
       experiment, arm, metric, value, unit, label,
       rows_processed, bytes_processed, repeats, p50, p90, minimum, maximum,
       environment, notes, measured_at
  FROM platform.benchmark_result
 ORDER BY experiment, arm, metric, measured_at DESC;
