"""Bug-8380: consistent_read helper uses REPEATABLE READ isolation for
live-model snapshots, preventing mixed-time-state export bundles.

Verifies the contract: consistent_snapshot opens a REPEATABLE READ session
from the NullPool snapshot factory and passes it to snapshot_model, so
every SELECT in the serialiser observes one consistent committed state.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

pytestmark = pytest.mark.unit


class _FakeSessionCtx:
    """Mimics async_sessionmaker().__call__() which returns an async context
    manager (a synchronous call, not a coroutine)."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        pass


@pytest.mark.asyncio
async def test_consistent_snapshot_uses_repeatable_read():
    """Bug-8380: consistent_snapshot must establish REPEATABLE READ isolation
    before calling snapshot_model, using the NullPool snapshot factory."""
    model_id = uuid4()
    tenant_id = "test-tenant"
    expected_snap = {"schema_version": 3, "model": {"id": str(model_id)}}

    # Track that connection() was called with REPEATABLE READ
    mock_session = AsyncMock()
    mock_session.info = {}
    mock_session.connection = AsyncMock()

    mock_snapshot_model = AsyncMock(return_value=expected_snap)

    # async_sessionmaker() is a SYNCHRONOUS callable that returns an async
    # context manager, so use MagicMock (not AsyncMock) for the factory.
    mock_factory = MagicMock(return_value=_FakeSessionCtx(mock_session))

    with (
        patch(
            "shared.model_snapshot.consistent_read.get_tenant_snapshot_session_factory",
            AsyncMock(return_value=mock_factory),
        ),
        patch(
            "shared.model_snapshot.consistent_read.snapshot_model",
            mock_snapshot_model,
        ),
    ):
        from shared.model_snapshot.consistent_read import consistent_snapshot

        result = await consistent_snapshot(tenant_id, model_id)

    assert result == expected_snap
    # Verify REPEATABLE READ was established
    mock_session.connection.assert_awaited_once_with(
        execution_options={"isolation_level": "REPEATABLE READ"}
    )
    # Verify tenant_id was set in session info
    assert mock_session.info.get("tenant_id") == tenant_id
    # Verify snapshot_model was called with the snapshot session, not the
    # caller's main session
    mock_snapshot_model.assert_awaited_once_with(
        model_id, mock_session, include_versions=False,
    )


@pytest.mark.asyncio
async def test_consistent_snapshot_forwards_include_versions():
    """Bug-8380: include_versions kwarg is forwarded to snapshot_model
    (needed by project export v2 bundles)."""
    model_id = uuid4()
    tenant_id = "test-tenant"
    expected_snap = {"schema_version": 3, "model": {"id": str(model_id)}}

    mock_session = AsyncMock()
    mock_session.info = {}
    mock_session.connection = AsyncMock()

    mock_snapshot_model = AsyncMock(return_value=expected_snap)

    mock_factory = MagicMock(return_value=_FakeSessionCtx(mock_session))

    with (
        patch(
            "shared.model_snapshot.consistent_read.get_tenant_snapshot_session_factory",
            AsyncMock(return_value=mock_factory),
        ),
        patch(
            "shared.model_snapshot.consistent_read.snapshot_model",
            mock_snapshot_model,
        ),
    ):
        from shared.model_snapshot.consistent_read import consistent_snapshot

        result = await consistent_snapshot(
            tenant_id, model_id, include_versions=True,
        )

    assert result == expected_snap
    mock_snapshot_model.assert_awaited_once_with(
        model_id, mock_session, include_versions=True,
    )
