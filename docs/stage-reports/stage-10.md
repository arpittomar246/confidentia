# Stage 10 — The serving layer

Everything stages 1–9b built is real and, until this stage, reachable only from a
shell on the machine holding the keystore. Stage 10 puts an HTTP surface over it:
**36 read-only endpoints**, bearer authentication, a three-role ladder, and a
committed OpenAPI schema for Stage 11 to generate a client from.

The interesting content of this stage is the four things the API is *not* allowed
to do. Those are ADR 0016; this report is what was built and what was measured.

## What was built

`src/confidentia/api/` — 1,832 lines across nine routers, security, deps and the
application factory.

| Router | Endpoints | Reads |
|---|---|---|
| `health` | 3 | nothing / `SELECT 1` / settings |
| `runs` | 3 | `platform.run_summary` |
| `quality` | 8 | `dq_score`, `dq_rule`, `dq_result`, `dq_freshness`, `quarantine_record` |
| `evaluation` | 3 | `dq_evaluation_latest` |
| `gate` | 3 | `promotion_gate`, `promotion_blocked_now` |
| `ingestion` | 3 | `ingest_batch`, `watermark` |
| `lineage` | 5 | `lineage_entry`, `lineage_latest`, `lineage_head` |
| `audit` | 5 | `key_release_audit`, `attestation_policy`, `disclosure_log`, `restatement_log` |
| `marts` | 3 | `gold.*`, discovered from `information_schema` |

Placed in `src/confidentia/api/` rather than a top-level `backend/`, which is what
the plan said. It imports `confidentia.store.db` and `confidentia.config.settings`,
so it can never ship independently of the package; inside `src/` it is covered by
mypy's `files = ["src"]`, by coverage, and by test imports with no path handling.
`docker/backend/Dockerfile` keeps its name.

## The role ladder

`viewer < operator < auditor`, resolved from the real dependency tree:

```
$ confidentia api routes
method  path                                         role
GET     /audit/key-releases                          auditor
GET     /gate/blocked                                viewer
GET     /quality/quarantine                          operator
GET     /quality/quarantine/{quarantine_id}          auditor
...
```

17 viewer, 11 operator, 5 auditor, 3 public (plus the four FastAPI built-ins).

`require_role` tags each guard with the role it enforces and `api routes` reads that
tag back, so the listing cannot drift from what is actually enforced. A
hand-maintained table of routes and roles is a table that goes stale silently, and
the failure mode is a route everyone believes is guarded.

**Where the split falls, and why:**

- Scores, rules, evaluation results, gate status and the marts are **viewer**. They
  are counts and ratios about data.
- Ingestion state, quarantine *reason codes*, gate decision history and the lineage
  entry list are **operator**. A batch digest plus a source path describes where a
  tenant's data comes from.
- The key-release audit, attestation policy, lineage *payloads and signatures*, and
  quarantine *record contents* are **auditor**. `redacted_record` had its sensitive
  fields removed, which is not the same as being safe to show anyone.

Since the API is read-only, this is a disclosure ladder rather than a capability
model — stated plainly in ADR 0016 rather than dressed up. `_RANK` is a mapping so
that when triggering exists, `operator` becomes a capability check instead of a rung.

## Authentication, and the attack it is arranged around

Two modes. `oidc` verifies RS256/ES256 against a provider's JWKS with `iss` and
`aud` both checked. `local` verifies HS256 tokens minted by `confidentia api token`,
and is refused outside `CONFIDENTIA_ENV=local` by a settings validator — an API that
issues and verifies its own tokens in a deployed environment has no authentication.

The permitted-algorithm list is asymmetric-only in OIDC mode. This is the
algorithm-confusion attack: the JWKS document is public, so an attacker takes the
RSA public key, uses its PEM bytes as an HMAC secret, and signs an auditor token
that a verifier accepting both RS256 and HS256 will validate.

The test for it forges the token by hand with `hmac`/`base64`, because `jwt.encode`
**refuses** to use a PEM as an HMAC secret:

```
jwt.exceptions.InvalidKeyError: The specified key is an asymmetric key or x509
certificate and should not be used as an HMAC secret.
```

That refusal is a guard on the *signing* side. An attacker is not using PyJWT to
sign, so the first version of this test proved only that PyJWT protects the person
holding the key — not that this verifier rejects the forgery. Forging it manually is
what makes the test about the verifier.

## What was measured

**Every endpoint, against the live populated database** (621 runs, 150 scores, 398
evaluations, 72 lineage entries, 345 key-release audit rows, 30,951 quarantine
records, 6 marts). `scripts/verify_api.sh` starts a real uvicorn process and drives
it over HTTP:

```
unauthenticated
  ok    GET /health                                          200
  ok    GET /runs without a token                            401
  ok    GET /runs with a bad token                           401
the role ladder
  ok    viewer   -> /quality/quarantine                      403
  ok    operator -> /quality/quarantine                      200
  ok    operator -> /audit/key-releases                      403
  ok    auditor  -> /audit/key-releases                      200
refusals
  ok    POST /runs is read-only                              405
  ok    malformed uuid is 400                                400
  ok    a table outside gold is 404                          404
every endpoint, with data
  ok    /evaluation/coverage                                 0 row(s)
  ok    /lineage/latest                                     29 row(s)
  ok    /ingestion/watermarks                               51 row(s)
  ... 28 endpoints, all 200
```

`/evaluation/coverage` returning **0 rows** is Stage 7c's result holding: no defect
family is left without a rule.

**Latency**, in-process against the populated database, 20 samples after a warm-up:

| Endpoint | p50 | max |
|---|---|---|
| `/health` | 2.6 ms | 4.1 ms |
| `/gate/blocked` | 3.6 ms | 4.8 ms |
| `/quality/scores/latest` | 3.9 ms | 5.2 ms |
| `/evaluation/summary` | 3.9 ms | 5.4 ms |
| `/runs?limit=50` | 5.3 ms | 6.0 ms |
| `/marts/mart_encounters_daily?limit=100` | 5.6 ms | 6.8 ms |
| `/marts` | 10.4 ms | 13.1 ms |
| `/lineage/verify` | 14.2 ms | 43.4 ms |
| `/quality/quarantine?limit=50` | 21.8 ms | 23.1 ms |

These exclude network and ASGI server overhead — they are in-process TestClient
timings, so they measure the query and serialisation path, not a deployment.

Two are worth reading. `/lineage/verify` at 14 ms re-derives all 72 chain links and
verifies 72 ECDSA P-256 signatures; the shallow check is cheap enough to be a
dashboard tile. `/quality/quarantine` at 22 ms is the slowest, and it is the only
endpoint whose table has 30,951 rows; the `count(*)` it runs for pagination is the
likely cost. Not investigated further and no index added — 22 ms does not yet
justify either, and guessing at a plan without reading one is how indexes that help
nothing get committed.

## Tests

70 tests in `tests/api/` — 18 unit, 52 integration.

The integration tests run against a real PostgreSQL schema but need **no data**. An
empty schema still catches what they exist for: every query in this package is a
literal SQL string, so a column that does not exist is invisible until the statement
runs, and a dashboard panel nobody opens often is exactly where that 500 hides.

They caught nothing during this stage — the column names were taken from
`information_schema` before any query was written, and the first full endpoint pass
returned 200 on all 31 paths. Their value is regression, not discovery: `dq_score`
and `run_summary` will change again, and the next change gets a failing test rather
than a silent 500.

The role matrix is asserted at the HTTP layer rather than against `require_role`
directly. A correct guard attached to nothing protects nothing, and the property
worth testing is that the route has it.

Explicitly asserted, because each is a claim the payload makes:

- `/lineage/verify` reports `lake_contents_rehashed: false`
- `/evaluation/latest` carries the `duplicate_record` caveat
- `/marts` payloads warn that a NULL may be suppression
- `/runs` list responses do not contain `error_message`

## CI

- `unit-tests` and `integration-tests` now install the `api` extra.
- New job **`openapi-schema`**: regenerates the schema and fails if
  `docs/api/openapi.json` is stale. The schema is committed so Stage 11 can generate
  a typed client without standing up the API; a route changed without a re-export is
  precisely the drift a committed artefact should surface.
- `scripts/ci_local.sh` runs the same check and names `verify_api.sh` in its footer
  of things automated nowhere.

## What this stage deliberately did not do

**No trigger endpoints.** Airflow owns scheduling, retries and the gate.
`max_active_runs=1` is a per-DAG guarantee, not a per-machine one, so an API that
can start a Bronze write can start one while Airflow is running one. Writes are
refused by middleware with `405 read_only`, not merely by an absence of POST routes.

**No deep lineage verification or record proofs over HTTP.** Both need a Spark
session and minutes of wall clock. They stay in the CLI. When they arrive over HTTP
they belong behind a submitted job and a polled result.

**No user table, no passwords.** `argon2-cffi` is declared and unused; it is named
in ADR 0016 so the next reader does not go looking for a credential path.

**No identity provider integration.** The OIDC path is tested against a locally
generated RSA keypair with an injected JWKS lookup, which exercises signature
verification, the claim checks and the algorithm restriction. "Works against
Entra/Keycloak" is not a claim this project has earned, and is not made.

## Known gaps

1. **`docker/backend/Dockerfile` does not exist**, so the `api` compose service
   cannot be built. The service now carries the correct `command`, `API_HOST` and a
   healthcheck for when it does. `docker compose config` validates syntax and
   interpolation only, so `compose-validate` passes regardless — that job asserts
   less than its name suggests, and the same is true of the `spark`, `airflow` and
   `frontend` services. Stage 13.
2. **No rate limiting.** An unauthenticated caller can drive `/health` and `/ready`
   as fast as they like, and `/ready` opens a connection each time.
3. **No index behind `/quality/quarantine`.** See the latency table.
4. **`/marts/{name}` has no `ORDER BY`.** Mart schemas are inferred at publish time
   and differ, so there is no column guaranteed to exist. Pages can therefore shift
   under a client if a mart is republished mid-scroll; the honest fix is a stable
   sort key in `MartSpec`, which is a Gold change, not an API one.

## Reproducing

```bash
confidentia api serve                  # http://127.0.0.1:8000/docs
confidentia api token me --role auditor
confidentia api routes                 # every route and the role it needs
bash scripts/verify_api.sh             # needs a populated database
pytest tests/api -q
python scripts/export_openapi.py --check
```
