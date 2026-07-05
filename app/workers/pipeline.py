"""Pipeline task definitions and DAG orchestration.

DAG: ocr -> (metadata || chunking) -> external_call, expressed as
    chain(ocr, chord(group(metadata, chunking), external_call)).
"""

import contextvars
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
import structlog
from celery import Task, chain, chord, group

from app.core.logging import get_logger
from app.models import Document
from app.services.status import StepName, StepStatus
from app.workers.celery_app import celery_app
from app.workers.db import worker_session
from app.workers.steps import chunking, external_call, metadata, ocr
from app.workers.transitions import load_step, transition_step

log = get_logger("pipeline")

_task_started_at: contextvars.ContextVar[float] = contextvars.ContextVar("_task_started_at")

_RETRYABLE = (TimeoutError, ValueError, ConnectionError)
_RETRY_KWARGS: dict[str, Any] = {
    "autoretry_for": _RETRYABLE,
    "retry_backoff": True,
    "retry_backoff_max": 30,
    "retry_jitter": False,
    "max_retries": 5,
    "acks_late": True,
}


def _request_id(request: Any) -> str | None:
    """Read the propagated request_id off the task request (version-tolerant)."""
    rid = getattr(request, "request_id", None)
    if rid:
        return rid
    headers = getattr(request, "headers", None) or {}
    return headers.get("request_id") if isinstance(headers, dict) else None


class PipelineTask(Task):
    """Base task: binds the log context, times the execution, and records
    retry/failure as step transitions.
    """

    step_name: StepName

    def before_start(self, task_id: str, args: Any, kwargs: Any) -> None:
        # Thread pool reuses threads, so start from a clean context every time.
        structlog.contextvars.clear_contextvars()
        bound = {"task_id": task_id, "step": str(self.step_name)}
        request_id = _request_id(self.request)
        if request_id:
            bound["request_id"] = request_id
        if args:
            bound["document_id"] = args[0]
        structlog.contextvars.bind_contextvars(**bound)
        _task_started_at.set(time.perf_counter())
        log.info("task started", attempt=self.request.retries + 1)

    def after_return(
        self, status: str, retval: Any, task_id: str, args: Any, kwargs: Any, einfo: Any
    ) -> None:
        try:
            duration_ms = round((time.perf_counter() - _task_started_at.get()) * 1000.0, 2)
        except LookupError:
            duration_ms = None
        log.info("task finished", outcome=status, duration_ms=duration_ms)
        structlog.contextvars.clear_contextvars()

    def on_retry(self, exc: Exception, task_id: str, args: Any, kwargs: Any, einfo: Any) -> None:
        retry_in = min(_RETRY_KWARGS["retry_backoff_max"], 2**self.request.retries)
        log.warning(
            "step retry scheduled",
            attempt=self.request.retries + 1,
            error_type=type(exc).__name__,
            error=str(exc),
            retry_in_seconds=retry_in,
        )
        document_id = uuid.UUID(args[0])
        with worker_session() as session:
            transition_step(
                session, document_id, self.step_name, StepStatus.RETRYING, error=str(exc)
            )

    def on_failure(self, exc: Exception, task_id: str, args: Any, kwargs: Any, einfo: Any) -> None:
        log.error(
            "step failed permanently",
            attempts=self.request.retries + 1,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        document_id = uuid.UUID(args[0])
        with worker_session() as session:
            transition_step(session, document_id, self.step_name, StepStatus.FAILED, error=str(exc))


def _begin(document_id: uuid.UUID, step_name: StepName) -> bool:
    """Begin an execution attempt: count it and move the step to RUNNING."""
    with worker_session() as session:
        step = transition_step(
            session, document_id, step_name, StepStatus.RUNNING, count_attempt=True
        )
        return step.status is StepStatus.RUNNING


def _complete(document_id: uuid.UUID, step_name: StepName, result: Any) -> None:
    with worker_session() as session:
        transition_step(session, document_id, step_name, StepStatus.DONE, result=result)


def _read_result(document_id: uuid.UUID, step_name: StepName) -> Any:
    with worker_session() as session:
        return load_step(session, document_id, step_name).result


@celery_app.task(bind=True, base=PipelineTask, **_RETRY_KWARGS)
def ocr_task(self: Task, document_id: str) -> str:
    doc_id = uuid.UUID(document_id)
    if _begin(doc_id, StepName.OCR):
        _complete(doc_id, StepName.OCR, ocr())
    return document_id


@celery_app.task(bind=True, base=PipelineTask, **_RETRY_KWARGS)
def metadata_task(self: Task, document_id: str) -> str:
    doc_id = uuid.UUID(document_id)
    if _begin(doc_id, StepName.METADATA):
        text = _read_result(doc_id, StepName.OCR)
        _complete(doc_id, StepName.METADATA, metadata(text))
    return document_id


@celery_app.task(bind=True, base=PipelineTask, **_RETRY_KWARGS)
def chunking_task(self: Task, document_id: str) -> str:
    doc_id = uuid.UUID(document_id)
    if _begin(doc_id, StepName.CHUNKING):
        text = _read_result(doc_id, StepName.OCR)
        _complete(doc_id, StepName.CHUNKING, chunking(text))
    return document_id


@celery_app.task(bind=True, base=PipelineTask, **_RETRY_KWARGS)
def external_call_task(self: Task, document_id: str) -> str:
    doc_id = uuid.UUID(document_id)
    if _begin(doc_id, StepName.EXTERNAL_CALL):
        ocr_text = _read_result(doc_id, StepName.OCR)
        meta = _read_result(doc_id, StepName.METADATA)
        chunks = _read_result(doc_id, StepName.CHUNKING)
        job_id = external_call(str(doc_id), ocr_text, meta, chunks)
        # Persist the partner job_id and the DONE transition in one transaction.
        with worker_session() as session:
            document = session.get(Document, doc_id)
            document.partner_job_id = job_id
            transition_step(session, doc_id, StepName.EXTERNAL_CALL, StepStatus.DONE, result=job_id)
    return document_id


ocr_task.step_name = StepName.OCR
metadata_task.step_name = StepName.METADATA
chunking_task.step_name = StepName.CHUNKING
external_call_task.step_name = StepName.EXTERNAL_CALL


def build_pipeline(document_id: uuid.UUID) -> chain:
    """Build the DAG. Immutable signatures (`.si`) so each task gets only the
    document id; ordering (chain/chord), not argument passing, drives the flow.
    """
    doc = str(document_id)
    return chain(
        ocr_task.si(doc),
        chord(
            group(metadata_task.si(doc), chunking_task.si(doc)),
            external_call_task.si(doc),
        ),
    )


def enqueue_pipeline(document_id: uuid.UUID) -> None:
    """Publish the pipeline to the broker (synchronous call)."""
    build_pipeline(document_id).apply_async()
    log.info("pipeline enqueued", document_id=str(document_id))


async def enqueue_pipeline_async(document_id: uuid.UUID) -> None:
    """Publish off the event loop so the request handler never blocks on the broker."""
    await anyio.to_thread.run_sync(enqueue_pipeline, document_id)


PipelineEnqueuer = Callable[[uuid.UUID], Awaitable[None]]


def get_pipeline_enqueuer() -> PipelineEnqueuer:
    """FastAPI dependency (overridable in tests)."""
    return enqueue_pipeline_async
