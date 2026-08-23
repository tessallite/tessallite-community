"""Bug-9396 / F-029-07 scratchpad rename uniqueness contract."""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy.exc import IntegrityError

from shared.db.models import ScratchpadMeasure
from src.auth.middleware import CurrentUser, get_current_user
from src.main import app
from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    TEST_TENANT,
    TEST_USER_ID,
    async_gen_from,
    make_mock_db,
)


BASE = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"
    "/scratchpad-measures"
)


def _measure(*, name: str = "Revenue") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=name,
        display_name=None,
        expression="SUM(amount)",
        data_type="numeric",
        format=None,
        created_by=TEST_USER_ID,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
def auth():
    user = CurrentUser(
        user_id=TEST_USER_ID,
        tenant_id=TEST_TENANT,
        email=TEST_USER_ID,
    )
    app.dependency_overrides[get_current_user] = lambda: user
    yield user
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
async def client(auth):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as ac:
        yield ac


def _db_for(row: types.SimpleNamespace, *, collision: object | None) -> AsyncMock:
    db = make_mock_db()

    async def _get(cls, ident):
        if cls is ScratchpadMeasure and ident == row.id:
            return row
        return None

    db.get = AsyncMock(side_effect=_get)
    result = MagicMock()
    result.scalar_one_or_none.return_value = collision
    db.execute = AsyncMock(return_value=result)
    db.refresh = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_f02907_rename_collision_returns_409_before_commit(client):
    row = _measure()
    db = _db_for(row, collision=_measure(name="Margin"))

    with (
        patch("src.api.scratchpad_measures.get_tenant_db", async_gen_from(db)),
        patch(
            "src.api.scratchpad_measures.ensure_model_in_project",
            new=AsyncMock(return_value=types.SimpleNamespace(slug="sales")),
        ),
    ):
        response = await client.patch(f"{BASE}/{row.id}", json={"name": "Margin"})

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]
    db.commit.assert_not_awaited()
    assert row.name == "Revenue"


@pytest.mark.asyncio
async def test_f02907_concurrent_rename_collision_is_also_409(client):
    row = _measure()
    db = _db_for(row, collision=None)
    db.commit = AsyncMock(
        side_effect=IntegrityError("UPDATE scratchpad_measures", {}, Exception())
    )

    with (
        patch("src.api.scratchpad_measures.get_tenant_db", async_gen_from(db)),
        patch(
            "src.api.scratchpad_measures.ensure_model_in_project",
            new=AsyncMock(return_value=types.SimpleNamespace(slug="sales")),
        ),
    ):
        response = await client.patch(f"{BASE}/{row.id}", json={"name": "Margin"})

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]
    db.rollback.assert_awaited_once()
