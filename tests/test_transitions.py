"""Unit tests for the step-transition helper (infra-free, fake session).

These exercise the single choke-point that every pipeline step change goes
through: validity, timestamps, attempts, result/error, and the derived document
status. The full Celery DAG is verified live, not here.
"""

import uuid
from typing import Any

from app.models import Document, ProcessingStep
from app.services.status import DocumentStatus, StepName, StepStatus
from app.workers.transitions import transition_step


class FakeScalars:
    def __init__(self, value: Any) -> None:
        self.value = value

    def all(self) -> list[Any]:
        return list(self.value)


class FakeResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one(self) -> Any:
        return self.value

    def scalars(self) -> FakeScalars:
        return FakeScalars(self.value)


class FakeSession:
    """Returns preset results in order: step, then (document, steps) for refresh."""

    def __init__(self, results: list[FakeResult]) -> None:
        self._results = results
        self.committed = False
        self.added: list[Any] = []

    def execute(self, _stmt: Any) -> FakeResult:
        return self._results.pop(0)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        self.committed = True


DOC_ID = uuid.uuid4()


def _step(name: StepName, status: StepStatus, attempts: int = 0) -> ProcessingStep:
    return ProcessingStep(document_id=DOC_ID, name=name, status=status, attempts=attempts)


def _document() -> Document:
    return Document(
        id=DOC_ID,
        organization_id=uuid.uuid4(),
        uploaded_by=uuid.uuid4(),
        filename="f.pdf",
        storage_path="k",
        status=DocumentStatus.PENDING,
    )


def _session(step: ProcessingStep, all_steps: list[ProcessingStep]) -> tuple[FakeSession, Document]:
    document = _document()
    session = FakeSession([FakeResult(step), FakeResult(document), FakeResult(all_steps)])
    return session, document


def test_running_transition_sets_started_at_and_increments_attempts() -> None:
    step = _step(StepName.OCR, StepStatus.PENDING)
    session, document = _session(step, [step])

    transition_step(session, DOC_ID, StepName.OCR, StepStatus.RUNNING, count_attempt=True)

    assert step.status is StepStatus.RUNNING
    assert step.started_at is not None
    assert step.attempts == 1
    assert session.committed is True
    assert document.status is DocumentStatus.PROCESSING


def test_done_transition_sets_finished_at_and_result() -> None:
    step = _step(StepName.OCR, StepStatus.RUNNING)
    session, _ = _session(step, [step])

    transition_step(session, DOC_ID, StepName.OCR, StepStatus.DONE, result="lorem ipsum...")

    assert step.status is StepStatus.DONE
    assert step.finished_at is not None
    assert step.result == "lorem ipsum..."
    # A success attempt (no error) is appended to the history.
    assert len(session.added) == 1
    assert session.added[0].error is None


def test_retrying_transition_records_error_in_history() -> None:
    step = _step(StepName.OCR, StepStatus.RUNNING, attempts=1)
    session, _ = _session(step, [step])

    transition_step(session, DOC_ID, StepName.OCR, StepStatus.RETRYING, error="boom")

    assert step.status is StepStatus.RETRYING
    # The error lives on the attempt row, not on the step.
    assert len(session.added) == 1
    assert session.added[0].error == "boom"
    assert session.added[0].attempt == 1


def test_duplicate_failure_still_appended_to_history() -> None:
    # A second execution fails while the step is already RETRYING: no status
    # change, but its error is still recorded — every execution's error is kept.
    step = _step(StepName.OCR, StepStatus.RETRYING, attempts=2)
    session, _ = _session(step, [step])

    transition_step(session, DOC_ID, StepName.OCR, StepStatus.RETRYING, error="second failure")

    assert len(session.added) == 1
    assert session.added[0].error == "second failure"
    assert session.committed is True


def test_failed_step_makes_document_failed() -> None:
    step = _step(StepName.CHUNKING, StepStatus.RUNNING)
    others = [
        _step(StepName.OCR, StepStatus.DONE),
        _step(StepName.METADATA, StepStatus.DONE),
        step,
        _step(StepName.EXTERNAL_CALL, StepStatus.PENDING),
    ]
    session, document = _session(step, others)

    transition_step(session, DOC_ID, StepName.CHUNKING, StepStatus.FAILED, error="chunking failed")

    assert step.status is StepStatus.FAILED
    assert document.status is DocumentStatus.FAILED


def test_external_call_done_makes_document_waiting_partner() -> None:
    step = _step(StepName.EXTERNAL_CALL, StepStatus.RUNNING)
    others = [
        _step(StepName.OCR, StepStatus.DONE),
        _step(StepName.METADATA, StepStatus.DONE),
        _step(StepName.CHUNKING, StepStatus.DONE),
        step,
    ]
    session, document = _session(step, others)

    transition_step(session, DOC_ID, StepName.EXTERNAL_CALL, StepStatus.DONE, result="j_abc")

    assert document.status is DocumentStatus.WAITING_PARTNER


def test_same_status_without_count_is_noop() -> None:
    step = _step(StepName.OCR, StepStatus.RUNNING, attempts=1)
    session = FakeSession([FakeResult(step)])

    transition_step(session, DOC_ID, StepName.OCR, StepStatus.RUNNING)

    assert step.attempts == 1
    assert session.committed is False  # no write on a pure no-op


def test_duplicate_execution_still_counts_the_attempt() -> None:
    # A duplicate delivery finds the step already RUNNING: no state change, but the
    # execution is real, so `attempts` is incremented (executions, not state moves).
    step = _step(StepName.OCR, StepStatus.RUNNING, attempts=1)
    session, _ = _session(step, [step])

    transition_step(session, DOC_ID, StepName.OCR, StepStatus.RUNNING, count_attempt=True)

    assert step.status is StepStatus.RUNNING
    assert step.attempts == 2
    assert session.committed is True


def test_invalid_transition_is_idempotent_noop() -> None:
    # Racing/duplicate delivery can attempt an illegal move; it must be ignored
    # (logged no-op), never raised — raising would break the chord. A terminal
    # step is also not counted as a new attempt.
    step = _step(StepName.OCR, StepStatus.DONE, attempts=2)
    session = FakeSession([FakeResult(step)])

    transition_step(session, DOC_ID, StepName.OCR, StepStatus.RUNNING, count_attempt=True)

    assert step.status is StepStatus.DONE  # unchanged
    assert step.attempts == 2  # not counted
    assert session.committed is False  # no write
