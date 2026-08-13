# Stage 11 — The dashboard

Nine modules over the Stage 10 API, in React and TypeScript, with the API client's
paths and query parameters generated from the committed OpenAPI schema.

The interesting part of this stage was not building it. It was that **running it in
a browser found three defects that 72 passing backend tests could not**, and one of
them was a number the console was presenting dishonestly.

## What was built

`frontend/` — Vite 6, React 19, TanStack Query, Tailwind 4, React Router 7.

| Module | Reads | Minimum role |
|---|---|---|
| Overview | `/runs/stats`, `/quality/scores/latest`, `/gate/blocked`, `/evaluation/*`, `/lineage/head` | viewer |
| Pipeline runs | `/runs`, `/runs/{id}` | viewer |
| Data quality | `/quality/{scores,rules,failures,freshness,quarantine}` | viewer → auditor |
| Evaluation | `/evaluation/{latest,summary,coverage}` | viewer |
| Promotion gate | `/gate/{blocked,decisions,overrides}` | viewer / operator |
| Gold marts | `/marts`, `/marts/{name}`, `/marts/{name}/schema` | viewer |
| Ingestion | `/ingestion/{batches,watermarks,summary}` | operator |
| Lineage | `/lineage/{head,latest,entries,verify}` | operator / auditor |
| Audit | `/audit/{key-releases,attestation-policy,disclosure,restatements}` | auditor |

Bundle: **103 kB gzipped**, plus a 112 kB chart chunk loaded only on the Quality
module. Recharts more than doubled the initial bundle when statically imported, so
it is behind `React.lazy` — one chart does not justify making eight other modules
pay for it.

### The client is generated where it can be, and honest where it cannot

Paths, methods and **query parameters** come from `docs/api/openapi.json` via
`openapi-typescript` — including enums, so `tenant: "helthcare"` is a compile error.
CI regenerates the types and fails on a diff.

Response bodies are `unknown`, and that is a decision rather than a gap. The API's
handlers return rows straight from SQL; adding Pydantic `response_model`s would type
them at the cost of a worse failure, because `response_model` *filters* the payload
and a column added to a query but not to the model would disappear silently. So row
shapes live in `src/api/rows.ts` as declared expectations, and that file is the one
place in the frontend that can drift from the backend.

## The three defects the browser found

### 1. Every `numeric` column was arriving as a string

`"duration_ms": "2.901000"`. psycopg maps Postgres `numeric` to `Decimal`, and both
FastAPI and Pydantic serialise a `Decimal` as a **string** to preserve exact
precision. The console threw `toFixed is not a function` on the first sign-in.

Every backend test passed throughout, because a response full of stringified numbers
is still a 200 and every assertion was about status codes and payload keys.

Fixed in the API, not the client — `deps.py` now normalises `Decimal` at the query
boundary, integral values to `int` and the rest to `float`. That is a real precision
decision and it is documented as one: float64 covers millisecond durations, `sum()`
aggregates and ratios, and would **not** be safe for exact monetary values. There
are none — `publish_mart` types mart columns as BIGINT or DOUBLE PRECISION, never
NUMERIC. A regression test now asserts the seven affected fields are JSON numbers.

### 2. The Overview was publishing a contaminated metric as "rule quality"

The tile read **F1 0.251** for `financial/transaction`. The number was correct and
the presentation was not: at Bronze a duplicate and a legitimate redelivery are the
same bytes, so every redelivered row counts as a false positive for
`duplicate_record` — **19,855 of the 20,450** false positives on that dataset.
Excluding it, transaction scores 0.891.

Stage 7b found this and Stage 7c documented it; `/evaluation/latest` already carried
the caveat. `/evaluation/summary` did not, and the Overview reads the summary. So the
one number on the landing page was the one the project had twice written down as not
being a measure of rule quality.

Fixed by making the API return both, because a client that subtracted the
contaminated family itself would be computing a metric — exactly what ADR 0016
forbids, and the first client to forget would publish 0.251 as a result:

| Dataset | F1 raw | F1 excl. | Families excluded |
|---|---|---|---|
| financial/account | 0.992 | **0.991** | 1 |
| financial/transaction | 0.251 | **0.891** | 1 |
| healthcare/claim_line | 0.884 | **0.868** | 1 |
| healthcare/encounter | 0.919 | **0.910** | 1 |
| healthcare/patient | 0.998 | **0.998** | 1 |

Three of the five go *down* when the family is removed, because its true positives
go with it. The console shows the excluded figure as the headline and the raw one
beside it — hiding the raw number would be a different kind of dishonesty.

### 3. The suppression renderer mislabelled "not applicable" as "suppressed"

The mart viewer rendered every `null` as `suppressed`. On an *unsuppressed* row,
`_suppression_reason` is null because no suppression applied — so the column read
"suppressed" on rows that were not.

The mart already carries an explicit `_suppressed` boolean, so the fix was to read
what Gold recorded instead of inferring it from a null. Same conflation of "blank"
and "withheld" this module exists to prevent, running in the opposite direction.

## The five honesty requirements, verified in a browser

Checked against the live database (627 runs, 72 lineage entries, 6 marts), signed in
as an auditor:

| # | Requirement | Verified |
|---|---|---|
| 1 | `MODELLED` label visible on every page | Header banner: *"enclave behaviour is simulated… Nothing on this console is hardware-attested"* |
| 2 | Suppression rendered as suppression | `encounter_count` → `suppressed`, `_suppression_reason` → `below_k_5`, unsuppressed rows → `—` |
| 3 | `rule_exists = false` shown as uncovered, not as a low score | Own "no rule" state; tile reads **0** |
| 4 | `duplicate_record` caveat attached to the number | Caveat panel above the table; `F1*` columns are the decontaminated figures |
| 5 | `lake_contents_rehashed: false` visible | Verification panel: `✗ lake contents re-hashed` with the CLI command that does |

## Environment findings

**Node was not installed in the distro.** The `npm` on `PATH` was the *Windows* one
at `/mnt/c/Program Files/nodejs/npm`, inherited through WSL's PATH interop — the
exact trap `PROJECT_CONTEXT` §13 warned about. Node 22 is now installed inside
Ubuntu at `/usr/bin/node`.

**Windows Firewall blocks Node's listeners on this machine.** The Vite dev server is
reachable from inside WSL and invisible from Windows, on any port; the API on 8000
and a Python static server on 8001 are both fine. Python is allowed through the
firewall and Node is not. This stage was therefore verified by serving the
production build with `python -m http.server`, which is also how the missing SPA
history fallback surfaced. **The user must allow Node through Windows Firewall
before `npm run dev` is usable from a Windows browser.**

**Vite binds IPv6 `localhost` by default**, which WSL2 does not forward. `vite.config.ts`
now pins `host: "127.0.0.1"`. Not sufficient on its own here because of the firewall,
but it is a real bug on any WSL setup.

**WSL has 3.9 GB**, not the host's 7.4 GB. Vite + API + Postgres is comfortable;
Vite + Spark is not.

## Deployment

`docker/frontend/Dockerfile` — multi-stage, Node builds and nginx serves.

Two things in `nginx.conf` are load-bearing and both came from observed failures:

- `try_files $uri $uri/ /index.html`. React Router owns `/evaluation`, but a reload
  or a pasted link asks the *server* for it. Verifying against `python -m http.server`
  returned 404 on the first attempt to open `/evaluation` directly.
- `index.html` is `no-cache` while `/assets/` is immutable. A cached `index.html`
  names asset hashes the new build deleted; during this stage that produced a blank
  page and a 404 in the console, and cost a confusing ten minutes.

nginx proxies `/api` to the api service, so the containerised console is same-origin
and CORS is not involved. `VITE_API_BASE_URL` is deliberately **not** set in compose:
Vite inlines it at image-build time, so setting it as a runtime environment variable
would silently do nothing — which is the sort of thing that looks like it works until
someone changes it.

## CI

New job **`frontend`**: `npm ci`, regenerate the API types, `git diff --exit-code` on
the generated file, then `npm run build`. The diff check is what catches a backend
response shape that changed without the schema being re-exported.

There is **no frontend test suite**. `tsc -b` and a successful build are the whole
check, and the job comment says so rather than implying coverage that does not exist.

## Known gaps

1. **No frontend tests.** The three defects above were found by a human driving a
   browser, which is not a repeatable process. A Playwright smoke test that signs in
   and asserts each module renders would have caught all three.
2. **Token is lost on refresh.** In-memory by design — `localStorage` is
   XSS-readable — but there is no silent renewal, so a 30-minute token means signing
   in again every 30 minutes.
3. **The compose stack still cannot be brought up end to end.** `docker/frontend/`
   exists now; `docker/backend/`, `docker/spark/` and `docker/airflow/` do not.
   Stage 13.
4. **Only the Overview polls.** Everything else refreshes on navigation. There is no
   push channel, and adding one is an API change, not a console change.
5. **`rows.ts` can drift from the backend** — see the note on response typing. It is
   the deliberate cost of not filtering payloads through `response_model`.

## Reproducing

```bash
confidentia api serve
cd frontend && npm install && npm run dev    # http://127.0.0.1:5173
confidentia api token me --role auditor      # paste into the sign-in screen
npm run build && npm run lint
```
