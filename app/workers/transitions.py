"""The single choke-point for pipeline step state changes.

Every step status change goes through `transition_step`: it validates the move
against the pure state machine, updates timestamps/attempts/result/error, and
recomputes the document's derived status. Keeping it the *only* writer means the
real-time phase (phase 7) has exactly one place to hook event publishing.
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.events import publisher
from app.models import Document, ProcessingStep, StepAttempt
from app.services.status import (
    DocumentStatus,
    StepName,
    StepStatus,
    derive_document_status,
    is_valid_transition,
)
from app.workers.redis import redis_client

logger = logging.getLogger(__name__)


def load_step(session: Session, document_id: uuid.UUID, step_name: StepName) -> ProcessingStep:
    return session.execute(
        select(ProcessingStep).where(
            ProcessingStep.document_id == document_id,
            ProcessingStep.name == step_name,
        )
    ).scalar_one()


def transition_step(
    session: Session,
    document_id: uuid.UUID,
    step_name: StepName,
    to_status: StepStatus,
    *,
    result: Any | None = None,
    error: str | None = None,
    count_attempt: bool = False,
) -> ProcessingStep:
    """Move one step to `to_status`, then refresh the document's derived status.

    Locks the step row `FOR UPDATE`. With `acks_late`, tasks are delivered
    at-least-once, so duplicate/concurrent executions racing on the same step are
    expected (e.g. one delivery marks it `retrying` while a duplicate reports
    `done`). Two safeguards keep this correct:

    - An illegal move (e.g. a laggard retry after completion) is a logged no-op,
      not an exception — raising would fail the task and break the chord.
    - `count_attempt` increments `attempts` for *any* execution that begins work,
      including a duplicate that finds the step already RUNNING, so the counter
      reflects executions rather than only state changes.
    """
    step = session.execute(
        select(ProcessingStep)
        .where(
            ProcessingStep.document_id == document_id,
            ProcessingStep.name == step_name,
        )
        .with_for_update()
    ).scalar_one()

    dirty = False
    status_changed = False

    if step.status == to_status:
        # No state change. But a re-entering execution that finds the step already
        # RUNNING (a concurrent duplicate) is still a real attempt — `attempts`
        # counts executions, not just state moves — so count it.
        if count_attempt and to_status is StepStatus.RUNNING:
            step.attempts += 1
            dirty = True
    elif not is_valid_transition(step.status, to_status):
        # Illegal move (race/redelivery, e.g. a laggard retry after completion):
        # log and skip. Not counted as an attempt — the work will not run here.
        logger.warning(
            "Ignoring illegal step transition (race/redelivery): %s %s -> %s",
            step_name,
            step.status,
            to_status,
        )
    else:
        now = datetime.now(UTC)
        step.status = to_status
        if to_status is StepStatus.RUNNING:
            step.started_at = now
            if count_attempt:
                step.attempts += 1
        elif to_status in (StepStatus.DONE, StepStatus.FAILED):
            step.finished_at = now
        if result is not None:
            step.result = result
        dirty = True
        status_changed = True

    # Append-only history: every execution outcome gets its own row, so all errors
    # are kept (not just the last). Recorded even when the status transition above
    # was a no-op under a race — the execution still happened.
    if to_status in (StepStatus.DONE, StepStatus.RETRYING, StepStatus.FAILED):
        session.add(
            StepAttempt(
                step_id=step.id,
                attempt=step.attempts,
                error=error,  # None on success
                started_at=step.started_at,
                finished_at=datetime.now(UTC),
            )
        )
        dirty = True

    document_status: DocumentStatus | None = None
    if dirty:
        document_status = _refresh_document_status(session, document_id)
        session.commit()

    if status_changed and document_status is not None:
        publisher.publish_step_update(
            redis_client,
            document_id,
            step_name=step_name,
            step_status=step.status,
            attempts=step.attempts,
            document_status=document_status,
        )
    return step


def _refresh_document_status(session: Session, document_id: uuid.UUID) -> DocumentStatus:
    """Persist the derived document status. Rule stays in status.py; DB reflects.

    Locks the document row so the concurrent metadata/chunking transitions can't
    clobber each other's status update.
    """
    document = session.execute(
        select(Document).where(Document.id == document_id).with_for_update()
    ).scalar_one()
    steps = (
        session.execute(select(ProcessingStep).where(ProcessingStep.document_id == document_id))
        .scalars()
        .all()
    )
    mapping = {step.name: step.status for step in steps}
    # The pipeline never sets `ready`; that only comes from the partner webhook.
    document.status = derive_document_status(mapping, webhook_received=False)
    return document.status
