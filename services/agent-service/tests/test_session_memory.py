"""Tests for agent session memory management (Phase 3, Block E)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from src.prompt.assembler import _format_history, _truncation_boundary
from .conftest import (
    TEST_PROJECT_ID,
    TEST_TENANT,
    async_gen_from,
    make_mock_db,
    make_turn,
)


class TestTruncationBoundary:

    def test_depth_greater_than_turns(self):
        turns = [make_turn(turn_index=i) for i in range(5)]
        assert _truncation_boundary(turns, 10) == 0

    def test_depth_equal_to_turns(self):
        turns = [make_turn(turn_index=i) for i in range(5)]
        assert _truncation_boundary(turns, 5) == 0

    def test_depth_less_than_turns(self):
        turns = [make_turn(turn_index=i) for i in range(10)]
        assert _truncation_boundary(turns, 5) == 5

    def test_depth_one(self):
        turns = [make_turn(turn_index=i) for i in range(3)]
        assert _truncation_boundary(turns, 1) == 2

    def test_depth_zero_returns_zero(self):
        turns = [make_turn(turn_index=i) for i in range(5)]
        assert _truncation_boundary(turns, 0) == 0

    def test_empty_turns(self):
        assert _truncation_boundary([], 5) == 0


class TestFormatHistory:

    def test_no_turns(self):
        assert _format_history([]) == "(first turn — no prior context)"

    def test_all_within_depth(self):
        turns = [
            make_turn(turn_index=0, user_message="Q1", answer_text="A1"),
            make_turn(turn_index=1, user_message="Q2", answer_text="A2"),
        ]
        result = _format_history(turns, session_history_depth=5)
        assert "User (turn 1): Q1" in result
        assert "Assistant: A1" in result
        assert "User (turn 2): Q2" in result
        assert "Assistant: A2" in result
        assert "[earlier question]" not in result

    def test_truncation_older_turns(self):
        conv_id = uuid.uuid4()
        turns = [
            make_turn(conversation_id=conv_id, turn_index=i, user_message=f"Q{i}", answer_text=f"A{i}")
            for i in range(10)
        ]
        result = _format_history(turns, session_history_depth=5)
        for i in range(5):
            assert f"[earlier question] (turn {i + 1}): Q{i}" in result
            assert f"Assistant: A{i}" not in result
        for i in range(5, 10):
            assert f"User (turn {i + 1}): Q{i}" in result
            assert f"Assistant: A{i}" in result

    def test_depth_one_only_last_full(self):
        turns = [
            make_turn(turn_index=0, user_message="old", answer_text="old-answer"),
            make_turn(turn_index=1, user_message="recent", answer_text="recent-answer"),
        ]
        result = _format_history(turns, session_history_depth=1)
        assert "[earlier question] (turn 1): old" in result
        assert "old-answer" not in result
        assert "User (turn 2): recent" in result
        assert "Assistant: recent-answer" in result

    def test_no_answer_text(self):
        turns = [make_turn(turn_index=0, user_message="Q", answer_text=None)]
        result = _format_history(turns, session_history_depth=5)
        assert "User (turn 1): Q" in result
        assert "Assistant" not in result


class TestConversationStats:

    @pytest.mark.asyncio
    async def test_stats_empty(self, client):
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[0, None])

        with patch("src.api.maintenance.get_tenant_db", async_gen_from(db)):
            resp = await client.get(
                f"/api/v1/admin/agent/conversations/stats?project_id={TEST_PROJECT_ID}"
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["oldest_at"] is None

    @pytest.mark.asyncio
    async def test_stats_with_data(self, client):
        db = make_mock_db()
        oldest = datetime(2025, 6, 1, tzinfo=timezone.utc)
        db.scalar = AsyncMock(side_effect=[42, oldest])

        with patch("src.api.maintenance.get_tenant_db", async_gen_from(db)):
            resp = await client.get(
                f"/api/v1/admin/agent/conversations/stats?project_id={TEST_PROJECT_ID}"
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 42
        assert "2025-06-01" in data["oldest_at"]


class TestPurgeConversations:

    @pytest.mark.asyncio
    async def test_purge_by_age(self, client):
        db = make_mock_db()
        conv_id = uuid.uuid4()

        conv_result = MagicMock()
        conv_result.all.return_value = [(conv_id,)]
        db.execute = AsyncMock(return_value=conv_result)

        with patch("src.api.maintenance.get_tenant_db", async_gen_from(db)):
            resp = await client.delete(
                f"/api/v1/admin/agent/conversations/purge?project_id={TEST_PROJECT_ID}&older_than_days=30"
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted_conversations"] == 1
        assert data["older_than_days"] == 30

    @pytest.mark.asyncio
    async def test_purge_all(self, client):
        db = make_mock_db()
        ids = [uuid.uuid4() for _ in range(5)]

        conv_result = MagicMock()
        conv_result.all.return_value = [(i,) for i in ids]
        db.execute = AsyncMock(return_value=conv_result)

        with patch("src.api.maintenance.get_tenant_db", async_gen_from(db)):
            resp = await client.delete(
                f"/api/v1/admin/agent/conversations/purge?project_id={TEST_PROJECT_ID}&older_than_days=0"
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted_conversations"] == 5
        assert data["older_than_days"] == 0

    @pytest.mark.asyncio
    async def test_purge_nothing(self, client):
        db = make_mock_db()

        conv_result = MagicMock()
        conv_result.all.return_value = []
        db.execute = AsyncMock(return_value=conv_result)

        with patch("src.api.maintenance.get_tenant_db", async_gen_from(db)):
            resp = await client.delete(
                f"/api/v1/admin/agent/conversations/purge?project_id={TEST_PROJECT_ID}&older_than_days=365"
            )
        assert resp.status_code == 200
        assert resp.json()["deleted_conversations"] == 0
