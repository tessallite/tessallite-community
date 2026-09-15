"""Bug-9602 regression tests for the glossary-share lifecycle.

The three mutating share routes must use the model advisory lock. Regeneration
must revoke and issue in one transaction so two concurrent requests cannot
leave two replacement links active. The real PostgreSQL interleaving proof is
in ``tests/integration/test_bug9602_glossary_share_concurrency_db.py``; these
route tests pin the session ownership, event ordering, and public responses.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.auth.middleware import CurrentUser
from shared.db.models import GlossaryShareToken
from src.api import glossary


def _user() -> CurrentUser:
    return CurrentUser(
        user_id=str(uuid.uuid4()),
        tenant_id="test-tenant",
        email="modeler@example.com",
    )


def _db(rows=()):
    db = AsyncMock()
    db.info = {"tenant_id": "test-tenant"}
    db.add = MagicMock()
    db.commit = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = list(rows)
    db.execute = AsyncMock(return_value=result)
    return db


def _tenant_db(db, counter: list[int] | None = None):
    async def _gen(_tenant_id):
        if counter is not None:
            counter.append(1)
        yield db

    return _gen


@pytest.mark.asyncio
async def test_issue_share_token_locks_before_persisting_and_emits_after_commit():
    project_id, model_id = uuid.uuid4(), uuid.uuid4()
    db = _db()
    events: list[object] = []

    async def _ensure(*_args, **_kwargs):
        events.append("ensure")

    async def _lock(*_args, **_kwargs):
        events.append("lock")

    async def _audit(*_args, **kwargs):
        events.append(("audit", kwargs["action"]))

    async def _commit():
        events.append("commit")

    async def _webhook(*args, **_kwargs):
        events.append(("webhook", args[1]))

    db.add.side_effect = lambda item: events.append(("add", item))
    db.commit.side_effect = _commit
    with (
        patch("src.api.glossary.get_tenant_db", _tenant_db(db)),
        patch("src.api.glossary.ensure_model_in_project", _ensure),
        patch("src.api.glossary.acquire_model_definition_lock", _lock),
        patch("src.api.glossary.audit_required", _audit),
        patch("src.api.glossary.emit_webhook", _webhook),
    ):
        response = await glossary.issue_share_token(project_id, model_id, _user())

    assert response["token"]
    assert response["frontend_path"] == f"/g/{response['token']}"
    assert [event for event in events if event in {"ensure", "lock", "commit"}] == [
        "ensure", "lock", "commit",
    ]
    assert events.index("lock") < next(
        index for index, event in enumerate(events) if isinstance(event, tuple) and event[0] == "add"
    )
    assert events.index("commit") < next(
        index for index, event in enumerate(events) if isinstance(event, tuple) and event[0] == "webhook"
    )
    added = next(event[1] for event in events if isinstance(event, tuple) and event[0] == "add")
    assert isinstance(added, GlossaryShareToken)
    assert added.model_id == model_id


@pytest.mark.asyncio
async def test_revoke_share_tokens_locks_before_read_modify_write():
    project_id, model_id = uuid.uuid4(), uuid.uuid4()
    rows = [
        types.SimpleNamespace(id=uuid.uuid4(), revoked_at=None),
        types.SimpleNamespace(id=uuid.uuid4(), revoked_at=None),
    ]
    db = _db(rows)
    events: list[object] = []

    async def _ensure(*_args, **_kwargs):
        events.append("ensure")

    async def _lock(*_args, **_kwargs):
        events.append("lock")

    async def _execute(*_args, **_kwargs):
        events.append("select")
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        return result

    async def _audit(*_args, **kwargs):
        events.append(("audit", kwargs["action"]))

    async def _commit():
        events.append("commit")

    async def _webhook(*args, **_kwargs):
        events.append(("webhook", args[1]))

    db.execute.side_effect = _execute
    db.commit.side_effect = _commit
    with (
        patch("src.api.glossary.get_tenant_db", _tenant_db(db)),
        patch("src.api.glossary.ensure_model_in_project", _ensure),
        patch("src.api.glossary.acquire_model_definition_lock", _lock),
        patch("src.api.glossary.audit_required", _audit),
        patch("src.api.glossary.emit_webhook", _webhook),
    ):
        response = await glossary.revoke_share_tokens(project_id, model_id, _user())

    assert response == {"revoked_count": 2}
    assert all(row.revoked_at is not None for row in rows)
    assert events.index("lock") < events.index("select")
    assert events.index("commit") < next(
        index for index, event in enumerate(events) if isinstance(event, tuple) and event[0] == "webhook"
    )


@pytest.mark.asyncio
async def test_regenerate_share_token_uses_one_session_commit_and_preserves_event_order():
    project_id, model_id = uuid.uuid4(), uuid.uuid4()
    old = types.SimpleNamespace(id=uuid.uuid4(), revoked_at=None)
    db = _db([old])
    sessions: list[int] = []
    events: list[object] = []

    async def _ensure(*_args, **_kwargs):
        events.append("ensure")

    async def _lock(*_args, **_kwargs):
        events.append("lock")

    async def _execute(*_args, **_kwargs):
        events.append("select")
        result = MagicMock()
        result.scalars.return_value.all.return_value = [old]
        return result

    async def _audit(*_args, **kwargs):
        events.append(("audit", kwargs["action"]))

    async def _commit():
        events.append("commit")

    async def _webhook(*args, **_kwargs):
        events.append(("webhook", args[1]))

    db.execute.side_effect = _execute
    db.add.side_effect = lambda item: events.append(("add", item))
    db.commit.side_effect = _commit
    with (
        patch("src.api.glossary.get_tenant_db", _tenant_db(db, sessions)),
        patch("src.api.glossary.ensure_model_in_project", _ensure),
        patch("src.api.glossary.acquire_model_definition_lock", _lock),
        patch("src.api.glossary.audit_required", _audit),
        patch("src.api.glossary.emit_webhook", _webhook),
    ):
        response = await glossary.regenerate_share_token(project_id, model_id, _user())

    assert sessions == [1], "regeneration must own one tenant session"
    assert db.commit.await_count == 1, "revoke and issue must commit atomically"
    assert response["token"]
    assert old.revoked_at is not None
    added = next(event[1] for event in events if isinstance(event, tuple) and event[0] == "add")
    assert added.model_id == model_id
    assert events.count("lock") == 1
    assert events.count("commit") == 1
    webhook_indices = [
        index for index, event in enumerate(events)
        if isinstance(event, tuple) and event[0] == "webhook"
    ]
    assert [events[index][1] for index in webhook_indices] == [
        "glossary.share_token.revoke",
        "glossary.share_token.issue",
    ]
    assert max(webhook_indices) > events.index("commit")
