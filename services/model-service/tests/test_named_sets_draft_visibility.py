"""Bug-8767: named-set draft visibility gate.

Mirrors the KPI draft-visibility tests (test_kpi_governance.py::TestDraftVisibility).
A non-privileged caller (viewer) must not see draft named sets via list or get;
modelers and admins must still see them.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from .result_fakes import FakeScalarResult

from src.auth.middleware import CurrentUser, get_current_user
from src.main import app

from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    TEST_TENANT,
    async_gen_from,
    make_mock_db,
    make_model,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/named-sets"


class _ScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return FakeScalarResult(self._items)

    def all(self):
        return list(self._items)


def _named_set(
    *,
    ns_id: uuid.UUID | None = None,
    name: str = "Test Set",
    certification_status: str = "draft",
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=ns_id or uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=name,
        display_name=name,
        description=None,
        display_folder=None,
        scope=1,
        expression="{ [Dim].[Dim].Members }",
        dimensions=None,
        builder_definition=None,
        list_type="advanced_mdx",
        certification_status=certification_status,
        owner_user_id=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _make_user(role: str) -> CurrentUser:
    return CurrentUser(
        user_id="user@example.com",
        tenant_id=TEST_TENANT,
        email="user@example.com",
        role=role,
    )


@pytest.fixture(autouse=True)
def _default_effective_role(monkeypatch):
    """Keep token-role cases focused; binding-specific cases override this."""
    async def _caller_has_role(_db, current_user, _project_id, _role, _model_id):
        return current_user.role in {
            "modeler", "admin", "tenant_admin", "system_admin",
        }

    monkeypatch.setattr("src.api.named_sets.caller_has_role", _caller_has_role)


# ---------------------------------------------------------------------------
# list_named_sets draft filter
# ---------------------------------------------------------------------------


class TestListDraftVisibility:
    """Bug-8767: list_named_sets must hide draft rows from non-privileged callers."""

    @pytest.mark.asyncio
    async def test_modeler_sees_draft_named_sets(self, client):
        modeler = _make_user(role="modeler")
        app.dependency_overrides[get_current_user] = lambda: modeler

        draft_ns = _named_set(certification_status="draft")
        db = make_mock_db()
        db.get = AsyncMock(return_value=make_model())
        db.execute = AsyncMock(return_value=_ScalarResult([draft_ns]))

        with (
            patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
            patch("src.api.named_sets.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        ):
            resp = await client.get(PREFIX)

        assert resp.status_code == 200
        assert len(resp.json()) == 1

    @pytest.mark.asyncio
    async def test_project_bound_modeler_sees_drafts_with_member_token(self, client):
        """The in-handler decision must use the effective model binding."""
        member = _make_user(role="member")
        app.dependency_overrides[get_current_user] = lambda: member
        draft_ns = _named_set(certification_status="draft")
        db = make_mock_db()
        db.get = AsyncMock(return_value=make_model())
        db.execute = AsyncMock(return_value=_ScalarResult([draft_ns]))

        with (
            patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
            patch(
                "src.api.named_sets.caller_has_role",
                new=AsyncMock(return_value=True),
            ) as has_role,
            patch(
                "src.api.named_sets.resolve_effective_persona",
                new=AsyncMock(return_value=None),
            ),
        ):
            resp = await client.get(PREFIX)

        assert resp.status_code == 200
        assert [row["name"] for row in resp.json()] == ["Test Set"]
        has_role.assert_awaited_once_with(
            db, member, TEST_PROJECT_ID, "modeler", TEST_MODEL_ID,
        )

    @pytest.mark.asyncio
    async def test_restrictive_binding_hides_drafts_despite_coarse_admin_token(self, client):
        """A coarse token role must not override a restrictive model binding."""
        admin = _make_user(role="admin")
        app.dependency_overrides[get_current_user] = lambda: admin
        db = make_mock_db()
        db.get = AsyncMock(return_value=make_model())
        db.execute = AsyncMock(return_value=_ScalarResult([]))

        with (
            patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
            patch(
                "src.api.named_sets.caller_has_role",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "src.api.named_sets.resolve_effective_persona",
                new=AsyncMock(return_value=None),
            ),
        ):
            resp = await client.get(PREFIX)

        assert resp.status_code == 200
        assert resp.json() == []
        statement = str(db.execute.await_args.args[0])
        assert "certification_status" in statement

    @pytest.mark.asyncio
    async def test_viewer_does_not_see_draft_named_sets(self, client):
        viewer = _make_user(role="viewer")
        app.dependency_overrides[get_current_user] = lambda: viewer

        certified_ns = _named_set(certification_status="certified", name="Certified Set")
        db = make_mock_db()
        db.get = AsyncMock(return_value=make_model())
        # The SQL-level filter excludes drafts before execution, so the mock
        # returns only the certified row (the filter runs inside the DB).
        db.execute = AsyncMock(return_value=_ScalarResult([certified_ns]))

        with (
            patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
            patch("src.api.named_sets.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        ):
            resp = await client.get(PREFIX)

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "Certified Set"

    @pytest.mark.asyncio
    async def test_admin_sees_draft_named_sets(self, client):
        admin = _make_user(role="admin")
        app.dependency_overrides[get_current_user] = lambda: admin

        draft_ns = _named_set(certification_status="draft")
        db = make_mock_db()
        db.get = AsyncMock(return_value=make_model())
        db.execute = AsyncMock(return_value=_ScalarResult([draft_ns]))

        with (
            patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
            patch("src.api.named_sets.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        ):
            resp = await client.get(PREFIX)

        assert resp.status_code == 200
        assert len(resp.json()) == 1

    @pytest.mark.asyncio
    async def test_tenant_admin_sees_draft_named_sets(self, client):
        tenant_admin = _make_user(role="tenant_admin")
        app.dependency_overrides[get_current_user] = lambda: tenant_admin

        draft_ns = _named_set(certification_status="draft")
        db = make_mock_db()
        db.get = AsyncMock(return_value=make_model())
        db.execute = AsyncMock(return_value=_ScalarResult([draft_ns]))

        with (
            patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
            patch("src.api.named_sets.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        ):
            resp = await client.get(PREFIX)

        assert resp.status_code == 200
        assert len(resp.json()) == 1

    @pytest.mark.asyncio
    async def test_system_admin_sees_draft_named_sets(self, client):
        system_admin = _make_user(role="system_admin")
        app.dependency_overrides[get_current_user] = lambda: system_admin

        draft_ns = _named_set(certification_status="draft")
        db = make_mock_db()
        db.get = AsyncMock(return_value=make_model())
        db.execute = AsyncMock(return_value=_ScalarResult([draft_ns]))

        with (
            patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
            patch("src.api.named_sets.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        ):
            resp = await client.get(PREFIX)

        assert resp.status_code == 200
        assert len(resp.json()) == 1


# ---------------------------------------------------------------------------
# get_named_set draft filter
# ---------------------------------------------------------------------------


class TestGetDraftVisibility:
    """Bug-8767: get_named_set must return 404 for draft rows when the caller
    is a non-privileged role (viewer), mirroring get_kpi's identical gate."""

    @pytest.mark.asyncio
    async def test_viewer_cannot_get_draft_named_set(self, client):
        viewer = _make_user(role="viewer")
        app.dependency_overrides[get_current_user] = lambda: viewer

        draft_ns = _named_set(certification_status="draft")
        db = make_mock_db()
        db.get = AsyncMock(side_effect=[make_model(), draft_ns])

        with (
            patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
        ):
            resp = await client.get(f"{PREFIX}/{draft_ns.id}")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_modeler_can_get_draft_named_set(self, client):
        modeler = _make_user(role="modeler")
        app.dependency_overrides[get_current_user] = lambda: modeler

        draft_ns = _named_set(certification_status="draft")
        db = make_mock_db()
        db.get = AsyncMock(side_effect=[make_model(), draft_ns])

        with (
            patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
            patch("src.api.named_sets.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        ):
            resp = await client.get(f"{PREFIX}/{draft_ns.id}")

        assert resp.status_code == 200
        assert resp.json()["name"] == "Test Set"

    @pytest.mark.asyncio
    async def test_project_bound_modeler_can_get_draft_with_member_token(self, client):
        member = _make_user(role="member")
        app.dependency_overrides[get_current_user] = lambda: member
        draft_ns = _named_set(certification_status="draft")
        db = make_mock_db()
        db.get = AsyncMock(side_effect=[make_model(), draft_ns])

        with (
            patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
            patch(
                "src.api.named_sets.caller_has_role",
                new=AsyncMock(return_value=True),
            ) as has_role,
            patch(
                "src.api.named_sets.resolve_effective_persona",
                new=AsyncMock(return_value=None),
            ),
        ):
            resp = await client.get(f"{PREFIX}/{draft_ns.id}")

        assert resp.status_code == 200
        has_role.assert_awaited_once_with(
            db, member, TEST_PROJECT_ID, "modeler", TEST_MODEL_ID,
        )

    @pytest.mark.asyncio
    async def test_admin_can_get_draft_named_set(self, client):
        admin = _make_user(role="admin")
        app.dependency_overrides[get_current_user] = lambda: admin

        draft_ns = _named_set(certification_status="draft")
        db = make_mock_db()
        db.get = AsyncMock(side_effect=[make_model(), draft_ns])

        with (
            patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
            patch("src.api.named_sets.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        ):
            resp = await client.get(f"{PREFIX}/{draft_ns.id}")

        assert resp.status_code == 200
        assert resp.json()["name"] == "Test Set"

    @pytest.mark.asyncio
    async def test_viewer_can_get_certified_named_set(self, client):
        """A non-draft (certified) set is visible to a viewer."""
        viewer = _make_user(role="viewer")
        app.dependency_overrides[get_current_user] = lambda: viewer

        cert_ns = _named_set(certification_status="certified", name="Certified Set")
        db = make_mock_db()
        db.get = AsyncMock(side_effect=[make_model(), cert_ns])

        with (
            patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
            patch("src.api.named_sets.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        ):
            resp = await client.get(f"{PREFIX}/{cert_ns.id}")

        assert resp.status_code == 200
        assert resp.json()["name"] == "Certified Set"

    @pytest.mark.asyncio
    async def test_viewer_can_get_shared_named_set(self, client):
        """A 'shared' set (certified-equivalent) is visible to a viewer."""
        viewer = _make_user(role="viewer")
        app.dependency_overrides[get_current_user] = lambda: viewer

        shared_ns = _named_set(certification_status="shared", name="Shared Set")
        db = make_mock_db()
        db.get = AsyncMock(side_effect=[make_model(), shared_ns])

        with (
            patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
            patch("src.api.named_sets.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        ):
            resp = await client.get(f"{PREFIX}/{shared_ns.id}")

        assert resp.status_code == 200
        assert resp.json()["name"] == "Shared Set"
