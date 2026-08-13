-- Databases the stack needs before anything can start.
--
-- Mounted into the postgres container at /docker-entrypoint-initdb.d, which the
-- official image runs **once**, on an empty data directory. Re-running it against
-- an existing volume does nothing, so this file must stay idempotent-by-omission:
-- it creates databases and nothing else. Schema is the migrations' job
-- (`confidentia db migrate`), and putting a table here would give the platform two
-- places that define one.
--
-- Without this file the compose stack fails in two ways that look unrelated:
-- Airflow cannot connect to its metadata database, and the CDC collector has no
-- source to replicate from.

-- The OLTP source the CDC path replicates from. Created by migration 003 on a
-- developer machine; created here because the container's Postgres starts empty
-- and migration 003 needs the database to exist before it can connect to it.
SELECT 'CREATE DATABASE ehr_oltp'
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'ehr_oltp')\gexec

-- Airflow's own metadata database. `AIRFLOW__DATABASE__SQL_ALCHEMY_CONN` in
-- docker-compose.yml points at it; without it the airflow container exits during
-- `airflow db migrate` with a connection error naming a database nobody created.
SELECT 'CREATE DATABASE airflow'
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'airflow')\gexec
