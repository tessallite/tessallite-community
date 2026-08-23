"""Tests for the Saved Query Library API, focused on personal/shared scope,
ownership, and delete protection (F-029-03 / Bug-9396).

Saved queries default personal. Once the owner shares one, only its owner or a
modeler+ may edit or delete it — a viewer cannot rewrite or destroy a
colleague's shared query. ``created_by`` records the owner identity.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from .result_fakes import FakeScalarResult

from src.main import app
from src.auth.middleware import CurrentUser, get_current_user
from shared.db.models import Model
from .conftest import (
    TEST_TENANT,
    TEST_USER_ID,
    TEST_PROJECT_ID,
    TEST_MODEL_ID,
    NOW,
    async_gen_from,
    make_mock_db,
    make_model,
)

OWNER_EMAIL = TEST_USER_ID            # the fixture user owns this query
OTHER_EMAIL = "colleague@example.com"  # a different user


def _make_query(
    query_id: uuid.UUID | None = None,
    created_by: str = OWNER_EMAIL,
    name: str = "Top accounts",
    is_shared: bool = False,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=query_id or uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=name,
        description="A shared query",
        query_text="SELECT 1",
        query_type="sql",
        created_by=created_by,
        is_shared=is_shared,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
def auth():
    user = CurrentUser(
        user_id=TEST_USER_ID, tenant_id=TEST_TENANT, email=OWNER_EMAIL
    )
    app.dependency_overrides[get_current_user] = lambda: user
    yield user
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
async def client(auth):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac


def _db_with(query: types.SimpleNamespace) -> AsyncMock:
    """A mock session whose ``get`` returns the model for a Model lookup and
    whose scoped query returns the given visible saved query."""
    db = make_mock_db()
    model = make_model()

    async def _get(cls, ident):
        if cls is Model:
            return model
        return None

    db.get = AsyncMock(side_effect=_get)
    visible_query = (
        query
        if query is None
        or query.created_by == OWNER_EMAIL
        or query.is_shared
        else None
    )
    result = types.SimpleNamespace(scalar_one_or_none=lambda: visible_query)
    db.execute = AsyncMock(return_value=result)

    async def _refresh(obj):
        return None

    db.refresh = AsyncMock(side_effect=_refresh)
    return db


BASE = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/saved-queries"


class TestSavedQueryOwnership:

    @pytest.mark.asyncio
    async def test_owner_can_delete_their_query(self, client):
        q = _make_query(created_by=OWNER_EMAIL)
        db = _db_with(q)
        with patch("src.api.saved_queries.get_tenant_db", async_gen_from(db)):
            resp = await client.delete(f"{BASE}/{q.id}")
        assert resp.status_code == 204
        db.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_viewer_cannot_delete_other_users_query(self, client):
        # A viewer (no modeler role) deleting a colleague's query is rejected.
        q = _make_query(created_by=OTHER_EMAIL, is_shared=True)
        db = _db_with(q)
        with patch("src.api.saved_queries.get_tenant_db", async_gen_from(db)), \
             patch("src.api.saved_queries.caller_has_role",
                   AsyncMock(return_value=False)):
            resp = await client.delete(f"{BASE}/{q.id}")
        assert resp.status_code == 403
        db.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_modeler_can_delete_other_users_query(self, client):
        q = _make_query(created_by=OTHER_EMAIL, is_shared=True)
        db = _db_with(q)
        with patch("src.api.saved_queries.get_tenant_db", async_gen_from(db)), \
             patch("src.api.saved_queries.caller_has_role",
                   AsyncMock(return_value=True)):
            resp = await client.delete(f"{BASE}/{q.id}")
        assert resp.status_code == 204
        db.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_viewer_cannot_edit_other_users_query(self, client):
        q = _make_query(created_by=OTHER_EMAIL, is_shared=True)
        db = _db_with(q)
        with patch("src.api.saved_queries.get_tenant_db", async_gen_from(db)), \
             patch("src.api.saved_queries.caller_has_role",
                   AsyncMock(return_value=False)):
            resp = await client.patch(
                f"{BASE}/{q.id}", json={"query_text": "SELECT 2"}
            )
        assert resp.status_code == 403
        # The query must not have been rewritten.
        assert q.query_text == "SELECT 1"

    @pytest.mark.asyncio
    async def test_owner_can_edit_their_query(self, client):
        q = _make_query(created_by=OWNER_EMAIL)
        db = _db_with(q)
        with patch("src.api.saved_queries.get_tenant_db", async_gen_from(db)):
            resp = await client.patch(
                f"{BASE}/{q.id}", json={"query_text": "SELECT 2"}
            )
        assert resp.status_code == 200
        assert q.query_text == "SELECT 2"

    @pytest.mark.asyncio
    async def test_delete_missing_query_404(self, client):
        db = _db_with(None)  # SavedQuery lookup returns None
        with patch("src.api.saved_queries.get_tenant_db", async_gen_from(db)):
            resp = await client.delete(f"{BASE}/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestSavedQueryCanEditFlag:
    """Bug-5983: the list response must expose ``can_edit`` distinctly from
    ``is_owner`` so the panel can show edit/delete for a modeler viewing a
    colleague's query, not just the query's actual owner."""

    def _db_with_list(self, rows: list) -> AsyncMock:
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        class _ScalarResult:
            def scalars(self):
                return FakeScalarResult(rows)

            def all(self):
                return rows

        db.execute = AsyncMock(return_value=_ScalarResult())
        return db

    @pytest.mark.asyncio
    async def test_owner_row_is_editable(self, client):
        q = _make_query(created_by=OWNER_EMAIL)
        db = self._db_with_list([q])
        with patch("src.api.saved_queries.get_tenant_db", async_gen_from(db)), \
             patch("src.api.saved_queries.caller_has_role",
                   AsyncMock(return_value=False)):
            resp = await client.get(BASE)
        assert resp.status_code == 200
        row = resp.json()[0]
        assert row["is_owner"] is True
        assert row["can_edit"] is True

    @pytest.mark.asyncio
    async def test_modeler_non_owner_row_is_editable(self, client):
        """A modeler is not the owner but the backend mutation endpoints
        allow them to edit/delete -- the UI flag must reflect that, not
        hide controls the modeler can actually use."""
        q = _make_query(created_by=OTHER_EMAIL, is_shared=True)
        db = self._db_with_list([q])
        with patch("src.api.saved_queries.get_tenant_db", async_gen_from(db)), \
             patch("src.api.saved_queries.caller_has_role",
                   AsyncMock(return_value=True)):
            resp = await client.get(BASE)
        assert resp.status_code == 200
        row = resp.json()[0]
        assert row["is_owner"] is False
        assert row["can_edit"] is True

    @pytest.mark.asyncio
    async def test_viewer_non_owner_row_is_not_editable(self, client):
        q = _make_query(created_by=OTHER_EMAIL, is_shared=True)
        db = self._db_with_list([q])
        with patch("src.api.saved_queries.get_tenant_db", async_gen_from(db)), \
             patch("src.api.saved_queries.caller_has_role",
                   AsyncMock(return_value=False)):
            resp = await client.get(BASE)
        assert resp.status_code == 200
        row = resp.json()[0]
        assert row["is_owner"] is False
        assert row["can_edit"] is False


class TestBug9396SavedQueryPersonalScope:
    """F-029-15: saved queries are personal unless their owner shares them."""

    @pytest.mark.asyncio
    async def test_list_query_limits_rows_to_owner_or_shared(self, client):
        q = _make_query(is_shared=False)
        db = TestSavedQueryCanEditFlag()._db_with_list([q])
        with patch("src.api.saved_queries.get_tenant_db", async_gen_from(db)), \
             patch("src.api.saved_queries.caller_has_role",
                   AsyncMock(return_value=False)):
            resp = await client.get(BASE)

        assert resp.status_code == 200
        statement = db.execute.await_args.args[0]
        where_sql = str(statement.whereclause)
        assert "saved_queries.created_by" in where_sql
        assert "saved_queries.is_shared" in where_sql
        assert " OR " in where_sql
        assert resp.json()[0]["is_shared"] is False

    @pytest.mark.asyncio
    async def test_other_users_personal_query_is_not_disclosed_by_id(self, client):
        q = _make_query(created_by=OTHER_EMAIL, is_shared=False)
        db = _db_with(q)
        with patch("src.api.saved_queries.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{BASE}/{q.id}")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_other_users_shared_query_is_visible_by_id(self, client):
        q = _make_query(created_by=OTHER_EMAIL, is_shared=True)
        db = _db_with(q)
        with patch("src.api.saved_queries.get_tenant_db", async_gen_from(db)), \
             patch("src.api.saved_queries.caller_has_role",
                   AsyncMock(return_value=False)):
            resp = await client.get(f"{BASE}/{q.id}")
        assert resp.status_code == 200
        assert resp.json()["is_shared"] is True

    @pytest.mark.asyncio
    async def test_non_owner_cannot_change_sharing_even_if_modeler(self, client):
        q = _make_query(created_by=OTHER_EMAIL, is_shared=True)
        db = _db_with(q)
        with patch("src.api.saved_queries.get_tenant_db", async_gen_from(db)), \
             patch("src.api.saved_queries.caller_has_role",
                   AsyncMock(return_value=True)):
            resp = await client.patch(
                f"{BASE}/{q.id}", json={"is_shared": False}
            )
        assert resp.status_code == 403
        assert q.is_shared is True

    @pytest.mark.asyncio
    async def test_owner_can_share_their_personal_query(self, client):
        q = _make_query(created_by=OWNER_EMAIL, is_shared=False)
        db = _db_with(q)
        with patch("src.api.saved_queries.get_tenant_db", async_gen_from(db)):
            resp = await client.patch(
                f"{BASE}/{q.id}", json={"is_shared": True}
            )
        assert resp.status_code == 200
        assert q.is_shared is True
        assert resp.json()["is_shared"] is True
