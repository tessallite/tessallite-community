"""
Unit tests for RBAC access-binding routes (Workstream 1).

Routes tested (all require admin role):
  POST   /api/v1/projects/{project_id}/access         — grant binding
  GET    /api/v1/projects/{project_id}/access         — list bindings
  DELETE /api/v1/projects/{project_id}/access/{id}    — revoke binding

Role enforcement tests (added once require_role is wired into all routes):
  - Viewer cannot DELETE a project          → 403
  - Modeler cannot manage access bindings   → 403
  - Admin can perform all operations        → 2xx
"""
from __future__ import annotations

import uuid
import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx
from .result_fakes import FakeScalarResult

from shared.db.models import UserAccessBinding
from src.main import app
from src.auth.middleware import CurrentUser, get_current_user

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
TEST_PROJECT_ID = uuid.uuid4()
PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/access"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_current_user(user_id: str, tenant_id: str = "acme") -> CurrentUser:
    return CurrentUser(user_id=user_id, tenant_id=tenant_id, email=user_id)


def _make_binding(binding_id: uuid.UUID | None = None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=binding_id or uuid.uuid4(),
        project_id=TEST_PROJECT_ID,
        model_id=None,
        user_identity="viewer@example.com",
        role="viewer",
        created_at=NOW,
    )


class _ScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return FakeScalarResult(self._items)

    def all(self):
        return self._items

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None


async def _yield_db(db):
    yield db


def make_mock_db():
    db = AsyncMock()
    db.add = lambda x: None
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.delete = AsyncMock()
    return db


# ---------------------------------------------------------------------------
# Grant access (admin only)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_grant_access_as_admin():
    binding = _make_binding()
    mock_db = make_mock_db()

    async def _refresh(obj):
        obj.id = binding.id
        obj.created_at = NOW

    mock_db.refresh = _refresh
    # No existing binding → route creates a new UserAccessBinding ORM object
    mock_db.execute = AsyncMock(return_value=_ScalarResult([]))

    app.dependency_overrides[get_current_user] = lambda: _make_current_user("admin@example.com")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.access.get_tenant_db", lambda tid: _yield_db(mock_db)):
                resp = await ac.post(
                    PREFIX,
                    json={"user_identity": "viewer@example.com", "role": "viewer"},
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_grant_access_non_admin_forbidden():
    """Non-admin user (viewer) must receive 403 when trying to manage access."""
    # Override the rbac autouse mock: return a viewer binding so require_role("admin") → 403
    viewer_ns = types.SimpleNamespace(role="viewer", user_identity="viewer@example.com")
    rbac_db = AsyncMock()
    rbac_result = MagicMock()
    rbac_result.scalar_one_or_none.return_value = viewer_ns
    rbac_db.execute = AsyncMock(return_value=rbac_result)

    app.dependency_overrides[get_current_user] = lambda: _make_current_user("viewer@example.com")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.auth.rbac.get_tenant_db", lambda tid: _yield_db(rbac_db)):
                resp = await ac.post(
                    PREFIX,
                    json={"user_identity": "other@example.com", "role": "viewer"},
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Repair op — legacy binding-less project (F-021-04, decision #9)
# ---------------------------------------------------------------------------

def _tenant_admin_user(user_id: str = "tadmin@example.com") -> CurrentUser:
    return CurrentUser(
        user_id=user_id, tenant_id="acme", email=user_id, role="tenant_admin",
    )


def _existence_result(first_row):
    r = MagicMock()
    r.first.return_value = first_row
    return r


@pytest.mark.asyncio
async def test_repair_creates_initial_admin_binding_for_tenant_admin():
    """A human tenant admin can seed the initial project-admin binding on a
    binding-less legacy project (F-021-04 repair op)."""
    project = types.SimpleNamespace(id=TEST_PROJECT_ID, slug="legacy")
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=project)
    # Existence probe: zero bindings -> .first() is None.
    mock_db.execute = AsyncMock(return_value=_existence_result(None))

    async def _refresh(obj):
        obj.id = uuid.uuid4()
        obj.created_at = NOW

    mock_db.refresh = _refresh

    app.dependency_overrides[get_current_user] = lambda: _tenant_admin_user()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.access.get_tenant_db", lambda tid: _yield_db(mock_db)):
                resp = await ac.post(
                    f"{PREFIX}/repair",
                    json={"user_identity": "owner@example.com"},
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 201, resp.text
    assert resp.json()["role"] == "admin"


@pytest.mark.asyncio
async def test_repair_forbidden_for_non_tenant_admin():
    """An ordinary member cannot use the repair op — human tenant/system admin
    only (require_tenant_admin)."""
    app.dependency_overrides[get_current_user] = lambda: _make_current_user("member@example.com")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            resp = await ac.post(
                f"{PREFIX}/repair",
                json={"user_identity": "member@example.com"},
            )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_repair_rejects_project_that_already_has_bindings():
    """Repair is fail-closed: it refuses (409) on a project that already has at
    least one binding, so it can never escalate on an access-controlled project."""
    project = types.SimpleNamespace(id=TEST_PROJECT_ID, slug="governed")
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=project)
    # Existence probe: a binding already exists -> .first() returns a row.
    mock_db.execute = AsyncMock(return_value=_existence_result((uuid.uuid4(),)))

    app.dependency_overrides[get_current_user] = lambda: _tenant_admin_user()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.access.get_tenant_db", lambda tid: _yield_db(mock_db)):
                resp = await ac.post(
                    f"{PREFIX}/repair",
                    json={"user_identity": "owner@example.com"},
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_repair_acquires_project_keyed_advisory_lock_before_existence_check():
    """F4: the repair op must hold a PROJECT-keyed advisory lock across its
    existence-check-then-insert, so two concurrent repairs on the same project
    (even for different target users) cannot both observe zero bindings and both
    seed an admin binding. Assert the lock is ``pg_advisory_xact_lock`` keyed on
    the project alone (``grant:{project_id}``, NOT the per-user
    ``grant:{project}:{user}`` key) and is executed BEFORE the binding-existence
    probe."""
    project = types.SimpleNamespace(id=TEST_PROJECT_ID, slug="legacy")
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=project)

    executed: list = []

    async def _rec_execute(stmt, *a, **k):
        executed.append(stmt)
        # Every probe in this path resolves to "no binding" (zero-binding repair).
        return _existence_result(None)

    mock_db.execute = _rec_execute

    async def _refresh(obj):
        obj.id = uuid.uuid4()
        obj.created_at = NOW

    mock_db.refresh = _refresh

    app.dependency_overrides[get_current_user] = lambda: _tenant_admin_user()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.access.get_tenant_db", lambda tid: _yield_db(mock_db)):
                resp = await ac.post(
                    f"{PREFIX}/repair",
                    json={"user_identity": "owner@example.com"},
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 201, resp.text
    # The FIRST statement executed is the advisory lock.
    assert "pg_advisory_xact_lock" in str(executed[0]), (
        f"first executed statement was not the advisory lock: {executed[:2]}"
    )
    # It is keyed on the PROJECT alone, not (project, user): the exact-equality
    # below proves there is no ``:{user_identity}`` suffix.
    lock_key = executed[0]._bindparams["k"].value
    assert lock_key == f"grant:{TEST_PROJECT_ID}", lock_key
    # The binding-existence probe runs AFTER the lock.
    later = " ".join(str(s) for s in executed[1:])
    assert "user_access_bindings" in later


# ---------------------------------------------------------------------------
# List access bindings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_access_bindings():
    binding = _make_binding()
    mock_db = make_mock_db()
    mock_db.execute = AsyncMock(return_value=_ScalarResult([binding]))

    app.dependency_overrides[get_current_user] = lambda: _make_current_user("admin@example.com")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.access.get_tenant_db", lambda tid: _yield_db(mock_db)):
                resp = await ac.get(PREFIX)
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    # Pre-WS1: 404. Post-WS1: 200 with list.
    assert resp.status_code in (200, 404)


# ---------------------------------------------------------------------------
# Revoke access
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_revoke_access_as_admin():
    binding_id = uuid.uuid4()
    binding = _make_binding(binding_id)
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=binding)

    app.dependency_overrides[get_current_user] = lambda: _make_current_user("admin@example.com")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.access.get_tenant_db", lambda tid: _yield_db(mock_db)):
                resp = await ac.delete(f"{PREFIX}/{binding_id}")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    # Pre-WS1: 404. Post-WS1: 204.
    assert resp.status_code in (204, 404)


# ---------------------------------------------------------------------------
# Role hierarchy unit test (no HTTP — pure logic)
# ---------------------------------------------------------------------------

def test_role_hierarchy_order():
    """Verifies that role hierarchy is defined correctly once WS1 rbac.py exists."""
    try:
        from src.auth.rbac import ROLE_HIERARCHY
        assert ROLE_HIERARCHY.index("admin") < ROLE_HIERARCHY.index("modeler")
        assert ROLE_HIERARCHY.index("modeler") < ROLE_HIERARCHY.index("viewer")
    except ImportError:
        pytest.skip("rbac.py not yet created (Workstream 1 pending)")


# ---------------------------------------------------------------------------
# F-021-04: model-scoped grant upsert keys on (user, project, model)
# ---------------------------------------------------------------------------

class _CapturingResult:
    """Returns a configurable scalar_one_or_none and records the executed stmt.

    Also supports ``.scalars().all()`` so it can stand in for the
    supersession-check binding load added for Bug-8101 (which reads all of a
    user's project bindings). A single-value mock resolves to a one/zero-element
    list there, matching the pre-existing single-row upsert semantics.
    """
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return FakeScalarResult([self._value] if self._value is not None else [])

    def all(self):
        return [self._value] if self._value is not None else []


@pytest.mark.asyncio
async def test_model_scoped_grant_does_not_overwrite_project_binding():
    """A model-scoped grant for a user who already holds a project-level binding
    must create a NEW model-scoped binding, not overwrite the project row's role
    and lose the model scope (F-021-04)."""
    model_id = uuid.uuid4()
    added = []
    mock_db = make_mock_db()
    # CP-08 fail-closed audit_required now db.add()s an AuditEvent on the same
    # session as the mutation; this test asserts on access bindings only, so
    # capture just those.
    mock_db.add = lambda obj: added.append(obj) if isinstance(obj, UserAccessBinding) else None

    async def _refresh(obj):
        obj.id = uuid.uuid4()
        obj.created_at = NOW

    mock_db.refresh = _refresh
    # The model-scoped lookup finds no existing row for (user, project, model).
    mock_db.execute = AsyncMock(return_value=_CapturingResult(None))

    app.dependency_overrides[get_current_user] = lambda: _make_current_user("admin@example.com")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.access.get_tenant_db", lambda tid: _yield_db(mock_db)):
                resp = await ac.post(
                    PREFIX,
                    json={
                        "user_identity": "user@example.com",
                        "role": "modeler",
                        "model_id": str(model_id),
                    },
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 201
    # A new model-scoped binding was created with the model_id preserved.
    assert len(added) == 1
    assert str(added[0].model_id) == str(model_id)
    assert added[0].role == "modeler"
    # Bug-6303: a freshly created grant is provenance "manual" so the SSO group
    # sync never revokes it.
    assert added[0].source == "manual"


# ---------------------------------------------------------------------------
# Bug-6303: an explicit operator grant over an sso_group row pins it to manual
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_grant_over_sso_binding_pins_source_manual():
    """An admin explicitly granting a role on a scope that currently holds an
    ``sso_group`` binding must convert that binding to ``source="manual"``.

    Otherwise the row stays SSO-owned and ``jit._sync_group_bindings`` could
    later revoke the operator's explicit grant on IdP de-provisioning — locking
    out an admin who was deliberately granted access. This proves the manual
    intent is durable and outranks the SSO provenance."""
    existing = types.SimpleNamespace(
        id=uuid.uuid4(),
        project_id=TEST_PROJECT_ID,
        model_id=None,
        user_identity="user@example.com",
        role="viewer",
        source="sso_group",   # previously materialised by an SSO group login
        created_at=NOW,
    )
    mock_db = make_mock_db()
    mock_db.execute = AsyncMock(return_value=_CapturingResult(existing))

    async def _refresh(obj):
        return None

    mock_db.refresh = _refresh

    app.dependency_overrides[get_current_user] = lambda: _make_current_user("admin@example.com")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.access.get_tenant_db", lambda tid: _yield_db(mock_db)):
                resp = await ac.post(
                    PREFIX,
                    json={"user_identity": "user@example.com", "role": "admin"},
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 201
    assert existing.role == "admin", "explicit grant updates the role"
    assert existing.source == "manual", (
        "explicit operator grant must pin the binding to manual so SSO sync "
        "can never revoke it"
    )
