"""One error shape for the whole API.

FastAPI's default is ``{"detail": ...}``, which is a string for an HTTPException
and a list of objects for a validation failure. A client cannot write one error
handler against that, so every failure here -- including validation -- is
normalised to the same envelope with a stable machine-readable ``code``.

``request_id`` is included so a user reporting "it returned 500" hands over
something that finds the log line.
"""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """Base for errors that map to a deliberate HTTP status.

    Anything that is *not* an ``ApiError`` is a bug, and the handler in ``app.py``
    treats it as one: 500, no detail in the response body, full traceback in the log.
    """

    status_code: int = 500
    code: str = "error"

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context = context


class NotFoundError(ApiError):
    status_code = 404
    code = "not_found"


class BadRequestError(ApiError):
    status_code = 400
    code = "bad_request"


class ReadOnlyError(ApiError):
    """The serving layer refuses to mutate platform state. See ADR 0016."""

    status_code = 405
    code = "read_only"


class DependencyUnavailableError(ApiError):
    """Postgres did not answer. Distinct from 500: the API itself is fine."""

    status_code = 503
    code = "dependency_unavailable"


def envelope(code: str, message: str, request_id: str | None, **context: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if context:
        body["error"]["context"] = context
    if request_id:
        body["error"]["request_id"] = request_id
    return body
