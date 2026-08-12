"""Endpoint tests against a real PostgreSQL schema.

Marked ``integration`` because they need the migrations applied. They deliberately
do **not** need data: an empty platform schema still catches the defect these tests
exist for, which is a column name that does not exist. Every query in the API is a
literal SQL string, so a typo is invisible until the statement runs -- and a
dashboard is exactly where nobody notices a 500 on a panel they rarely open.

The role matrix is asserted here rather than in the unit tests because what matters
is that the guard is wired to the route, not that the guard function works. A
correct ``require_role`` attached to nothing protects nothing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from confidentia.api.app import create_app
from confidentia.api.security import Role, issue_local_token

pytestmark = [pytest.mark.integration]


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture(scope="module")
def auth() -> dict[str, dict[str, str]]:
    return {
        role.value: {"Authorization": f"Bearer {issue_local_token(role.value, [role])}"}
        for role in Role
    }


# Every authenticated route, at the role that should be able to reach it. The list
# is explicit rather than derived from `app.routes` on purpose: a route added
# without a test is meant to show up as a gap here, and a derived list would
# silently absorb it.
READ_PATHS = [
    ("viewer", "/runs?limit=1"),
    ("viewer", "/runs/stats"),
    ("viewer", "/quality/scores?limit=1"),
    ("viewer", "/quality/scores/latest"),
    ("viewer", "/quality/rules?limit=1"),
    ("viewer", "/quality/failures?limit=1"),
    ("viewer", "/quality/freshness"),
    ("viewer", "/evaluation/latest"),
    ("viewer", "/evaluation/summary"),
    ("viewer", "/evaluation/coverage"),
    ("viewer", "/gate/blocked"),
    ("viewer", "/lineage/head"),
    ("viewer", "/lineage/latest"),
    ("viewer", "/marts"),
    ("operator", "/quality/quarantine?limit=1"),
    ("operator", "/quality/quarantine/summary"),
    ("operator", "/gate/decisions?limit=1"),
    ("operator", "/gate/overrides"),
    ("operator", "/ingestion/batches?limit=1"),
    ("operator", "/ingestion/watermarks"),
    ("operator", "/ingestion/summary"),
    ("operator", "/lineage/entries?limit=1"),
    ("operator", "/lineage/verify"),
    ("auditor", "/audit/key-releases?limit=1"),
    ("auditor", "/audit/key-releases/summary"),
    ("auditor", "/audit/attestation-policy"),
    ("auditor", "/audit/disclosure?limit=1"),
    ("auditor", "/audit/restatements?limit=1"),
]


@pytest.mark.parametrize(("role", "path"), READ_PATHS)
def test_every_query_runs_against_the_real_schema(
    client: TestClient, auth: dict[str, dict[str, str]], role: str, path: str
) -> None:
    response = client.get(path, headers=auth[role])
    assert response.status_code == 200, response.text


# --------------------------------------------------------------- unauthenticated


@pytest.mark.parametrize("path", ["/health", "/ready", "/meta"])
def test_probes_need_no_token(client: TestClient, path: str) -> None:
    """A probe behind auth turns an identity-provider outage into a platform outage."""
    assert client.get(path).status_code in {200, 503}


def test_meta_reports_the_tee_label(client: TestClient) -> None:
    """The dashboard must be able to tell a modelled enclave from a real one."""
    body = client.get("/meta").json()
    assert body["tee"]["label"] in {"MODELLED", "MEASURED"}
    assert body["tee"]["hardware_rooted"] is (body["tee"]["label"] == "MEASURED")


def test_missing_token_is_401_with_a_challenge(client: TestClient) -> None:
    response = client.get("/runs")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "unauthenticated"


def test_garbage_token_is_401_not_500(client: TestClient) -> None:
    assert client.get("/runs", headers={"Authorization": "Bearer nonsense"}).status_code == 401


# ------------------------------------------------------------------------ RBAC


@pytest.mark.parametrize(
    ("role", "path"),
    [
        ("viewer", "/quality/quarantine"),
        ("viewer", "/ingestion/batches"),
        ("viewer", "/audit/key-releases"),
        ("operator", "/audit/key-releases"),
        ("operator", "/audit/attestation-policy"),
        ("operator", "/lineage/entries/1"),
        ("operator", "/quality/quarantine/1"),
    ],
)
def test_insufficient_role_is_403(
    client: TestClient, auth: dict[str, dict[str, str]], role: str, path: str
) -> None:
    response = client.get(path, headers=auth[role])
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "forbidden"


def test_auditor_reaches_everything(
    client: TestClient, auth: dict[str, dict[str, str]]
) -> None:
    for _role, path in READ_PATHS:
        assert client.get(path, headers=auth["auditor"]).status_code == 200, path


# ------------------------------------------------------------------- refusals


def test_writes_are_refused(client: TestClient, auth: dict[str, dict[str, str]]) -> None:
    """Read-only is enforced by middleware, not merely by the absence of routes."""
    response = client.post("/runs", headers=auth["auditor"])
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "read_only"


def test_malformed_uuid_is_a_client_error(
    client: TestClient, auth: dict[str, dict[str, str]]
) -> None:
    """Not a 500: an invalid uuid literal reaching Postgres would raise one."""
    response = client.get("/runs/definitely-not-a-uuid", headers=auth["viewer"])
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_unknown_run_is_404(client: TestClient, auth: dict[str, dict[str, str]]) -> None:
    response = client.get(
        "/runs/00000000-0000-0000-0000-000000000000", headers=auth["viewer"]
    )
    assert response.status_code == 404


def test_a_mart_outside_the_gold_schema_is_not_reachable(
    client: TestClient, auth: dict[str, dict[str, str]]
) -> None:
    """`pg_class` exists and is readable; it is not in the gold schema, so it is 404."""
    assert client.get("/marts/pg_class", headers=auth["viewer"]).status_code == 404


def test_page_size_is_clamped(client: TestClient, auth: dict[str, dict[str, str]]) -> None:
    """Clamped rather than rejected, so a client asking for too much still gets data."""
    body = client.get("/runs?limit=100000", headers=auth["viewer"]).json()
    from confidentia.config.settings import get_settings

    assert body["page"]["limit"] == get_settings().api.max_page_size


def test_invalid_query_parameter_uses_the_shared_error_envelope(
    client: TestClient, auth: dict[str, dict[str, str]]
) -> None:
    """FastAPI's default validation body has a different shape from every other error."""
    response = client.get("/runs?limit=0", headers=auth["viewer"])
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_run_list_does_not_leak_error_messages(
    client: TestClient, auth: dict[str, dict[str, str]]
) -> None:
    """A failure message can echo a fragment of the offending record."""
    body = client.get("/runs?limit=5", headers=auth["viewer"]).json()
    for item in body["items"]:
        assert "error_message" not in item


def test_lineage_verify_declares_what_it_did_not_check(
    client: TestClient, auth: dict[str, dict[str, str]]
) -> None:
    """A passing shallow verification must not read as proof the lake still matches."""
    body = client.get("/lineage/verify", headers=auth["operator"]).json()
    assert body["scope"]["lake_contents_rehashed"] is False


def test_evaluation_carries_the_duplicate_record_caveat(
    client: TestClient, auth: dict[str, dict[str, str]]
) -> None:
    """Bronze cannot tell a duplicate from a redelivery; the number needs the caveat."""
    body = client.get("/evaluation/latest", headers=auth["viewer"]).json()
    assert any(c["applies_to"]["family"] == "duplicate_record" for c in body["caveats"])


def test_mart_payloads_warn_about_suppression(
    client: TestClient, auth: dict[str, dict[str, str]]
) -> None:
    """A NULL cell is usually k-anonymity suppression, and must not be back-computed."""
    body = client.get("/marts", headers=auth["viewer"]).json()
    assert "suppression" in body["note"]
