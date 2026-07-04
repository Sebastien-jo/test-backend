"""Celery application."""

from celery import Celery

from app.core.config import settings

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
