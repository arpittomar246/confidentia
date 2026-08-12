# ADR 0016 — What the serving layer may read, and what it may do

- **Status:** Accepted
- **Date:** 2026-08-13
- **Stage:** 10 (the API)

## Context

Stages 1–9b built a platform whose only interface is a CLI. Every confidentiality
property it claims — encrypted columns, attested key release, disclosure-controlled
aggregates, signed lineage — is real and none of it is reachable by anything other
than a shell on the machine that holds the keystore.

The serving layer changes that, and the risk it introduces is not cryptographic. It
is that an HTTP surface is the easiest place in a system to accidentally recompute a
number, hold a key, or acquire a second scheduler. This ADR fixes four boundaries
before the dashboard exists to argue with them.

## 1. The API reads PostgreSQL. It does not read the lake.

**Decision.** Every endpoint is a query against `platform.*` or `gold.*`. No
endpoint opens a Delta table.

This is forced, not preferred. `delta-rs` — the JVM-free Python reader — cannot
decrypt Parquet Modular Encryption. A Python API process therefore *cannot* read
Bronze, Silver or Gold in the lake at all, whatever we would prefer. That constraint
was already load-bearing at Stage 6: it is why `publish_mart` writes
disclosure-controlled aggregates into the `gold` schema in Postgres rather than
leaving them in the lake for a reader to aggregate.

The alternative is a JVM in the API process — a SparkSession per worker, key release
through attestation on every request, and the API inside `MEASURED_PACKAGES`. That
trades a read-only metadata service for a component that holds DEKs and whose source
is part of the attested measurement, so that editing a route invalidates every
attestation policy entry. The constraint and the design agree, which is convenient
but is not why the decision was made.

**Consequence.** An endpoint that would need a Spark session belongs in the CLI.
Two exist today: `lineage verify --deep`, which re-reads the encrypted lake and
recomputes Merkle roots, and `lineage prove`, which re-derives Merkle leaves to
build an inclusion proof. Both take minutes and hold a session. Neither is exposed.
`GET /lineage/verify` runs the shallow check — links, signatures, and columns
against the signed payload — which is pure SQL plus in-process crypto, and its
response names `lake_contents_rehashed: false` so that a passing result cannot be
read as a claim it does not make.

## 2. The API computes no metrics of its own.

**Decision.** Every number returned comes from a table or a view the pipeline
already writes. Where a derived value is needed, it is derived in SQL over those
tables, not in Python over fetched rows.

The failure this prevents is specific and has already happened once in this project.
`promotion_blocked_now` is a *view* rather than a query inside `gate.py`, because
"currently blocked" is a definition — the latest decision per dataset, not overridden
— and a second copy of that definition drifts from the first. An API that answered
`/gate/blocked` with its own version of that logic would give the dashboard a
different answer from `confidentia quality gate`, and the resulting bug report would
be about the dashboard.

The one place this is visibly awkward is `/evaluation/summary`, which micro-averages
precision and recall across defect families. Averaging per-family F1 would be
simpler and is wrong — it weights a family with three defects like one with four
hundred — so the aggregation is a `WITH` clause summing TP/FP/FN and dividing once,
in the database, next to the data it aggregates.

## 3. The API is read-only. Airflow owns triggering.

**Decision.** No endpoint mutates platform state or starts a pipeline run. A
non-GET request is refused by middleware with 405 and `code: read_only`.

A trigger endpoint is a second orchestrator. Airflow already owns scheduling,
retries, the promotion gate and the attestation preflight, and enforces
`max_active_runs=1` per DAG. That guarantee is per-DAG, not per-machine: an API that
can start a Bronze write can start one *while Airflow is running one*, against the
same Delta table, with two processes holding keys for the same tenant. Nothing in
the DAG would notice.

The refusal is middleware rather than merely an absence of POST routes. Every route
in the package is a GET, so a POST already 405s — but the middleware states the
reason, and it survives someone adding a router without reading this file.

**Consequence.** `operator` currently grants no capability that `viewer` lacks
except breadth of read access. That is honest for a read-only API: the roles form a
disclosure ladder, `viewer < operator < auditor`, and `_RANK` in `security.py` is
written as a mapping precisely so that when triggering does arrive, `operator`
becomes a capability check rather than a rung on that ladder.

## 4. The API holds no key material and is not attested.

**Decision.** The API does not import the broker, does not request keys, does not
read the keystore passphrase, and is not in `MEASURED_PACKAGES`.

`platform.key_release_audit` is served — grants *and* the four refusal paths — but
`key_ids` in that table are identifiers of the form `tenant/sensitivity/version`.
No key material is stored there and none is added here.

Being outside the attested boundary is deliberate. `MEASURED_PACKAGES` is
`("crypto", "pipelines", "quality", "lineage")`, and the measurement gates key
release. Adding the API would mean that editing a route changes the measurement,
which revokes every attestation policy entry and stops the pipeline — coupling a
presentation change to the confidentiality mechanism. The API is untrusted by
construction instead: it can read what Postgres will give it and nothing else.

## Authentication

**Decision.** Bearer tokens, verified two ways, selected by `AUTH_MODE`.

`oidc` is the real mode: RS256/ES256 verified against the provider's JWKS, with
`iss` and `aud` both checked and the permitted-algorithm list asymmetric-only. The
API holds no signing key, so compromising it does not let an attacker mint an
auditor token.

`local` mints and verifies HS256 tokens against a shared secret. It exists because
this project has no identity provider to point at, and it is refused outside
`CONFIDENTIA_ENV=local` by a settings validator — a deployed API that both issues
and verifies its own tokens has no authentication.

The algorithm list is closed on purpose. Accepting HS256 alongside RS256 is the
algorithm-confusion attack: the JWKS document is public, so an attacker uses the
RSA public key's PEM bytes as an HMAC secret and signs whatever they like. There is
a test that forges exactly that token by hand — `jwt.encode` refuses to do it, and a
test relying on that refusal would be testing PyJWT's signing guard rather than this
verifier.

**Not built:** no user table, no password storage, no refresh-token rotation. This
project has enough real cryptography without inventing credential storage, and a
half-built one would be the weakest thing in it. `argon2-cffi` remains a declared
dependency and is unused; it is listed here so the next reader does not conclude
there is a password path somewhere they have not found.

**Not verified:** no third-party identity provider has been integrated. The OIDC
path is tested against a locally generated RSA keypair with an injected JWKS lookup,
which exercises signature verification, the claim checks and the algorithm
restriction — but "works against Entra/Keycloak" is not a claim this project has
earned.

## Consequences

- The dashboard (Stage 11) can be generated from a committed OpenAPI schema, and
  `scripts/export_openapi.py --check` fails CI when a route changes without a
  re-export.
- Anything needing the lake stays in the CLI. If deep verification or record proofs
  are wanted over HTTP later, they belong behind a submitted job and a polled
  result, never a blocking GET.
- The API cannot be the cause of a data-integrity incident, because it cannot write.
  It can still be the cause of a *disclosure* incident, which is why the role ladder
  guards quarantine contents, lineage payloads and the key-release audit rather than
  treating all reads as equivalent.
