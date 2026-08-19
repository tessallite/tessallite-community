"""Tests for SSE streaming endpoint (G-10)."""
from __future__ import annotations

import json
import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from src.main import app
from src.auth.middleware import CurrentUser, get_current_user
from src.sse.events import EventPublisher
from .conftest import async_gen_from, make_mock_db

TEST_TENANT = "stream-tenant"
TEST_PROJECT_ID = uuid.uuid4()
TEST_CONV_ID = uuid.uuid4()


def _override_user():
    return CurrentUser(
        user_id="user@test.com",
        tenant_id=TEST_TENANT,
        email="user@test.com",
        role="tenant_admin",
    )


def _make_db_with_conv():
    db = make_mock_db()
    conv = types.SimpleNamespace(
        id=TEST_CONV_ID,
        project_id=TEST_PROJECT_ID,
        deleted_at=None,
    )
    db.get = AsyncMock(return_value=conv)
    db.scalar = AsyncMock(return_value=-1)
    return db


def _collect_sse(text: str) -> list[dict]:
    """Parse SSE response body into list of {'event': name, 'data': dict}."""
    events = []
    current_event: str | None = None
    for line in text.splitlines():
        if line.startswith("event:"):
            current_event = line[6:].strip()
        elif line.startswith("data:"):
            raw = line[5:].strip()
            if raw:
                try:
                    events.append({"event": current_event, "data": json.loads(raw)})
                except json.JSONDecodeError:
                    pass
            current_event = None
    return events


@pytest.mark.asyncio
async def test_stream_endpoint_returns_event_stream_content_type():
    """POST /messages/stream must respond with text/event-stream."""
    db = _make_db_with_conv()
    app.dependency_overrides[get_current_user] = _override_user

    async def fake_run(**kwargs):
        pub: EventPublisher = kwargs["publisher"]
        await pub.emit("turn.started", turn_index=0)
        await pub.close()

    with (
        patch("src.api.conversations._run_turn_into_publisher", side_effect=fake_run),
        patch("src.api.conversations.get_tenant_db", async_gen_from(db)),
        patch("src.api.conversations._require_project_access_and_agent", new_callable=AsyncMock),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            resp = await client.post(
                f"/api/v1/projects/{TEST_PROJECT_ID}"
                f"/agent/conversations/{TEST_CONV_ID}/messages/stream",
                json={"text": "hello"},
            )

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")

    app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_stream_narration_delta_events_accumulate():
    """narration.delta events should be emitted one per token."""
    db = _make_db_with_conv()
    app.dependency_overrides[get_current_user] = _override_user
    tokens = ["The ", "answer ", "is ", "42."]

    async def fake_run(**kwargs):
        pub: EventPublisher = kwargs["publisher"]
        await pub.emit("turn.started", turn_index=0)
        for tok in tokens:
            await pub.emit("narration.delta", text=tok)
        await pub.emit(
            "turn.completed",
            turn_id=str(uuid.uuid4()),
            status="ok",
            latency_ms=100,
            answer_text="".join(tokens),
            citations=[],
        )
        await pub.close()

    with (
        patch("src.api.conversations._run_turn_into_publisher", side_effect=fake_run),
        patch("src.api.conversations.get_tenant_db", async_gen_from(db)),
        patch("src.api.conversations._require_project_access_and_agent", new_callable=AsyncMock),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            resp = await client.post(
                f"/api/v1/projects/{TEST_PROJECT_ID}"
                f"/agent/conversations/{TEST_CONV_ID}/messages/stream",
                json={"text": "what is the answer?"},
            )

    events = _collect_sse(resp.text)
    delta_texts = [e["data"]["text"] for e in events if e.get("event") == "narration.delta"]
    assert delta_texts == tokens

    app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_stream_ends_with_closed_sentinel():
    """Stream body must contain the turn.stream.closed sentinel line."""
    db = _make_db_with_conv()
    app.dependency_overrides[get_current_user] = _override_user

    async def fake_run(**kwargs):
        pub: EventPublisher = kwargs["publisher"]
        await pub.emit(
            "turn.completed",
            turn_id=str(uuid.uuid4()),
            status="ok",
            latency_ms=50,
            answer_text="done",
            citations=[],
        )
        await pub.close()

    with (
        patch("src.api.conversations._run_turn_into_publisher", side_effect=fake_run),
        patch("src.api.conversations.get_tenant_db", async_gen_from(db)),
        patch("src.api.conversations._require_project_access_and_agent", new_callable=AsyncMock),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            resp = await client.post(
                f"/api/v1/projects/{TEST_PROJECT_ID}"
                f"/agent/conversations/{TEST_CONV_ID}/messages/stream",
                json={"text": "done?"},
            )

    assert "turn.stream.closed" in resp.text

    app.dependency_overrides.pop(get_current_user, None)
