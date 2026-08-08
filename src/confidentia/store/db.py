"""PostgreSQL access for the platform metadata store.

Deliberately psycopg 3 directly rather than an ORM. The metadata store is written
almost exclusively by pipeline code doing single-row upserts and counter updates
on a hot path, and read by the API with hand-tuned queries. An ORM would add a
session lifecycle to reason about inside Spark tasks and Airflow operators for no
benefit, and would obscure the ``ON CONFLICT`` semantics that idempotency depends
on.

A connection pool is used because collectors open and close connections per batch,
and Postgres connection setup is expensive enough to show up in ingestion latency
at small batch sizes.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from confidentia.common.logging import get_logger
from confidentia.config.settings import Settings, get_settings

logger = get_logger(__name__)

_pools: dict[str, ConnectionPool] = {}
_pool_lock = threading.Lock()


def _dsn(settings: Settings, database: str | None) -> str:
    return settings.postgres.dsn(database, driver="postgresql")


def get_pool(database: str | None = None, settings: Settings | None = None) -> ConnectionPool:
    """One pool per database, created lazily and shared process-wide."""
    settings = settings or get_settings()
    name = database or settings.postgres.database
    with _pool_lock:
        pool = _pools.get(name)
        if pool is None:
            pool = ConnectionPool(
                conninfo=_dsn(settings, name),
                min_size=1,
                max_size=settings.postgres.pool_size,
                # Fail fast rather than hanging a pipeline task on a dead database.
                timeout=10.0,
                open=True,
                kwargs={"row_factory": dict_row},
            )
            _pools[name] = pool
            logger.debug("pool_opened", database=name, max_size=settings.postgres.pool_size)
        return pool


@contextmanager
def connection(database: str | None = None) -> Iterator[psycopg.Connection]:
    """A pooled connection. Commits on clean exit, rolls back on exception."""
    pool = get_pool(database)
    with pool.connection() as conn:
        yield conn


@contextmanager
def cursor(database: str | None = None) -> Iterator[psycopg.Cursor]:
    with connection(database) as conn, conn.cursor() as cur:
        yield cur


def fetch_all(sql: str, params: Any = None, *, database: str | None = None) -> list[dict[str, Any]]:
    with cursor(database) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(sql: str, params: Any = None, *, database: str | None = None) -> dict[str, Any] | None:
    with cursor(database) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def execute(sql: str, params: Any = None, *, database: str | None = None) -> int:
    with cursor(database) as cur:
        cur.execute(sql, params)
        return cur.rowcount


def close_pools() -> None:
    with _pool_lock:
        for name, pool in list(_pools.items()):
            pool.close()
            _pools.pop(name, None)
            logger.debug("pool_closed", database=name)


def healthcheck(database: str | None = None) -> bool:
    try:
        return fetch_one("SELECT 1 AS ok", database=database) is not None
    except Exception as exc:  # noqa: BLE001 - a health check must not raise
        logger.warning("database_unreachable", database=database, error=str(exc))
        return False
