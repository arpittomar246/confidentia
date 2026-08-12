"""Audit trails: key release, attestation policy, disclosure control, restatement.

``key_release_audit`` records every key request and, importantly, every refusal.
It is auditor-only. Note what it does **not** contain and what this router does not
add: ``key_ids`` are identifiers of the form ``tenant/sensitivity/version``, never
key material. The API is not in ``MEASURED_PACKAGES``, holds no keystore passphrase
and never calls the broker -- if an endpoint here needed a DEK, the design would be
wrong (ADR 0016).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from confidentia.api.deps import (
    PageDep,
    TenantQuery,
    fetch_all,
    fetch_one,
    optional_filters,
    paginated,
)
from confidentia.api.security import AuditorDep, OperatorDep

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/key-releases", summary="Key grants and refusals", dependencies=[AuditorDep])
def key_releases(
    page: PageDep,
    tenant: TenantQuery = None,
    granted: bool | None = None,
) -> dict[str, Any]:
    """Refusals are the interesting rows, so they are not filtered out by default.

    An audit log that only records successes cannot answer the question it exists
    for. ``granted=false`` isolates the four refusal paths: unknown measurement,
    revoked policy, tenant not permitted, and stale or replayed evidence.
    """
    where, params = optional_filters({"tenant": tenant})
    if granted is not None:
        where += " AND granted = %s"
        params.append(granted)
    rows = fetch_all(
        f"""
        SELECT audit_id, requested_at, measurement, tenant, key_ids, granted, reason,
               evidence_digest, run_id, tee_backend, hardware_rooted
          FROM platform.key_release_audit WHERE 1=1 {where}
         ORDER BY requested_at DESC LIMIT %s OFFSET %s
        """,
        [*params, page.limit, page.offset],
    )
    total = fetch_one(
        f"SELECT count(*) AS n FROM platform.key_release_audit WHERE 1=1 {where}", params
    )
    return paginated(rows, page, total=int(total["n"]) if total else None)


@router.get("/key-releases/summary", summary="Grants and refusals by reason", dependencies=[AuditorDep])
def key_release_summary() -> dict[str, Any]:
    return {
        "items": fetch_all(
            """
            SELECT tenant, granted, reason, tee_backend, hardware_rooted,
                   count(*) AS requests, max(requested_at) AS latest
              FROM platform.key_release_audit
             GROUP BY 1, 2, 3, 4, 5 ORDER BY requests DESC
            """
        )
    }


@router.get("/attestation-policy", summary="Which measurements may hold which keys",
            dependencies=[AuditorDep])
def attestation_policy() -> dict[str, Any]:
    """``hardware_rooted`` is returned per row and must be rendered.

    Under the ``sim`` backend a measurement is a hash of this repository's source,
    not an SGX MRENCLAVE. The policy row carries that distinction so a client cannot
    present a simulated attestation as a hardware-rooted one.
    """
    return {
        "items": fetch_all(
            """
            SELECT measurement, description, tenants, approved_by, approved_at,
                   revoked_at, tee_backend, hardware_rooted, notes
              FROM platform.attestation_policy ORDER BY approved_at DESC
            """
        )
    }


@router.get("/disclosure", summary="Suppression applied to each published mart",
            dependencies=[OperatorDep])
def disclosure(page: PageDep, tenant: TenantQuery = None) -> dict[str, Any]:
    """How much of each mart C4 blanked, and why.

    ``complementary_suppressed`` is the count that matters: primary suppression
    alone leaves a small cell recoverable by subtracting the published cells from a
    published total, so a mart with primary suppression and no complementary
    suppression has usually not been protected.
    """
    where, params = optional_filters({"tenant": tenant})
    rows = fetch_all(
        f"""
        SELECT disclosure_id, published_at, run_id, tenant, mart, min_cell_count,
               total_cells, primary_suppressed, complementary_suppressed,
               differential_privacy, epsilon, rows_published
          FROM platform.disclosure_log WHERE 1=1 {where}
         ORDER BY published_at DESC LIMIT %s OFFSET %s
        """,
        [*params, page.limit, page.offset],
    )
    return paginated(rows, page)


@router.get("/restatements", summary="Marts recomputed after late-arriving data",
            dependencies=[OperatorDep])
def restatements(page: PageDep, tenant: TenantQuery = None) -> dict[str, Any]:
    where, params = optional_filters({"tenant": tenant})
    rows = fetch_all(
        f"""
        SELECT restatement_id, restated_at, run_id, tenant, mart, partitions_restated,
               trigger_reason, oldest_partition, newest_partition, lag_days
          FROM platform.restatement_log WHERE 1=1 {where}
         ORDER BY restated_at DESC LIMIT %s OFFSET %s
        """,
        [*params, page.limit, page.offset],
    )
    return paginated(rows, page)
