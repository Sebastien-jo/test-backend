"""Tests for the partner webhook + dev signing endpoint (infra-free, via fakes)."""

import json
import uuid
from typing import Any

from httpx import ASGITransport, AsyncClient

from app.core.db import get_db
from app.core.security import compute_partner_signature
from app.main import app
from app.models import Document
from app.services.status import DocumentStatus


class FakeWebhookSession:
    """Stand-in for the request AsyncSession used by the webhook flow."""

    def __init__(self, document: Document | None = None) -> None:
        self._document = document
        self.added: list[Any] = []
        self.commits = 0

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def scalar(self, _stmt: Any) -> Document | None:
        return self._document


def _document(status: DocumentStatus = DocumentStatus.WAITING_PARTNER) -> Document:
    return Document(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        uploaded_by=uuid.uuid4(),
        filename="f.pdf",
        storage_path="k",
        status=status,
        partner_job_id="j_1",
    )


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _post_webhook(session: Any, body: bytes, signature: str | None) -> Any:
    app.dependency_overrides[get_db] = lambda: session
    headers = {"X-Partner-Signature": signature} if signature is not None else {}
    try:
        async with await _client() as client:
            return await client.post("/webhooks/partner", content=body, headers=headers)
    finally:
        app.dependency_overrides.clear()


# --- signature / auth -------------------------------------------------------


async def test_valid_completed_webhook_marks_document_ready() -> None:
    document = _document()
    body = b'{"job_id":"j_1","status":"completed"}'

    response = await _post_webhook(
        FakeWebhookSession(document), body, compute_partner_signature(body)
    )

    assert response.status_code == 200
    assert response.json() == {"status": "received"}
    assert document.status is DocumentStatus.READY


async def test_missing_signature_returns_401_but_is_audited() -> None:
    session = FakeWebhookSession()
    response = await _post_webhook(session, b'{"job_id":"j_1","status":"completed"}', None)

    assert response.status_code == 401
    # Audited even though rejected, with signature_valid=False.
    assert len(session.added) == 1
    assert session.added[0].signature_valid is False


async def test_invalid_signature_returns_401() -> None:
    response = await _post_webhook(
        FakeWebhookSession(), b'{"job_id":"j_1","status":"completed"}', "deadbeef"
    )
    assert response.status_code == 401


async def test_signature_is_verified_over_raw_bytes_not_reparsed() -> None:
    # Body with non-canonical spacing.
    body = b'{"job_id": "j_1",   "status": "completed"}'

    # Signed over the exact bytes -> accepted.
    ok = await _post_webhook(_session_ready(), body, compute_partner_signature(body))
    assert ok.status_code == 200

    # Signature computed over a *re-serialized* version -> rejected (proves we hash
    # the raw bytes, not a parsed/re-dumped JSON).
    reparsed = json.dumps(json.loads(body), separators=(",", ":")).encode()
    assert reparsed != body
    bad = await _post_webhook(FakeWebhookSession(), body, compute_partner_signature(reparsed))
    assert bad.status_code == 401


def _session_ready() -> FakeWebhookSession:
    return FakeWebhookSession(_document())


# --- processing -------------------------------------------------------------


async def test_unknown_job_id_returns_200_and_audits() -> None:
    session = FakeWebhookSession(document=None)  # correlation finds nothing
    body = b'{"job_id":"j_unknown","status":"completed"}'

    response = await _post_webhook(session, body, compute_partner_signature(body))

    assert response.status_code == 200  # opaque, never 404
    assert len(session.added) == 1  # still audited


async def test_valid_signature_but_malformed_payload_returns_422() -> None:
    session = FakeWebhookSession(_document())
    body = b'{"status":"completed"}'  # missing job_id

    response = await _post_webhook(session, body, compute_partner_signature(body))

    assert response.status_code == 422
    assert len(session.added) == 1  # audited before the parse


async def test_failed_status_marks_document_failed() -> None:
    document = _document()
    body = b'{"job_id":"j_1","status":"failed"}'

    await _post_webhook(FakeWebhookSession(document), body, compute_partner_signature(body))

    assert document.status is DocumentStatus.FAILED


async def test_terminal_document_is_noop() -> None:
    document = _document(status=DocumentStatus.FAILED)  # already terminal
    body = b'{"job_id":"j_1","status":"completed"}'

    await _post_webhook(FakeWebhookSession(document), body, compute_partner_signature(body))

    assert document.status is DocumentStatus.FAILED  # terminal stays terminal


async def test_duplicate_webhooks_are_idempotent() -> None:
    document = _document()
    session = FakeWebhookSession(document)
    body = b'{"job_id":"j_1","status":"completed"}'
    sig = compute_partner_signature(body)

    # Same session across two identical deliveries (partner retry).
    app.dependency_overrides[get_db] = lambda: session
    try:
        async with await _client() as client:
            r1 = await client.post(
                "/webhooks/partner", content=body, headers={"X-Partner-Signature": sig}
            )
            r2 = await client.post(
                "/webhooks/partner", content=body, headers={"X-Partner-Signature": sig}
            )
    finally:
        app.dependency_overrides.clear()

    assert r1.status_code == r2.status_code == 200
    assert document.status is DocumentStatus.READY  # single effect
    assert len(session.added) == 2  # both deliveries audited


# --- dev signing endpoint (end-to-end with the webhook) ---------------------


async def test_dev_sign_webhook_produces_signature_the_webhook_accepts() -> None:
    body = b'{"job_id":"j_1","status":"completed"}'
    async with await _client() as client:
        signed = await client.post(
            "/dev/sign-webhook", content=body, headers={"Content-Type": "application/json"}
        )
    assert signed.status_code == 200
    signature = signed.json()["signature"]  # clean JSON object response

    # The SAME body + that signature is accepted by the webhook.
    response = await _post_webhook(FakeWebhookSession(_document()), body, signature)
    assert response.status_code == 200
