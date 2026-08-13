"""REST collector: paged, retrying, incremental.

Resumes from a **timestamp watermark** held in Postgres, so a second run fetches
only what appeared since the first. The bound is strictly exclusive on the server
side; an inclusive bound would redeliver the boundary record on every run and
quietly inflate every downstream count.

The watermark advances **only after the batch is durably written**. Advancing it
per page would mean a crash mid-batch loses the unwritten pages forever, since the
next run would start after them. This is the difference between at-least-once and
silently-lossy.

Error classification is the other load-bearing decision:

* 429 / 503 / 502 / 504 / timeouts / connection resets -> ``RetryableError``
* 400 / 401 / 403 / 404 / malformed payload            -> ``TerminalError``

Retrying a 401 just burns the rate limit; retrying a 503 is exactly right.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

import httpx
import pandas as pd

from confidentia.common.hashing import sha256_json
from confidentia.common.logging import get_logger
from confidentia.config.settings import get_settings
from confidentia.crypto.landing import LandingCrypto
from confidentia.ingestion.base import (
    IngestResult,
    RetryableError,
    RetryPolicy,
    TerminalError,
    add_envelope,
    with_retry,
    write_landing,
)
from confidentia.store.repositories import (
    BatchDecision,
    BatchRegistry,
    RunType,
    WatermarkStore,
    track_run,
)

logger = get_logger(__name__)

SOURCE = "rest_api"

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
TIME_COLUMNS = ("event_ts", "admit_ts", "created_at", "filed_ts")


class RestCollector:
    def __init__(
        self,
        *,
        base_url: str = "http://localhost:8081",
        page_size: int = 1000,
        timeout_seconds: float = 10.0,
        policy: RetryPolicy | None = None,
        max_pages: int | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.page_size = page_size
        self.timeout = timeout_seconds
        self.policy = policy or RetryPolicy(max_attempts=5, base_seconds=0.4)
        self.max_pages = max_pages
        self.lake_root = get_settings().lake_root
        self.registry = BatchRegistry()
        self.watermarks = WatermarkStore()

    # ------------------------------------------------------------------

    def _fetch_page(self, client: httpx.Client, tenant: str, dataset: str, page: int, since: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "page_size": self.page_size}
        if since:
            params["since"] = since

        try:
            response = client.get(f"/api/v1/{tenant}/{dataset}", params=params, timeout=self.timeout)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise RetryableError(f"transport failure on page {page}: {exc}") from exc

        if response.status_code in RETRYABLE_STATUS:
            # Honour Retry-After when the server sets it; guessing a backoff when
            # the server has told you the answer is how clients get banned.
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                # A malformed Retry-After is not worth failing over; fall through
                # to the client's own backoff.
                with contextlib.suppress(ValueError):
                    time.sleep(min(float(retry_after), 10.0))
            raise RetryableError(f"HTTP {response.status_code} on page {page}")

        if response.status_code >= 400:
            raise TerminalError(f"HTTP {response.status_code} on page {page}: {response.text[:200]}")

        try:
            return response.json()
        except ValueError as exc:
            raise TerminalError(f"page {page} was not valid JSON: {exc}") from exc

    # ------------------------------------------------------------------

    def collect(self, tenant: str, dataset: str, *, full_refresh: bool = False) -> IngestResult:
        started = time.perf_counter()
        since = None if full_refresh else self.watermarks.value(SOURCE, tenant, dataset)

        with track_run(
            RunType.INGEST,
            tenant=tenant,
            dataset=dataset,
            source=SOURCE,
            metadata={
                "base_url": self.base_url,
                "since": since,
                "mode": "full_refresh" if full_refresh else "incremental",
            },
        ) as run:
            attempts: list[dict[str, Any]] = []
            frames: list[pd.DataFrame] = []
            page = 1
            total_available = 0

            with httpx.Client(base_url=self.base_url) as client:
                while True:
                    # Bound as default arguments rather than captured, so each
                    # closure sees the page it was created for instead of the loop
                    # variable's final value.
                    def fetch(page_number: int = page) -> dict[str, Any]:
                        return self._fetch_page(client, tenant, dataset, page_number, since)

                    def note(
                        attempt: int, exc: Exception | None, page_number: int = page
                    ) -> None:
                        attempts.append(
                            {
                                "page": page_number,
                                "attempt": attempt,
                                "error": str(exc)[:120] if exc else None,
                            }
                        )

                    payload = with_retry(
                        fetch,
                        policy=self.policy,
                        description=f"GET {dataset} page {page}",
                        on_attempt=note,
                    )
                    total_available = payload["total"]
                    items = payload["items"]
                    if items:
                        frames.append(pd.DataFrame(items))
                    if not payload["has_more"]:
                        break
                    page += 1
                    if self.max_pages and page > self.max_pages:
                        logger.warning("page_limit_reached", pages=self.max_pages)
                        break

            retried = sum(1 for a in attempts if a["error"])
            run.note(pages=page, retried_requests=retried, total_available=total_available)

            if not frames:
                # Nothing new is a success, not a failure. The watermark stays put.
                logger.info("no_new_records", dataset=dataset, since=since)
                return IngestResult(
                    source=SOURCE,
                    tenant=tenant,
                    dataset=dataset,
                    decision=BatchDecision.NEW,
                    duration_seconds=time.perf_counter() - started,
                    detail={"pages": page, "retried_requests": retried},
                )

            frame = pd.concat(frames, ignore_index=True)
            # A REST page has no manifest, so batch identity is derived from the
            # content itself: the digest of the ordered record payloads.
            batch_digest = sha256_json(
                {
                    "tenant": tenant,
                    "dataset": dataset,
                    "since": since,
                    "rows": len(frame),
                    "first": frame.iloc[0].astype(str).to_dict(),
                    "last": frame.iloc[-1].astype(str).to_dict(),
                }
            )

            decision = self.registry.offer(
                batch_digest=batch_digest,
                source=SOURCE,
                tenant=tenant,
                dataset=dataset,
                file_count=page,
                row_count=len(frame),
                bytes_=int(frame.memory_usage(deep=True).sum()),
                run_id=run.run_id,
                metadata={"since": since, "pages": page},
            )
            if decision is BatchDecision.DUPLICATE:
                run.note(decision="duplicate")
                return IngestResult(
                    source=SOURCE,
                    tenant=tenant,
                    dataset=dataset,
                    decision=decision,
                    batch_digest=batch_digest,
                    duration_seconds=time.perf_counter() - started,
                )

            enveloped = add_envelope(
                frame,
                run_id=run.run_id,
                batch_digest=batch_digest,
                source_file=f"{self.base_url}/api/v1/{tenant}/{dataset}",
                source_system="EHR-API",
            )
            write_landing(
                enveloped,
                lake_root=self.lake_root,
                tenant=tenant,
                dataset=dataset,
                batch_digest=batch_digest,
                part=0,
                crypto=LandingCrypto.for_tenant(tenant, run_id=run.run_id),
            )
            run.add(read=len(frame), written=len(enveloped))
            self.registry.mark(batch_digest, "ingested", run.run_id)

            # Only now, with the data durable, is it safe to move the watermark.
            new_watermark = _max_timestamp(frame)
            if new_watermark:
                self.watermarks.advance(
                    source=SOURCE,
                    tenant=tenant,
                    dataset=dataset,
                    kind="timestamp",
                    value=new_watermark,
                    run_id=run.run_id,
                )

            return IngestResult(
                source=SOURCE,
                tenant=tenant,
                dataset=dataset,
                decision=decision,
                batch_digest=batch_digest,
                records_read=len(frame),
                records_written=len(enveloped),
                output_path=self.lake_root / "landing" / tenant / dataset,
                duration_seconds=time.perf_counter() - started,
                detail={
                    "pages": page,
                    "retried_requests": retried,
                    "watermark": new_watermark,
                },
            )


def _max_timestamp(frame: pd.DataFrame) -> str | None:
    for column in TIME_COLUMNS:
        if column in frame.columns:
            values = pd.to_datetime(frame[column], utc=True, errors="coerce").dropna()
            if not values.empty:
                return values.max().isoformat()
    return None
