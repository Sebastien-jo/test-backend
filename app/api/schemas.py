"""API response schemas. SQLAlchemy models are never exposed directly."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.services.status import DocumentStatus, StepName, StepStatus


class DocumentCreatedResponse(BaseModel):
    id: uuid.UUID
    filename: str
    status: DocumentStatus
    created_at: datetime


class StepAttemptSchema(BaseModel):
    attempt: int
    error: str | None  # null = this attempt succeeded
    started_at: datetime | None
    finished_at: datetime | None


class StepSchema(BaseModel):
    name: StepName
    status: StepStatus
    attempts: int
    started_at: datetime | None
    finished_at: datetime | None
    # Last error, surfaced only once the step has definitively failed. While a
    # step is still retrying or has succeeded, the top-level error stays null;
    # the per-attempt errors are always available in `attempt_history`.
    error: str | None
    attempt_history: list[StepAttemptSchema]


class DocumentDetailResponse(BaseModel):
    id: uuid.UUID
    filename: str
    status: DocumentStatus  # derived via status.py, not the stored column
    created_at: datetime
    updated_at: datetime
    steps: list[StepSchema]


class DocumentListItem(BaseModel):
    id: uuid.UUID
    filename: str
    status: DocumentStatus  # derived
    uploader_email: str
    created_at: datetime


class DocumentListResponse(BaseModel):
    items: list[DocumentListItem]
    limit: int
    offset: int


class PartnerWebhookPayload(BaseModel):
    """Inbound partner notification (parsed only after the signature is verified)."""

    job_id: str
    status: str  # "completed" -> ready; anything else -> failed
    result: dict[str, Any] | None = None
    occurred_at: datetime | None = None


# Example payload (matches docs/ASSIGNMENT.md), shared by the webhook endpoint and
# the dev signer so their /docs examples stay identical.
PARTNER_WEBHOOK_EXAMPLE = {
    "job_id": "j_abc123def4567890",
    "status": "completed",
    "result": {"indexed_at": "2026-05-21T14:23:11Z"},
    "occurred_at": "2026-05-21T14:23:11Z",
}


class WebhookReceivedResponse(BaseModel):
    status: str = "received"


class SignWebhookResponse(BaseModel):
    signature: str  # send this in X-Partner-Signature with the same body
