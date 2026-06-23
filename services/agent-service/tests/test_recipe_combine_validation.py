"""Recipe create/update must accept real combine expression trees (Bug-5346).

Business outcome under test: a modeller saving a combine expression tree
(ratio of two steps) gets a 201, and the saved tree evaluates to the expected
number against real step rows at execution time. Structurally or semantically
invalid trees (unknown step, unknown measure, bad op, malformed shape) get 422.
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
from src.api.recipes import RecipeStep, _validate_combine
from src.recipes.eval import evaluate_combine

from .conftest import TEST_TENANT, TEST_PROJECT_ID, make_mock_db, async_gen_from


def _ref(step, measure):
    return {"ref": {"step": step, "measure": measure}}


def _op(name, *args):
    return {"op": name, "args": list(args)}


def _c(v):
    return {"const": v}


# round(sales.revenue / units.qty * 100, 2)
def _ratio_pct():
    return _op("round", _op("mul", _op("div", _ref("sales", "revenue"),
                                       _ref("units", "qty")), _c(100)), _c(2))


def _steps() -> list[RecipeStep]:
    return [
        RecipeStep(name="sales", model_id=uuid.uuid4(), measures=["revenue"]),
        RecipeStep(name="units", model_id=uuid.uuid4(), measures=["qty"]),
    ]


# ---------------------------------------------------------------------------
# Validator unit behaviour
# ---------------------------------------------------------------------------


class TestValidateCombine:
    def test_documented_ratio_tree_is_accepted(self):
        _validate_combine(_op("div", _ref("sales", "revenue"), _ref("units", "qty")), _steps())

    def test_round_ratio_tree_is_accepted(self):
        _validate_combine(_ratio_pct(), _steps())

    def test_none_combine_is_accepted(self):
        _validate_combine(None, _steps())

    def test_unknown_step_rejected(self):
        with pytest.raises(HTTPException) as exc:
            _validate_combine(_op("div", _ref("ghosts", "revenue"), _ref("units", "qty")), _steps())
        assert exc.value.status_code == 422
        assert "ghosts" in exc.value.detail

    def test_unknown_measure_rejected(self):
        with pytest.raises(HTTPException) as exc:
            _validate_combine(_op("div", _ref("sales", "profit"), _ref("units", "qty")), _steps())
        assert exc.value.status_code == 422
        assert "profit" in exc.value.detail

    def test_bad_op_rejected(self):
        with pytest.raises(HTTPException) as exc:
            _validate_combine(_op("system", _ref("sales", "revenue")), _steps())
        assert exc.value.status_code == 422

    def test_malformed_tree_rejected(self):
        with pytest.raises(HTTPException) as exc:
            _validate_combine(_op("div", _ref("sales", "revenue")), _steps())  # div needs 2 args
        assert exc.value.status_code == 422

    def test_validated_tree_evaluates_to_expected_number(self):
        """The validator and the runtime evaluator must agree: a tree that
        saves also computes the business result. Step rows alias measures by
        name, so the runtime combine context is {step_name: first_row}."""
        expr = _op("round", _op("div", _ref("sales", "revenue"), _ref("units", "qty")), _c(2))
        _validate_combine(expr, _steps())  # saves
        value = evaluate_combine(
            expr,
            {"sales": {"revenue": 100.0}, "units": {"qty": 4}},
        )
        assert value == 25.0


# ---------------------------------------------------------------------------
# Endpoint behaviour — POST /recipes with a real expression tree returns 201
# ---------------------------------------------------------------------------


def _recipe_payload(combine) -> dict:
    return {
        "name": "Revenue per unit",
        "description": "Cross-model ratio",
        "parameters": [],
        "steps": [
            {
                "name": "sales",
                "model_id": str(uuid.uuid4()),
                "measures": ["revenue"],
                "dimensions": [],
                "filters": [],
                "limit": 100,
            },
            {
                "name": "units",
                "model_id": str(uuid.uuid4()),
                "measures": ["qty"],
                "dimensions": [],
                "filters": [],
                "limit": 100,
            },
        ],
        "combine": combine,
        "notes": None,
    }


def _user():
    return CurrentUser(
        user_id="modeller@test.com",
        tenant_id=TEST_TENANT,
        email="modeller@test.com",
        role="tenant_admin",
    )


def _db_for_create() -> AsyncMock:
    db = make_mock_db()
    cfg_result = MagicMock()
    cfg_result.scalar_one_or_none.return_value = types.SimpleNamespace(
        project_id=TEST_PROJECT_ID
    )
    db.execute = AsyncMock(return_value=cfg_result)

    async def _get(_cls, pk):
        return types.SimpleNamespace(id=pk, project_id=TEST_PROJECT_ID)

    db.get = AsyncMock(side_effect=_get)

    async def _refresh(record):
        if getattr(record, "id", None) is None:
            record.id = uuid.uuid4()

    db.refresh = AsyncMock(side_effect=_refresh)
    return db


@pytest.mark.asyncio
async def test_create_recipe_with_real_combine_tree_returns_201():
    app.dependency_overrides[get_current_user] = _user
    db = _db_for_create()
    combine = _op("div", _ref("sales", "revenue"), _ref("units", "qty"))
    try:
        with patch("src.api.recipes.get_tenant_db", async_gen_from(db)):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                resp = await client.post(
                    f"/api/v1/projects/{TEST_PROJECT_ID}/agent/recipes",
                    json=_recipe_payload(combine),
                )
        assert resp.status_code == 201, resp.text
        assert resp.json()["combine"] == combine
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_create_recipe_with_unknown_measure_returns_422():
    app.dependency_overrides[get_current_user] = _user
    db = _db_for_create()
    combine = _op("div", _ref("sales", "nonexistent"), _ref("units", "qty"))
    try:
        with patch("src.api.recipes.get_tenant_db", async_gen_from(db)):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                resp = await client.post(
                    f"/api/v1/projects/{TEST_PROJECT_ID}/agent/recipes",
                    json=_recipe_payload(combine),
                )
        assert resp.status_code == 422
        assert "combine expression rejected" in resp.text
    finally:
        app.dependency_overrides.pop(get_current_user, None)
