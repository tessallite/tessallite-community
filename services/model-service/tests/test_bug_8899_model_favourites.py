"""Bug-8183 / Bug-8899 — favouriting a MODEL.

Bug-8183 asked for model favourites that follow the user rather than the
browser. Bug-8899 is the trap in delivering it: ``_validate_entity_exists``
proves membership with ``entity.model_id``, and the ``Model`` ORM class has no
``model_id`` attribute at all — its identity column is ``id``. Registering
``{"model": Model}`` in ``_ENTITY_MODELS`` and changing nothing else therefore
turns every model toggle into ``AttributeError`` -> HTTP 500.

These tests hold both halves: the model branch must WORK, and it must stay
scoped — a user cannot pin a model through another model's route.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from shared.db.models import Model
from src.auth.middleware import CurrentUser, get_current_user
from src.main import app

from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    TEST_TENANT,
    TEST_USER_ID,
    FakeResult,
    async_gen_from,
    make_mock_db,
    make_model,
)

BASE = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"
PROJECT_BASE = f"/api/v1/projects/{TEST_PROJECT_ID}/preferences"


@pytest.fixture
def auth():
    user = CurrentUser(user_id=TEST_USER_ID, tenant_id=TEST_TENANT, email=TEST_USER_ID)
    app.dependency_overrides[get_current_user] = lambda: user
    yield user
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
async def client(auth):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac


def _db(rows=()) -> AsyncMock:
    """A session whose ``get(Model, ...)`` answers the path model.

    The stand-in carries no ``model_id`` attribute, exactly like the real
    ``Model`` class — which is what makes the Bug-8899 regression visible here
    rather than only against a live database.
    """
    db = make_mock_db()
    model = make_model()

    async def _get(cls, ident):
        return model if cls is Model else None

    db.get = AsyncMock(side_effect=_get)
    db.execute = AsyncMock(return_value=FakeResult(rows))
    return db


def _pref(entity_id, *, entity_type="model", preference_type="favourite"):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        user_id=TEST_USER_ID,
        model_id=TEST_MODEL_ID,
        entity_type=entity_type,
        entity_id=entity_id,
        preference_type=preference_type,
        created_at=NOW,
    )


class TestModelFavouriteToggle:
    @pytest.mark.asyncio
    async def test_favouriting_the_path_model_succeeds(self, client):
        """Bug-8899: this is the call that used to raise AttributeError -> 500."""
        db = _db()
        with patch("src.api.preferences.get_tenant_db", async_gen_from(db)):
            resp = await client.put(
                f"{BASE}/preferences/favourite",
                json={"entity_type": "model", "entity_id": str(TEST_MODEL_ID)},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"favourited": True}
        assert db.add.called

    @pytest.mark.asyncio
    async def test_favouriting_a_different_model_is_refused(self, client):
        """A model is its own scope: entity_id must BE the model in the path.

        Without this, the route would happily persist a preference row naming
        any model id the caller invented — including one in another project,
        since nothing else in the handler looks the entity up.
        """
        db = _db()
        with patch("src.api.preferences.get_tenant_db", async_gen_from(db)):
            resp = await client.put(
                f"{BASE}/preferences/favourite",
                json={"entity_type": "model", "entity_id": str(uuid.uuid4())},
            )
        assert resp.status_code == 404, resp.text
        assert not db.add.called

    @pytest.mark.asyncio
    async def test_unfavouriting_removes_the_existing_row(self, client):
        db = _db()
        existing = _pref(TEST_MODEL_ID)
        db.execute = AsyncMock(return_value=FakeResult([existing]))
        with patch("src.api.preferences.get_tenant_db", async_gen_from(db)):
            resp = await client.put(
                f"{BASE}/preferences/favourite",
                json={"entity_type": "model", "entity_id": str(TEST_MODEL_ID)},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"favourited": False}
        db.delete.assert_awaited_once_with(existing)

    @pytest.mark.asyncio
    async def test_recently_used_accepts_the_path_model(self, client):
        """The other write path shares ``_validate_entity_exists``; Bug-8899
        would have made it 500 for exactly the same reason."""
        db = _db()
        with patch("src.api.preferences.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                f"{BASE}/preferences/recently-used",
                json={"entity_type": "model", "entity_id": str(TEST_MODEL_ID)},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"recorded": True}


class TestModelFavouriteRead:
    @pytest.mark.asyncio
    async def test_per_model_get_reports_the_model_bucket(self, client):
        """A row the write path stores must be readable, or it is junk.

        The read path filters on a hard-coded vocabulary; leaving "model" out
        of it would store favourites nobody could ever see.
        """
        db = _db([_pref(TEST_MODEL_ID)])
        with patch("src.api.preferences.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{BASE}/preferences")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["favourites"]["model"] == [str(TEST_MODEL_ID)]
        assert body["favourites"]["kpi"] == []
        assert body["recently_used"]["model"] == []

    @pytest.mark.asyncio
    async def test_project_read_returns_favourited_model_ids(self, client):
        db = _db([TEST_MODEL_ID])
        with patch("src.api.preferences.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{PROJECT_BASE}/favourite-models")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"model_ids": [str(TEST_MODEL_ID)]}

    @pytest.mark.asyncio
    async def test_project_read_is_scoped_to_caller_project_and_type(self, client):
        """The answer must be narrowed on every axis that could leak.

        Dropping any one of these turns the route into another user's
        favourites, another project's favourites, or a dump of KPI and
        named-set preference rows mislabelled as models.
        """
        captured: list = []
        db = _db([TEST_MODEL_ID])

        async def _execute(stmt):
            captured.append(stmt)
            return FakeResult([TEST_MODEL_ID])

        db.execute = AsyncMock(side_effect=_execute)
        with patch("src.api.preferences.get_tenant_db", async_gen_from(db)):
            await client.get(f"{PROJECT_BASE}/favourite-models")

        assert len(captured) == 1
        # The default dialect renders a UUID literal without its dashes.
        sql = str(captured[0].compile(compile_kwargs={"literal_binds": True}))
        assert f"user_entity_preferences.user_id = '{TEST_USER_ID}'" in sql
        assert "user_entity_preferences.entity_type = 'model'" in sql
        assert "user_entity_preferences.preference_type = 'favourite'" in sql
        assert f"models.project_id = '{TEST_PROJECT_ID.hex}'" in sql
        # Scoped by a join, not by trusting the stored model_id: a favourite
        # whose model has been deleted drops out instead of coming back as an
        # id the caller cannot resolve.
        assert "JOIN models ON models.id = user_entity_preferences.model_id" in sql

    @pytest.mark.asyncio
    async def test_project_read_is_empty_when_nothing_is_favourited(self, client):
        db = _db([])
        with patch("src.api.preferences.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{PROJECT_BASE}/favourite-models")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"model_ids": []}
