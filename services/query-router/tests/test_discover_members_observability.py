"""Tests for discover-members observability on early-exit paths (Bug-5415).

Verifies that _handle_discover_members writes a QueryLog row with
status='error' when it early-returns empty on SemanticBindingError
or on no resolved dimensions — paths that previously had no
observability at all.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ir.logical_query import SemanticBindingError


# ---------------------------------------------------------------------------
# Unit test for _log_discover_members_early_exit
# ---------------------------------------------------------------------------

class _FakeDB:
    def __init__(self):
        self.added: list = []
        self.committed = False

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass


@pytest.mark.asyncio
async def test_log_discover_members_early_exit_records_error():
    """Bug-5415: the helper must write a QueryLog row with status='error'."""
    from src.api.routes import _log_discover_members_early_exit

    db = _FakeDB()
    model_id = str(uuid.uuid4())
    await _log_discover_members_early_exit(
        db, model_id, "user@example.com", "fp123", "region",
        error_type="binding_error",
        error_detail="Dimension 'region' not found",
    )
    assert len(db.added) == 1
    entry = db.added[0]
    assert entry.status == "error"
    assert entry.error_type == "binding_error"
    assert "region" in entry.error_detail
    assert entry.protocol == "discover_members"
    assert db.committed


# ---------------------------------------------------------------------------
# Integration: _handle_discover_members early-exit paths
# ---------------------------------------------------------------------------

def _user():
    return types.SimpleNamespace(
        user_id="user@example.com",
        tenant_id="test-tenant",
        email="user@example.com",
        role="member",
    )


@pytest.mark.asyncio
async def test_semantic_binding_error_logs_and_returns_empty():
    """Bug-5415: SemanticBindingError early-return must persist a QueryLog
    row before returning the empty response."""
    from src.api.routes import _handle_discover_members

    model_id = str(uuid.uuid4())
    body = types.SimpleNamespace(
        model_id=model_id,
        dimension_name="nonexistent",
        persona_id=None,
    )

    log_calls: list[dict] = []

    async def _capture_log(db, mid, user, fp, dim, *, error_type, error_detail):
        log_calls.append({
            "model_id": mid,
            "error_type": error_type,
            "error_detail": error_detail,
        })

    db = AsyncMock()

    with (
        patch(
            "src.api.routes.bind_query_to_model",
            AsyncMock(side_effect=SemanticBindingError("Dimension 'nonexistent' not found")),
        ),
        patch(
            "src.api.routes._log_discover_members_early_exit",
            _capture_log,
        ),
    ):
        resp = await _handle_discover_members(body, db, current_user=_user())

    assert resp.members == []
    assert resp.levels == []
    assert len(log_calls) == 1
    assert log_calls[0]["error_type"] == "binding_error"


@pytest.mark.asyncio
async def test_empty_resolved_dimensions_logs_and_returns_empty():
    """Bug-5415: when bind succeeds but resolves to zero dimensions,
    the early return must still log."""
    from src.api.routes import _handle_discover_members

    model_id = str(uuid.uuid4())
    body = types.SimpleNamespace(
        model_id=model_id,
        dimension_name="phantom",
        persona_id=None,
    )

    log_calls: list[dict] = []

    async def _capture_log(db, mid, user, fp, dim, *, error_type, error_detail):
        log_calls.append({
            "error_type": error_type,
            "error_detail": error_detail,
        })

    bound = MagicMock()
    bound.resolved_dimensions = []  # empty

    db = AsyncMock()

    with (
        patch("src.api.routes.bind_query_to_model", AsyncMock(return_value=bound)),
        patch("src.api.routes._log_discover_members_early_exit", _capture_log),
    ):
        resp = await _handle_discover_members(body, db, current_user=_user())

    assert resp.members == []
    assert len(log_calls) == 1
    assert log_calls[0]["error_type"] == "no_resolved_dimensions"
