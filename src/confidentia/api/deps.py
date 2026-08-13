"""Shared request dependencies: paging, common filters, and database access.

Every query in this package goes through ``fetch_all``/``fetch_one`` here rather
than calling ``store.db`` directly, for two reasons. A dead database must produce
503 (the dependency failed) and not 500 (the API is broken) -- the difference
decides whether an on-call engineer looks at Postgres or at this process. And slow
queries need to be visible without an APM, so the duration is measured in one
place and logged when it crosses ``api.slow_query_ms``.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any, cast

import psycopg
from fastapi import Depends, Query
from psycopg.sql import Composable

from confidentia.api.errors import DependencyUnavailableError
from confidentia.common.logging import get_logger
from confidentia.config.settings import Layer, Tenant, get_settings
from confidentia.store import db

logger = get_logger(__name__)


def _run(fn: Any, sql: Any, params: Any) -> Any:
    started = time.perf_counter()
    try:
        return fn(sql, params)
    except psycopg.Error as exc:
        # psycopg raises the same base class for "connection refused" and for
        # "syntax error in SQL". Only the first is a dependency failure; a bad
        # statement is this package's bug and must not be dressed up as one.
        if isinstance(exc, psycopg.OperationalError):
            logger.error("database_unavailable", error=str(exc))
            raise DependencyUnavailableError("the metadata store did not answer") from exc
        raise
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000
        if elapsed_ms > get_settings().api.slow_query_ms:
            # Composables have no .split(); repr is enough to identify the query.
            text = sql if isinstance(sql, str) else repr(sql)
            logger.warning(
                "slow_query", duration_ms=round(elapsed_ms, 1), sql=" ".join(text.split())[:200]
            )


def _json_number(value: Any) -> Any:
    """Turn a Postgres ``numeric`` into a JSON number.

    psycopg maps ``numeric`` to ``Decimal``, and both FastAPI and Pydantic serialise
    a ``Decimal`` as a **string** to preserve exact precision. The result is that
    ``duration_ms`` arrives as ``"2.901000"`` and every consumer has to parse it --
    which the dashboard did not, and found out in the browser rather than in a test,
    because a response full of stringified numbers is still a 200.

    Integral values become ``int`` and the rest become ``float``. That is a real
    precision decision, not a formatting one: float64 carries 15-17 significant
    digits, which covers every ``numeric`` this API serves -- millisecond durations,
    row-count aggregates from ``sum()``, and ratios in [0, 1]. It would **not** be
    safe for exact monetary values, and there are none here: ``publish_mart`` types
    mart columns as BIGINT or DOUBLE PRECISION and never NUMERIC. If a monetary
    ``numeric`` column is ever served, it needs a string and an explicit contract,
    not this.
    """
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def _normalise(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: _json_number(value) for key, value in row.items()} for row in rows]


def fetch_all(sql: str, params: Any = None) -> list[dict[str, Any]]:
    return _normalise(_run(db.fetch_all, sql, params))


def fetch_all_composed(statement: Composable, params: Any = None) -> list[dict[str, Any]]:
    """Run a statement built with ``psycopg.sql`` composables.

    Exists for the mart reader, the only endpoint whose SQL contains an identifier
    that came from the request. That identifier is checked against
    ``information_schema`` first *and* quoted by ``sql.Identifier`` here -- either
    alone would be sufficient, and doing both means a future edit that drops one
    check is not immediately exploitable.
    """

    def _execute(stmt: Any, prm: Any) -> list[dict[str, Any]]:
        with db.cursor() as cur:
            cur.execute(stmt, prm)
            # The pool sets dict_row, which psycopg's stubs do not model.
            return cast("list[dict[str, Any]]", cur.fetchall())

    return _normalise(_run(_execute, statement, params))


def fetch_one(sql: str, params: Any = None) -> dict[str, Any] | None:
    row = _run(db.fetch_one, sql, params)
    return None if row is None else _normalise([row])[0]


@dataclass(frozen=True)
class Page:
    limit: int
    offset: int


def paging(
    limit: Annotated[int | None, Query(ge=1, description="Rows to return")] = None,
    offset: Annotated[int, Query(ge=0, description="Rows to skip")] = 0,
) -> Page:
    """Limit/offset paging, clamped to ``api.max_page_size``.

    Clamped rather than rejected: a client asking for 10,000 rows gets the maximum
    and a correct ``page.limit`` in the response telling it what it actually got,
    which is easier to handle than a 422 mid-scroll. ``platform.quarantine_record``
    holds ~31k rows, so an unbounded limit is a real memory risk, not a theoretical one.
    """
    api = get_settings().api
    requested = limit if limit is not None else api.default_page_size
    return Page(limit=min(requested, api.max_page_size), offset=offset)


PageDep = Annotated[Page, Depends(paging)]

TenantQuery = Annotated[Tenant | None, Query(description="Filter to one tenant")]
LayerQuery = Annotated[Layer | None, Query(description="Filter to one medallion layer")]
DatasetQuery = Annotated[str | None, Query(max_length=64, description="Filter to one dataset")]


def paginated(rows: list[dict[str, Any]], page: Page, total: int | None = None) -> dict[str, Any]:
    return {
        "items": rows,
        "page": {"limit": page.limit, "offset": page.offset, "returned": len(rows)},
        **({"total": total} if total is not None else {}),
    }


def optional_filters(columns: dict[str, Any]) -> tuple[str, list[Any]]:
    """Build ``AND col = %s`` fragments for whichever filters were supplied.

    Takes a dict rather than ``**kwargs`` because several of these columns are
    Postgres enums and must be compared as ``status::text`` -- not a Python
    identifier, so it cannot be a keyword argument.

    Column expressions are literals from the call site, never from the request;
    only the values become parameters. The one endpoint that takes an identifier
    from the client -- the mart reader -- resolves it against ``information_schema``
    first and never interpolates it.
    """
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in columns.items():
        if value is not None:
            clauses.append(f" AND {column} = %s")
            params.append(str(value))
    return "".join(clauses), params


def as_uuid(value: str, *, field: str = "id") -> str:
    """Validate a path parameter that reaches a ``uuid`` column.

    Without this, a malformed id reaches Postgres, raises ``invalid input syntax
    for type uuid``, and surfaces as a 500 -- reporting a server fault for what is
    a client mistake.
    """
    from uuid import UUID

    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        from confidentia.api.errors import BadRequestError

        raise BadRequestError(f"{field} must be a UUID, got {value!r}") from exc
