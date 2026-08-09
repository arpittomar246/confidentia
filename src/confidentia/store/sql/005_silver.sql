-- Silver layer: token vault and quarantine.

-- Reversible tokenisation, for the narrow set of fields where an authorised role
-- must be able to re-identify (a clinician looking up their own patient, a fraud
-- analyst confirming an account).
--
-- The vault stores the ORIGINAL VALUE ENCRYPTED, never in plaintext. A vault that
-- held plaintext would simply relocate the PII rather than protect it, and would
-- become the single most attractive table in the platform.
CREATE TABLE IF NOT EXISTS platform.token_vault (
    token           TEXT PRIMARY KEY,
    tenant          TEXT NOT NULL,
    field           TEXT NOT NULL,
    -- AES-256-GCM: base64(nonce || ciphertext || tag), under a key derived from
    -- the MEK and released only through the broker.
    ciphertext      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- How many source records mapped to this token. A count of 1 on a supposedly
    -- shared identifier is a data quality signal, not a curiosity.
    occurrence_count BIGINT NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS token_vault_field_idx ON platform.token_vault (tenant, field);

-- Every re-identification, recorded. Detokenisation is the one operation that
-- undoes the platform's privacy guarantee, so it is the one that most needs an
-- audit trail.
CREATE TABLE IF NOT EXISTS platform.detokenisation_audit (
    audit_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    principal    TEXT NOT NULL,
    tenant       TEXT NOT NULL,
    field        TEXT NOT NULL,
    token_count  INTEGER NOT NULL,
    justification TEXT,
    granted      BOOLEAN NOT NULL,
    reason       TEXT
);

CREATE INDEX IF NOT EXISTS detokenisation_audit_time_idx
    ON platform.detokenisation_audit USING brin (requested_at);

-- Records that failed Silver validation. NOT dropped -- quarantined with a reason
-- and a replay handle, so a fixed upstream can be reprocessed rather than lost.
CREATE TABLE IF NOT EXISTS platform.quarantine_record (
    quarantine_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id         UUID REFERENCES platform.run (run_id) ON DELETE SET NULL,
    tenant         TEXT NOT NULL,
    dataset        TEXT NOT NULL,
    layer          TEXT NOT NULL DEFAULT 'silver',
    business_key   TEXT,
    batch_digest   TEXT,
    reason_code    TEXT NOT NULL,
    failed_rule    TEXT,
    severity       TEXT NOT NULL DEFAULT 'critical',
    -- The offending record, already tokenised and with sensitive columns removed.
    -- Quarantine must not become a plaintext side channel around the encrypted
    -- lakehouse.
    redacted_record JSONB,
    quarantined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    replayed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS quarantine_dataset_idx
    ON platform.quarantine_record (tenant, dataset, reason_code, quarantined_at DESC);

CREATE INDEX IF NOT EXISTS quarantine_unreplayed_idx
    ON platform.quarantine_record (quarantined_at DESC) WHERE replayed_at IS NULL;

COMMENT ON TABLE platform.token_vault IS
    'Reversible tokenisation. Originals are stored encrypted, never in plaintext.';
COMMENT ON TABLE platform.quarantine_record IS
    'Records that failed Silver validation. Redacted; never a plaintext side channel.';
