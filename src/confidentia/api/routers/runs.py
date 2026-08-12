"""Pipeline run history, from ``platform.run_summary``.

``run_summary`` is a view over ``platform.run`` that already computes duration and
rejection rate. This router queries the view rather than the table for exactly the
reason ADR 0016 gives: if the API divided ``records_rejected`` by ``records_read``
itself, the CLI and the dashboard would eventually disagree about what a rejection
rate is.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query

from confidentia.api.deps import (
    DatasetQuery,
    PageDep,
    TenantQuery,
    as_uuid,
    fetch_all,
    fetch_one,
    optional_filters,
    paginated,
)
from confidentia.api.errors import NotFoundError
from confidentia.api.security import ViewerDep

router = APIRouter(prefix="/runs", tags=["runs"], dependencies=[ViewerDep])

_COLUMNS = """
    run_id, run_type::text AS run_type, source, tenant, dataset, status::text AS status,
    attempt, started_at, finished_at, duration_ms, records_read, records_written,
    records_rejected, rejection_rate, error_type
"""


@router.get("", summary="Recent pipeline runs")
def list_runs(
    page: PageDep,
    tenant: TenantQuery = None,
    dataset: DatasetQuery = None,
    status: Annotated[str | None, Query(max_length=32)] = None,
    run_type: Annotated[str | None, Query(max_length=32)] = None,
) -> dict[str, Any]:
    """Newest first. ``error_message`` is deliberately omitted from the list.

    A failure message can carry a fragment of the offending record -- a malformed
    row echoed back by a driver, for instance -- and the list endpoint is the one a
    dashboard polls. It is available on the single-run endpoint, where fetching it
    is a deliberate act.
    """
    where, params = optional_filters(
        {
            "tenant": tenant,
            "dataset": dataset,
            "status::text": status,
            "run_type::text": run_type,
        }
    )
    rows = fetch_all(
        f"SELECT {_COLUMNS} FROM platform.run_summary WHERE 1=1 {where} "
        "ORDER BY started_at DESC LIMIT %s OFFSET %s",
        [*params, page.limit, page.offset],
    )
    total = fetch_one(
        f"SELECT count(*) AS n FROM platform.run_summary WHERE 1=1 {where}", params
    )
    return paginated(rows, page, total=int(total["n"]) if total else None)


@router.get("/stats", summary="Run counts and durations by status")
def run_stats(tenant: TenantQuery = None) -> dict[str, Any]:
    """Aggregated in SQL, not in Python.

    The alternative -- fetching runs and grouping them here -- would put a second
    definition of "failed" in the codebase and would page 621 rows over the wire to
    produce six numbers.
    """
    where, params = optional_filters({"tenant": tenant})
    return {
        "by_status": fetch_all(
            f"""
            SELECT status::text AS status, count(*) AS runs,
                   percentile_disc(0.5) WITHIN GROUP (ORDER BY duration_ms) AS median_duration_ms,
                   max(duration_ms) AS max_duration_ms,
                   sum(records_written) AS records_written
              FROM platform.run_summary WHERE 1=1 {where}
             GROUP BY 1 ORDER BY 2 DESC
            """,
            params,
        ),
        "by_type": fetch_all(
            f"""
            SELECT run_type::text AS run_type, count(*) AS runs,
                   count(*) FILTER (WHERE status::text = 'failed') AS failed
              FROM platform.run_summary WHERE 1=1 {where}
             GROUP BY 1 ORDER BY 2 DESC
            """,
            params,
        ),
    }


@router.get("/{run_id}", summary="One run, including its error message")
def get_run(run_id: Annotated[str, Path(description="Run UUID")]) -> dict[str, Any]:
    row = fetch_one(
        f"SELECT {_COLUMNS}, error_message FROM platform.run_summary WHERE run_id = %s",
        (as_uuid(run_id, field="run_id"),),
    )
    if row is None:
        raise NotFoundError(f"no run {run_id}")
    return row
