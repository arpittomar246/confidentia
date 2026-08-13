# ADR 0019 — What is worth measuring, and what OpenTelemetry would have added

- **Status:** Accepted
- **Date:** 2026-08-13
- **Stage:** 12 (observability)

## Context

`monitoring/` had been an empty directory since Stage 1, bind-mounted by two
compose services. The brief named Prometheus, Grafana and OpenTelemetry.

The failure mode for a stage like this is specific and common: a stack that renders
convincingly and measures nothing anyone acts on. So each decision below is against
one test — **would a person do something about this number?**

## Decision 1 — Two metric families, and the platform one is the point

**HTTP metrics** describe this process: requests, statuses, latency. Every service
has them and they say nothing about whether the pipeline is healthy.

**Platform metrics** are gauges read from the tables the pipeline already writes —
quality scores, gate decisions, key-release refusals, quarantine depth, freshness
lag, chain length, rule F1. These are the numbers someone would be paged about.

The constraint that keeps them honest is the same as ADR 0016: **no gauge is
computed from anything except a table the pipeline writes.** Each entry in
`PLATFORM_QUERIES` is one SQL statement against `platform.*`, so a number on the
dashboard can be traced to a row. A metric derived in the exporter would be a
metric the CLI, the API and Grafana could eventually disagree about.

`confidentia_rule_f1` is the sharpest case. It exports the F1 **excluding** the
contaminated Bronze `duplicate_record` family, because the raw figure is 0.251
against 0.891 on `financial/transaction` and measures the layer's blindness rather
than the rules. The same decision the API makes at `/evaluation/summary`, made the
same way, in SQL.

## Decision 2 — `/metrics` is authenticated by default

It carries per-dataset quality scores and per-tenant counts, which is exactly the
class of data `/gate/blocked` guards behind the viewer role. An unauthenticated
metrics endpoint on a published port is a way to read platform state the rest of
this API refuses to hand over, and "it's only metrics" is how that gets waved
through.

So `API_METRICS_PUBLIC` defaults to **false** and Prometheus is expected to present
a bearer token; `prometheus.yml` carries the commented `credentials_file` block
showing where it goes.

The compose stack sets it true, and that is a real trade recorded rather than
hidden: there the endpoint is part of a single-host demo scraped over the internal
network. The honest deployment answer is to put metrics on a port that is **not
published**, not to disable authentication on one that is.

## Decision 3 — Route templates, cached collection

Two implementation choices that are load-bearing rather than stylistic.

**Labels use the matched route template** (`/runs/{run_id}`), never the raw path.
Labelling by path gives Prometheus one time series per UUID ever requested, which is
the classic way to take down a monitoring stack with the metric intended to protect
it. Unmatched requests collapse to a single `<unmatched>` series so that an
unauthenticated scanner cannot create series at will. Both are asserted in tests.

**Platform gauges are cached for 30 seconds.** They cost eight aggregate queries,
Prometheus scrapes every 30 seconds, and the values move when a pipeline run
finishes — minutes apart. Collecting per scrape would put permanent read load on the
metadata store to observe numbers that had not changed. The scrape interval is set
to match the cache, so a faster scrape cannot silently return stale data twice.

**A failing platform query is logged and skipped, never raised.** A monitoring
endpoint that returns nothing during an outage is useless exactly when it is needed,
so a degraded metadata store makes the platform gauges disappear while the HTTP
metrics keep flowing — which is itself the signal that the store is the problem
rather than the process.

## Decision 4 — Alerts only where someone would act

Eight rules. There is no alert on CPU, memory or request rate. A rule that fires on
a number nobody acts on trains people to ignore the channel, and then the one that
mattered is ignored too.

`KeyReleaseRefusalBurst` is the one specific to this platform: a refusal is the
attestation mechanism working, but more than five in fifteen minutes almost always
means a measured package was edited without `confidentia keys approve`, and the
pipeline is failing closed — correct, and also not processing anything.

`PromotionBlockedTooLong` fires after an hour, not immediately, because blocked *is*
the gate working; the alert is that nobody has acted.

`ApiErrorRate` counts 5xx only. A 401 or 403 is the API refusing correctly and is
counted separately by `confidentia_auth_failures_total` — alerting on 4xx would page
someone every time a viewer opened the audit module.

## Decision 5 — OpenTelemetry is not used, and the reason is not effort

`opentelemetry-api` and `opentelemetry-sdk` remain declared in the `observability`
extra and are **not imported anywhere**.

Distributed tracing answers "where did the time go in this request, across
services". This platform's expensive work is not request-shaped: it is Spark stages
running for tens of seconds, invoked by Airflow, and the platform **already records
every one of them** in `platform.run` with a duration, a status, a row count and a
run id that lineage and quality both reference. `confidentia_runs_total` and
`/runs/stats` read exactly that.

So tracing here would produce a second, less complete record of what
`platform.run_summary` already holds — a second source of truth, which is the thing
ADR 0016 exists to prevent — and it would need a collector and a backend to store
spans nobody would read. Adding an SDK that exports to nowhere so the report can say
"OpenTelemetry" is Rule 2 of the brief exactly.

It is named here rather than quietly omitted, in the same way `argon2-cffi` is named
in ADR 0016: a declared dependency that nothing imports should have a written reason.

**If tracing did become worthwhile**, the case would be the API under concurrent
load, where per-request database time is not visible in `run_summary`. That is not
today's problem: measured p95s are 3–22 ms.

## Consequences

- The `demo` profile comes up. `verify_docker.sh` asserts the scrape target is
  `up`, that a metric is queryable through Prometheus, and that Grafana's datasource
  and dashboard provisioned — not merely that the containers started.
- Dashboards and datasources are provisioned as code. A dashboard built in the UI
  lives in a volume, disappears on the first `down -v`, and cannot be reviewed in a
  diff.
- `prometheus-client` moved from the `observability` extra into `api`. It is a hard
  dependency of a route that is enabled by default; leaving it optional made the
  container image die on first boot with `ModuleNotFoundError`.
