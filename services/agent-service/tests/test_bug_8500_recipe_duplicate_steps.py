"""Bug-8500 — Recipe API must reject duplicate step names at create/update.

Business outcome under test: a modeller who accidentally uses the same step
name twice gets a clear 422 error at save time rather than silently wrong
numbers at execution time.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx
from fastapi import HTTPException

from src.main import app
from src.auth.middleware import CurrentUser, get_current_user
from src.api.recipes import (
    RecipeStep,
    _validate_step_names_unique,
)

from .conftest import TEST_TENANT, TEST_PROJECT_ID, make_mock_db, async_gen_from


# ---------------------------------------------------------------------------
# Unit tests — validator in isolation
# ---------------------------------------------------------------------------

class TestValidateStepNamesUnique:
    def test_unique_names_pass(self):
        steps = [
            RecipeStep(name="sales", model_id=uuid.uuid4()),
            RecipeStep(name="units", model_id=uuid.uuid4()),
            RecipeStep(name="combine", model_id=uuid.uuid4()),
        ]
        _validate_step_names_unique(steps)  # no exception

    def test_exact_duplicate_rejected(self):
        model_id = uuid.uuid4()
        steps = [
            RecipeStep(name="sales", model_id=model_id),
            RecipeStep(name="sales", model_id=model_id),
        ]
        with pytest.raises(HTTPException) as exc:
            _validate_step_names_unique(steps)
        assert exc.value.status_code == 422
        assert "Duplicate step name 'sales'" in exc.value.detail
        assert "indices 0 and 1" in exc.value.detail

    def test_case_insensitive_duplicate_rejected(self):
        steps = [
            RecipeStep(name="Sales", model_id=uuid.uuid4()),
            RecipeStep(name="sales", model_id=uuid.uuid4()),
        ]
        with pytest.raises(HTTPException) as exc:
            _validate_step_names_unique(steps)
        assert exc.value.status_code == 422
        assert "Duplicate step name" in exc.value.detail
        assert "indices 0 and 1" in exc.value.detail

    def test_empty_steps_pass(self):
        _validate_step_names_unique([])  # no exception

    def test_single_step_passes(self):
        _validate_step_names_unique(
            [RecipeStep(name="only", model_id=uuid.uuid4())]
        )  # no exception

    def test_duplicate_not_adjacent_rejected(self):
        steps = [
            RecipeStep(name="sales", model_id=uuid.uuid4()),
            RecipeStep(name="units", model_id=uuid.uuid4()),
            RecipeStep(name="sales", model_id=uuid.uuid4()),
        ]
        with pytest.raises(HTTPException) as exc:
            _validate_step_names_unique(steps)
        assert exc.value.status_code == 422
        assert "indices 0 and 2" in exc.value.detail


# ---------------------------------------------------------------------------
# Endpoint behaviour — POST /recipes rejects duplicates
# ---------------------------------------------------------------------------

def _recipe_payload(steps: list[dict]) -> dict:
    return {
        "name": "Test recipe",
        "description": "Duplicate test",
        "parameters": [],
        "steps": steps,
        "combine": None,
        "notes": None,
    }


def _step(name: str, model_id: uuid.UUID | None = None) -> dict:
    return {
        "name": name,
        "model_id": str(model_id or uuid.uuid4()),
        "measures": [],
        "dimensions": [],
        "filters": [],
        "limit": 100,
    }


def _user():
    return CurrentUser(
        user_id="modeller@test.com",
        tenant_id=TEST_TENANT,
        email="modeller@test.com",
        role="tenant_admin",
    )


def _db_for_write() -> AsyncMock:
    db = make_mock_db()
    cfg_result = MagicMock()
    cfg_result.scalar_one_or_none.return_value = types.SimpleNamespace(
        project_id=TEST_PROJECT_ID
    )
    db.execute = AsyncMock(return_value=cfg_result)
    return db


@pytest.mark.asyncio
async def test_create_recipe_with_duplicate_names_returns_422():
    """Duplicate step names at create time must be rejected with 422.

    ``db.get``/``db.refresh`` must resolve and persist a real record (same
    stubs as the sibling 201 test below) so that, with the guard removed,
    this test fails on the recorded pre-fix defect — 201 with the
    duplicate persisted — rather than short-circuiting earlier on
    ``_validate_step_models``'s unrelated "model not in project" 400, or on
    an unstubbed ``record.id`` producing a response-serialisation crash.
    """
    app.dependency_overrides[get_current_user] = _user
    db = _db_for_write()

    async def _get(_cls, pk):
        return types.SimpleNamespace(id=pk, project_id=TEST_PROJECT_ID)

    db.get = AsyncMock(side_effect=_get)

    async def _refresh(record):
        if getattr(record, "id", None) is None:
            record.id = uuid.uuid4()

    db.refresh = AsyncMock(side_effect=_refresh)

    steps = [_step("load_sales"), _step("load_sales")]
    payload = _recipe_payload(steps)
    try:
        with patch("src.api.recipes.get_tenant_db", async_gen_from(db)):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                resp = await client.post(
                    f"/api/v1/projects/{TEST_PROJECT_ID}/agent/recipes",
                    json=payload,
                )
        assert resp.status_code == 422, resp.text
        assert "Duplicate step name" in resp.json()["detail"]
        # Bug-8500 review gap: the guard must run BEFORE persistence, not
        # merely exist somewhere in the route. Assert nothing was written.
        db.commit.assert_not_awaited()
        db.add.assert_not_called()
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_create_recipe_with_unique_names_returns_201():
    """Unique step names must be accepted."""
    app.dependency_overrides[get_current_user] = _user
    db = _db_for_write()
    cfg_result = MagicMock()
    cfg_result.scalar_one_or_none.return_value = types.SimpleNamespace(
        project_id=TEST_PROJECT_ID
    )
    cfg_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=cfg_result)

    async def _get(_cls, pk):
        return types.SimpleNamespace(id=pk, project_id=TEST_PROJECT_ID)

    db.get = AsyncMock(side_effect=_get)

    async def _refresh(record):
        if getattr(record, "id", None) is None:
            record.id = uuid.uuid4()

    db.refresh = AsyncMock(side_effect=_refresh)

    steps = [_step("load_sales"), _step("load_units")]
    payload = _recipe_payload(steps)
    try:
        with (
            patch("src.api.recipes.get_tenant_db", async_gen_from(db)),
            patch(
                "src.api.recipes.acquire_model_definition_lock",
                new_callable=AsyncMock,
            ),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                resp = await client.post(
                    f"/api/v1/projects/{TEST_PROJECT_ID}/agent/recipes",
                    json=payload,
                )
        assert resp.status_code == 201, resp.text
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_update_recipe_introduces_duplicates_returns_422():
    """Updating a recipe to introduce duplicate step names must be rejected."""
    app.dependency_overrides[get_current_user] = _user
    db = _db_for_write()

    existing_id = uuid.uuid4()
    existing_record = types.SimpleNamespace(
        id=existing_id,
        project_id=TEST_PROJECT_ID,
        steps=[],
    )
    db.get = AsyncMock(return_value=existing_record)
    db.refresh = AsyncMock()

    steps = [_step("load_sales"), _step("LOAD_SALES")]
    payload = _recipe_payload(steps)
    try:
        with patch("src.api.recipes.get_tenant_db", async_gen_from(db)):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                resp = await client.put(
                    f"/api/v1/projects/{TEST_PROJECT_ID}/agent/recipes/{existing_id}",
                    json=payload,
                )
        assert resp.status_code == 422, resp.text
        assert "Duplicate step name" in resp.json()["detail"]
        # Bug-8500 review gap: the guard must run BEFORE persistence, not
        # merely exist somewhere in the route. Assert nothing was written.
        db.commit.assert_not_awaited()
        db.add.assert_not_called()
    finally:
        app.dependency_overrides.pop(get_current_user, None)
