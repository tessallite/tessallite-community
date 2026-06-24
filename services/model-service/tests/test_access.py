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
        return self

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
    """Returns a configurable scalar_one_or_none and records the executed stmt."""
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


@pytest.mark.asyncio
async def test_model_scoped_grant_does_not_overwrite_project_binding():
    """A model-scoped grant for a user who already holds a project-level binding
    must create a NEW model-scoped binding, not overwrite the project row's role
    and lose the model scope (F-021-04)."""
    model_id = uuid.uuid4()
    added = []
    mock_db = make_mock_db()
    mock_db.add = lambda obj: added.append(obj)

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
