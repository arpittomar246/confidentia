-- The source-of-record OLTP system, standing in for a hospital EHR.
--
-- Applied to the `ehr_oltp` database, NOT to the platform database. This is
-- deliberately a separate database: the CDC collector must replicate from a system
-- it does not own, over the replication protocol, exactly as it would in
-- production. Reading from a table in its own database would prove nothing.

CREATE TABLE IF NOT EXISTS patient (
    patient_id               TEXT PRIMARY KEY,
    mrn                      TEXT NOT NULL UNIQUE,
    first_name               TEXT,
    last_name                TEXT,
    date_of_birth            DATE,
    sex                      TEXT,
    ssn                      TEXT,
    email                    TEXT,
    phone                    TEXT,
    address_line1            TEXT,
    city                     TEXT,
    state                    TEXT,
    postal_code              TEXT,
    plan_id                  TEXT,
    primary_care_provider_id TEXT,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- REPLICA IDENTITY FULL makes the WAL carry the *old* row on UPDATE and DELETE,
-- not just the primary key. Without it an update event says "patient PAT-42
-- changed" but not what it changed from -- and SCD2 cannot close the previous
-- version correctly, nor can the change be audited. It costs WAL volume, which is
-- the trade-off being accepted deliberately.
ALTER TABLE patient REPLICA IDENTITY FULL;

CREATE INDEX IF NOT EXISTS patient_updated_at_idx ON patient (updated_at);

-- Maintaining updated_at in a trigger rather than in application code: the CDC
-- consumer must be able to trust this column regardless of which client wrote the
-- row, and the mutation replayer is not the only writer.
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS patient_touch_updated_at ON patient;
CREATE TRIGGER patient_touch_updated_at
    BEFORE UPDATE ON patient
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- The publication the logical replication slot subscribes to. Scoped to the
-- tables the platform is entitled to replicate rather than FOR ALL TABLES, so
-- adding an unrelated table to the source system does not silently start feeding
-- it into the lakehouse.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_publication WHERE pubname = 'confidentia_patient_pub') THEN
        CREATE PUBLICATION confidentia_patient_pub FOR TABLE patient;
    END IF;
END
$$;

COMMENT ON TABLE patient IS
    'Source-of-record patient master. Replicated to the lakehouse over logical replication, not polled.';
