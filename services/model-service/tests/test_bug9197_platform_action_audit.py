"""Bug-9197 — platform actions must leave durable audit evidence.

Role grants/revokes, licence install/uninstall and tenant delete are the
security-relevant platform mutations. ``tenant.delete`` and ``access.grant``
already had guards; ``access.revoke``, ``license.install`` and
``license.uninstall`` did not, so nothing prevented the audit call being
dropped from those handlers again.

The licence tests drive the REAL ``system_audit`` (not a mock of it), so they
also pin the Bug-9552 contract: the pre-migration probe must recognise the
system audit table, otherwise ``system_audit`` returns ``None`` and every
platform-plane event is silently discarded even though the handler calls it.

Contract under test, per action:
- the audit event is emitted on the SAME session as the mutation, and
- it is emitted BEFORE ``commit``, so a failed audit write rolls the mutation
  back instead of leaving an unevidenced state change.
"""
from __future__ import annotations

import re
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.db.models import SystemAuditEvent
from src.api import access as access_mod
from src.api import admin as admin_mod
from src.auth.middleware import CurrentUser

pytestmark = pytest.mark.unit

_AUDIT_TABLE = (
    f"{SystemAuditEvent.__table__.schema}.{SystemAuditEvent.__table__.name}"
)


def _system_admin() -> CurrentUser:
    return CurrentUser(
        user_id="root",
        tenant_id="__system__",
        email="root@example.com",
        role="system_admin",
    )


def _recording_system_session():
    """A system session that records ``add`` order against ``commit``."""
    db = AsyncMock()
    events: list = []
    order: list[str] = []

    def _add(obj):
        events.append(obj)
        order.append("add")

    async def _commit():
        order.append("commit")

    # Model a real database for the Bug-9552 probe: ``to_regclass`` returns a
    # value ONLY for the table that actually exists. A probe naming any other
    # table (as the pre-fix literal did) resolves to NULL, ``system_audit``
    # skips the event, and these tests fail — which is the point.
    async def _execute(statement, params=None):
        if params:
            target = str(next(iter(params.values())))
        else:
            match = re.search(
                r"to_regclass\(\s*'([^']+)'\s*\)", str(statement)
            )
            target = match.group(1) if match else ""
        result = MagicMock()
        result.scalar.return_value = target if target == _AUDIT_TABLE else None
        return result

    db.execute = _execute
    db.add = _add
    db.flush = AsyncMock()
    db.commit = _commit
    db.rollback = AsyncMock()
    db._events = events
    db._order = order
    return db


def _single_session(db):
    async def _gen():
        yield db

    return _gen


def _audit_actions(db) -> list[str]:
    return [
        e.action for e in db._events if isinstance(e, SystemAuditEvent)
    ]


@pytest.mark.asyncio
async def test_bug9197_license_install_emits_system_audit(monkeypatch):
    db = _recording_system_session()
    licence = SimpleNamespace(license_id="LIC-1", edition="enterprise")

    monkeypatch.setattr(admin_mod, "get_system_db", _single_session(db))
    monkeypatch.setattr(admin_mod, "verify_license", lambda *a, **k: licence)
    monkeypatch.setattr(admin_mod, "build_registry", lambda *a, **k: {})
    monkeypatch.setattr(admin_mod, "license_public_keys", lambda *a, **k: {})
    monkeypatch.setattr(admin_mod, "store_license_doc", AsyncMock())
    monkeypatch.setattr(admin_mod, "reload_license_manager", AsyncMock())
    monkeypatch.setattr(admin_mod, "emit_webhook", AsyncMock())
    monkeypatch.setattr(admin_mod, "_license_status", AsyncMock(return_value={}))

    await admin_mod.install_license(body={"any": "doc"}, current_user=_system_admin())

    assert "license.install" in _audit_actions(db)
    assert db._order.index("add") < db._order.index("commit"), (
        "the licence audit event must be written before commit"
    )


@pytest.mark.asyncio
async def test_bug9197_license_uninstall_emits_system_audit(monkeypatch):
    db = _recording_system_session()

    monkeypatch.setattr(admin_mod, "get_system_db", _single_session(db))
    monkeypatch.setattr(admin_mod, "clear_license_doc", AsyncMock(return_value=True))
    monkeypatch.setattr(admin_mod, "reload_license_manager", AsyncMock())
    monkeypatch.setattr(admin_mod, "emit_webhook", AsyncMock())
    monkeypatch.setattr(
        admin_mod, "load_license_doc_from_db", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(admin_mod, "_license_status", AsyncMock(return_value={}))

    await admin_mod.uninstall_license(current_user=_system_admin())

    assert "license.uninstall" in _audit_actions(db)
    assert db._order.index("add") < db._order.index("commit"), (
        "the licence audit event must be written before commit"
    )


@pytest.mark.asyncio
async def test_bug9197_access_revoke_emits_required_audit(monkeypatch):
    project_id = uuid.uuid4()
    binding_id = uuid.uuid4()
    binding = SimpleNamespace(
        id=binding_id,
        project_id=project_id,
        user_identity="analyst@example.com",
        role="viewer",
    )

    order: list[str] = []
    db = AsyncMock()
    db.get = AsyncMock(return_value=binding)

    async def _delete(_obj):
        order.append("delete")

    async def _commit():
        order.append("commit")

    db.delete = _delete
    db.commit = _commit

    calls: list[dict] = []

    async def _audit_required(_db, **kwargs):
        order.append("audit")
        calls.append(kwargs)
        return None

    monkeypatch.setattr(access_mod, "get_tenant_db", lambda _t: _single_session(db)())
    monkeypatch.setattr(access_mod, "audit_required", _audit_required)
    monkeypatch.setattr(access_mod, "emit_webhook", AsyncMock())

    user = CurrentUser(
        user_id="admin",
        tenant_id="acme",
        email="admin@example.com",
        role="admin",
    )
    await access_mod.revoke_access(
        project_id=project_id, binding_id=binding_id, current_user=user
    )

    assert [c["action"] for c in calls] == ["access.revoke"]
    assert calls[0]["target_id"] == binding_id
    assert calls[0]["detail"]["role"] == "viewer"
    # Evidence first: the binding must not be deleted or committed before the
    # audit write, or a failed audit leaves an unevidenced revocation.
    assert order.index("audit") < order.index("delete") < order.index("commit")
