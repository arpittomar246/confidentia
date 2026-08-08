"""A stand-in source system exposing a paged REST API.

Real ingestion problems only appear against a real HTTP server: paging boundaries,
rate limits, transient 503s, slow responses, and clients that must resume from a
watermark rather than refetching everything. Mocking the transport would test the
mock.

Fault injection is configurable so the retry policy is *demonstrated* rather than
asserted:

    SOURCE_API_FAIL_RATE=0.25   # fraction of requests answered 503
    SOURCE_API_SLOW_RATE=0.10   # fraction delayed past the client's patience
    SOURCE_API_RATE_LIMIT=0     # requests/second, 0 disables

Run with:
    uvicorn confidentia.ingestion.source_api:app --port 8081
"""

from __future__ import annotations

import os
import random
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Response

from confidentia.common.logging import get_logger
from confidentia.config.settings import get_settings

logger = get_logger(__name__)

app = FastAPI(
    title="EHR Clinical Data API (source system stand-in)",
    version="1.0.0",
    description="Paged read API over the source-of-record clinical datasets.",
)

MAX_PAGE_SIZE = 5_000
_last_request_at = 0.0


def _fault_rate(name: str, default: float = 0.0) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@lru_cache(maxsize=16)
def _load(tenant: str, dataset: str) -> pd.DataFrame:
    """Load a generated dataset once and serve pages from memory.

    Cached because the point of this service is to exercise the *client*; re-reading
    Parquet per request would measure the server's disk, not the collector.
    """
    root = get_settings().data_root / "raw" / tenant / dataset
    parts = sorted(root.glob("part-*.parquet"))
    if not parts:
        raise HTTPException(status_code=404, detail=f"no data for {tenant}/{dataset}")
    frame = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    # Serve in event order so that a timestamp watermark is a valid resume point.
    for column in ("event_ts", "admit_ts", "created_at", "filed_ts"):
        if column in frame.columns:
            return frame.sort_values(column).reset_index(drop=True)
    return frame


def _inject_faults() -> None:
    global _last_request_at

    limit = _fault_rate("SOURCE_API_RATE_LIMIT", 0.0)
    if limit > 0:
        min_gap = 1.0 / limit
        elapsed = time.monotonic() - _last_request_at
        if elapsed < min_gap:
            # 429 with Retry-After is what a real API does, and the client is
            # expected to honour it rather than hammering.
            raise HTTPException(
                status_code=429,
                detail="rate limit exceeded",
                headers={"Retry-After": f"{min_gap - elapsed:.2f}"},
            )
    _last_request_at = time.monotonic()

    if random.random() < _fault_rate("SOURCE_API_FAIL_RATE"):
        raise HTTPException(status_code=503, detail="upstream temporarily unavailable")

    if random.random() < _fault_rate("SOURCE_API_SLOW_RATE"):
        time.sleep(random.uniform(1.5, 4.0))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/{tenant}/{dataset}")
def read_dataset(
    tenant: str,
    dataset: str,
    response: Response,
    page: int = Query(1, ge=1),
    page_size: int = Query(1000, ge=1, le=MAX_PAGE_SIZE),
    since: str | None = Query(None, description="ISO timestamp; exclusive lower bound"),
) -> dict[str, Any]:
    """Return one page of records, optionally bounded below by a watermark."""
    _inject_faults()

    frame = _load(tenant, dataset)

    if since:
        time_column = next(
            (c for c in ("event_ts", "admit_ts", "created_at", "filed_ts") if c in frame.columns),
            None,
        )
        if time_column is None:
            raise HTTPException(status_code=400, detail=f"{dataset} has no time column to filter on")
        try:
            bound = pd.Timestamp(since)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid `since`: {exc}") from exc
        if bound.tz is None:
            bound = bound.tz_localize("UTC")
        # Strictly greater than: an inclusive bound would re-deliver the boundary
        # record on every incremental run.
        frame = frame[frame[time_column] > bound]

    total = len(frame)
    start = (page - 1) * page_size
    window = frame.iloc[start : start + page_size]

    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Page"] = str(page)

    return {
        "tenant": tenant,
        "dataset": dataset,
        "page": page,
        "page_size": page_size,
        "total": total,
        "returned": len(window),
        "has_more": start + len(window) < total,
        "items": window.to_dict(orient="records"),
    }


@app.get("/api/v1/{tenant}")
def list_datasets(tenant: str) -> dict[str, Any]:
    root: Path = get_settings().data_root / "raw" / tenant
    if not root.exists():
        raise HTTPException(status_code=404, detail=f"unknown tenant {tenant}")
    datasets = sorted(
        d.name for d in root.iterdir() if d.is_dir() and any(d.glob("part-*.parquet"))
    )
    return {"tenant": tenant, "datasets": datasets}
