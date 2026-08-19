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
from src.api.recipes import RecipeStep, _lock_recipe_models, _validate_combine
from shared.recipes.schema import (
    COMBINE_OP_ARITY,
    CombineSchemaError,
    collect_combine_references,
)
from src.recipes.eval import CombineEvalError, check_node_shape, evaluate_combine

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

    @pytest.mark.parametrize("op_value", [None, False, 0, 1.5, [], {}, ""])
    def test_agent_validation_matches_canonical_operator_path(self, op_value):
        node = {"op": op_value, "args": [_c(1), _c(2)]}
        with pytest.raises(CombineSchemaError) as canonical:
            collect_combine_references(node, path="expression")
        with pytest.raises(ValueError) as agent:
            check_node_shape(node)
        assert str(agent.value) == str(canonical.value)
        assert "expression.op" in str(agent.value)

    @pytest.mark.parametrize("op_value", [None, False, 0, 1.5, [], {}, ""])
    def test_legacy_runtime_malformed_operator_is_combine_error(self, op_value):
        with pytest.raises(CombineEvalError, match=r"expression\.op"):
            evaluate_combine(
                {"op": op_value, "args": [_c(1), _c(2)]},
                {},
            )

    @pytest.mark.parametrize("op", sorted(COMBINE_OP_ARITY))
    def test_agent_validation_accepts_every_canonical_operator_minimum(self, op):
        minimum, _maximum = COMBINE_OP_ARITY[op]
        check_node_shape({"op": op, "args": [_c(1)] * minimum})

    @pytest.mark.parametrize("op", sorted(COMBINE_OP_ARITY))
    def test_agent_validation_rejects_every_canonical_operator_below_minimum(
        self, op
    ):
        minimum, _maximum = COMBINE_OP_ARITY[op]
        with pytest.raises(ValueError, match=r"expression\.args"):
            check_node_shape({"op": op, "args": [_c(1)] * (minimum - 1)})


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
    cfg_result.scalars.return_value.all.return_value = ["revenue", "qty"]

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
    payload = _recipe_payload(combine)
    try:
        with (
            patch("src.api.recipes.get_tenant_db", async_gen_from(db)),
            patch(
                "src.api.recipes.acquire_model_definition_lock",
                new_callable=AsyncMock,
            ) as acquire_lock,
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                resp = await client.post(
                    f"/api/v1/projects/{TEST_PROJECT_ID}/agent/recipes",
                    json=payload,
                )
        assert resp.status_code == 201, resp.text
        assert resp.json()["combine"] == combine
        expected_ids = sorted(
            (uuid.UUID(step["model_id"]) for step in payload["steps"]),
            key=lambda value: value.int,
        )
        assert [call.args[1] for call in acquire_lock.await_args_list] == expected_ids
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_create_recipe_with_unknown_measure_returns_422():
    app.dependency_overrides[get_current_user] = _user
    db = _db_for_create()
    combine = _op("div", _ref("sales", "nonexistent"), _ref("units", "qty"))
    try:
        with (
            patch("src.api.recipes.get_tenant_db", async_gen_from(db)),
            patch(
                "src.api.recipes.acquire_model_definition_lock",
                new_callable=AsyncMock,
            ),
        ):
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


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["post", "put"])
@pytest.mark.parametrize("op_value", [[], {}], ids=["array-op", "object-op"])
async def test_recipe_write_malformed_operator_returns_422_not_500(
    method, op_value
):
    app.dependency_overrides[get_current_user] = _user
    payload = _recipe_payload({"op": op_value, "args": [_c(1), _c(2)]})
    url = f"/api/v1/projects/{TEST_PROJECT_ID}/agent/recipes"
    if method == "put":
        url += f"/{uuid.uuid4()}"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await getattr(client, method)(url, json=payload)
        assert response.status_code == 422, response.text
        assert "expression.op" in response.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_recipe_model_locks_cover_existing_and_new_models_in_stable_order():
    db = AsyncMock()
    first, second, third = sorted(
        (uuid.uuid4(), uuid.uuid4(), uuid.uuid4()), key=lambda value: value.int
    )
    steps = [RecipeStep(name="new", model_id=third, measures=[])]
    persisted = [
        {"name": "old", "model_id": str(second), "measures": []},
        {"name": "duplicate", "model_id": str(third), "measures": []},
        {"name": "earliest", "model_id": str(first), "measures": []},
    ]

    with patch(
        "src.api.recipes.acquire_model_definition_lock", new_callable=AsyncMock
    ) as acquire_lock:
        await _lock_recipe_models(db, steps, persisted)

    assert [call.args[1] for call in acquire_lock.await_args_list] == [
        first,
        second,
        third,
    ]
