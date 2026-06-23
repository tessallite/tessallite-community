"""
Tests for GET /api/v1/projects/{project_id}/models/{model_id}/refresh/stream.

Coverage:
  - Stream emits SSE-formatted events for active refresh runs.
  - Stream emits "done" event and closes when all runs reach terminal state.
  - Stream emits "connected" heartbeat as the first event.

Run from tessallite/services/model-service/:
    pytest tests/test_refresh_stream.py
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    make_mock_db,
)

_STREAM_URL = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/refresh/stream"

_DB_PATCH = "src.api.refresh_stream.get_tenant_db"
_SLEEP_PATCH = "src.api.refresh_stream.asyncio.sleep"

NOW = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _make_agg_run(status: str = "running") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        refresh_mode="full",
        started_at=NOW,
        completed_at=None,
        rows_written=None,
        error_message=None,
    )


def _make_pocket_run(status: str = "running") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        refresh_mode="full",
        started_at=NOW,
        completed_at=None,
        rows_written=None,
        error_message=None,
    )


def _db_returning(agg_runs=(), pocket_runs=()):
    db = make_mock_db()

    def _exec(stmt):
        text = str(stmt)
        result = MagicMock()
        if "AggregateRefreshRun" in text or "aggregate_refresh_runs" in text.lower():
            result.scalars.return_value.all.return_value = list(agg_runs)
        elif "PocketRefreshRun" in text or "pocket_refresh_runs" in text.lower():
            result.scalars.return_value.all.return_value = list(pocket_runs)
        else:
            result.scalars.return_value.all.return_value = []
            result.scalar_one_or_none.return_value = None
        return result

    db.execute = AsyncMock(side_effect=_exec)
    return db


# ---------------------------------------------------------------------------
# Heartbeat emitted as first event
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_emits_connected_heartbeat(client):
    """The stream always sends a 'connected' event as the first line."""
    run = _make_agg_run(status="completed")
    db = _db_returning(agg_runs=[run])

    with (
        patch(_DB_PATCH, async_gen_from(db)),
        patch(_SLEEP_PATCH, AsyncMock()),
    ):
        resp = await client.get(_STREAM_URL)

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert "event: connected" in resp.text


# ---------------------------------------------------------------------------
# Run data emitted as SSE data lines
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_emits_run_data(client):
    """Active run rows are emitted as 'data: {json}' lines."""
    run = _make_agg_run(status="completed")
    db = _db_returning(agg_runs=[run])

    with (
        patch(_DB_PATCH, async_gen_from(db)),
        patch(_SLEEP_PATCH, AsyncMock()),
    ):
        resp = await client.get(_STREAM_URL)

    body = resp.text
    assert "data:" in body
    assert str(run.id) in body


# ---------------------------------------------------------------------------
# Terminal state closes stream with "done" event
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_closes_on_terminal_state(client):
    """When all runs are terminal, stream ends with 'event: done'."""
    run = _make_agg_run(status="completed")
    db = _db_returning(agg_runs=[run])

    with (
        patch(_DB_PATCH, async_gen_from(db)),
        patch(_SLEEP_PATCH, AsyncMock()),
    ):
        resp = await client.get(_STREAM_URL)

    assert "event: done" in resp.text
