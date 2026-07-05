"""Partner webhook processing: audit, correlate, apply the result."""

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import PartnerWebhookPayload
from app.core.redis import redis_client
from app.events import publisher
from app.models import Document, WebhookEvent
from app.services.status import DocumentStatus

logger = logging.getLogger(__name__)


def _lenient_parse(raw_body: bytes) -> tuple[str, dict[str, Any]]:
    """Best-effort parse for the audit row only (never for processing).

    Returns (job_id, payload). Untrusted content — we store what we received but
    never act on it before the signature is verified.
    """
    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError, UnicodeDecodeError:
        return "", {"raw": raw_body.decode("utf-8", errors="replace")}
    if isinstance(data, dict):
        job_id = data.get("job_id")
        return (job_id if isinstance(job_id, str) else ""), data
    return "", {"raw": data}


async def record_event(db: AsyncSession, *, raw_body: bytes, signature_valid: bool) -> None:
    """Append the received request to the audit trail, before any processing."""
    job_id, payload = _lenient_parse(raw_body)
    db.add(WebhookEvent(job_id=job_id, payload=payload, signature_valid=signature_valid))
    await db.commit()


async def process_webhook(db: AsyncSession, payload: PartnerWebhookPayload) -> None:
    """Correlate the (verified) payload to its document and apply the result."""
    document = await db.scalar(
        select(Document).where(Document.partner_job_id == payload.job_id).with_for_update()
    )
    if document is None:
        return

    changed = _apply_partner_result(document, payload.status)
    await db.commit()

    if changed:
        await publisher.publish_document_update(
            redis_client, document.id, document_status=document.status
        )


def _apply_partner_result(document: Document, partner_status: str) -> bool:
    """Document-level transition. Returns whether the status actually changed.

    Terminal stays terminal: a webhook for an already `ready`/`failed` document is
    a no-op. This is our idempotency (a partner retry re-POSTs the same job_id) and
    it decides the failed-then-completed ordering — once terminal, we don't flip.
    """
    if document.status in (DocumentStatus.READY, DocumentStatus.FAILED):
        logger.info("Webhook no-op: document %s already %s", document.id, document.status)
        return False
    document.status = (
        DocumentStatus.READY if partner_status == "completed" else DocumentStatus.FAILED
    )
    return True
