-- Stage 7b: the quality layer scored against the ground-truth answer key.
--
-- Everything else in this schema records what the platform *did*. This table
-- records how well it did it, measured against the corruptions Stage 2 injected
-- and recorded. It is the difference between "the quality framework flagged 7,474
-- rows" and "it found 91% of the nulls, 8% of the unit errors, and here is which
-- columns had no rule at all".

-- Evaluation is not a pipeline stage: it reads the answer key, which no pipeline
-- package may do. Giving it its own run type keeps that visible in
-- platform.run_summary rather than hiding it among the quality runs.
--
-- Safe inside the migration transaction: PostgreSQL 12+ permits ALTER TYPE ... ADD
-- VALUE in a transaction block provided the new value is not used in that same
-- transaction, and nothing here inserts a run.
ALTER TYPE platform.run_type ADD VALUE IF NOT EXISTS 'evaluation';

CREATE TABLE IF NOT EXISTS platform.dq_evaluation (
    evaluation_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id           UUID REFERENCES platform.run (run_id) ON DELETE CASCADE,
    tenant           TEXT NOT NULL,
    dataset          TEXT NOT NULL,
    -- Always 'bronze' in practice. Stored rather than assumed because a Silver
    -- evaluation would be measuring the platform's own cleaning, and a row that
    -- did not say so would be indistinguishable from a real result.
    layer            TEXT NOT NULL,
    -- 'overall' | 'family' | 'field' | 'unscoreable'. One table rather than four,
    -- because the rollups are the same confusion matrix at different granularity
    -- and splitting them would mean three near-identical schemas kept in step.
    scope            TEXT NOT NULL,
    family           TEXT NOT NULL,
    -- NULL on family and overall rollups; set on field-level rows.
    field            TEXT,

    true_positives   BIGINT NOT NULL,
    false_positives  BIGINT NOT NULL,
    false_negatives  BIGINT NOT NULL,
    -- The denominators, kept alongside the ratios. A recall of 1.0 over three
    -- injected defects is not the same finding as a recall of 1.0 over three
    -- thousand, and a stored ratio alone cannot tell them apart.
    truth_claims     BIGINT NOT NULL,
    detected_claims  BIGINT NOT NULL,

    -- NULL where the metric is undefined rather than zero: precision is undefined
    -- when nothing was detected, recall when nothing was injected. Writing 0.0
    -- there would assert a failure that did not happen.
    precision        DOUBLE PRECISION,
    recall           DOUBLE PRECISION,
    f1               DOUBLE PRECISION,

    -- Whether a rule existed for this family (and field) at all, read from
    -- platform.dq_rule. Separates "the rule ran and missed it" from "nothing was
    -- ever checked" -- two very different findings that look identical in a recall
    -- of 0.0.
    rule_exists      BOOLEAN,
    -- FALSE for families the platform detects but the generator never injects
    -- (future_event, late_arrival). Their detections are counted, never scored:
    -- treating them as false positives would slander a working rule.
    scoreable        BOOLEAN NOT NULL DEFAULT TRUE,

    -- Recorded per row so a future change of criterion cannot be mistaken for a
    -- change in the platform's accuracy.
    matching_criterion TEXT NOT NULL,
    evaluated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT dq_evaluation_scope_valid
        CHECK (scope IN ('overall', 'family', 'field', 'unscoreable')),
    CONSTRAINT dq_evaluation_field_scope
        CHECK ((scope = 'field') = (field IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS dq_evaluation_dataset_idx
    ON platform.dq_evaluation (tenant, dataset, scope, evaluated_at DESC);
CREATE INDEX IF NOT EXISTS dq_evaluation_family_idx
    ON platform.dq_evaluation (family) WHERE scope = 'family';

COMMENT ON TABLE platform.dq_evaluation IS
    'Precision/recall/F1 of the quality layer against the Stage 2 answer key. '
    'Scored on Bronze: Silver has already quarantined the defects.';
COMMENT ON COLUMN platform.dq_evaluation.rule_exists IS
    'Distinguishes a rule that missed from a column nobody checked.';

-- The headline view: per-family scores from the most recent evaluation of each
-- dataset. Worst recall first, because the low numbers are the result.
CREATE OR REPLACE VIEW platform.dq_evaluation_latest AS
SELECT e.tenant, e.dataset, e.layer, e.family,
       e.true_positives, e.false_positives, e.false_negatives,
       e.truth_claims, e.detected_claims,
       e.precision, e.recall, e.f1, e.rule_exists, e.evaluated_at
  FROM platform.dq_evaluation e
  JOIN (
        SELECT tenant, dataset, max(evaluated_at) AS latest
          FROM platform.dq_evaluation
         GROUP BY tenant, dataset
       ) m
    ON m.tenant = e.tenant AND m.dataset = e.dataset AND m.latest = e.evaluated_at
 WHERE e.scope = 'family'
 ORDER BY e.recall NULLS LAST, e.tenant, e.dataset;
