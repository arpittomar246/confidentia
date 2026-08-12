"""Liveness, readiness, and what this deployment is.

``/health`` and ``/ready`` answer different questions and a load balancer needs
both. ``/health`` says the process is running and can serve a request; restarting
on a failure here is correct. ``/ready`` says its dependency answers; a pod that
fails readiness should be taken out of rotation but **not** restarted, because
restarting it will not fix Postgres.

Both are unauthenticated. A probe that needs a token is a probe that reports
failure when the identity provider is down, which converts an auth outage into a
platform outage.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Response

from confidentia.api.deps import fetch_one
from confidentia.api.errors import DependencyUnavailableError
from confidentia.config.settings import get_settings

router = APIRouter(tags=["health"])

_STARTED_AT = time.time()


@router.get("/health", summary="Process liveness")
def health() -> dict[str, Any]:
    return {"status": "ok", "uptime_seconds": round(time.time() - _STARTED_AT, 1)}


@router.get("/ready", summary="Dependency readiness")
def ready(response: Response) -> dict[str, Any]:
    """503 with a body, not an exception, so the body is machine-readable either way."""
    started = time.perf_counter()
    try:
        fetch_one("SELECT 1 AS ok")
    except DependencyUnavailableError as exc:
        response.status_code = 503
        return {"status": "degraded", "postgres": {"reachable": False, "error": str(exc)}}
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    return {"status": "ready", "postgres": {"reachable": True, "latency_ms": latency_ms}}


@router.get("/meta", summary="What this deployment is, and what it is not")
def meta() -> dict[str, Any]:
    """Unauthenticated on purpose, and therefore carries no operational data.

    ``tee`` is here because every confidentiality claim this platform makes is
    conditional on it. Under the ``sim`` backend the enclave is *modelled*, and a
    dashboard that renders "attested" without qualification would be making a claim
    the platform cannot support. The label is served from the same setting the
    pipeline uses, so the two cannot drift.
    """
    settings = get_settings()
    return {
        "service": "confidentia-api",
        "environment": settings.env.value,
        "read_only": settings.api.read_only,
        "auth_mode": settings.auth.mode.value,
        "tee": {
            "backend": settings.tee.backend.value,
            "hardware_rooted": settings.tee.is_measured,
            "label": settings.tee.measurement_label,
        },
    }
