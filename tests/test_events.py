"""Tests for real-time events: publisher + SSE stream logic (infra-free)."""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from httpx import ASGITransport, AsyncClient

from app.api.deps import CurrentUser, get_current_user
from app.api.events import _format_sse, _sse_stream
from app.core.db import get_db
from app.events import publisher
from app.main import app
from app.services.status import DocumentStatus, StepName, StepStatus

# --- publisher --------------------------------------------------------------


class FakeSyncRedis:
    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[tuple[str, str]] = []
        self._seq = 0
        self._fail = fail

    def incr(self, _key: str) -> int:
        if self._fail:
            raise RuntimeError("redis down")
        self._seq += 1
        return self._seq

    def publish(self, ch: str, data: str) -> None:
        self.published.append((ch, data))


class FakeAsyncRedis:
    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[tuple[str, str]] = []
        self._seq = 0
        self._fail = fail

    async def incr(self, _key: str) -> int:
        if self._fail:
            raise RuntimeError("redis down")
        self._seq += 1
        return self._seq

    async def publish(self, ch: str, data: str) -> None:
        self.published.append((ch, data))


def _publish_step(redis: FakeSyncRedis, document_id: uuid.UUID) -> None:
    publisher.publish_step_update(
        redis,
        document_id,
        step_name=StepName.OCR,
        step_status=StepStatus.RUNNING,
        attempts=1,
        document_status=DocumentStatus.PROCESSING,
    )


def test_step_event_payload_is_self_contained() -> None:
    redis, doc = FakeSyncRedis(), uuid.uuid4()
    _publish_step(redis, doc)

    ch, data = redis.published[0]
    assert ch == f"doc:{doc}"
    event = json.loads(data)
    assert event["type"] == "step_update"
    assert event["document_id"] == str(doc)
    assert event["document_status"] == "processing"
    assert event["step"] == {"name": "ocr", "status": "running", "attempts": 1}
    assert event["event_id"] == 1
    assert "timestamp" in event


def test_event_id_is_monotonic() -> None:
    redis, doc = FakeSyncRedis(), uuid.uuid4()
    for _ in range(3):
        _publish_step(redis, doc)
    assert [json.loads(d)["event_id"] for _, d in redis.published] == [1, 2, 3]


def test_publish_survives_redis_down() -> None:
    redis = FakeSyncRedis(fail=True)
    _publish_step(redis, uuid.uuid4())  # must not raise
    assert redis.published == []


async def test_document_event_payload() -> None:
    redis, doc = FakeAsyncRedis(), uuid.uuid4()
    await publisher.publish_document_update(redis, doc, document_status=DocumentStatus.READY)

    event = json.loads(redis.published[0][1])
    assert event["type"] == "document_update"
    assert event["document_status"] == "ready"
    assert event["step"] is None
    assert event["event_id"] == 1


async def test_async_publish_survives_redis_down() -> None:
    redis = FakeAsyncRedis(fail=True)
    await publisher.publish_document_update(
        redis, uuid.uuid4(), document_status=DocumentStatus.READY
    )
    assert redis.published == []


# --- SSE stream logic -------------------------------------------------------


async def _fake_messages(items: list[Any]) -> AsyncIterator[Any]:
    for item in items:
        yield item


def _snapshot(status: str = "processing", event_id: int = 0) -> dict[str, Any]:
    return {
        "type": "snapshot",
        "event_id": event_id,
        "document_id": "d",
        "document_status": status,
        "steps": [],
        "timestamp": "t",
    }


async def _collect(snapshot: dict[str, Any], boundary: int, events: list[Any]) -> list[str]:
    return [chunk async for chunk in _sse_stream(snapshot, boundary, _fake_messages(events))]


async def test_snapshot_is_sent_first() -> None:
    chunks = await _collect(_snapshot("processing", 5), 5, [])
    assert chunks[0].startswith("event: snapshot\nid: 5\ndata: ")


async def test_events_at_or_before_boundary_are_skipped() -> None:
    events = [
        {"type": "step_update", "event_id": 4, "document_status": "processing"},  # skip
        {"type": "step_update", "event_id": 6, "document_status": "processing"},  # keep
    ]
    chunks = await _collect(_snapshot("processing", 5), 5, events)
    assert len(chunks) == 2  # snapshot + the one after the boundary
    assert "id: 6" in chunks[1]


async def test_stream_stops_after_terminal_event() -> None:
    events = [
        {"type": "document_update", "event_id": 1, "document_status": "ready"},
        {"type": "step_update", "event_id": 2, "document_status": "ready"},  # never reached
    ]
    chunks = await _collect(_snapshot("processing", 0), 0, events)
    assert len(chunks) == 2  # snapshot + the ready event, then close


async def test_terminal_snapshot_closes_immediately() -> None:
    events = [{"type": "step_update", "event_id": 4, "document_status": "processing"}]
    chunks = await _collect(_snapshot("ready", 3), 3, events)
    assert len(chunks) == 1  # only the snapshot; a client on a done doc just resyncs


async def test_heartbeat_on_idle_tick() -> None:
    events = [None, {"type": "step_update", "event_id": 1, "document_status": "ready"}]
    chunks = await _collect(_snapshot("processing", 0), 0, events)
    assert chunks[1] == ": keepalive\n\n"


def test_sse_format() -> None:
    out = _format_sse({"type": "step_update", "event_id": 7, "document_status": "processing"})
    assert out.startswith("event: step_update\nid: 7\ndata: ")
    assert out.endswith("\n\n")
    assert json.loads(out.split("data: ", 1)[1].strip())["event_id"] == 7


# --- endpoint (cross-tenant / auth) -----------------------------------------


class FakeScalarSession:
    def __init__(self, document: Any) -> None:
        self._document = document

    async def scalar(self, _stmt: Any) -> Any:
        return self._document


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_events_requires_authentication() -> None:
    async with await _client() as client:
        response = await client.get(f"/documents/{uuid.uuid4()}/events")
    assert response.status_code == 401


async def test_events_cross_tenant_returns_404() -> None:
    user = CurrentUser(user_id=uuid.uuid4(), organization_id=uuid.uuid4())
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: FakeScalarSession(None)  # another org -> None
    try:
        async with await _client() as client:
            response = await client.get(f"/documents/{uuid.uuid4()}/events")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 404
