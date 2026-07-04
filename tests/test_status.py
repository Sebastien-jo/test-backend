"""Exhaustive unit tests for the pipeline state machine (infra-free)."""

import pytest

from app.services.status import (
    DocumentStatus,
    StepName,
    StepStatus,
    derive_document_status,
    is_valid_transition,
)

OCR = StepName.OCR
META = StepName.METADATA
CHUNK = StepName.CHUNKING
EXT = StepName.EXTERNAL_CALL

PENDING = StepStatus.PENDING
RUNNING = StepStatus.RUNNING
RETRYING = StepStatus.RETRYING
DONE = StepStatus.DONE
FAILED = StepStatus.FAILED


@pytest.mark.parametrize(
    ("steps", "webhook_received", "expected"),
    [
        # --- no work / pending ---
        pytest.param({}, False, DocumentStatus.PENDING, id="no-steps"),
        pytest.param(
            {OCR: PENDING, META: PENDING, CHUNK: PENDING, EXT: PENDING},
            False,
            DocumentStatus.PENDING,
            id="all-pending",
        ),
        # --- processing ---
        pytest.param({OCR: RUNNING}, False, DocumentStatus.PROCESSING, id="ocr-running"),
        pytest.param({OCR: RETRYING}, False, DocumentStatus.PROCESSING, id="ocr-retrying"),
        pytest.param(
            {OCR: DONE, META: RUNNING, CHUNK: PENDING},
            False,
            DocumentStatus.PROCESSING,
            id="ocr-done-meta-running",
        ),
        pytest.param(
            {OCR: DONE, META: DONE, CHUNK: RUNNING},
            False,
            DocumentStatus.PROCESSING,
            id="meta-done-chunking-running",
        ),
        pytest.param(
            {OCR: DONE, META: DONE, CHUNK: DONE, EXT: RUNNING},
            False,
            DocumentStatus.PROCESSING,
            id="external-call-running",
        ),
        # --- waiting on the partner ---
        pytest.param(
            {OCR: DONE, META: DONE, CHUNK: DONE, EXT: DONE},
            False,
            DocumentStatus.WAITING_PARTNER,
            id="external-call-done-no-webhook",
        ),
        # --- ready ---
        pytest.param(
            {OCR: DONE, META: DONE, CHUNK: DONE, EXT: DONE},
            True,
            DocumentStatus.READY,
            id="external-call-done-webhook-received",
        ),
        # --- failed (a failure anywhere is terminal) ---
        pytest.param({OCR: FAILED}, False, DocumentStatus.FAILED, id="ocr-failed"),
        pytest.param(
            {OCR: DONE, META: DONE, CHUNK: FAILED},
            False,
            DocumentStatus.FAILED,
            id="metadata-done-chunking-failed",
        ),
        pytest.param(
            {OCR: DONE, META: FAILED, CHUNK: DONE},
            False,
            DocumentStatus.FAILED,
            id="chunking-done-metadata-failed",
        ),
        # --- precedence: failure wins even if a webhook somehow arrived ---
        pytest.param(
            {OCR: DONE, META: DONE, CHUNK: FAILED, EXT: DONE},
            True,
            DocumentStatus.FAILED,
            id="failure-beats-webhook",
        ),
    ],
)
def test_derive_document_status(
    steps: dict[StepName, StepStatus],
    webhook_received: bool,
    expected: DocumentStatus,
) -> None:
    assert derive_document_status(steps, webhook_received) is expected


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        (PENDING, RUNNING),
        (RUNNING, DONE),
        (RUNNING, RETRYING),
        (RUNNING, FAILED),
        (RETRYING, RUNNING),
        # A terminal outcome is authoritative from an active state (concurrency).
        (RETRYING, DONE),
        (RETRYING, FAILED),
    ],
)
def test_valid_transitions(from_status: StepStatus, to_status: StepStatus) -> None:
    assert is_valid_transition(from_status, to_status) is True


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        (PENDING, DONE),  # cannot finish without running
        (PENDING, FAILED),  # cannot fail without running
        (PENDING, RETRYING),
        (RUNNING, PENDING),  # no going back
        (DONE, RUNNING),  # terminal
        (DONE, FAILED),  # terminal
        (FAILED, RUNNING),  # terminal
        (FAILED, RETRYING),  # terminal
        (RUNNING, RUNNING),  # no self-transition
    ],
)
def test_invalid_transitions(from_status: StepStatus, to_status: StepStatus) -> None:
    assert is_valid_transition(from_status, to_status) is False
