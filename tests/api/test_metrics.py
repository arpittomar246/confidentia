"""The Prometheus endpoint.

The tests that matter here are about **cardinality and access**, not about whether
a counter increments. A metrics endpoint fails in production two ways: it leaks
data the rest of the API guards, or it creates unbounded label cardinality and
takes the monitoring stack down. Both are asserted.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from confidentia.api.app import create_app
from confidentia.api.metrics import PLATFORM_QUERIES
from confidentia.api.security import Role, issue_local_token
from confidentia.config.settings import Settings, get_settings

pytestmark = [pytest.mark.integration]


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture(scope="module")
def viewer() -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_local_token('metrics', [Role.VIEWER])}"}


def test_metrics_requires_a_role_by_default(client: TestClient) -> None:
    """`/metrics` carries per-dataset scores -- the same class `/gate/blocked` guards."""
    assert get_settings().api.metrics_public is False
    assert client.get("/metrics").status_code == 401


def test_a_viewer_can_scrape(client: TestClient, viewer: dict[str, str]) -> None:
    response = client.get("/metrics", headers=viewer)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


def test_metrics_can_be_made_public_for_a_scraper(monkeypatch: pytest.MonkeyPatch) -> None:
    """What the compose stack sets, so the setting must actually work."""
    monkeypatch.setenv("API_METRICS_PUBLIC", "true")
    get_settings.cache_clear()
    try:
        public = TestClient(create_app(Settings()), raise_server_exceptions=False)
        assert public.get("/metrics").status_code == 200
    finally:
        monkeypatch.delenv("API_METRICS_PUBLIC", raising=False)
        get_settings.cache_clear()


def test_every_platform_family_is_exposed(client: TestClient, viewer: dict[str, str]) -> None:
    """Each gauge maps to one SQL query against a table the pipeline writes.

    An empty database still emits the family names, so this holds in CI where no
    pipeline has ever run.
    """
    body = client.get("/metrics", headers=viewer).text
    for name in PLATFORM_QUERIES:
        assert f"# HELP {name}" in body or f"# TYPE {name}" in body, f"{name} missing"


def test_route_labels_are_templates_not_paths(
    client: TestClient, viewer: dict[str, str]
) -> None:
    """The cardinality bomb.

    Labelling by `request.url.path` gives one time series per UUID ever requested,
    which is the classic way to take down a monitoring stack with the metric meant
    to protect it.
    """
    client.get("/runs/6f1c2d3e-0000-0000-0000-000000000000", headers=viewer)
    body = client.get("/metrics", headers=viewer).text
    assert 'route="/runs/{run_id}"' in body
    assert "6f1c2d3e" not in body


def test_unmatched_paths_collapse_to_one_series(
    client: TestClient, viewer: dict[str, str]
) -> None:
    """Otherwise an unauthenticated scanner can create series at will."""
    for path in ("/aaa", "/aab", "/aac"):
        client.get(path)
    body = client.get("/metrics", headers=viewer).text
    assert 'route="<unmatched>"' in body
    assert 'route="/aaa"' not in body


def test_refusals_are_counted_separately_from_http_status(
    client: TestClient, viewer: dict[str, str]
) -> None:
    """A 403 is the access control working, not an error.

    Someone alerting on a burst of refusals should not need to know which status
    code the API happens to use for it.
    """
    client.get("/audit/key-releases", headers=viewer)  # viewer cannot: 403
    client.get("/runs")  # no token: 401
    body = client.get("/metrics", headers=viewer).text
    assert 'confidentia_auth_failures_total{reason="forbidden"}' in body
    assert 'confidentia_auth_failures_total{reason="unauthenticated"}' in body


def test_platform_gauges_survive_a_broken_query(
    client: TestClient, viewer: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A degraded metadata store must not take the HTTP metrics with it.

    A monitoring endpoint that returns nothing during an outage is useless exactly
    when it is needed, so a failing platform query is logged and skipped rather
    than raised.
    """
    from confidentia.api import metrics as metrics_module

    def explode(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        raise RuntimeError("metadata store is gone")

    monkeypatch.setattr("confidentia.api.deps.fetch_all", explode)
    # The collector caches; a fresh app gets a fresh collector.
    fresh = TestClient(create_app(), raise_server_exceptions=False)
    body = fresh.get("/metrics", headers=viewer).text
    assert "confidentia_http_requests_total" in body
    assert metrics_module.PLATFORM_QUERIES  # the families are configured...
    assert "confidentia_dq_score{" not in body  # ...and simply absent while broken
