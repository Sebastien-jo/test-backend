"""Dev-only endpoints. Registered only when DEV_ENDPOINTS_ENABLED is true."""

from fastapi import APIRouter, Request

from app.api.schemas import PARTNER_WEBHOOK_EXAMPLE, SignWebhookResponse
from app.core.security import compute_partner_signature

router = APIRouter(prefix="/dev", tags=["dev"])


@router.post(
    "/sign-webhook",
    response_model=SignWebhookResponse,
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
async def sign_webhook(request: Request) -> SignWebhookResponse:
    """DEV ONLY — return the HMAC signature for the exact request body.

    Put the returned `signature` in the `X-Partner-Signature` header of
    `POST /webhooks/partner` and send it the **same** JSON body. The signature is
    over the exact bytes, so the two bodies must be byte-identical — the easy way
    is to copy the same JSON into both endpoints.

    Never enable in production: it is a signature oracle — anyone could forge a
    valid webhook for any payload.
    """
    raw_body = await request.body()
    return SignWebhookResponse(signature=compute_partner_signature(raw_body))
