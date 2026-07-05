"""Structured logging — shared by the API and the Celery worker."""

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

from app.core.config import settings

_REDIRECTED_LOGGERS = ("uvicorn", "uvicorn.error", "celery", "sqlalchemy.engine")


def _add_service_context(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Attach the Datadog-standard `service`/`env`/`status` fields."""
    event_dict["service"] = settings.service_name
    event_dict["env"] = settings.environment
    level = event_dict.get("level")
    if level is not None:
        event_dict["status"] = level
    return event_dict


def configure_logging() -> None:
    """Idempotently configure structlog + stdlib logging for this process.

    Called once at API startup and at worker startup. Both structlog loggers and
    plain stdlib loggers (uvicorn, celery, ...) are rendered by a single
    formatter, so the output stream is one consistent format.
    """
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        _add_service_context,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=True)
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    for name in _REDIRECTED_LOGGERS:
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    logging.getLogger("uvicorn.access").disabled = True


def get_logger(name: str | None = None) -> Any:
    """Return a bound structlog logger (configure_logging must have run)."""
    return structlog.get_logger(name)
