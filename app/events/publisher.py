"""Real-time event publishing to Redis pub/sub."""

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from redis import Redis
from redis.asyncio import Redis as AsyncRedis

from app.services.status import DocumentStatus, StepName, StepStatus

logger = logging.getLogger(__name__)


def channel(document_id: uuid.UUID) -> str:
    return f"doc:{document_id}"


def seq_key(document_id: uuid.UUID) -> str:
    """Key of a per-document monotonic counter (INCR).

    Ordering and resume ride on this sequence, not on the timestamp: two events in
    the same millisecond, or a small clock skew, would make timestamps ambiguous.
    """
    return f"doc:{document_id}:seq"


def _step_event(
    document_id: uuid.UUID,
    step_name: StepName,
    step_status: StepStatus,
    attempts: int,
    document_status: DocumentStatus,
) -> dict[str, Any]:
    return {
        "type": "step_update",
        "document_id": str(document_id),
        "document_status": str(document_status),
        "step": {"name": str(step_name), "status": str(step_status), "attempts": attempts},
        "timestamp": datetime.now(UTC).isoformat(),
    }


def _document_event(document_id: uuid.UUID, document_status: DocumentStatus) -> dict[str, Any]:
    return {
        "type": "document_update",
        "document_id": str(document_id),
        "document_status": str(document_status),
        "step": None,
        "timestamp": datetime.now(UTC).isoformat(),
    }


def publish_step_update(
    redis: Redis,
    document_id: uuid.UUID,
    *,
    step_name: StepName,
    step_status: StepStatus,
    attempts: int,
    document_status: DocumentStatus,
) -> None:
    """Publish a step transition."""
    event = _step_event(document_id, step_name, step_status, attempts, document_status)
    try:
        event["event_id"] = redis.incr(seq_key(document_id))
        redis.publish(channel(document_id), json.dumps(event))
    except Exception:
        logger.warning("Real-time publish failed (best-effort): %s", document_id, exc_info=True)


async def publish_document_update(
    redis: AsyncRedis,
    document_id: uuid.UUID,
    *,
    document_status: DocumentStatus,
) -> None:
    """Publish a document transition (ASYNC — from the API/webhook world)."""
    event = _document_event(document_id, document_status)
    try:
        event["event_id"] = await redis.incr(seq_key(document_id))
        await redis.publish(channel(document_id), json.dumps(event))
    except Exception:
        logger.warning("Real-time publish failed: %s", document_id, exc_info=True)
