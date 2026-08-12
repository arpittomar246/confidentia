"""Data-quality scores, the rule catalogue, per-rule results, freshness, quarantine.

The role split here is the one place in this API where RBAC does visible work.
Scores and rules are viewer-level: they are counts and ratios about the data.
Quarantine *records* are not -- ``redacted_record`` holds the offending row, and
"redacted" means sensitive fields were removed, not that the remainder is public.
Listing quarantine is operator-level and carries reason codes only; reading one
record's contents is auditor-level.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query

from confidentia.api.deps import (
    DatasetQuery,
    LayerQuery,
    PageDep,
    TenantQuery,
    fetch_all,
    fetch_one,
    optional_filters,
    paginated,
)
from confidentia.api.errors import NotFoundError
from confidentia.api.security import AuditorDep, OperatorDep, ViewerDep

router = APIRouter(prefix="/quality", tags=["quality"])


@router.get("/scores", summary="Dimension scores per dataset run", dependencies=[ViewerDep])
def scores(
    page: PageDep,
    tenant: TenantQuery = None,
    dataset: DatasetQuery = None,
    layer: LayerQuery = None,
) -> dict[str, Any]:
    where, params = optional_filters({"tenant": tenant, "dataset": dataset, "layer": layer})
    rows = fetch_all(
        f"""
        SELECT score_id, run_id, tenant, dataset, layer, overall_score,
               completeness, validity, consistency, uniqueness, integrity, timeliness,
               rows_total, rows_failing, rules_evaluated, rules_failed,
               promotion_blocked, scored_at
          FROM platform.dq_score WHERE 1=1 {where}
         ORDER BY scored_at DESC LIMIT %s OFFSET %s
        """,
        [*params, page.limit, page.offset],
    )
    total = fetch_one(f"SELECT count(*) AS n FROM platform.dq_score WHERE 1=1 {where}", params)
    return paginated(rows, page, total=int(total["n"]) if total else None)


@router.get("/scores/latest", summary="The most recent score per dataset", dependencies=[ViewerDep])
def latest_scores(tenant: TenantQuery = None) -> dict[str, Any]:
    """One row per (tenant, dataset, layer), chosen by ``DISTINCT ON``.

    Postgres-specific and deliberately so: the portable alternative is a window
    function over the whole table or a correlated subquery, and this table is
    written once per pipeline run forever. ``DISTINCT ON`` reads the index.
    """
    where, params = optional_filters({"tenant": tenant})
    return {
        "items": fetch_all(
            f"""
            SELECT DISTINCT ON (tenant, dataset, layer)
                   tenant, dataset, layer, overall_score, completeness, validity,
                   consistency, uniqueness, integrity, timeliness,
                   rows_total, rows_failing, rules_failed, promotion_blocked, scored_at
              FROM platform.dq_score WHERE 1=1 {where}
             ORDER BY tenant, dataset, layer, scored_at DESC
            """,
            params,
        )
    }


@router.get("/rules", summary="The rule catalogue", dependencies=[ViewerDep])
def rules(
    page: PageDep,
    tenant: TenantQuery = None,
    dataset: DatasetQuery = None,
    dimension: Annotated[str | None, Query(max_length=32)] = None,
    severity: Annotated[str | None, Query(max_length=16)] = None,
    active_only: bool = True,
) -> dict[str, Any]:
    where, params = optional_filters(
        {"tenant": tenant, "dataset": dataset, "dimension": dimension, "severity": severity}
    )
    if active_only:
        where += " AND is_active"
    rows = fetch_all(
        f"""
        SELECT rule_id, tenant, dataset, dimension, family, field, severity,
               description, is_derived, is_active, created_at
          FROM platform.dq_rule WHERE 1=1 {where}
         ORDER BY tenant, dataset, dimension, rule_id LIMIT %s OFFSET %s
        """,
        [*params, page.limit, page.offset],
    )
    total = fetch_one(f"SELECT count(*) AS n FROM platform.dq_rule WHERE 1=1 {where}", params)
    return paginated(rows, page, total=int(total["n"]) if total else None)


@router.get("/failures", summary="Rules that failed, worst first", dependencies=[ViewerDep])
def failures(
    page: PageDep,
    tenant: TenantQuery = None,
    dataset: DatasetQuery = None,
    layer: LayerQuery = None,
) -> dict[str, Any]:
    """Joined to ``dq_rule`` so a failure arrives with its description and severity.

    Without the join a client gets ``rule_id`` and a pass rate, and has to fetch the
    catalogue to render anything -- which every client would then do on every poll.
    """
    where, params = optional_filters(
        {"r.tenant": tenant, "r.dataset": dataset, "r.layer": layer}
    )
    rows = fetch_all(
        f"""
        SELECT r.result_id, r.run_id, r.rule_id, r.tenant, r.dataset, r.layer,
               r.rows_evaluated, r.rows_failed, r.pass_rate, r.evaluated_at,
               d.dimension, d.family, d.field, d.severity, d.description
          FROM platform.dq_result r
          LEFT JOIN platform.dq_rule d ON d.rule_id = r.rule_id
         WHERE r.rows_failed > 0 {where}
         ORDER BY r.rows_failed DESC, r.evaluated_at DESC LIMIT %s OFFSET %s
        """,
        [*params, page.limit, page.offset],
    )
    return paginated(rows, page)


@router.get("/freshness", summary="Lag against the freshness SLA", dependencies=[ViewerDep])
def freshness(tenant: TenantQuery = None, breached_only: bool = False) -> dict[str, Any]:
    where, params = optional_filters({"tenant": tenant})
    if breached_only:
        where += " AND breached"
    return {
        "items": fetch_all(
            f"""
            SELECT tenant, dataset, layer, max_event_ts, max_ingest_ts, observed_at,
                   lag_minutes, sla_minutes, breached
              FROM platform.dq_freshness WHERE 1=1 {where}
             ORDER BY breached DESC, lag_minutes DESC
            """,
            params,
        )
    }


@router.get("/quarantine", summary="Quarantined rows: reasons only", dependencies=[OperatorDep])
def quarantine(
    page: PageDep,
    tenant: TenantQuery = None,
    dataset: DatasetQuery = None,
    reason_code: Annotated[str | None, Query(max_length=64)] = None,
) -> dict[str, Any]:
    """``redacted_record`` is not selected here. See ``/quarantine/{id}``."""
    where, params = optional_filters(
        {"tenant": tenant, "dataset": dataset, "reason_code": reason_code}
    )
    rows = fetch_all(
        f"""
        SELECT quarantine_id, run_id, tenant, dataset, layer, business_key,
               batch_digest, reason_code, failed_rule, severity,
               quarantined_at, replayed_at
          FROM platform.quarantine_record WHERE 1=1 {where}
         ORDER BY quarantined_at DESC LIMIT %s OFFSET %s
        """,
        [*params, page.limit, page.offset],
    )
    total = fetch_one(
        f"SELECT count(*) AS n FROM platform.quarantine_record WHERE 1=1 {where}", params
    )
    return paginated(rows, page, total=int(total["n"]) if total else None)


@router.get(
    "/quarantine/summary", summary="Quarantine counts by reason", dependencies=[OperatorDep]
)
def quarantine_summary(tenant: TenantQuery = None) -> dict[str, Any]:
    where, params = optional_filters({"tenant": tenant})
    return {
        "items": fetch_all(
            f"""
            SELECT tenant, dataset, reason_code, severity, count(*) AS records,
                   count(*) FILTER (WHERE replayed_at IS NOT NULL) AS replayed,
                   max(quarantined_at) AS latest
              FROM platform.quarantine_record WHERE 1=1 {where}
             GROUP BY 1, 2, 3, 4 ORDER BY records DESC
            """,
            params,
        )
    }


@router.get(
    "/quarantine/{quarantine_id}",
    summary="One quarantined row, including its redacted contents",
    dependencies=[AuditorDep],
)
def quarantine_record(quarantine_id: Annotated[int, Path(ge=1)]) -> dict[str, Any]:
    """Auditor-only.

    ``redacted_record`` had its sensitive fields removed when it was written, which
    is not the same as being safe to show anyone: it is still a real row from a real
    tenant, and the reason it is here is that something about it was wrong.
    """
    row = fetch_one(
        """
        SELECT quarantine_id, run_id, tenant, dataset, layer, business_key,
               batch_digest, reason_code, failed_rule, severity, redacted_record,
               quarantined_at, replayed_at
          FROM platform.quarantine_record WHERE quarantine_id = %s
        """,
        (quarantine_id,),
    )
    if row is None:
        raise NotFoundError(f"no quarantine record {quarantine_id}")
    return row
