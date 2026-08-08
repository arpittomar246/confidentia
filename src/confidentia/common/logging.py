"""Structured logging.

Every log line the platform emits is a JSON object in non-local environments and
a human-readable line locally. Two things are bound automatically and must never
be logged by hand:

``run_id`` / ``task_id``
    So that the Pipeline Monitoring dashboard can join logs to a run without
    parsing message text.

``tee_backend`` / ``measured``
    So that any timing appearing in a log can be traced back to whether it came
    from real hardware or from the cost model. This is the logging-layer half of
    the never-present-modelled-numbers-as-measurements rule.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog

from confidentia.config.settings import get_settings

_configured = False


def _redact_secrets(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Drop anything that looks like key material before it reaches a sink.

    A confidential-computing platform that leaks a data-encryption key into a log
    file has defeated itself, and log aggregation is a classic path out of the
    trust boundary.
    """
    sensitive = ("password", "secret", "token", "key_material", "dek", "kek", "private")
    for field in list(event_dict):
        lowered = field.lower()
        if any(marker in lowered for marker in sensitive) and lowered not in {
            "key_id",
            "master_key_id",
            "idempotency_key",
        }:
            event_dict[field] = "<redacted>"
    return event_dict


def configure_logging(force: bool = False) -> None:
    global _configured
    if _configured and not force:
        return

    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    # Spark and py4j are extremely chatty at INFO and will bury pipeline events.
    for noisy in ("py4j", "pyspark", "botocore", "urllib3", "kafka"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _redact_secrets,
    ]
    renderer = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    structlog.contextvars.bind_contextvars(
        tee_backend=settings.tee.backend.value,
        measured=settings.tee.is_measured,
        env=settings.env.value,
    )
    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    configure_logging()
    return structlog.get_logger(name)


@contextmanager
def log_context(**kwargs: Any) -> Iterator[None]:
    """Bind fields for the duration of a block (e.g. a task inside a run)."""
    configure_logging()
    tokens = structlog.contextvars.bind_contextvars(**kwargs)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)
