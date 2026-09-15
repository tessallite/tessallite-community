"""Bug-9614: webhook events describe only supported, committed mutations."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from shared.db.models import SystemAuditEvent, SystemTenant
from shared.webhooks.event_types import (
    WEBHOOK_EVENT_TYPES,
    event_catalogue,
    is_valid_filter,
)
from shared.schemas.pydantic_models import UserAccessBindingCreate
from src.api import access as access_api
from src.api import tenants as tenants_api
from src.auth.middleware import (
    CurrentEmbedUser,
    CurrentServiceUser,
    CurrentUser,
    get_current_user,
)
from src.main import app

pytestmark = pytest.mark.unit


class _ScalarResult:
    def scalar_one_or_none(self):
        return None


class _ExistenceResult:
    def first(self):
        return None


class _RecordingSession:
    def __init__(self, events: list, *, commit_fails: bool = False):
        self.events = events
        self.commit_fails = commit_fails
        self.added = []

    def add(self, value):
        self.added.append(value)

    async def execute(self, _statement):
        return _ScalarResult()

    async def flush(self):
        self.events.append("flush")
        for value in self.added:
            if getattr(value, "id", None) is None:
                value.id = uuid4()
            if getattr(value, "created_at", None) is None:
                value.created_at = datetime.now(timezone.utc)

    async def commit(self):
        self.events.append("commit")
        if self.commit_fails:
            raise RuntimeError("injected authoritative commit failure")

    async def refresh(self, _value):
        self.events.append("refresh")

    async def delete(self, _value):
        self.events.append("delete")


class _TenantResult:
    def __init__(self, *, tenant=None, scalar=None):
        self._tenant = tenant
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._tenant

    def scalar(self):
        return self._scalar


class _TenantDeleteSession:
    def __init__(
        self,
        tenant: SystemTenant,
        events: list,
        *,
        fail_commit_number: int | None = None,
    ):
        self.tenant = tenant
        self.events = events
        self.fail_commit_number = fail_commit_number
        self.commit_count = 0
        self.execute_count = 0
        self.added: list = []
        self.deleted: list = []

    async def execute(self, _statement):
        self.execute_count += 1
        if self.execute_count == 1:
            self.events.append("tenant.lookup")
            return _TenantResult(tenant=self.tenant)
        self.events.append("audit.table_probe")
        return _TenantResult(scalar="tess_system.system_audit_events")

    def add(self, value):
        self.added.append(value)
        self.events.append("audit.add")

    async def flush(self):
        self.events.append("audit.flush")

    async def commit(self):
        self.commit_count += 1
        marker = f"system.commit.{self.commit_count}"
        self.events.append(marker)
        if self.commit_count == self.fail_commit_number:
            raise RuntimeError(f"injected failure at {marker}")

    async def delete(self, value):
        self.deleted.append(value)
        self.events.append("tenant.row.delete")


class _TenantDeleteConnection:
    def __init__(self, events: list, *, fail_schema_drop: bool):
        self.events = events
        self.fail_schema_drop = fail_schema_drop

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, statement):
        sql = str(statement)
        self.events.append(("schema.drop", sql))
        if self.fail_schema_drop:
            raise RuntimeError("injected tenant schema drop failure")


class _TenantDeleteEngine:
    def __init__(self, events: list, *, fail_schema_drop: bool = False):
        self.events = events
        self.fail_schema_drop = fail_schema_drop

    def begin(self):
        return _TenantDeleteConnection(
            self.events,
            fail_schema_drop=self.fail_schema_drop,
        )

    async def dispose(self):
        self.events.append("engine.dispose")


async def _yield_session(session):
    yield session


def _current_user() -> CurrentUser:
    return CurrentUser(
        user_id="admin@example.com",
        tenant_id="acme",
        email="admin@example.com",
        role="tenant_admin",
    )


def _system_admin() -> CurrentUser:
    return CurrentUser(
        user_id="root@example.com",
        tenant_id="__system__",
        email="root@example.com",
        role="system_admin",
    )


def _tenant() -> SystemTenant:
    tenant = SystemTenant(
        slug="acme",
        display_name="Acme Corp",
        encrypted_db_url=b"encrypted",
        db_schema_prefix="acme",
        is_active=True,
    )
    tenant.id = uuid4()
    return tenant


def _request():
    return SimpleNamespace(client=SimpleNamespace(host="203.0.113.10"))


async def _emit_recorder(events: list, tenant_id: str, event_type: str, payload: dict):
    events.append(("emit", tenant_id, event_type, payload))


async def _run_grant(events: list, *, commit_fails: bool):
    session = _RecordingSession(events, commit_fails=commit_fails)
    project_id = uuid4()
    body = UserAccessBindingCreate(
        user_identity="viewer@example.com",
        role="viewer",
    )
    with (
        patch.object(access_api, "get_tenant_db", lambda _tenant: _yield_session(session)),
        patch.object(access_api, "_lock_user_project_grants", AsyncMock()),
        patch.object(access_api, "_load_user_project_bindings", AsyncMock(return_value=[])),
        patch.object(access_api, "audit_required", AsyncMock()),
        patch.object(access_api, "emit_webhook", lambda *args: _emit_recorder(events, *args)),
        patch.object(
            access_api.UserAccessBindingResponse,
            "model_validate",
            return_value=SimpleNamespace(role="viewer"),
        ),
    ):
        return await access_api.grant_access(project_id, body, _current_user())


async def _run_repair(events: list, *, commit_fails: bool):
    session = _RecordingSession(events, commit_fails=commit_fails)
    project_id = uuid4()
    session.get = AsyncMock(return_value=SimpleNamespace(id=project_id, slug="legacy"))
    session.execute = AsyncMock(return_value=_ExistenceResult())
    body = access_api.ProjectAdminRepairRequest(user_identity="owner@example.com")
    with (
        patch.object(access_api, "get_tenant_db", lambda _tenant: _yield_session(session)),
        patch.object(access_api, "_lock_project_grants", AsyncMock()),
        patch.object(access_api, "audit_required", AsyncMock()),
        patch.object(access_api, "emit_webhook", lambda *args: _emit_recorder(events, *args)),
        patch.object(
            access_api.UserAccessBindingResponse,
            "model_validate",
            return_value=SimpleNamespace(role="admin"),
        ),
    ):
        return await access_api.repair_project_admin_binding(
            project_id, body, _current_user()
        )


async def _run_revoke(events: list, *, commit_fails: bool):
    session = _RecordingSession(events, commit_fails=commit_fails)
    project_id = uuid4()
    binding_id = uuid4()
    session.get = AsyncMock(
        return_value=SimpleNamespace(
            id=binding_id,
            project_id=project_id,
            user_identity="viewer@example.com",
            role="viewer",
        )
    )
    with (
        patch.object(access_api, "get_tenant_db", lambda _tenant: _yield_session(session)),
        patch.object(access_api, "audit_required", AsyncMock()),
        patch.object(access_api, "emit_webhook", lambda *args: _emit_recorder(events, *args)),
    ):
        await access_api.revoke_access(project_id, binding_id, _current_user())


def _tenant_delete_context(
    events: list,
    *,
    fail_schema_drop: bool = False,
    fail_commit_number: int | None = None,
):
    tenant = _tenant()
    session = _TenantDeleteSession(
        tenant,
        events,
        fail_commit_number=fail_commit_number,
    )
    engine = _TenantDeleteEngine(events, fail_schema_drop=fail_schema_drop)
    emit = AsyncMock()
    evict = AsyncMock()
    return SimpleNamespace(
        tenant=tenant,
        session=session,
        engine=engine,
        emit=emit,
        evict=evict,
    )


async def _run_tenant_delete(context):
    with (
        patch.object(tenants_api, "decrypt_str", return_value="postgresql+asyncpg://db/system"),
        patch.object(
            tenants_api,
            "normalize_tenant_db_url",
            return_value="postgresql+asyncpg://db/acme",
        ),
        patch.object(
            tenants_api,
            "create_async_engine",
            return_value=context.engine,
        ),
        patch.object(tenants_api, "evict_tenant_engine", context.evict),
        patch.object(tenants_api, "emit_webhook", context.emit, create=True),
    ):
        await tenants_api.delete_tenant(
            tenant_id=context.tenant.slug,
            request=_request(),
            sys_db=context.session,
            _admin=_system_admin(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [_run_grant, _run_repair, _run_revoke])
async def test_bug9614_access_webhooks_emit_once_after_authoritative_commit(operation):
    events: list = []

    await operation(events, commit_fails=False)

    emitted = [event for event in events if isinstance(event, tuple)]
    assert len(emitted) == 1
    assert events.index("commit") < events.index(emitted[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [_run_grant, _run_repair, _run_revoke])
async def test_bug9614_failed_access_commit_emits_no_webhook(operation):
    events: list = []

    with pytest.raises(RuntimeError, match="injected authoritative commit failure"):
        await operation(events, commit_fails=True)

    assert not [event for event in events if isinstance(event, tuple)]


def test_bug9614_tenant_deleted_is_retired_from_tenant_scoped_catalogue():
    assert "tenant.deleted" not in WEBHOOK_EVENT_TYPES
    assert "tenant.deleted" not in {entry["value"] for entry in event_catalogue()}
    assert is_valid_filter("tenant.deleted") is False


@pytest.mark.asyncio
async def test_bug9614_successful_tenant_deletion_keeps_system_audit_without_event():
    events: list = []
    context = _tenant_delete_context(events)

    await _run_tenant_delete(context)

    audit_rows = [
        value
        for value in context.session.added
        if isinstance(value, SystemAuditEvent)
    ]
    assert len(audit_rows) == 1
    assert audit_rows[0].action == "tenant.delete"
    assert audit_rows[0].tenant_slug == context.tenant.slug
    assert audit_rows[0].severity == "critical"
    assert context.session.commit_count == 2
    first_drop = next(i for i, event in enumerate(events) if isinstance(event, tuple))
    assert events.index("system.commit.1") < first_drop
    assert context.session.deleted == [context.tenant]
    context.emit.assert_not_awaited()
    context.evict.assert_awaited_once_with(context.tenant.slug)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["schema_drop", "final_system_commit"])
async def test_bug9614_failed_tenant_deletion_keeps_system_audit_without_event(
    failure_stage,
):
    events: list = []
    context = _tenant_delete_context(
        events,
        fail_schema_drop=failure_stage == "schema_drop",
        fail_commit_number=2 if failure_stage == "final_system_commit" else None,
    )

    with pytest.raises(Exception) as exc_info:
        await _run_tenant_delete(context)

    if failure_stage == "schema_drop":
        assert isinstance(exc_info.value, HTTPException)
        assert exc_info.value.status_code == 502
    else:
        assert isinstance(exc_info.value, RuntimeError)
        assert "system.commit.2" in str(exc_info.value)

    # The audit transaction commits before either destructive failure point.
    # It therefore remains the durable system-plane deletion-attempt record.
    assert events.index("audit.add") < events.index("system.commit.1")
    audit_commit = events.index("system.commit.1")
    first_drop = next(i for i, event in enumerate(events) if isinstance(event, tuple))
    assert audit_commit < first_drop

    audit_rows = [
        value
        for value in context.session.added
        if isinstance(value, SystemAuditEvent)
    ]
    assert len(audit_rows) == 1
    assert audit_rows[0].action == "tenant.delete"
    assert audit_rows[0].tenant_slug == context.tenant.slug
    context.emit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "caller",
    [
        CurrentUser("tenant-admin", "acme", "admin@acme.test", "tenant_admin"),
        CurrentUser("member", "acme", "member@acme.test", "member"),
        CurrentUser("wrong-system", "acme", "root@acme.test", "system_admin"),
        CurrentServiceUser(
            principal="scheduler",
            tenant_id="__system__",
            role="system_admin",
            scopes=[],
        ),
        CurrentEmbedUser(
            user_id="embed",
            tenant_id="__system__",
            email="embed@system.test",
            rls_role="system_admin",
        ),
    ],
    ids=["tenant-admin", "member", "wrong-tenant", "service", "embed"],
)
async def test_bug9614_tenant_delete_route_rejects_non_system_admin(client, caller):
    system_db = AsyncMock()

    async def _system_db_override():
        yield system_db

    prior_user = app.dependency_overrides.get(get_current_user)
    prior_db = app.dependency_overrides.get(tenants_api.get_system_db)
    app.dependency_overrides[get_current_user] = lambda: caller
    app.dependency_overrides[tenants_api.get_system_db] = _system_db_override
    try:
        response = await client.delete("/api/v1/tenants/acme")
    finally:
        if prior_user is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = prior_user
        if prior_db is None:
            app.dependency_overrides.pop(tenants_api.get_system_db, None)
        else:
            app.dependency_overrides[tenants_api.get_system_db] = prior_db

    assert response.status_code == 403
    assert response.json()["detail"] == "System admin access required"
    system_db.execute.assert_not_awaited()
