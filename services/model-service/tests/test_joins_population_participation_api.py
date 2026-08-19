"""Join CRUD contract for ``population_participation`` (Bug-8615 phase G1).

The write boundary is where the "zero behaviour change" invariant is either
kept or lost: a caller that has never heard of this field — which is every
frontend call until phase G2 and every existing script — must keep creating
exactly the join it created before. And the read must never 500 on a row whose
value predates the vocabulary.
"""
from __future__ import annotations

import types
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from shared.db.models import Join, Model, ModelColumn, ModelTable
from src.api.joins import _auto_hide_dim_join_key, _auto_unhide_dim_join_key

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    client,
    make_mock_db,
)

pytestmark = pytest.mark.unit

_JOIN_ID = uuid.uuid4()
_LEFT_TABLE = uuid.uuid4()
_RIGHT_TABLE = uuid.uuid4()
_LEFT_COL = uuid.uuid4()
_RIGHT_COL = uuid.uuid4()
_URL = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/joins/{_JOIN_ID}"
)


def _fixture(participation="preserve_base_rows"):
    join = types.SimpleNamespace(
        id=_JOIN_ID,
        model_id=TEST_MODEL_ID,
        left_table_id=_LEFT_TABLE,
        right_table_id=_RIGHT_TABLE,
        join_type="inner",
        cardinality=None,
        population_participation=participation,
        left_column_id=_LEFT_COL,
        right_column_id=_RIGHT_COL,
        created_at=datetime.now(UTC),
    )
    rows = {
        (Model, TEST_MODEL_ID): types.SimpleNamespace(
            id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID,
        ),
        (Join, _JOIN_ID): join,
        (ModelColumn, _LEFT_COL): types.SimpleNamespace(
            id=_LEFT_COL, column_name="customer_id", data_type="INTEGER",
        ),
        (ModelColumn, _RIGHT_COL): types.SimpleNamespace(
            id=_RIGHT_COL, column_name="id", data_type="INTEGER",
        ),
        (ModelTable, _LEFT_TABLE): types.SimpleNamespace(
            id=_LEFT_TABLE, physical_name="demo.fact",
        ),
        (ModelTable, _RIGHT_TABLE): types.SimpleNamespace(
            id=_RIGHT_TABLE, physical_name="demo.dim",
        ),
    }
    db = make_mock_db()
    db.get = AsyncMock(side_effect=lambda entity, pk: rows.get((entity, pk)))
    return db, join


@pytest.mark.asyncio
async def test_get_returns_the_declared_value(client) -> None:
    db, _join = _fixture(participation="population_defining")
    with patch("src.api.joins.get_tenant_db", async_gen_from(db)):
        response = await client.get(_URL)
    assert response.status_code == 200, response.text
    assert response.json()["population_participation"] == "population_defining"


@pytest.mark.asyncio
async def test_a_legacy_or_tampered_value_does_not_break_the_read(client) -> None:
    """Coerced on read, exactly as ``join_type`` is coerced at render time."""
    db, _join = _fixture(participation="something_unknown")
    with patch("src.api.joins.get_tenant_db", async_gen_from(db)):
        response = await client.get(_URL)
    assert response.status_code == 200, response.text
    assert response.json()["population_participation"] == "undeclared"


@pytest.mark.asyncio
async def test_patch_sets_the_population_participation(client) -> None:
    db, join = _fixture()
    with patch("src.api.joins.get_tenant_db", async_gen_from(db)):
        response = await client.patch(
            _URL, json={"population_participation": "enrichment_only"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["population_participation"] == "enrichment_only"
    assert join.population_participation == "enrichment_only"


@pytest.mark.asyncio
async def test_a_patch_that_omits_the_field_leaves_it_alone(client) -> None:
    """The whole point of the default being safe: an unrelated edit from a
    caller that does not know about the field must not silently re-declare the
    join's population intent."""
    db, join = _fixture(participation="population_defining")
    with patch("src.api.joins.get_tenant_db", async_gen_from(db)):
        response = await client.patch(_URL, json={"join_type": "left"})
    assert response.status_code == 200, response.text
    assert join.population_participation == "population_defining"
    assert response.json()["population_participation"] == "population_defining"


@pytest.mark.asyncio
async def test_an_invalid_value_is_rejected_at_the_write_boundary(client) -> None:
    """Unlike the READ path, the WRITE path is Literal-constrained: a bad value
    must never reach storage in the first place."""
    db, join = _fixture()
    with patch("src.api.joins.get_tenant_db", async_gen_from(db)):
        response = await client.patch(
            _URL, json={"population_participation": "banana"},
        )
    assert response.status_code == 422
    assert join.population_participation == "preserve_base_rows"


@pytest.mark.asyncio
async def test_create_persists_an_explicit_non_default_value(client) -> None:
    """Bug-8657. PATCH wiring was mutation-proven; CREATE wiring was not, so
    dropping ``population_participation=body.population_participation`` from
    the constructor would silently fall back to the default with every test
    still green. This is the guard for that."""
    created: list = []
    db = make_mock_db()
    db.get = AsyncMock(side_effect=lambda entity, pk: {
        (Model, TEST_MODEL_ID): types.SimpleNamespace(
            id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID,
        ),
        (ModelTable, _LEFT_TABLE): types.SimpleNamespace(
            id=_LEFT_TABLE, model_id=TEST_MODEL_ID, physical_name="demo.fact",
            table_type="fact",
        ),
        (ModelTable, _RIGHT_TABLE): types.SimpleNamespace(
            id=_RIGHT_TABLE, model_id=TEST_MODEL_ID, physical_name="demo.dim",
            table_type="dim_detail",
        ),
        (ModelColumn, _LEFT_COL): types.SimpleNamespace(
            id=_LEFT_COL, column_name="customer_id", data_type="INTEGER",
        ),
        (ModelColumn, _RIGHT_COL): types.SimpleNamespace(
            id=_RIGHT_COL, column_name="id", data_type="INTEGER",
        ),
    }.get((entity, pk)))
    db.add = lambda obj: created.append(obj)

    async def _resolve(
        _db, table_id, _name, _dt="unknown", *,
        model_id=None, project_id=None, field_name=None,
    ):
        # The engine now REQUIRES the path model context (it refuses to
        # insert a ModelColumn into a table it cannot prove the model owns).
        # Assert the route threads it through rather than accepting anything,
        # so a caller that drops the scope fails here instead of silently
        # writing into another project's table.
        assert model_id == TEST_MODEL_ID, model_id
        assert project_id == TEST_PROJECT_ID, project_id
        return (
            types.SimpleNamespace(
                id=_LEFT_COL, column_name="customer_id", data_type="INTEGER",
                hidden_reason=None, is_hidden=False,
            )
            if table_id == _LEFT_TABLE else
            types.SimpleNamespace(
                id=_RIGHT_COL, column_name="id", data_type="INTEGER",
                hidden_reason=None, is_hidden=False,
            )
        )

    async def _refresh(obj, *_a, **_kw):
        # Stand in for the flush that would assign the ORM's Python-side
        # defaults (id) and the DB's server defaults (created_at).
        obj.id = obj.id or uuid.uuid4()
        obj.created_at = datetime.now(UTC)

    db.refresh = AsyncMock(side_effect=_refresh)

    with patch("src.api.joins.get_tenant_db", async_gen_from(db)),             patch("src.api.joins.resolve_column", _resolve):
        response = await client.post(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/joins",
            json={
                "left_table_id": str(_LEFT_TABLE),
                "right_table_id": str(_RIGHT_TABLE),
                "join_type": "inner",
                "left_column_name": "customer_id",
                "right_column_name": "id",
                "population_participation": "population_defining",
            },
        )

    assert response.status_code == 201, response.text
    assert response.json()["population_participation"] == "population_defining"
    assert created, "the route never added a Join"
    assert created[0].population_participation == "population_defining"


@pytest.mark.asyncio
async def test_create_without_the_field_uses_the_safe_default(client) -> None:
    """The zero-behaviour-change path at the write boundary."""
    created: list = []
    db = make_mock_db()
    db.get = AsyncMock(side_effect=lambda entity, pk: {
        (Model, TEST_MODEL_ID): types.SimpleNamespace(
            id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID,
        ),
        (ModelTable, _LEFT_TABLE): types.SimpleNamespace(
            id=_LEFT_TABLE, model_id=TEST_MODEL_ID, physical_name="demo.fact",
            table_type="fact",
        ),
        (ModelTable, _RIGHT_TABLE): types.SimpleNamespace(
            id=_RIGHT_TABLE, model_id=TEST_MODEL_ID, physical_name="demo.dim",
            table_type="dim_detail",
        ),
    }.get((entity, pk)))
    db.add = lambda obj: created.append(obj)

    async def _resolve(
        _db, table_id, _name, _dt="unknown", *,
        model_id=None, project_id=None, field_name=None,
    ):
        # The engine now REQUIRES the path model context (it refuses to
        # insert a ModelColumn into a table it cannot prove the model owns).
        # Assert the route threads it through rather than accepting anything,
        # so a caller that drops the scope fails here instead of silently
        # writing into another project's table.
        assert model_id == TEST_MODEL_ID, model_id
        assert project_id == TEST_PROJECT_ID, project_id
        return types.SimpleNamespace(
            id=_LEFT_COL if table_id == _LEFT_TABLE else _RIGHT_COL,
            column_name="k", data_type="INTEGER",
            hidden_reason=None, is_hidden=False,
        )

    async def _refresh(obj, *_a, **_kw):
        # Stand in for the flush that would assign the ORM's Python-side
        # defaults (id) and the DB's server defaults (created_at).
        obj.id = obj.id or uuid.uuid4()
        obj.created_at = datetime.now(UTC)

    db.refresh = AsyncMock(side_effect=_refresh)

    with patch("src.api.joins.get_tenant_db", async_gen_from(db)),             patch("src.api.joins.resolve_column", _resolve):
        response = await client.post(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/joins",
            json={
                "left_table_id": str(_LEFT_TABLE),
                "right_table_id": str(_RIGHT_TABLE),
                "join_type": "inner",
                "left_column_name": "k",
                "right_column_name": "k",
            },
        )

    assert response.status_code == 201, response.text
    assert created[0].population_participation == "preserve_base_rows"


@pytest.mark.asyncio
async def test_join_routes_reject_a_model_from_another_project(client) -> None:
    db = make_mock_db()
    db.get = AsyncMock(
        return_value=types.SimpleNamespace(
            id=TEST_MODEL_ID, project_id=uuid.uuid4(),
        )
    )

    with patch("src.api.joins.get_tenant_db", async_gen_from(db)):
        response = await client.get(_URL)

    assert response.status_code == 404
    assert not any(
        call.args and call.args[0] is Join for call in db.get.await_args_list
    ), "the route read a join before proving the model belongs to the path project"


# ---------------------------------------------------------------------------
# Bug-8626 / L14: the auto-hide/auto-unhide fact-vs-dimension classification
# now goes through ``shared.semantic.graph_order.is_fact_table`` instead of a
# private ``table.table_type == "fact"`` comparison. These directly exercise
# the converted lines with the same SimpleNamespace-shaped rows the API tests
# above already use, so a regression in either the conversion or the shared
# primitive's ORM-row handling fails here rather than only in a route test
# that never asserts the hidden-column outcome.
# ---------------------------------------------------------------------------

def _table_row(table_type: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(id=uuid.uuid4(), table_type=table_type)


def _col_row() -> types.SimpleNamespace:
    return types.SimpleNamespace(hidden_reason=None, is_hidden=False)


@pytest.mark.asyncio
async def test_auto_hide_hides_the_dimension_side_of_a_fact_to_dim_join() -> None:
    db = make_mock_db()
    db.flush = AsyncMock()
    fact = _table_row("fact")
    dim = _table_row("dim_detail")
    left_col, right_col = _col_row(), _col_row()

    await _auto_hide_dim_join_key(db, fact, dim, left_col, right_col)

    assert right_col.is_hidden is True
    assert right_col.hidden_reason == "join"
    assert left_col.is_hidden is False


@pytest.mark.asyncio
async def test_auto_hide_is_orientation_independent() -> None:
    """Same join, fact on the right this time — the other classification
    branch (``is_fact_table(right_table)``) must fire identically."""
    db = make_mock_db()
    db.flush = AsyncMock()
    dim = _table_row("dim_detail")
    fact = _table_row("fact")
    left_col, right_col = _col_row(), _col_row()

    await _auto_hide_dim_join_key(db, dim, fact, left_col, right_col)

    assert left_col.is_hidden is True
    assert left_col.hidden_reason == "join"
    assert right_col.is_hidden is False


@pytest.mark.asyncio
async def test_auto_hide_does_nothing_between_two_non_fact_tables() -> None:
    """Non-fact-to-dimension classification must not fire — proves the
    converted comparison still requires the FACT_TABLE_TYPE-exact match
    (``dim_aggregate`` is not a fact table) rather than hiding on any pairing."""
    db = make_mock_db()
    db.flush = AsyncMock()
    left = _table_row("dim_aggregate")
    right = _table_row("dim_detail")
    left_col, right_col = _col_row(), _col_row()

    await _auto_hide_dim_join_key(db, left, right, left_col, right_col)

    assert left_col.is_hidden is False
    assert right_col.is_hidden is False


@pytest.mark.asyncio
async def test_auto_unhide_reveals_the_dimension_side_once_unreferenced() -> None:
    db = make_mock_db()
    fact = _table_row("fact")
    dim = _table_row("dim_detail")
    right_col = types.SimpleNamespace(id=_RIGHT_COL, hidden_reason="join", is_hidden=True)
    left_col = types.SimpleNamespace(id=_LEFT_COL, hidden_reason=None, is_hidden=False)
    other_joins_result = types.SimpleNamespace(
        scalars=lambda: types.SimpleNamespace(all=lambda: [])
    )
    db.execute = AsyncMock(return_value=other_joins_result)

    await _auto_unhide_dim_join_key(db, TEST_MODEL_ID, fact, dim, left_col, right_col)

    assert right_col.is_hidden is False
    assert right_col.hidden_reason is None
