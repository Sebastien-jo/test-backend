"""Inbound partner webhook.

No JWT here: the caller is the partner, not a user — authentication IS the HMAC
signature over the raw body. Every request is audited before processing; the
response is always fast and opaque.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    PARTNER_WEBHOOK_EXAMPLE,
    PartnerWebhookPayload,
    WebhookReceivedResponse,
)
from app.core.db import get_db
from app.core.logging import get_logger
from app.core.security import verify_partner_signature
from app.services import webhooks as webhooks_service

log = get_logger("webhook")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

DbDep = Annotated[AsyncSession, Depends(get_db)]


@router.post(
    "/partner",
    response_model=WebhookReceivedResponse,
    # Same JSON-object body as /dev/sign-webhook. We read the raw bytes below and
    # verify the HMAC over them, so send the exact payload that was signed.
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"type": "object"},
                    "example": PARTNER_WEBHOOK_EXAMPLE,
                }
            },
        }
    },
)
async def partner_webhook(
    request: Request,
    db: DbDep,
    x_partner_signature: Annotated[str | None, Header()] = None,
) -> WebhookReceivedResponse:
    raw_body = await request.body()
    signature_valid = verify_partner_signature(raw_body, x_partner_signature or "")

    # Audit every request first (valid or not) — the trail precedes any processing.
    await webhooks_service.record_event(db, raw_body=raw_body, signature_valid=signature_valid)

    if not signature_valid:
        # Never log the signature itself, only that verification failed.
        log.warning("webhook signature invalid")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid signature")

    try:
        payload = PartnerWebhookPayload.model_validate_json(raw_body)
    except ValidationError:
        log.warning("webhook malformed")
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid payload") from None

    await webhooks_service.process_webhook(db, payload)
    return WebhookReceivedResponse()
