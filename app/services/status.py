"""Pipeline state machine — the single source of business rules."""

from collections.abc import Mapping
from enum import StrEnum


class DocumentStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    WAITING_PARTNER = "waiting_partner"
    READY = "ready"
    FAILED = "failed"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRYING = "retrying"
    DONE = "done"
    FAILED = "failed"


class StepName(StrEnum):
    OCR = "ocr"
    METADATA = "metadata"
    CHUNKING = "chunking"
    EXTERNAL_CALL = "external_call"


def derive_document_status(
    steps: Mapping[StepName, StepStatus],
    webhook_received: bool,
) -> DocumentStatus:
    """Derive the overall document status from its steps and partner confirmation.

    `steps` maps a step name to its current status; a step absent from the mapping
    is treated as not-yet-created (i.e. pending). `webhook_received` is True only
    once the partner webhook has arrived *and* its signature has been verified.

    Precedence (first match wins):
      1. any step FAILED (retries exhausted)         -> FAILED
      2. partner webhook received and verified        -> READY
      3. external_call DONE, webhook not yet received -> WAITING_PARTNER
      4. every step still PENDING                     -> PENDING
      5. otherwise (work in progress)                 -> PROCESSING
    """
    statuses = list(steps.values())

    # 1. A terminal step failure fails the whole document, regardless of the rest
    if any(status is StepStatus.FAILED for status in statuses):
        return DocumentStatus.FAILED

    # 2. Partner confirmation is the only thing that marks a document ready.
    if webhook_received:
        return DocumentStatus.READY

    # 3. The pipeline has done all it can and is now waiting on the partner.
    if steps.get(StepName.EXTERNAL_CALL) is StepStatus.DONE:
        return DocumentStatus.WAITING_PARTNER

    # 4. Nothing has started yet (also covers the no-steps case).
    if all(status is StepStatus.PENDING for status in statuses):
        return DocumentStatus.PENDING

    # 5. At least one step is running/retrying/done but the pipeline is not finished.
    return DocumentStatus.PROCESSING


_ALLOWED_TRANSITIONS: dict[StepStatus, frozenset[StepStatus]] = {
    StepStatus.PENDING: frozenset({StepStatus.RUNNING}),
    StepStatus.RUNNING: frozenset({StepStatus.DONE, StepStatus.RETRYING, StepStatus.FAILED}),
    StepStatus.RETRYING: frozenset({StepStatus.RUNNING, StepStatus.DONE, StepStatus.FAILED}),
    StepStatus.DONE: frozenset(),
    StepStatus.FAILED: frozenset(),
}


def is_valid_transition(from_status: StepStatus, to_status: StepStatus) -> bool:
    """Return whether a step may move from `from_status` to `to_status`."""
    return to_status in _ALLOWED_TRANSITIONS.get(from_status, frozenset())
