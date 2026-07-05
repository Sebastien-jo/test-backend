"""Server-Sent Events: real-time document status."""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, CurrentUserDep
from app.core.db import SessionFactory, get_db
from app.core.redis import redis_client
from app.events.publisher import channel, seq_key
from app.models import Document
from app.services import documents as documents_service
from app.services.status import DocumentStatus

router = APIRouter(tags=["events"])

_HEARTBEAT_SECONDS = 20.0
_TERMINAL = {DocumentStatus.READY.value, DocumentStatus.FAILED.value}


def _format_sse(event: dict[str, Any]) -> str:
    return f"event: {event['type']}\nid: {event['event_id']}\ndata: {json.dumps(event)}\n\n"


def _snapshot_event(document: Document, boundary: int) -> dict[str, Any]:
    return {
        "type": "snapshot",
        "event_id": boundary,
        "document_id": str(document.id),
        "document_status": str(document.status),
        "steps": [
            {"name": str(s.name), "status": str(s.status), "attempts": s.attempts}
            for s in document.steps
        ],
        "timestamp": datetime.now(UTC).isoformat(),
    }


async def _sse_stream(
    snapshot: dict[str, Any],
    boundary: int,
    messages: AsyncIterator[dict[str, Any] | None],
) -> AsyncIterator[str]:
    """Pure streaming logic (testable with a fake `messages` iterator).

    Emits the snapshot first, then channel events with `event_id > boundary`
    (older ones are already reflected in the snapshot), a heartbeat comment on
    idle ticks (`messages` yields None), and closes after a terminal status.
    """
    yield _format_sse(snapshot)
    if snapshot["document_status"] in _TERMINAL:
        return
    async for event in messages:
        if event is None:
            yield ": keepalive\n\n"  # traverse proxies, detect dead connections
            continue
        if event["event_id"] <= boundary:
            continue  # already in the snapshot — light dedup
        yield _format_sse(event)
        if event["document_status"] in _TERMINAL:
            return  # final event sent; close the stream


async def _pubsub_messages(pubsub: Any) -> AsyncIterator[dict[str, Any] | None]:
    while True:
        message = await pubsub.get_message(
            ignore_subscribe_messages=True, timeout=_HEARTBEAT_SECONDS
        )
        yield None if message is None else json.loads(message["data"])


async def _event_source(document_id: uuid.UUID, current_user: CurrentUser) -> AsyncIterator[str]:
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel(document_id))
    try:
        boundary = int(await redis_client.get(seq_key(document_id)) or 0)
        async with SessionFactory() as session:
            document = await documents_service.get_document(session, current_user, document_id)
        if document is None:
            return  # deleted between the access check and now
        async for chunk in _sse_stream(
            _snapshot_event(document, boundary), boundary, _pubsub_messages(pubsub)
        ):
            yield chunk
    finally:
        # Client disconnect cancels this generator -> unsubscribe + close, so no
        # leaked asyncio task or Redis subscription.
        await pubsub.unsubscribe(channel(document_id))
        await pubsub.aclose()


@router.get("/documents/{document_id}/events")
async def document_events(
    document_id: uuid.UUID,
    current_user: CurrentUserDep,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    if await documents_service.get_document(db, current_user, document_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    return StreamingResponse(
        _event_source(document_id, current_user),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
