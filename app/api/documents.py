"""Document endpoints — upload, detail, list. All require authentication."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUserDep
from app.api.schemas import (
    DocumentCreatedResponse,
    DocumentDetailResponse,
    DocumentListItem,
    DocumentListResponse,
    DocumentResultsResponse,
    StepAttemptSchema,
    StepSchema,
)
from app.core.config import settings
from app.core.db import get_db
from app.services import documents as documents_service
from app.services.documents import (
    DocumentNotReadyError,
    EmptyFileError,
    FileTooLargeError,
    PipelineEnqueuer,
    ResultsInvariantError,
    UnsupportedFileTypeError,
)
from app.services.status import DocumentStatus, StepStatus
from app.services.storage import FileStorage, get_storage
from app.workers.pipeline import get_pipeline_enqueuer

router = APIRouter(prefix="/documents", tags=["documents"])

DbDep = Annotated[AsyncSession, Depends(get_db)]
StorageDep = Annotated[FileStorage, Depends(get_storage)]
EnqueuerDep = Annotated[PipelineEnqueuer, Depends(get_pipeline_enqueuer)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=DocumentCreatedResponse)
async def upload_document(
    current_user: CurrentUserDep,
    db: DbDep,
    storage: StorageDep,
    enqueue: EnqueuerDep,
    file: Annotated[UploadFile, File()],
) -> DocumentCreatedResponse:
    """Upload a PDF for processing. The document is created in `pending`; the
    pipeline is not triggered in this phase.
    """
    # Early 413 using the declared size, before reading the body into memory.
    if file.size is not None and file.size > settings.max_upload_bytes:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File too large")

    try:
        document = await documents_service.create_document(db, current_user, file, storage, enqueue)
    except EmptyFileError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Uploaded file is empty") from None
    except FileTooLargeError:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File too large") from None
    except UnsupportedFileTypeError:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Only PDF files are accepted"
        ) from None

    return DocumentCreatedResponse(
        id=document.id,
        filename=document.filename,
        status=document.status,
        created_at=document.created_at,
    )


@router.get("/{document_id}", response_model=DocumentDetailResponse)
async def get_document(
    document_id: uuid.UUID,
    current_user: CurrentUserDep,
    db: DbDep,
) -> DocumentDetailResponse:
    document = await documents_service.get_document(db, current_user, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")

    return DocumentDetailResponse(
        id=document.id,
        filename=document.filename,
        status=document.status,
        created_at=document.created_at,
        updated_at=document.updated_at,
        results_available=document.status is DocumentStatus.READY,
        steps=[
            StepSchema(
                name=step.name,
                status=step.status,
                attempts=step.attempts,
                started_at=step.started_at,
                finished_at=step.finished_at,
                # Surface the last error only once the step has definitively failed.
                error=(
                    step.attempt_history[-1].error
                    if step.status is StepStatus.FAILED and step.attempt_history
                    else None
                ),
                attempt_history=[
                    StepAttemptSchema(
                        attempt=attempt.attempt,
                        error=attempt.error,
                        started_at=attempt.started_at,
                        finished_at=attempt.finished_at,
                    )
                    for attempt in step.attempt_history
                ],
            )
            for step in document.steps
        ],
    )


@router.get(
    "/{document_id}/results",
    response_model=DocumentResultsResponse,
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "Document does not exist or belongs to another organization",
        },
        status.HTTP_409_CONFLICT: {
            "description": "The document exists but processing is not finished (or it failed)",
            "content": {
                "application/json": {
                    "example": {"status": "processing", "detail": "extraction not finished"}
                }
            },
        },
    },
)
async def get_document_results(
    document_id: uuid.UUID,
    current_user: CurrentUserDep,
    db: DbDep,
) -> DocumentResultsResponse | JSONResponse:
    """Return the aggregated extracted data — only once the document is `ready`."""
    try:
        results = await documents_service.get_document_results(db, current_user, document_id)
    except DocumentNotReadyError as exc:
        detail = (
            "processing failed"
            if exc.status is DocumentStatus.FAILED
            else "extraction not finished"
        )
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"status": str(exc.status), "detail": detail},
        )
    except ResultsInvariantError:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Inconsistent document state"
        ) from None

    if results is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")

    return DocumentResultsResponse(
        document_id=results.document_id,
        status=results.status,
        ocr_text=results.ocr_text,
        metadata=results.metadata,
        chunks=results.chunks,
        partner=results.partner,
    )


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    current_user: CurrentUserDep,
    db: DbDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DocumentListResponse:
    documents = await documents_service.list_documents(db, current_user, limit, offset)
    return DocumentListResponse(
        items=[
            DocumentListItem(
                id=document.id,
                filename=document.filename,
                status=document.status,
                uploader_email=document.uploader.email,
                created_at=document.created_at,
            )
            for document in documents
        ],
        limit=limit,
        offset=offset,
    )
