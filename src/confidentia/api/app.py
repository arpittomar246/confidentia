"""FastAPI application factory.

A factory rather than a module-level ``app`` because the test suite needs to build
an application against overridden settings, and because ``uvicorn --factory``
should be the deployment entry point: a module-level app opens a connection pool at
import time, which turns a config error into an import error inside the worker.

The middleware order below matters. The request-id middleware is added last, so it
runs *outermost*, so an exception raised anywhere inside -- including in another
middleware -- still gets an id attached and still gets logged.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from confidentia.api.errors import ApiError, ReadOnlyError, envelope
from confidentia.api.routers import (
    audit,
    evaluation,
    gate,
    health,
    ingestion,
    lineage,
    marts,
    quality,
    runs,
)
from confidentia.api.security import TokenVerifier
from confidentia.common.logging import get_logger
from confidentia.config.settings import Settings, get_settings

logger = get_logger(__name__)

DESCRIPTION = """
Read-only access to the CONFIDENTIA platform's metadata store and its
disclosure-controlled Gold marts.

Every value returned is read from a table or view the pipeline already writes. The
API computes no metrics of its own, holds no key material, and cannot trigger a
pipeline run -- see ADR 0016.

Authentication is a bearer token. Roles form a disclosure ladder:
`viewer` < `operator` < `auditor`.
"""


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    logger.info(
        "api_starting",
        env=settings.env.value,
        auth_mode=settings.auth.mode.value,
        read_only=settings.api.read_only,
    )
    try:
        yield
    finally:
        # The pool is process-wide and shared with any CLI code in the same
        # process, so it is closed here rather than left to interpreter shutdown,
        # where Postgres would see the connections drop rather than close.
        from confidentia.store import db

        db.close_pools()
        logger.info("api_stopped")


def create_app(settings: Settings | None = None, *, verifier: TokenVerifier | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title="CONFIDENTIA API",
        description=DESCRIPTION,
        version="0.1.0",
        lifespan=_lifespan,
        root_path=settings.api.root_path,
    )
    app.state.settings = settings
    app.state.verifier = verifier or TokenVerifier(settings)

    for module in (health, runs, quality, evaluation, gate, ingestion, lineage, marts, audit):
        app.include_router(module.router)

    # Credentials are not allowed: this API authenticates with a bearer token in a
    # header, never a cookie, so `allow_credentials=True` would widen what a
    # browser will send cross-origin for no benefit here.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.api.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    # Installed before the request-context middleware below so that the read-only
    # guard it registers ends up *inside* it, and a refused write still gets an id.
    _install_error_handlers(app, settings)

    @app.middleware("http")
    async def _request_context(request: Request, call_next: Any) -> Any:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        response.headers["x-request-id"] = request_id
        logger.info(
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration_ms,
            request_id=request_id,
        )
        return response

    return app


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _install_error_handlers(app: FastAPI, settings: Settings) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope(exc.code, exc.message, _request_id(request), **exc.context),
            # Returning 401 without this header is a protocol violation, and it is
            # what tells a browser client to obtain a token rather than retry.
            headers={"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=envelope(
                "invalid_request",
                "one or more parameters are invalid",
                _request_id(request),
                fields=[
                    {"location": list(e.get("loc", [])), "message": e.get("msg", "")}
                    for e in exc.errors()
                ],
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # The message is logged, not returned. An unhandled exception's text
        # routinely contains a SQL fragment or a filesystem path, and this response
        # goes to whoever asked.
        logger.exception(
            "unhandled_exception",
            path=request.url.path,
            request_id=_request_id(request),
            error=str(exc),
        )
        return JSONResponse(
            status_code=500,
            content=envelope("internal_error", "an unexpected error occurred",
                             _request_id(request)),
        )

    if settings.api.read_only:
        # A blanket refusal rather than an absence of routes. Every route in this
        # package is a GET, so a POST currently 405s anyway -- but this makes the
        # refusal explicit and survives someone adding a router without reading
        # ADR 0016.
        @app.middleware("http")
        async def _refuse_writes(request: Request, call_next: Any) -> Any:
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                exc = ReadOnlyError(
                    f"{request.method} is refused: this API is read-only. Pipeline "
                    "runs are triggered through Airflow, not here (ADR 0016)."
                )
                return JSONResponse(
                    status_code=exc.status_code,
                    content=envelope(exc.code, exc.message, _request_id(request)),
                )
            return await call_next(request)
