# Stage 12 — Observability

`monitoring/` had been an empty directory since Stage 1, bind-mounted by two compose
services that could never start. This fills it: a Prometheus scrape endpoint on the
API, eight platform gauges read from the pipeline's own tables, eight alert rules,
and a provisioned Grafana dashboard.

The trap for a stage like this is a stack that renders convincingly and measures
nothing anyone acts on. Every decision below was made against one test: **would a
person do something about this number?**

## What was built

| | |
|---|---|
| `src/confidentia/api/metrics.py` | Registry, HTTP collectors, platform collector |
| `src/confidentia/api/routers/observability.py` | `/metrics`, authenticated by default |
| `monitoring/prometheus/prometheus.yml` | Scrape config |
| `monitoring/prometheus/alerts.yml` | 8 rules |
| `monitoring/grafana/datasources/prometheus.yml` | Datasource as code |
| `monitoring/grafana/dashboards/` | Provider + an 11-panel dashboard |
| `tests/api/test_metrics.py` | 8 tests |

## The metrics that matter come from the pipeline's own tables

HTTP metrics describe the process. The interesting family is the platform gauges:

| Metric | Source table |
|---|---|
| `confidentia_dq_score` | `platform.dq_score` |
| `confidentia_promotion_blocked` | `platform.promotion_blocked_now` |
| `confidentia_freshness_lag_minutes` | `platform.dq_freshness` |
| `confidentia_quarantine_records` | `platform.quarantine_record` |
| `confidentia_key_release_total` | `platform.key_release_audit` |
| `confidentia_runs_total` | `platform.run_summary` |
| `confidentia_lineage_entries` | `platform.lineage_entry` |
| `confidentia_rule_f1` | `platform.dq_evaluation_latest` |

Each is one SQL statement, so any number on a dashboard traces to a row. **Verified
against the tables rather than assumed:**

```
  lineage entries                        metric=72.0          table=72.0          OK
  blocked datasets                       metric=6             table=6             OK
  dq_score financial/account/bronze      metric=0.9965631082  table=0.9965631082  OK
  key releases refused                   metric=7.0           table=7.0           OK
  rule_f1 transaction (excl duplicates)  metric=0.891         expect 0.891
```

That last one is the sharpest case. `confidentia_rule_f1` exports F1 **excluding**
the contaminated Bronze `duplicate_record` family — 0.891 rather than the raw
0.251 — the same decision `/evaluation/summary` makes, made the same way in SQL. A
dashboard showing 0.251 as "rule quality" would repeat the exact mistake Stage 11
found and fixed.

## `/metrics` is authenticated by default

It carries per-dataset scores and per-tenant counts — the same class of data
`/gate/blocked` guards behind the viewer role. `API_METRICS_PUBLIC` defaults to
false; `prometheus.yml` carries a commented `credentials_file` block showing where
the token goes.

Compose sets it true, and that is a recorded trade rather than a hidden one: there
it is a single-host demo scraped over the internal network. The honest deployment
answer is metrics on an **unpublished** port, not authentication disabled on a
published one.

## Two things that would have broken this in production

**Route templates, not paths.** Labels use `/runs/{run_id}`, never
`/runs/6f1c2d3e-...`. Labelling by raw path gives Prometheus one time series per
UUID ever requested — the classic way to take down a monitoring stack with the
metric meant to protect it. Unmatched paths collapse to a single `<unmatched>`
series so an unauthenticated scanner cannot create series by requesting `/aaa`,
`/aab`, `/aac`. Both are asserted:

```python
assert 'route="/runs/{run_id}"' in body
assert "6f1c2d3e" not in body
```

**A degraded database must not take the HTTP metrics with it.** A failing platform
query is logged and skipped, never raised — so during an outage the platform gauges
vanish while `confidentia_http_requests_total` keeps flowing, which is itself the
signal that the store is the problem rather than the process. A monitoring endpoint
that returns nothing during an outage is useless exactly when it is needed.

## Alerts only where someone would act

Eight rules, and no alert on CPU, memory or request rate. A rule that fires on a
number nobody acts on trains people to ignore the channel.

- **`KeyReleaseRefusalBurst`** — the one specific to this platform. A refusal is the
  attestation mechanism working; more than five in fifteen minutes almost always
  means a measured package was edited without `confidentia keys approve`, and the
  pipeline is failing closed.
- **`PromotionBlockedTooLong`** fires after an hour, not immediately — blocked *is*
  the gate working, and the alert is that nobody acted.
- **`FreshnessSLABreached`** is the one that catches a pipeline that has silently
  stopped; every other metric looks healthy when no runs happen at all.
- **`ApiErrorRate`** counts 5xx only. A 403 is the API refusing correctly and is
  counted separately by `confidentia_auth_failures_total` — alerting on 4xx would
  page someone every time a viewer opened the audit module.

## Verified running, not just written

`scripts/verify_docker.sh` now asserts the whole path:

```
monitoring
  ok    prometheus healthy
  ok    alert rules loaded (8 rules)
  ok    prometheus is scraping the API
  ok    platform metrics queryable through prometheus
  ok    grafana healthy
  ok    grafana datasource provisioned
  ok    grafana dashboard provisioned
```

The scrape target must be `up` and a metric must be **queryable through
Prometheus** — not merely that the containers started. Two defects surfaced doing
this:

1. **The scrape returned 404.** The running `api` container predated the metrics
   router. Obvious in hindsight; invisible without checking the target's health.
2. **The rebuilt container died with `ModuleNotFoundError: prometheus_client`.**
   The image installs `[api,storage]` and `prometheus-client` lived in
   `observability`. It is a hard dependency of a route enabled by default, so it
   moved into the `api` extra.

## OpenTelemetry is deliberately not used

`opentelemetry-api` and `opentelemetry-sdk` stay declared and are imported nowhere.

Tracing answers "where did the time go across services". This platform's expensive
work is not request-shaped — it is Spark stages running for tens of seconds — and
**every one is already recorded** in `platform.run` with a duration, a status, a row
count and a run id that lineage and quality both reference.

So tracing would produce a second, less complete record of what
`platform.run_summary` already holds — a second source of truth, the thing ADR 0016
exists to prevent — and would need a collector and a backend to store spans nobody
reads. Adding an SDK that exports to nowhere so the report can say "OpenTelemetry"
is Rule 2 of the brief exactly.

If tracing became worthwhile it would be for the API under concurrent load, where
per-request database time is not visible in `run_summary`. Measured p95s are 3–22 ms,
so that is not today's problem.

## Known gaps

1. **No Alertmanager.** Rules evaluate and fire in Prometheus; nothing routes them
   to email, Slack or a pager. Adding Alertmanager without a destination would be
   another container that renders and does nothing.
2. **The dashboard has not been viewed against a populated database in a
   container.** The compose Postgres starts empty, so panels were verified by
   querying Prometheus rather than by looking at rendered charts. Values were
   checked against the real database through the native API, above.
3. **No recording rules.** Every panel computes from raw series. At this cardinality
   — roughly 16 dataset/layer combinations — that is fine and pre-aggregating would
   be optimising nothing.
4. **`confidentia_runs_total` is a gauge over a `count(*)`**, not a counter. It
   cannot be used with `rate()` across a database restore, and `increase()` on it
   would be wrong. The alert on refusals uses `increase()` and has the same caveat.

## Reproducing

```bash
confidentia api serve   # then: curl -H "Authorization: Bearer $(confidentia api token me --role viewer)" localhost:8000/metrics
bash scripts/verify_docker.sh --no-build     # brings up core + monitoring and checks the scrape path
PROMETHEUS_PORT=19090 GRAFANA_PORT=13000 docker compose --profile demo up -d
pytest tests/api/test_metrics.py -q
```
