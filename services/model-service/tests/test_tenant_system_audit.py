"""Bug-9306: tenant.create / tenant.update write a system_audit_events row.

F-022-03: tenant.delete already emitted pre-commit system_audit; create and
update did not. These tests drive the handlers with a mocked system session
and assert the SystemAuditEvent is added on that session before commit.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.db.models import SystemAuditEvent, SystemTenant
from shared.schemas.pydantic_models import TenantCreate, TenantUpdate
from src.api import tenants as tmod
from src.auth.middleware import CurrentUser

pytestmark = pytest.mark.unit


def _admin():
    return CurrentUser(
        user_id="root", tenant_id="__system__", email="root@x", role="system_admin"
    )


def _request(host: str = "203.0.113.9"):
    return SimpleNamespace(client=SimpleNamespace(host=host))


def _engine_stub():
    class _Conn:
        async def execute(self, *_a, **_k):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    class _Engine:
        def begin(self):
            return _Conn()

        async def dispose(self):
            return None

    return _Engine()


def _session_recording_add():
    sys_db = AsyncMock()
    added: list = []

    def _add(obj):
        added.append(obj)

    sys_db.add = _add
    sys_db.flush = AsyncMock()
    sys_db.commit = AsyncMock()
    sys_db.rollback = AsyncMock()

    async def _refresh(obj):
        if getattr(obj, "created_at", None) is None:
            obj.created_at = datetime.now(timezone.utc)
        obj.updated_at = datetime.now(timezone.utc)
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()

    sys_db.refresh = _refresh
    sys_db._added = added
    return sys_db


@pytest.mark.asyncio
async def test_bug_9306_create_tenant_writes_system_audit_row(monkeypatch):
    monkeypatch.setattr(tmod, "enforce_create_cap", AsyncMock())
    monkeypatch.setattr(tmod, "encrypt_str", lambda _u: b"enc")
    monkeypatch.setattr(
        tmod, "normalize_tenant_db_url", lambda _url, slug: f"postgresql+asyncpg://x/{slug}"
    )
    monkeypatch.setattr(tmod, "create_async_engine", lambda *_a, **_k: _engine_stub())

    sys_db = _session_recording_add()
    empty = MagicMock()
    empty.scalar_one_or_none.return_value = None
    sys_db.execute = AsyncMock(return_value=empty)

    out = await tmod.create_tenant(
        body=TenantCreate(slug="acme", display_name="Acme Corp"),
        request=_request(),
        sys_db=sys_db,
        _admin=_admin(),
    )

    events = [o for o in sys_db._added if isinstance(o, SystemAuditEvent)]
    tenants = [o for o in sys_db._added if isinstance(o, SystemTenant)]
    assert tenants and tenants[0].slug == "acme"
    assert len(events) == 1
    assert events[0].action == "tenant.create"
    assert events[0].tenant_slug == "acme"
    assert events[0].severity == "critical"
    sys_db.flush.assert_awaited()
    sys_db.commit.assert_awaited()
    assert out.slug == "acme"


@pytest.mark.asyncio
async def test_bug_9306_update_tenant_writes_system_audit_row(monkeypatch):
    existing = SystemTenant(
        slug="acme",
        display_name="Old Name",
        encrypted_db_url=b"enc",
        db_schema_prefix="acme",
        is_active=True,
    )
    existing.created_at = datetime.now(timezone.utc)

    sys_db = _session_recording_add()
    found = MagicMock()
    found.scalar_one_or_none.return_value = existing
    sys_db.execute = AsyncMock(return_value=found)

    out = await tmod.update_tenant(
        tenant_id="acme",
        body=TenantUpdate(display_name="New Name"),
        request=_request(),
        sys_db=sys_db,
        _admin=_admin(),
    )

    events = [o for o in sys_db._added if isinstance(o, SystemAuditEvent)]
    assert len(events) == 1
    assert events[0].action == "tenant.update"
    assert events[0].tenant_slug == "acme"
    assert events[0].severity == "critical"
    sys_db.flush.assert_awaited()
    sys_db.commit.assert_awaited()
    assert out.display_name == "New Name"
