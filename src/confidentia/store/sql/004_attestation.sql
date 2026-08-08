-- Attestation policy and key-release audit.
--
-- The operational property this enables: a pipeline whose code has changed cannot
-- decrypt its own data until the new measurement is approved. That is unusual and
-- deliberately inconvenient -- it makes "what code touched this data" enforceable
-- rather than merely recorded.

CREATE TABLE IF NOT EXISTS platform.attestation_policy (
    measurement    TEXT PRIMARY KEY,
    description    TEXT NOT NULL,
    -- Which tenants this measurement may obtain keys for. Empty array means none;
    -- the wildcard '*' is accepted but has to be written deliberately.
    tenants        TEXT[] NOT NULL DEFAULT '{}',
    approved_by    TEXT NOT NULL,
    approved_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at     TIMESTAMPTZ,
    -- Recorded so an approval made under the simulator can never be mistaken for
    -- one backed by hardware attestation.
    tee_backend    TEXT NOT NULL,
    hardware_rooted BOOLEAN NOT NULL DEFAULT FALSE,
    notes          TEXT
);

CREATE INDEX IF NOT EXISTS attestation_policy_active_idx
    ON platform.attestation_policy (measurement) WHERE revoked_at IS NULL;

-- Every key release decision, including the refusals. The refusals are the
-- interesting ones: they are the evidence that the gate does something.
CREATE TABLE IF NOT EXISTS platform.key_release_audit (
    audit_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    requested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    measurement    TEXT NOT NULL,
    tenant         TEXT NOT NULL,
    -- Key identifiers only. Key material must never reach this table, and the
    -- column name says so to anyone tempted.
    key_ids        TEXT[] NOT NULL DEFAULT '{}',
    granted        BOOLEAN NOT NULL,
    reason         TEXT NOT NULL,
    nonce          TEXT,
    evidence_digest TEXT,
    run_id         UUID REFERENCES platform.run (run_id) ON DELETE SET NULL,
    tee_backend    TEXT NOT NULL,
    hardware_rooted BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS key_release_audit_time_idx
    ON platform.key_release_audit USING brin (requested_at);

CREATE INDEX IF NOT EXISTS key_release_denied_idx
    ON platform.key_release_audit (requested_at DESC) WHERE granted = FALSE;

COMMENT ON TABLE platform.attestation_policy IS
    'Measurements permitted to obtain keys. hardware_rooted=false means the measurement was self-reported under the simulator and provides no security guarantee.';
COMMENT ON TABLE platform.key_release_audit IS
    'Every key release decision. Key identifiers only -- never key material.';
