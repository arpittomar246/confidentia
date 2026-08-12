"""The promotion gate: what it stopped, and why.

``platform.promotion_blocked_now`` is a view, and this router queries it rather
than reproducing its logic. That is not a style preference: the view exists because
"currently blocked" is a definition (the latest decision per dataset, not
overridden), and a second copy of that definition in the API is how the dashboard
and the CLI come to disagree about whether a dataset is blocked.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

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
from confidentia.api.security import OperatorDep, ViewerDep

router = APIRouter(prefix="/gate", tags=["gate"])


@router.get("/blocked", summary="Datasets the gate is blocking right now", dependencies=[ViewerDep])
def blocked(tenant: TenantQuery = None) -> dict[str, Any]:
    where, params = optional_filters({"tenant": tenant})
    items = fetch_all(
        f"""
        SELECT tenant, dataset, layer, overall_score, threshold, decision, reason,
               overridden, decided_at
          FROM platform.promotion_blocked_now WHERE 1=1 {where}
         ORDER BY overall_score ASC
        """,
        params,
    )
    return {"items": items, "blocked_count": len(items)}


@router.get("/decisions", summary="Every gate decision, newest first", dependencies=[OperatorDep])
def decisions(
    page: PageDep,
    tenant: TenantQuery = None,
    dataset: DatasetQuery = None,
    layer: LayerQuery = None,
    blocked_only: bool = False,
) -> dict[str, Any]:
    where, params = optional_filters({"tenant": tenant, "dataset": dataset, "layer": layer})
    if blocked_only:
        where += " AND blocked"
    rows = fetch_all(
        f"""
        SELECT gate_id, run_id, score_id, tenant, dataset, layer, overall_score,
               threshold, blocked, decision, reason, overridden, decided_at
          FROM platform.promotion_gate WHERE 1=1 {where}
         ORDER BY decided_at DESC LIMIT %s OFFSET %s
        """,
        [*params, page.limit, page.offset],
    )
    total = fetch_one(
        f"SELECT count(*) AS n FROM platform.promotion_gate WHERE 1=1 {where}", params
    )
    return paginated(rows, page, total=int(total["n"]) if total else None)


@router.get("/overrides", summary="Decisions that were overridden", dependencies=[OperatorDep])
def overrides(page: PageDep) -> dict[str, Any]:
    """A gate nobody can override is a gate that gets disabled the first time it is
    wrong at an inconvenient moment. An override is therefore permitted and
    recorded -- and this endpoint exists so that "how often is the gate overridden"
    is a question with an answer.
    """
    rows = fetch_all(
        """
        SELECT gate_id, run_id, tenant, dataset, layer, overall_score, threshold,
               decision, reason, decided_at
          FROM platform.promotion_gate WHERE overridden
         ORDER BY decided_at DESC LIMIT %s OFFSET %s
        """,
        (page.limit, page.offset),
    )
    return paginated(rows, page)
