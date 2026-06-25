"""
Unit tests for project CRUD routes.

GET    /api/v1/projects
GET    /api/v1/projects/{id}
POST   /api/v1/projects
PATCH  /api/v1/projects/{id}
DELETE /api/v1/projects/{id}
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.auth.middleware import CurrentUser

from .conftest import (
    TEST_PROJECT_ID,
    TEST_TENANT,
    TEST_USER_ID,
    client,
    make_mock_db,
    make_project,
    async_gen_from,
)

pytestmark = pytest.mark.unit

PREFIX = "/api/v1/projects"

# Creating a project is tenant_admin-only (Explorer RBAC matrix); tests that
# exercise POST /projects authenticate as a tenant admin via indirect override.
_TENANT_ADMIN_USER = CurrentUser(
    user_id=TEST_USER_ID, tenant_id=TEST_TENANT, email=TEST_USER_ID, role="tenant_admin"
)
_as_tenant_admin = pytest.mark.parametrize(
    "override_auth", [_TENANT_ADMIN_USER], indirect=True
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _ScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None


# ---------------------------------------------------------------------------
# GET /projects — list
# ---------------------------------------------------------------------------

class _ExecuteResultWithAll:
    """Variant of _ScalarResult whose ``.all()`` returns (pid, uid)
    tuples like ``filter_projects_by_user_access`` expects from its
    UserAccessBinding query."""

    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items


@pytest.mark.asyncio
async def test_list_projects(client):
    project = make_project()
    mock_db = make_mock_db()

    # Two execute calls: first returns projects, second returns
    # access bindings (empty → bootstrap rule makes the project
    # visible to everyone).
    execute_results = iter(
        [
            _ScalarResult([project]),
            _ExecuteResultWithAll([]),
        ]
    )

    async def _execute(_stmt):
        return next(execute_results)

    mock_db.execute = AsyncMock(side_effect=_execute)

    with patch("src.api.projects.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.get(PREFIX)

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["slug"] == "test-project"


@pytest.mark.asyncio
async def test_list_projects_hides_projects_user_has_no_binding_on(client):
    """Bug-F1 regression: when at least one binding exists on a
    project, only users with a matching binding can see it in the
    tenant discovery list. Bootstrap projects (no bindings at all)
    stay visible."""
    visible_project = make_project(
        project_id=uuid.uuid4(), slug="visible-project"
    )
    hidden_project = make_project(
        project_id=uuid.uuid4(), slug="hidden-project"
    )
    bootstrap_project = make_project(
        project_id=uuid.uuid4(), slug="bootstrap-project"
    )

    mock_db = make_mock_db()

    # Bindings: the caller has a binding on visible_project, a
    # different user owns hidden_project, bootstrap_project has no
    # bindings at all.
    from .conftest import TEST_USER_ID

    bindings = [
        (visible_project.id, TEST_USER_ID),
        (hidden_project.id, "someone.else@example.com"),
    ]

    execute_results = iter(
        [
            _ScalarResult([visible_project, hidden_project, bootstrap_project]),
            _ExecuteResultWithAll(bindings),
        ]
    )

    async def _execute(_stmt):
        return next(execute_results)

    mock_db.execute = AsyncMock(side_effect=_execute)

    with patch("src.api.projects.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.get(PREFIX)

    assert resp.status_code == 200
    slugs = {p["slug"] for p in resp.json()}
    assert "visible-project" in slugs
    assert "bootstrap-project" in slugs  # bootstrap rule
    assert "hidden-project" not in slugs


# ---------------------------------------------------------------------------
# GET /projects/{id}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_project_found(client):
    project = make_project()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=project)

    with patch("src.api.projects.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.get(f"{PREFIX}/{TEST_PROJECT_ID}")

    assert resp.status_code == 200
    assert resp.json()["id"] == str(TEST_PROJECT_ID)


@pytest.mark.asyncio
async def test_get_project_not_found(client):
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=None)

    with patch("src.api.projects.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.get(f"{PREFIX}/{uuid.uuid4()}")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /projects
# ---------------------------------------------------------------------------

@_as_tenant_admin
@pytest.mark.asyncio
async def test_create_project(client):
    project = make_project(slug="new-proj", display_name="New")
    mock_db = make_mock_db()

    async def _refresh(obj):
        obj.id = project.id
        obj.created_at = project.created_at
        obj.updated_at = project.updated_at
        obj.is_active = True

    mock_db.refresh = _refresh

    with patch("src.api.projects.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.post(PREFIX, json={"slug": "new-proj", "display_name": "New"})

    assert resp.status_code == 201
    assert resp.json()["slug"] == "new-proj"


@_as_tenant_admin
@pytest.mark.asyncio
async def test_create_project_invalid_slug(client):
    # Slug must be lowercase alphanumeric+dash+underscore
    resp = await client.post(PREFIX, json={"slug": "UPPER CASE", "display_name": "Bad"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_project_rejected_for_non_tenant_admin(client):
    """A normal member (no tenant-admin role) cannot create a project — the
    backend enforces the tenant-level gate, not just the Explorer UI."""
    resp = await client.post(PREFIX, json={"slug": "nope", "display_name": "Nope"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# PATCH /projects/{id}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_project(client):
    project = make_project(display_name="Old Name")
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=project)

    async def _refresh(obj):
        pass  # obj is mutated in-place by route

    mock_db.refresh = _refresh

    with patch("src.api.projects.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.patch(
            f"{PREFIX}/{TEST_PROJECT_ID}",
            json={"display_name": "New Name"},
        )

    assert resp.status_code == 200
    # In-place mutation means project.display_name is now "New Name"
    assert resp.json()["display_name"] == "New Name"


@pytest.mark.asyncio
async def test_update_project_not_found(client):
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=None)

    with patch("src.api.projects.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.patch(
            f"{PREFIX}/{uuid.uuid4()}",
            json={"display_name": "x"},
        )

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /projects/{id}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_project(client):
    project = make_project()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=project)

    with (
        patch("src.api.projects.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.projects.delete_project_cascade", new_callable=AsyncMock, return_value=[]) as mock_cascade,
    ):
        resp = await client.delete(f"{PREFIX}/{TEST_PROJECT_ID}")

    assert resp.status_code == 204
    mock_cascade.assert_called_once_with(mock_db, TEST_PROJECT_ID)


@pytest.mark.asyncio
async def test_delete_project_not_found(client):
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=None)

    with patch("src.api.projects.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.delete(f"{PREFIX}/{uuid.uuid4()}")

    assert resp.status_code == 404
