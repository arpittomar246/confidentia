"""Ingestion state: batches, watermarks, rejects.

Operator-level rather than viewer-level. A batch digest plus a source path is
enough to describe where a tenant's data comes from and how much of it there is,
which is more than a dashboard viewer needs to see a quality score.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from confidentia.api.deps import (
    DatasetQuery,
    PageDep,
    TenantQuery,
    fetch_all,
    fetch_one,
    optional_filters,
    paginated,
)
from confidentia.api.security import OperatorDep

router = APIRouter(prefix="/ingestion", tags=["ingestion"], dependencies=[OperatorDep])


@router.get("/batches", summary="Ingest batches and their deduplication state")
def batches(
    page: PageDep,
    tenant: TenantQuery = None,
    dataset: DatasetQuery = None,
    status: Annotated[str | None, Query(max_length=32)] = None,
) -> dict[str, Any]:
    """``seen_count`` above 1 is the exactly-once guarantee working, not an error.

    A batch whose content digest has been presented several times was ingested once
    and skipped thereafter. Surfacing the count makes redelivery visible instead of
    silent, which matters because silent redelivery and silent data loss look
    identical from a row count.
    """
    where, params = optional_filters(
        {"tenant": tenant, "dataset": dataset, "status::text": status}
    )
    rows = fetch_all(
        f"""
        SELECT batch_digest, source, tenant, dataset, file_count, row_count, bytes,
               status::text AS status, first_seen_at, ingested_at, seen_count, run_id
          FROM platform.ingest_batch WHERE 1=1 {where}
         ORDER BY first_seen_at DESC LIMIT %s OFFSET %s
        """,
        [*params, page.limit, page.offset],
    )
    total = fetch_one(f"SELECT count(*) AS n FROM platform.ingest_batch WHERE 1=1 {where}", params)
    return paginated(rows, page, total=int(total["n"]) if total else None)


@router.get("/watermarks", summary="Current watermark per source and dataset")
def watermarks(tenant: TenantQuery = None) -> dict[str, Any]:
    """``watermark_kind`` is returned because the value alone is ambiguous.

    An LSN, a timestamp and a file sequence are all opaque strings in this column,
    and comparing two of different kinds is meaningless -- which is why the kind is
    a discriminator on the row rather than something a client infers from the shape.
    """
    where, params = optional_filters({"tenant": tenant})
    return {
        "items": fetch_all(
            f"""
            SELECT source, tenant, dataset, watermark_kind::text AS watermark_kind,
                   watermark_value, previous_value, updated_at, run_id
              FROM platform.watermark WHERE 1=1 {where}
             ORDER BY updated_at DESC
            """,
            params,
        )
    }


@router.get("/summary", summary="Rows and bytes ingested per tenant and dataset")
def summary(tenant: TenantQuery = None) -> dict[str, Any]:
    where, params = optional_filters({"tenant": tenant})
    return {
        "items": fetch_all(
            f"""
            SELECT tenant, dataset, source,
                   count(*) AS batches,
                   count(*) FILTER (WHERE seen_count > 1) AS redelivered,
                   sum(row_count) AS rows_ingested,
                   sum(bytes) AS bytes_ingested,
                   max(ingested_at) AS latest_ingest
              FROM platform.ingest_batch
             WHERE status::text = 'ingested' {where}
             GROUP BY 1, 2, 3 ORDER BY rows_ingested DESC NULLS LAST
            """,
            params,
        )
    }
