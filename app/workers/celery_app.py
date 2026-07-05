"""Celery application."""

from typing import Any

import structlog
from celery import Celery
from celery.signals import before_task_publish, setup_logging

from app.core.config import settings
from app.core.logging import configure_logging

celery_app = Celery(
    "primmo",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.workers.pipeline"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
)


@setup_logging.connect
def _configure_worker_logging(**_: Any) -> None:
    """Own the worker's logging: connecting this signal stops Celery from
    installing its own handlers, so worker output uses our structlog pipeline."""
    configure_logging()


@before_task_publish.connect
def _propagate_request_id(headers: dict[str, Any] | None = None, **_: Any) -> None:
    """Carry the current `request_id` into the published task's headers."""
    if headers is None:
        return
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    if request_id:
        headers["request_id"] = request_id
