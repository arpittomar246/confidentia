-- Stage 8: the promotion gate's decisions, including the ones that let data through.
--
-- `platform.dq_score.promotion_blocked` has been computed since Stage 7a and
-- nothing consumed it. A score that nobody acts on is a dashboard, not a control.
-- This table records what the gate actually decided and what it stopped, so that a
-- pipeline that halted overnight can be explained from the database without
-- opening the Airflow UI or reading a task log.
--
-- Passing decisions are recorded too. An audit trail that only holds refusals
-- cannot answer "was this gate even running when that bad batch went through",
-- which is the question asked after the fact -- the same reason
-- platform.key_release_audit records grants as well as denials.

CREATE TABLE IF NOT EXISTS platform.promotion_gate (
    gate_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- The run that evaluated the gate, not the quality run being judged.
    run_id         UUID REFERENCES platform.run (run_id) ON DELETE CASCADE,
    -- The dq_score row the decision was taken from. Nullable because a gate can
    -- refuse for want of a score at all, which is itself a decision worth keeping.
    score_id       BIGINT REFERENCES platform.dq_score (score_id) ON DELETE SET NULL,

    tenant         TEXT NOT NULL,
    dataset        TEXT NOT NULL,
    layer          TEXT NOT NULL,

    -- Stored rather than re-derived: the threshold is a setting and settings
    -- change, so a historical decision must carry the threshold it was taken
    -- against or it cannot be reinterpreted later.
    overall_score  DOUBLE PRECISION,
    threshold      DOUBLE PRECISION NOT NULL,
    blocked        BOOLEAN NOT NULL,
    -- 'passed' | 'blocked_low_score' | 'blocked_no_score' | 'overridden'
    decision       TEXT NOT NULL,
    -- Free text for a human: which rules dragged the score down, or why the
    -- override was granted.
    reason         TEXT,
    -- An override is a deliberate act by an operator and must never be silent.
    -- Recorded as its own boolean rather than inferred from `decision`, so
    -- "how often is this gate bypassed" is a COUNT rather than a string match.
    overridden     BOOLEAN NOT NULL DEFAULT FALSE,
    decided_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT promotion_gate_decision_valid
        CHECK (decision IN ('passed', 'blocked_low_score', 'blocked_no_score', 'overridden')),
    -- An override that still reports itself as blocking would stop the pipeline
    -- while claiming not to.
    CONSTRAINT promotion_gate_override_is_not_blocked
        CHECK (NOT overridden OR NOT blocked)
);

CREATE INDEX IF NOT EXISTS promotion_gate_dataset_idx
    ON platform.promotion_gate (tenant, dataset, decided_at DESC);
-- Partial index: "what is currently stopping the pipeline" is the query that gets
-- run under time pressure, and it only ever looks at blocking decisions.
CREATE INDEX IF NOT EXISTS promotion_gate_blocked_idx
    ON platform.promotion_gate (decided_at DESC) WHERE blocked;

COMMENT ON TABLE platform.promotion_gate IS
    'Promotion decisions taken from dq_score. Records passes as well as blocks.';

-- Why the pipeline is stopped, newest first. Deliberately a view rather than a CLI
-- query so the dashboard and the CLI cannot disagree about what "blocked" means.
CREATE OR REPLACE VIEW platform.promotion_blocked_now AS
SELECT DISTINCT ON (g.tenant, g.dataset)
       g.tenant, g.dataset, g.layer, g.overall_score, g.threshold,
       g.decision, g.reason, g.overridden, g.decided_at
  FROM platform.promotion_gate g
 ORDER BY g.tenant, g.dataset, g.decided_at DESC;
