"""D20/Bug-9613 session-owned webhook persistence guards."""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.webhooks import dispatcher


def _endpoint() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        url="https://receiver.example/hook",
        signing_secret=b"encrypted",
        event_filters=["*"],
        is_active=True,
    )


@pytest.mark.asyncio
async def test_bug9613_tenant_mismatch_fails_before_any_database_access() -> None:
    db = AsyncMock()
    db.info = {"tenant_id": "tenant-b"}

    with pytest.raises(
        dispatcher.WebhookPersistError,
        match="session identity mismatch",
    ):
        await dispatcher.emit_webhook_with_session(
            db, "tenant-a", "refresh.completed", {"run_id": "r1"},
        )

    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_bug9613_persistence_failure_rolls_back_and_raises() -> None:
    db = AsyncMock()
    db.info = {"tenant_id": "tenant-a"}
    db.execute.side_effect = RuntimeError("database unavailable")

    with pytest.raises(dispatcher.WebhookPersistError):
        await dispatcher.emit_webhook_with_session(
            db, "tenant-a", "refresh.completed", {"run_id": "r1"},
        )

    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_bug9613_public_wrapper_preserves_session_owning_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = AsyncMock()
    db.info = {"tenant_id": "tenant-a"}
    provider_closed = False

    async def tenant_db(_tenant_id: str):
        nonlocal provider_closed
        try:
            yield db
        finally:
            provider_closed = True

    owned = AsyncMock(return_value=dispatcher.WebhookEmitResult())
    monkeypatch.setattr(dispatcher, "get_tenant_db", tenant_db)
    monkeypatch.setattr(dispatcher, "emit_webhook_with_session", owned)

    await dispatcher.emit_webhook(
        "tenant-a", "refresh.completed", {"run_id": "r1"},
    )
    owned.assert_awaited_once_with(
        db, "tenant-a", "refresh.completed", {"run_id": "r1"},
    )
    assert provider_closed is True


@pytest.mark.asyncio
async def test_bug9613_public_wrapper_normalises_session_acquisition_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def tenant_db(_tenant_id: str):
        if False:
            yield None
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(dispatcher, "get_tenant_db", tenant_db)
    with pytest.raises(
        dispatcher.WebhookPersistError,
        match="Failed to acquire tenant session",
    ):
        await dispatcher.emit_webhook(
            "tenant-a", "refresh.completed", {"run_id": "r1"},
        )


@pytest.mark.asyncio
async def test_bug9613_commit_precedes_background_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint_result = MagicMock()
    endpoint_result.scalars.return_value.all.return_value = [_endpoint()]
    added = []
    db = AsyncMock()
    db.info = {"tenant_id": "tenant-a"}
    db.execute.return_value = endpoint_result
    db.add = MagicMock(side_effect=added.append)

    async def assign_delivery_id() -> None:
        added[-1].id = uuid.uuid4()

    db.flush.side_effect = assign_delivery_id
    spawned: list[tuple[str, list[uuid.UUID]]] = []

    async def fake_dispatch(
        tenant_id: str, delivery_ids: list[uuid.UUID],
    ) -> None:
        spawned.append((tenant_id, delivery_ids))

    def spawn(coro):
        assert db.commit.await_count == 1
        coro.close()

    monkeypatch.setattr(dispatcher, "seal_destination_url", lambda url: url)
    monkeypatch.setattr(dispatcher, "_dispatch_queued_deliveries", fake_dispatch)
    monkeypatch.setattr(dispatcher, "_spawn_background", spawn)
    result = await dispatcher.emit_webhook_with_session(
        db, "tenant-a", "refresh.completed", {"run_id": "r1"},
    )

    assert result.endpoints_matched == 1
    assert result.persisted == 1
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()
    assert spawned == []  # The test spawn seam owns the coroutine after commit.
