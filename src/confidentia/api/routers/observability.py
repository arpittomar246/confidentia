"""The Prometheus scrape endpoint.

**Authenticated by default.** `/metrics` carries per-dataset quality scores and
per-tenant counts, which is the same class of data `/gate/blocked` guards behind
the viewer role. An unauthenticated metrics endpoint on a published port would be a
way to read platform state that the rest of this API refuses to hand over — and
"it's only metrics" is exactly how that gets waved through.

`API_METRICS_PUBLIC=true` turns the guard off, and the compose stack sets it,
because there the endpoint is part of a single-host demo. The honest deployment
answer is different and is written down in ADR 0019: metrics belong on a port that
is not published, rather than on a public one with authentication disabled.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from confidentia.api.security import Principal, Role, require_role
from confidentia.config.settings import get_settings

router = APIRouter(tags=["observability"])


async def _guard(request: Request) -> Principal | None:
    """Viewer role unless metrics are explicitly public.

    Written as a plain dependency rather than `dependencies=[ViewerDep]` on the
    route, because the decision is a setting and a route-level dependency is fixed
    at import time.
    """
    if get_settings().api.metrics_public:
        return None
    return await require_role(Role.VIEWER)(await _principal(request))


async def _principal(request: Request) -> Principal:
    from fastapi.security import HTTPBearer

    from confidentia.api.security import current_principal

    credentials = await HTTPBearer(auto_error=False)(request)
    return await current_principal(request, credentials)


@router.get(
    "/metrics",
    summary="Prometheus scrape endpoint",
    response_class=Response,
    # Excluded from the OpenAPI schema: the body is Prometheus text format, not
    # JSON, and describing it as a JSON response would make the generated frontend
    # client offer a method that returns something it cannot parse.
    include_in_schema=False,
)
def metrics(request: Request, _: Annotated[Principal | None, Depends(_guard)]) -> Response:
    payload, content_type = request.app.state.metrics.render()
    return Response(content=payload, media_type=content_type)
