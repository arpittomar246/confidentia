-- Stage 9b (C3): the signed, hash-chained provenance ledger.
--
-- This is the one table in the schema whose value comes from what it *refuses* to
-- let you do. Every other table here is a record; this one is a commitment.
--
-- It lives in PostgreSQL and not in the lake on purpose. Appending a link to a hash
-- chain requires knowing the current tip and being the only writer to extend it --
-- a transaction with a uniqueness constraint. A chain kept in a Delta table with
-- concurrent writers would fork under exactly the conditions it exists to survive.

-- Recording provenance is its own kind of execution: it reads a table, digests
-- every record and signs the result, and when it fails the pipeline should be able
-- to say so. Folding it into 'quality' would hide it in run_summary.
--
-- Safe inside the migration transaction: PostgreSQL 12+ permits ALTER TYPE ... ADD
-- VALUE in a transaction block provided the new value is not used in that same
-- transaction, and nothing here inserts a run.
ALTER TYPE platform.run_type ADD VALUE IF NOT EXISTS 'lineage';

CREATE TABLE IF NOT EXISTS platform.lineage_entry (
    -- Display and query order only. The chain's real order is the
    -- previous_digest links; seq is what a human reads in a table.
    seq              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- H(previous_digest || H(payload)). Unique because two entries with the same
    -- digest would be the same claim recorded twice.
    entry_digest     TEXT NOT NULL UNIQUE,

    -- The structural defence against a forked chain. Two entries may not share a
    -- predecessor, so a concurrent second writer that read the same tip is
    -- rejected by the database rather than producing a second valid-looking
    -- history. Sixty-four zeros is the genesis predecessor, and the constraint
    -- therefore also enforces that there is exactly one chain.
    previous_digest  TEXT NOT NULL UNIQUE,

    -- SET NULL, deliberately not CASCADE. Every other child of platform.run
    -- cascades, because deleting a run should delete what it produced. Here that
    -- would make `DELETE FROM platform.run` a way to remove chain entries without
    -- breaking anything -- a tamper path through referential integrity. The run
    -- id also lives inside the signed payload, so losing the column loses a join,
    -- not a fact.
    run_id           UUID REFERENCES platform.run (run_id) ON DELETE SET NULL,

    -- Projections of the signed payload, for indexing and joining. NOT the
    -- authority: only `payload` is covered by the signature, so verification
    -- compares these against it and reports a divergence as tampering. Reading
    -- merkle_root from this column without verifying is reading unprotected data.
    tenant           TEXT NOT NULL,
    dataset          TEXT NOT NULL,
    layer            TEXT NOT NULL,
    transformation   TEXT NOT NULL,
    record_count     BIGINT NOT NULL,
    merkle_root      TEXT NOT NULL,
    code_measurement TEXT NOT NULL,

    -- False under every backend this project can run. Stored per entry rather than
    -- inferred from a setting, because a chain recorded under `sim` must keep
    -- saying so even if the platform is later deployed on real hardware.
    hardware_rooted  BOOLEAN NOT NULL,

    -- The authoritative, signed claim. Canonical JSON of LineageRecord.payload().
    payload          JSONB NOT NULL,
    -- ECDSA over the entry digest, base64. Covers the link as well as the payload:
    -- an entry replayed at a different chain position has a different predecessor
    -- and so a signature that no longer verifies.
    signature        TEXT NOT NULL,
    -- Stored so the chain can be verified without the keystore. Swapping it for an
    -- attacker's key is detected: the payload carries the key's fingerprint and is
    -- itself signed.
    public_key_pem   TEXT NOT NULL,

    recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT lineage_entry_digest_is_sha256
        CHECK (entry_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT lineage_entry_previous_is_sha256
        CHECK (previous_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT lineage_entry_no_self_reference
        CHECK (entry_digest <> previous_digest),
    CONSTRAINT lineage_entry_count_not_negative
        CHECK (record_count >= 0)
);

CREATE INDEX IF NOT EXISTS lineage_entry_dataset_idx
    ON platform.lineage_entry (tenant, dataset, layer, seq DESC);
CREATE INDEX IF NOT EXISTS lineage_entry_run_idx
    ON platform.lineage_entry (run_id);
-- "Which entries name this code measurement" is the question asked after a build
-- changes: everything produced by the old measurement is what needs re-checking.
CREATE INDEX IF NOT EXISTS lineage_entry_measurement_idx
    ON platform.lineage_entry (code_measurement);

COMMENT ON TABLE platform.lineage_entry IS
    'Signed, hash-chained provenance. Only `payload` is covered by the signature.';

-- The current commitment for each dataset: what `lineage prove` checks a row
-- against, and what a Gold consumer would cite.
CREATE OR REPLACE VIEW platform.lineage_latest AS
SELECT DISTINCT ON (e.tenant, e.dataset, e.layer)
       e.seq, e.tenant, e.dataset, e.layer, e.transformation,
       e.record_count, e.merkle_root, e.code_measurement,
       e.hardware_rooted, e.entry_digest, e.run_id, e.recorded_at
  FROM platform.lineage_entry e
 ORDER BY e.tenant, e.dataset, e.layer, e.seq DESC;

-- The tip of the chain. A verifier that has checked the whole chain once can
-- re-check only what arrived since, provided it remembers this digest.
CREATE OR REPLACE VIEW platform.lineage_head AS
SELECT seq, entry_digest, tenant, dataset, layer, recorded_at
  FROM platform.lineage_entry
 ORDER BY seq DESC
 LIMIT 1;
