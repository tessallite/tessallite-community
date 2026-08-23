"""Primary-key declaration contract for model table attributes."""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.db.models import Join, ModelColumn, ModelTable

from .conftest import TEST_MODEL_ID, TEST_PROJECT_ID, async_gen_from, make_mock_db

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_physical_attribute_list_exposes_declared_primary_key(client) -> None:
    table_id = uuid.uuid4()
    column_id = uuid.uuid4()
    db = make_mock_db()
    db.get = AsyncMock(return_value=types.SimpleNamespace(model_id=TEST_MODEL_ID))
    physical_result = MagicMock()
    physical_result.scalars.return_value.all.return_value = [
        types.SimpleNamespace(
            id=column_id,
            column_name="payment_id",
            display_name="Payment ID",
            description=None,
            is_hidden=True,
            hidden_reason=None,
            is_primary_key=True,
            data_type="bigint",
        )
    ]
    uda_result = MagicMock()
    uda_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(side_effect=[physical_result, uda_result])

    with (
        patch("src.api.table_attributes.get_tenant_db", async_gen_from(db)),
        patch("src.api.table_attributes.ensure_model_in_project", new=AsyncMock()),
    ):
        response = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/tables/{table_id}/attributes"
        )

    assert response.status_code == 200
    assert response.json()[0]["is_primary_key"] is True


@pytest.mark.asyncio
async def test_physical_attribute_update_persists_declared_primary_key(client) -> None:
    table_id = uuid.uuid4()
    column_id = uuid.uuid4()
    table = types.SimpleNamespace(model_id=TEST_MODEL_ID)
    column = types.SimpleNamespace(
        id=column_id,
        model_table_id=table_id,
        column_name="payment_id",
        display_name=None,
        description=None,
        is_hidden=False,
        hidden_reason=None,
        is_primary_key=False,
        data_type="bigint",
    )
    db = make_mock_db()
    db.get = AsyncMock(side_effect=[table, column])

    with (
        patch("src.api.table_attributes.get_tenant_db", async_gen_from(db)),
        patch("src.api.table_attributes.ensure_model_in_project", new=AsyncMock()),
    ):
        response = await client.patch(
            (
                f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"
                f"/tables/{table_id}/columns/{column_id}"
            ),
            json={"is_primary_key": True},
        )

    assert response.status_code == 200
    assert column.is_primary_key is True
    assert response.json()["is_primary_key"] is True
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_g4_sol_r1_b01_sync_columns_auto_flags_real_default_owned_join_only(client) -> None:
    """G4: the request boundary reaches default-owned ORM joins only."""
    fact_id = uuid.uuid4()
    dim_id = uuid.uuid4()
    ambiguous_dim_id = uuid.uuid4()
    fact_fk_id = uuid.uuid4()
    dim_pk_id = uuid.uuid4()
    ambiguous_pk_id = uuid.uuid4()
    second_ambiguous_pk_id = uuid.uuid4()
    join_id = uuid.uuid4()
    fact = ModelTable(
        id=fact_id, model_id=TEST_MODEL_ID, source_id=uuid.uuid4(),
        table_type="fact", physical_name="demo.fact", alias="fact",
        display_name="Fact",
    )
    dim = ModelTable(
        id=dim_id, model_id=TEST_MODEL_ID, source_id=uuid.uuid4(),
        table_type="dim_detail", physical_name="demo.dim", alias="dim",
        display_name="Dimension",
    )
    ambiguous_dim = ModelTable(
        id=ambiguous_dim_id, model_id=TEST_MODEL_ID, source_id=uuid.uuid4(),
        table_type="dim_detail", physical_name="demo.ambiguous_dim",
        alias="ambiguous_dim", display_name="Ambiguous Dimension",
    )
    join = Join(
        model_id=TEST_MODEL_ID,
        id=join_id,
        left_table_id=fact_id,
        right_table_id=dim_id,
        left_column_id=fact_fk_id,
        right_column_id=dim_pk_id,
        join_type="left",
        population_participation="preserve_base_rows",
        population_participation_source="default",
    )
    manual_join = Join(
        model_id=TEST_MODEL_ID,
        id=uuid.uuid4(), left_table_id=fact_id, right_table_id=dim_id,
        left_column_id=fact_fk_id, right_column_id=dim_pk_id, join_type="left",
        population_participation="preserve_base_rows",
        population_participation_source="manual",
    )
    reversed_join = Join(
        model_id=TEST_MODEL_ID,
        id=uuid.uuid4(), left_table_id=dim_id, right_table_id=fact_id,
        left_column_id=dim_pk_id, right_column_id=fact_fk_id, join_type="right",
        population_participation="preserve_base_rows",
        population_participation_source="default",
    )
    wrong_endpoint_join = Join(
        model_id=TEST_MODEL_ID,
        id=uuid.uuid4(), left_table_id=fact_id, right_table_id=dim_id,
        left_column_id=fact_fk_id, right_column_id=uuid.uuid4(), join_type="left",
        population_participation="preserve_base_rows",
        population_participation_source="default",
    )

    ambiguous_join = Join(
        model_id=TEST_MODEL_ID,
        id=uuid.uuid4(), left_table_id=fact_id, right_table_id=ambiguous_dim_id,
        left_column_id=fact_fk_id, right_column_id=ambiguous_pk_id, join_type="left",
        population_participation="preserve_base_rows",
        population_participation_source="default",
    )
    fact_fk = ModelColumn(
        id=fact_fk_id, model_table_id=fact_id, column_name="dim_id",
        data_type="bigint", is_primary_key=False,
    )
    dim_pk = ModelColumn(
        id=dim_pk_id, model_table_id=dim_id, column_name="id",
        data_type="bigint", is_primary_key=True,
    )
    ambiguous_pk = ModelColumn(
        id=ambiguous_pk_id, model_table_id=ambiguous_dim_id, column_name="id",
        data_type="bigint", is_primary_key=True,
    )
    second_ambiguous_pk = ModelColumn(
        id=second_ambiguous_pk_id, model_table_id=ambiguous_dim_id, column_name="tenant_id",
        data_type="bigint", is_primary_key=True,
    )

    existing_result = MagicMock()
    existing_result.scalars.return_value.all.return_value = []
    table_result = MagicMock()
    table_result.scalars.return_value.all.return_value = [fact, dim, ambiguous_dim]
    column_result = MagicMock()
    column_result.scalars.return_value.all.return_value = [
        fact_fk,
        dim_pk,
        ambiguous_pk,
        second_ambiguous_pk,
    ]
    join_result = MagicMock()
    join_result.scalars.return_value.all.return_value = [
        join, manual_join, reversed_join, wrong_endpoint_join, ambiguous_join,
    ]

    db = make_mock_db()
    rows_by_id = {
        join.id: join,
        manual_join.id: manual_join,
        reversed_join.id: reversed_join,
        wrong_endpoint_join.id: wrong_endpoint_join,
        ambiguous_join.id: ambiguous_join,
    }

    async def _get(entity, row_id):
        if entity is ModelTable:
            return {
                fact.id: fact, dim.id: dim, ambiguous_dim.id: ambiguous_dim,
            }.get(row_id)
        if entity is Join:
            return rows_by_id.get(row_id)
        return None

    db.get = AsyncMock(side_effect=_get)
    db.execute = AsyncMock(
        side_effect=[existing_result, table_result, column_result, join_result]
    )

    before = {
        row.id: (
            row.population_participation,
            row.population_participation_source,
            row.left_table_id,
            row.right_table_id,
            row.left_column_id,
            row.right_column_id,
        )
        for row in (manual_join, reversed_join, wrong_endpoint_join, ambiguous_join)
    }

    with (
        patch("src.api.table_attributes.get_tenant_db", async_gen_from(db)),
        patch("src.api.table_attributes.ensure_model_in_project", new=AsyncMock()),
    ):
        response = await client.post(
            (
                f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"
                f"/tables/{dim_id}/sync-columns"
            ),
            json=[
                {
                    "column_name": "id",
                    "data_type": "integer",
                    "is_nullable": False,
                    "is_primary_key": True,
                }
            ],
        )

    assert response.status_code == 204
    assert join.population_participation == "preserve_base_rows"
    assert join.population_participation_source == "auto"
    for row in (manual_join, reversed_join, wrong_endpoint_join, ambiguous_join):
        assert (
            row.population_participation,
            row.population_participation_source,
            row.left_table_id,
            row.right_table_id,
            row.left_column_id,
            row.right_column_id,
        ) == before[row.id]
    reread = await db.get(Join, join.id)
    assert reread is join
    assert reread.population_participation_source == "auto"

    # A second real request proves the ambiguity guard on a separately
    # introspected dimension.  Its two verified key columns must keep the
    # compatibility default rather than being guessed into an auto flag.
    db.execute.side_effect = [existing_result, table_result, column_result, join_result]
    with (
        patch("src.api.table_attributes.get_tenant_db", async_gen_from(db)),
        patch("src.api.table_attributes.ensure_model_in_project", new=AsyncMock()),
    ):
        ambiguous_response = await client.post(
            (
                f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"
                f"/tables/{ambiguous_dim_id}/sync-columns"
            ),
            json=[
                {
                    "column_name": "id",
                    "data_type": "integer",
                    "is_nullable": False,
                    "is_primary_key": True,
                }
            ],
        )
    assert ambiguous_response.status_code == 204
    assert ambiguous_join.population_participation_source == "default"
    assert db.commit.await_count == 2
