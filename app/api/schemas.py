"""API response schemas. SQLAlchemy models are never exposed directly."""

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.services.status import DocumentStatus, StepName, StepStatus


class DocumentCreatedResponse(BaseModel):
    id: uuid.UUID
    filename: str
    status: DocumentStatus
    created_at: datetime


class StepSchema(BaseModel):
    name: StepName
    status: StepStatus
    attempts: int
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None


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
