"""Prometheus metrics for the API and for the platform behind it.

Two families, and the second is the one worth having.

**HTTP metrics** describe this process: requests, statuses, latency. Every
service has them and they say nothing about whether the pipeline is healthy.

**Platform metrics** are read from the same PostgreSQL tables the API serves --
quality scores, gate decisions, key-release refusals, quarantine counts, freshness
lag, chain length. They are the numbers a person would actually be paged about, and
reading them from the pipeline's own tables is what keeps them honest: a dashboard
that renders is not evidence that anything is measured correctly, so **no gauge here
is computed from anything except a table the pipeline writes** (the same rule as
ADR 0016).

Two implementation details that are easy to get wrong and both bite in production.

*Route templates, not paths.* The HTTP labels use ``/runs/{run_id}``, never
``/runs/6f1c...``. Labelling by raw path gives Prometheus one time series per UUID
ever requested, which is the classic way to take down a monitoring stack with a
metric intended to protect it.

*Cached collection.* Platform gauges cost eight aggregate queries. Prometheus
scrapes every 15 seconds, and these values change when a pipeline run finishes --
minutes apart. Collecting on every scrape would put a permanent read load on the
metadata store to observe numbers that had not moved.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from confidentia.common.logging import get_logger
from confidentia.config.settings import Settings, get_settings

if TYPE_CHECKING:  # pragma: no cover
    from prometheus_client import CollectorRegistry

logger = get_logger(__name__)

# Buckets tuned to what this API actually does. Measured p50s at Stage 10 ranged
# from 2.6 ms (/health) to 21.8 ms (/quality/quarantine over 31k rows), so the
# default Prometheus buckets -- which start at 5 ms and are mostly above 100 ms --
# would put nearly every request in the first bucket and tell us nothing.
LATENCY_BUCKETS = (0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)


class Metrics:
    """The registry and its collectors.

    An instance per application rather than the process-global default registry:
    the test suite builds several applications in one process, and a global
    registry raises ``Duplicated timeseries`` on the second.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        from prometheus_client import CollectorRegistry, Counter, Histogram

        self._settings = settings or get_settings()
        self.registry: CollectorRegistry = CollectorRegistry()

        self.requests = Counter(
            "confidentia_http_requests_total",
            "HTTP requests by route template, method and status.",
            ["method", "route", "status"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "confidentia_http_request_duration_seconds",
            "HTTP request duration by route template.",
            ["method", "route"],
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        # Deliberately separate from the status="401"/"403" counter above. Those
        # are HTTP outcomes; this is a security signal, and someone alerting on a
        # burst of refusals should not have to know which status code the API
        # happens to use for it.
        self.auth_failures = Counter(
            "confidentia_auth_failures_total",
            "Requests refused for want of authentication or role.",
            ["reason"],
            registry=self.registry,
        )

        self._platform = _PlatformCollector(self._settings)
        self.registry.register(self._platform)

    def observe(self, *, method: str, route: str, status: int, duration_s: float) -> None:
        self.requests.labels(method=method, route=route, status=str(status)).inc()
        self.latency.labels(method=method, route=route).observe(duration_s)
        if status == 401:
            self.auth_failures.labels(reason="unauthenticated").inc()
        elif status == 403:
            self.auth_failures.labels(reason="forbidden").inc()

    def render(self) -> tuple[bytes, str]:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        return generate_latest(self.registry), CONTENT_TYPE_LATEST


def route_template(request: Any) -> str:
    """The matched route's path template, or a constant for anything unmatched.

    ``request.scope["route"]`` is set by Starlette once routing has happened. A
    request that matched nothing has no route, and using its raw path would let an
    unauthenticated scanner create unbounded label cardinality by requesting
    ``/aaa``, ``/aab``, ``/aac``... so unmatched requests collapse to one series.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    return "<unmatched>"


# --------------------------------------------------------------- platform gauges

# One query per metric family, each reading a table the pipeline writes. The
# cardinality of every label set here is bounded by the number of datasets (about
# 16 tenant/dataset/layer combinations), not by anything a client controls.
PLATFORM_QUERIES: dict[str, dict[str, Any]] = {
    "confidentia_dq_score": {
        "help": "Latest overall data-quality score per dataset and layer.",
        "labels": ("tenant", "dataset", "layer"),
        "sql": """
            SELECT DISTINCT ON (tenant, dataset, layer)
                   tenant, dataset, layer, overall_score AS value
              FROM platform.dq_score
             ORDER BY tenant, dataset, layer, scored_at DESC
        """,
    },
    "confidentia_promotion_blocked": {
        "help": "1 where the quality gate is currently blocking promotion.",
        "labels": ("tenant", "dataset", "layer"),
        "sql": """
            SELECT tenant, dataset, layer, 1 AS value
              FROM platform.promotion_blocked_now
        """,
    },
    "confidentia_freshness_lag_minutes": {
        "help": "Minutes between the newest event and now, per dataset.",
        "labels": ("tenant", "dataset", "layer"),
        "sql": """
            SELECT tenant, dataset, layer, lag_minutes AS value
              FROM platform.dq_freshness
        """,
    },
    "confidentia_quarantine_records": {
        "help": "Quarantined rows awaiting replay, by reason.",
        "labels": ("tenant", "dataset", "reason_code"),
        "sql": """
            SELECT tenant, dataset, reason_code, count(*) AS value
              FROM platform.quarantine_record
             WHERE replayed_at IS NULL
             GROUP BY 1, 2, 3
        """,
    },
    "confidentia_key_release_total": {
        "help": "Key-release requests by outcome. Refusals are the interesting series.",
        "labels": ("tenant", "granted"),
        "sql": """
            SELECT tenant, granted::text AS granted, count(*) AS value
              FROM platform.key_release_audit
             GROUP BY 1, 2
        """,
    },
    "confidentia_runs_total": {
        "help": "Recorded pipeline runs by type and status.",
        "labels": ("run_type", "status"),
        "sql": """
            SELECT run_type::text AS run_type, status::text AS status, count(*) AS value
              FROM platform.run_summary
             GROUP BY 1, 2
        """,
    },
    "confidentia_lineage_entries": {
        "help": "Length of the signed provenance chain.",
        "labels": (),
        "sql": "SELECT count(*) AS value FROM platform.lineage_entry",
    },
    "confidentia_rule_f1": {
        "help": (
            "Micro-averaged F1 of the quality rules against the ground-truth answer "
            "key, EXCLUDING the contaminated Bronze duplicate_record family."
        ),
        "labels": ("tenant", "dataset", "layer"),
        "sql": """
            WITH flagged AS (
                SELECT *, (layer = 'bronze' AND family = 'duplicate_record') AS contaminated
                  FROM platform.dq_evaluation_latest
            )
            SELECT tenant, dataset, layer,
                   CASE WHEN 2 * tp + fp + fn > 0
                        THEN (2.0 * tp) / (2 * tp + fp + fn) END AS value
              FROM (
                SELECT tenant, dataset, layer,
                       sum(true_positives)  FILTER (WHERE NOT contaminated) AS tp,
                       sum(false_positives) FILTER (WHERE NOT contaminated) AS fp,
                       sum(false_negatives) FILTER (WHERE NOT contaminated) AS fn
                  FROM flagged GROUP BY 1, 2, 3
              ) counted
        """,
    },
}


class _PlatformCollector:
    """A Prometheus custom collector over the platform tables.

    A collector rather than gauges updated on a timer: Prometheus pulls, and a
    background thread writing gauges would keep a connection pool busy in a process
    that might never be scraped.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cached: list[Any] = []
        self._collected_at = 0.0

    def collect(self) -> list[Any]:
        ttl = self._settings.api.metrics_cache_seconds
        now = time.monotonic()
        if self._cached and now - self._collected_at < ttl:
            return self._cached
        self._cached = self._collect_now()
        self._collected_at = now
        return self._cached

    def _collect_now(self) -> list[Any]:
        from prometheus_client.core import GaugeMetricFamily

        from confidentia.api.deps import fetch_all

        families: list[Any] = []
        for name, spec in PLATFORM_QUERIES.items():
            labels: tuple[str, ...] = spec["labels"]
            family = GaugeMetricFamily(name, spec["help"], labels=list(labels))
            try:
                rows = fetch_all(spec["sql"])
            except Exception as exc:  # noqa: BLE001 - a scrape must not 500
                # A degraded metadata store should make the *platform* metrics
                # vanish, not the HTTP ones -- a monitoring endpoint that returns
                # nothing at all during an outage is a monitoring endpoint that is
                # useless exactly when it is needed.
                logger.warning("platform_metric_unavailable", metric=name, error=str(exc))
                continue
            for row in rows:
                value = row.get("value")
                if value is None:
                    continue
                family.add_metric([str(row.get(label, "")) for label in labels], float(value))
            families.append(family)
        return families
