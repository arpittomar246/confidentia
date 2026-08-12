"""The disclosure-controlled Gold marts.

These are the only endpoints that read *data* rather than metadata, and they are
the reason the API reads Postgres at all: ``delta-rs`` cannot decrypt Parquet
Modular Encryption, so a JVM-less Python process cannot query Bronze, Silver or
Gold in the lake. Gold therefore publishes aggregates into the ``gold`` schema, and
those aggregates have already been through k-anonymity suppression (C4).

**A NULL cell here is usually suppression, not missing data.** Complementary
suppression blanks cells specifically so that a small cell cannot be recovered by
subtracting the published ones from a published total. Any client that "fixes" a
NULL by back-computing it from a margin has undone the control. The response says
so on every payload rather than in documentation nobody reads.

Mart tables are ``DROP``ped and recreated on every publish with column types
inferred from the frame, so their schemas are not fixed at migration time. That is
why this router discovers them from ``information_schema`` instead of declaring
response models -- a hard-coded schema would silently drift from what Gold writes.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path
from psycopg import sql

from confidentia.api.deps import PageDep, fetch_all, fetch_all_composed, fetch_one, paginated
from confidentia.api.errors import NotFoundError
from confidentia.api.security import ViewerDep

router = APIRouter(prefix="/marts", tags=["marts"], dependencies=[ViewerDep])

MART_SCHEMA = "gold"

SUPPRESSION_NOTE = (
    "NULL cells may be k-anonymity suppression rather than missing data. Do not "
    "recover them by subtracting published cells from a published total."
)


def _known_marts() -> set[str]:
    return {
        row["table_name"]
        for row in fetch_all(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (MART_SCHEMA,),
        )
    }


def _resolve(name: str) -> str:
    """Turn a request-supplied name into one this process has just seen in the catalogue.

    Membership in the live catalogue is the check that matters; the returned value
    is still passed through ``sql.Identifier`` at the call site.
    """
    if name not in _known_marts():
        raise NotFoundError(f"no mart {name!r} in schema {MART_SCHEMA!r}")
    return name


@router.get("", summary="Published marts, with their disclosure record")
def list_marts() -> dict[str, Any]:
    """Row counts come from ``count(*)`` per table, not from ``reltuples``.

    ``pg_class.reltuples`` is an estimate maintained by ANALYZE, and these tables
    are dropped and recreated on every publish -- so the estimate is frequently -1
    or stale by a whole run. There are six marts of a few thousand rows; an exact
    count is cheap and a wrong one is confusing.
    """
    items = []
    for name in sorted(_known_marts()):
        count = fetch_all_composed(
            sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(MART_SCHEMA, name))
        )
        disclosure = fetch_one(
            """
            SELECT published_at, run_id, tenant, min_cell_count, total_cells,
                   primary_suppressed, complementary_suppressed,
                   differential_privacy, epsilon, rows_published
              FROM platform.disclosure_log WHERE mart = %s
             ORDER BY published_at DESC LIMIT 1
            """,
            (name,),
        )
        items.append({"mart": name, "rows": int(count[0]["n"]), "disclosure": disclosure})
    return {"items": items, "note": SUPPRESSION_NOTE}


@router.get("/{name}/schema", summary="Columns of one mart")
def mart_schema(name: Annotated[str, Path(max_length=63)]) -> dict[str, Any]:
    resolved = _resolve(name)
    return {
        "mart": resolved,
        "columns": fetch_all(
            """
            SELECT column_name, data_type, is_nullable
              FROM information_schema.columns
             WHERE table_schema = %s AND table_name = %s
             ORDER BY ordinal_position
            """,
            (MART_SCHEMA, resolved),
        ),
    }


@router.get("/{name}", summary="Rows of one mart")
def read_mart(name: Annotated[str, Path(max_length=63)], page: PageDep) -> dict[str, Any]:
    """No ORDER BY.

    Mart schemas differ and are not known at write time, so there is no column this
    router could order by that is guaranteed to exist. An unordered paged read is
    honest about that; inventing an order from the first column would produce pages
    that shift under the client when the mart is republished. Clients that need a
    stable order should fetch the mart whole -- they are thousands of rows, not
    millions -- or sort client-side.
    """
    resolved = _resolve(name)
    rows = fetch_all_composed(
        sql.SQL("SELECT * FROM {} LIMIT %s OFFSET %s").format(
            sql.Identifier(MART_SCHEMA, resolved)
        ),
        (page.limit, page.offset),
    )
    total = fetch_all_composed(
        sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(MART_SCHEMA, resolved))
    )
    body = paginated(rows, page, total=int(total[0]["n"]))
    body["mart"] = resolved
    body["note"] = SUPPRESSION_NOTE
    return body
