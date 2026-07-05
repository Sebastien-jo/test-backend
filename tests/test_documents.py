"""Tests for the documents service and endpoints (infra-free, via fakes).

The DB is faked: service tests assert the org-scoping is present in the compiled
SQL; endpoint tests override get_db / get_current_user / get_storage. A true
cross-tenant check against a live DB is part of the from-zero verification.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects import postgresql

from app.api.deps import CurrentUser, get_current_user
from app.core.db import get_db
from app.main import app
from app.models import Document, ProcessingStep, User, WebhookEvent
from app.services.documents import (
    EmptyFileError,
    UnsupportedFileTypeError,
    create_document,
    get_document,
    get_document_results,
    list_documents,
)
from app.services.status import DocumentStatus, StepName, StepStatus
from app.services.storage import get_storage
from app.workers.pipeline import get_pipeline_enqueuer


class RecordingEnqueue:
    """Fake pipeline enqueuer that records the document ids it was called with."""

    def __init__(self) -> None:
        self.calls: list[uuid.UUID] = []

    async def __call__(self, document_id: uuid.UUID) -> None:
        self.calls.append(document_id)


async def _noop_enqueue(document_id: uuid.UUID) -> None:
    return None


class FakeStorage:
    def __init__(self) -> None:
        self.saved: dict[str, bytes] = {}
        self.deleted: list[str] = []

    async def save(self, key: str, content: bytes) -> None:
        self.saved[key] = content

    async def open(self, key: str) -> bytes:
        return self.saved[key]

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.saved.pop(key, None)


class FakeUpload:
    def __init__(self, filename: str, content: bytes) -> None:
        self.filename = filename
        self.size = len(content)
        self._content = content

    async def read(self) -> bytes:
        return self._content


class FakeCommitSession:
    """Stands in for AsyncSession during create_document."""

    def __init__(self, *, fail_commit: bool = False) -> None:
        self.added: list[Any] = []
        self.committed = False
        self._fail = fail_commit

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        if self._fail:
            raise RuntimeError("commit failed")
        self.committed = True

    async def refresh(self, obj: Any) -> None:
        # Simulate the DB populating server-side timestamps.
        if getattr(obj, "created_at", None) is None:
            obj.created_at = datetime.now(UTC)
        if getattr(obj, "updated_at", None) is None:
            obj.updated_at = datetime.now(UTC)


class FakeScalars:
    def __init__(self, items: Any) -> None:
        self._items = items

    def all(self) -> list[Any]:
        return list(self._items)


class FakeQuerySession:
    """Captures the statement so tests can assert org scoping in the SQL."""

    def __init__(self, *, scalar: Any = None, scalars: Any = ()) -> None:
        self.captured: Any = None
        self._scalar = scalar
        self._scalars = scalars

    async def scalar(self, stmt: Any) -> Any:
        self.captured = stmt
        return self._scalar

    async def scalars(self, stmt: Any) -> FakeScalars:
        self.captured = stmt
        return FakeScalars(self._scalars)


def _compiled(stmt: Any) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def _user() -> CurrentUser:
    return CurrentUser(user_id=uuid.uuid4(), organization_id=uuid.uuid4())


# --- create_document --------------------------------------------------------


async def test_create_document_persists_file_and_pending_rows() -> None:
    storage, db, user = FakeStorage(), FakeCommitSession(), _user()
    enqueue = RecordingEnqueue()

    document = await create_document(
        db, user, FakeUpload("My Report.pdf", b"%PDF-1.4"), storage, enqueue
    )

    assert document.status is DocumentStatus.PENDING
    assert document.organization_id == user.organization_id
    assert document.uploaded_by == user.user_id
    assert document.filename == "My Report.pdf"  # original name kept for display
    assert {s.name for s in document.steps} == set(StepName)
    assert all(s.status is StepStatus.PENDING for s in document.steps)
    assert db.committed is True
    # Pipeline enqueued once, after the commit, with the new document id.
    assert enqueue.calls == [document.id]

    # Stored under an org/doc-prefixed key (tenant isolation reflected in keys).
    (key,) = storage.saved
    assert key.startswith(f"{user.organization_id}/{document.id}/")
    assert key.endswith("My Report.pdf")


async def test_create_document_deletes_file_when_commit_fails() -> None:
    storage, db, user = FakeStorage(), FakeCommitSession(fail_commit=True), _user()
    enqueue = RecordingEnqueue()

    with pytest.raises(RuntimeError):
        await create_document(db, user, FakeUpload("x.pdf", b"%PDF-1.4 data"), storage, enqueue)

    # File was written then removed on rollback — no orphan blob, nothing enqueued.
    assert len(storage.deleted) == 1
    assert storage.saved == {}
    assert enqueue.calls == []


async def test_create_document_rejects_empty_file() -> None:
    with pytest.raises(EmptyFileError):
        await create_document(
            FakeCommitSession(), _user(), FakeUpload("e.pdf", b""), FakeStorage(), _noop_enqueue
        )


async def test_create_document_rejects_non_pdf() -> None:
    with pytest.raises(UnsupportedFileTypeError):
        await create_document(
            FakeCommitSession(),
            _user(),
            FakeUpload("notes.txt", b"just text"),
            FakeStorage(),
            _noop_enqueue,
        )


# --- org scoping (multi-tenant invariant) -----------------------------------


async def test_get_document_query_is_scoped_to_organization() -> None:
    user = _user()
    db = FakeQuerySession(scalar=None)

    result = await get_document(db, user, uuid.uuid4())

    assert result is None
    sql = _compiled(db.captured)
    assert "organization_id" in sql
    assert str(user.organization_id) in sql


async def test_list_documents_query_is_scoped_ordered_paginated() -> None:
    user = _user()
    db = FakeQuerySession(scalars=[])

    await list_documents(db, user, limit=10, offset=5)

    sql = _compiled(db.captured)
    assert "organization_id" in sql
    assert str(user.organization_id) in sql
    assert "ORDER BY documents.created_at DESC" in sql
    assert "LIMIT 10" in sql
    assert "OFFSET 5" in sql


# --- endpoints --------------------------------------------------------------


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_documents_require_authentication() -> None:
    async with await _client() as client:
        assert (await client.get("/documents")).status_code == 401
        assert (await client.get(f"/documents/{uuid.uuid4()}")).status_code == 401


async def test_get_document_missing_or_other_org_returns_404() -> None:
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_db] = lambda: FakeQuerySession(scalar=None)
    try:
        async with await _client() as client:
            response = await client.get(f"/documents/{uuid.uuid4()}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


async def test_upload_empty_file_returns_400() -> None:
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_db] = FakeCommitSession
    app.dependency_overrides[get_storage] = FakeStorage
    app.dependency_overrides[get_pipeline_enqueuer] = lambda: _noop_enqueue
    try:
        async with await _client() as client:
            response = await client.post(
                "/documents", files={"file": ("empty.pdf", b"", "application/pdf")}
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400


async def test_upload_success_returns_201_pending() -> None:
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_db] = FakeCommitSession
    app.dependency_overrides[get_storage] = FakeStorage
    app.dependency_overrides[get_pipeline_enqueuer] = lambda: _noop_enqueue
    try:
        async with await _client() as client:
            response = await client.post(
                "/documents", files={"file": ("report.pdf", b"%PDF-1.4 data", "application/pdf")}
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    body = response.json()
    assert body["filename"] == "report.pdf"
    assert body["status"] == "pending"
    assert body["id"] and body["created_at"]


async def test_upload_non_pdf_returns_415() -> None:
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_db] = FakeCommitSession
    app.dependency_overrides[get_storage] = FakeStorage
    app.dependency_overrides[get_pipeline_enqueuer] = lambda: _noop_enqueue
    try:
        async with await _client() as client:
            response = await client.post(
                "/documents", files={"file": ("notes.txt", b"plain text", "text/plain")}
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 415


async def test_list_documents_returns_items_with_uploader_and_status() -> None:
    user = _user()
    document = Document(
        id=uuid.uuid4(),
        organization_id=user.organization_id,
        uploaded_by=user.user_id,
        filename="a.pdf",
        storage_path="key",
        status=DocumentStatus.PENDING,
        steps=[ProcessingStep(name=name, status=StepStatus.PENDING) for name in StepName],
    )
    document.created_at = datetime.now(UTC)
    document.uploader = User(
        id=user.user_id,
        organization_id=user.organization_id,
        email="alice@acme.test",
        hashed_password="x",
    )

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: FakeQuerySession(scalars=[document])
    try:
        async with await _client() as client:
            response = await client.get("/documents")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == 20 and body["offset"] == 0
    assert body["items"][0]["filename"] == "a.pdf"
    assert body["items"][0]["uploader_email"] == "alice@acme.test"
    assert body["items"][0]["status"] == "pending"


# --- GET /documents/{id}/results --------------------------------------------


_JOB_ID = "j_abc123def4567890"


class FakeResultsSession:
    """Returns queued scalar results in order (document, then webhook event)."""

    def __init__(self, *scalars: Any) -> None:
        self._scalars = list(scalars)
        self.captured: list[Any] = []

    async def scalar(self, stmt: Any) -> Any:
        self.captured.append(stmt)
        return self._scalars.pop(0)


def _document(
    user: CurrentUser, doc_status: DocumentStatus, *, with_results: bool = True
) -> Document:
    def step(name: StepName, result: Any) -> ProcessingStep:
        return ProcessingStep(name=name, status=StepStatus.DONE, attempts=1, result=result)

    return Document(
        id=uuid.uuid4(),
        organization_id=user.organization_id,
        uploaded_by=user.user_id,
        filename="deed.pdf",
        storage_path="key",
        status=doc_status,
        partner_job_id=_JOB_ID,
        steps=[
            step(StepName.OCR, "lorem ipsum..." if with_results else None),
            step(StepName.METADATA, {"doc_type": "fake_type"} if with_results else None),
            step(StepName.CHUNKING, ["chunk_1", "chunk_2"] if with_results else None),
            step(StepName.EXTERNAL_CALL, _JOB_ID),
        ],
    )


def _webhook_event() -> WebhookEvent:
    return WebhookEvent(
        job_id=_JOB_ID,
        payload={"job_id": _JOB_ID, "status": "completed", "result": {"indexed_at": "2026-05-21"}},
        signature_valid=True,
    )


async def _get_results(
    session: Any, user: CurrentUser, document_id: uuid.UUID | None = None
) -> Any:
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: session
    try:
        async with await _client() as client:
            return await client.get(f"/documents/{document_id or uuid.uuid4()}/results")
    finally:
        app.dependency_overrides.clear()


async def test_results_ready_returns_full_aggregate() -> None:
    user = _user()
    session = FakeResultsSession(_document(user, DocumentStatus.READY), _webhook_event())

    response = await _get_results(session, user)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["ocr_text"] == "lorem ipsum..."
    assert body["metadata"] == {"doc_type": "fake_type"}
    assert body["chunks"] == ["chunk_1", "chunk_2"]
    assert body["partner"] == {"indexed_at": "2026-05-21"}


@pytest.mark.parametrize(
    "doc_status",
    [DocumentStatus.PENDING, DocumentStatus.PROCESSING, DocumentStatus.WAITING_PARTNER],
)
async def test_results_non_terminal_returns_409(doc_status: DocumentStatus) -> None:
    user = _user()
    session = FakeResultsSession(_document(user, doc_status))

    response = await _get_results(session, user)

    assert response.status_code == 409
    body = response.json()
    assert body["status"] == doc_status.value
    assert body["detail"] == "extraction not finished"


async def test_results_failed_returns_409() -> None:
    user = _user()
    session = FakeResultsSession(_document(user, DocumentStatus.FAILED))

    response = await _get_results(session, user)

    assert response.status_code == 409
    assert response.json()["status"] == "failed"


async def test_results_cross_tenant_returns_404() -> None:
    user = _user()
    # Another org's document is invisible: the scoped query returns nothing.
    session = FakeResultsSession(None)

    response = await _get_results(session, user)

    assert response.status_code == 404


async def test_results_ready_but_missing_result_returns_500() -> None:
    user = _user()
    # ready implies a complete DAG; a missing step result is a broken invariant.
    session = FakeResultsSession(_document(user, DocumentStatus.READY, with_results=False))

    response = await _get_results(session, user)

    assert response.status_code == 500


async def test_results_require_authentication() -> None:
    async with await _client() as client:
        assert (await client.get(f"/documents/{uuid.uuid4()}/results")).status_code == 401


async def test_get_document_results_query_is_scoped_to_organization() -> None:
    user = _user()
    db = FakeQuerySession(scalar=None)

    result = await get_document_results(db, user, uuid.uuid4())

    assert result is None
    sql = _compiled(db.captured)
    assert "organization_id" in sql
    assert str(user.organization_id) in sql
