"""Document application logic: create (persist file + rows), fetch, list.

Every query is scoped by `organization_id` — that is the multi-tenant invariant,
enforced here so endpoints cannot forget it.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.core.config import settings
from app.core.logging import get_logger
from app.models import Document, ProcessingStep, WebhookEvent
from app.services.status import DocumentStatus, StepName, StepStatus
from app.services.storage import FileStorage, sanitize_filename

log = get_logger("documents")

if TYPE_CHECKING:
    # Type-only import: the service reads attributes off CurrentUser but never
    # constructs it, so we avoid a runtime services -> api dependency.
    from app.api.deps import CurrentUser

# Enqueues the processing pipeline for a committed document.
PipelineEnqueuer = Callable[[uuid.UUID], Awaitable[None]]


class DocumentError(Exception):
    """Base class for document-creation problems mapped to HTTP errors."""


class EmptyFileError(DocumentError):
    pass


class FileTooLargeError(DocumentError):
    pass


class UnsupportedFileTypeError(DocumentError):
    pass


class DocumentNotReadyError(DocumentError):
    """Results requested before the document reached `ready` (or it failed)."""

    def __init__(self, status: DocumentStatus) -> None:
        self.status = status
        super().__init__(str(status))


class ResultsInvariantError(DocumentError):
    """`ready` document missing a step result — a broken invariant, never a 200."""


@dataclass
class DocumentResults:
    """Aggregated extracted data for a completed document."""

    document_id: uuid.UUID
    status: DocumentStatus
    ocr_text: Any
    metadata: Any
    chunks: Any
    partner: dict[str, Any] | None


_PDF_MAGIC = b"%PDF-"

_RESULT_STEPS = (StepName.OCR, StepName.METADATA, StepName.CHUNKING)


async def create_document(
    db: AsyncSession,
    current_user: CurrentUser,
    upload: UploadFile,
    storage: FileStorage,
    enqueue: PipelineEnqueuer,
) -> Document:
    """Persist the uploaded file and its document + step rows atomically, then
    enqueue the pipeline.

    The file is written first; if the DB commit fails, the file is deleted so we
    never leave an orphan blob or a row without its blob.
    """
    content = await upload.read()
    if not content:
        raise EmptyFileError
    if len(content) > settings.max_upload_bytes:
        raise FileTooLargeError
    if not content.startswith(_PDF_MAGIC):
        raise UnsupportedFileTypeError

    original_filename = upload.filename or "unnamed"
    document_id = uuid.uuid4()
    key = f"{current_user.organization_id}/{document_id}/{sanitize_filename(original_filename)}"

    await storage.save(key, content)
    try:
        document = Document(
            id=document_id,
            organization_id=current_user.organization_id,
            uploaded_by=current_user.user_id,
            filename=original_filename,
            storage_path=key,
            status=DocumentStatus.PENDING,
            steps=[ProcessingStep(name=name, status=StepStatus.PENDING) for name in StepName],
        )
        db.add(document)
        await db.commit()
    except Exception:
        await storage.delete(key)
        raise

    await db.refresh(document)

    structlog.contextvars.bind_contextvars(document_id=str(document.id))
    log.info(
        "upload accepted",
        filename=original_filename,
        size_bytes=len(content),
        organization_id=str(current_user.organization_id),
    )

    await enqueue(document.id)
    return document


async def get_document(
    db: AsyncSession,
    current_user: CurrentUser,
    document_id: uuid.UUID,
) -> Document | None:
    """Fetch one document scoped to the caller's organization (steps eager-loaded)."""
    stmt = (
        select(Document)
        .where(
            Document.id == document_id,
            Document.organization_id == current_user.organization_id,
        )
        .options(selectinload(Document.steps).selectinload(ProcessingStep.attempt_history))
    )
    return await db.scalar(stmt)


async def get_document_results(
    db: AsyncSession,
    current_user: CurrentUser,
    document_id: uuid.UUID,
) -> DocumentResults | None:
    """Aggregate the extracted data for a *ready* document."""
    document = await db.scalar(
        select(Document)
        .where(
            Document.id == document_id,
            Document.organization_id == current_user.organization_id,
        )
        .options(selectinload(Document.steps))
    )
    if document is None:
        return None

    if document.status is not DocumentStatus.READY:
        raise DocumentNotReadyError(document.status)

    results = {step.name: step.result for step in document.steps}
    missing = [name for name in _RESULT_STEPS if results.get(name) is None]
    if missing:
        log.error(
            "document ready but step result missing",
            document_id=str(document_id),
            missing=[str(name) for name in missing],
        )
        raise ResultsInvariantError

    partner = await _partner_result(db, document)

    ocr_text, chunks = results[StepName.OCR], results[StepName.CHUNKING]
    log.info(
        "document results retrieved",
        document_id=str(document_id),
        ocr_text_length=len(ocr_text) if isinstance(ocr_text, str) else None,
        chunk_count=len(chunks) if isinstance(chunks, list) else None,
    )
    return DocumentResults(
        document_id=document.id,
        status=document.status,
        ocr_text=ocr_text,
        metadata=results[StepName.METADATA],
        chunks=chunks,
        partner=partner,
    )


async def _partner_result(db: AsyncSession, document: Document) -> dict[str, Any] | None:
    """The `result` object from the partner webhook that validated this document."""
    if document.partner_job_id is None:
        return None
    event = await db.scalar(
        select(WebhookEvent)
        .where(
            WebhookEvent.job_id == document.partner_job_id,
            WebhookEvent.signature_valid.is_(True),
            WebhookEvent.payload["status"].astext == "completed",
        )
        .order_by(WebhookEvent.received_at.asc())
        .limit(1)
    )
    if event is None:
        return None
    result = event.payload.get("result")
    return result if isinstance(result, dict) else None


async def list_documents(
    db: AsyncSession,
    current_user: CurrentUser,
    limit: int,
    offset: int,
) -> Sequence[Document]:
    """List the caller's organization documents, newest first, paginated."""
    stmt = (
        select(Document)
        .where(Document.organization_id == current_user.organization_id)
        .order_by(Document.created_at.desc())
        .limit(limit)
        .offset(offset)
        .options(joinedload(Document.uploader), selectinload(Document.steps))
    )
    result = await db.scalars(stmt)
    return result.all()
