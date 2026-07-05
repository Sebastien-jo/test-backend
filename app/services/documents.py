"""Document application logic: create (persist file + rows), fetch, list.

Every query is scoped by `organization_id` — that is the multi-tenant invariant,
enforced here so endpoints cannot forget it.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.core.config import settings
from app.models import Document, ProcessingStep
from app.services.status import DocumentStatus, StepName, StepStatus
from app.services.storage import FileStorage, sanitize_filename

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


_PDF_MAGIC = b"%PDF-"


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
