"""Regression contract for Bug-8094 join type-mismatch warnings."""
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from shared.db.models import Join, Model, ModelColumn, ModelTable
from shared.schemas.pydantic_models import JoinResponse
from src.api.joins import _check_join_type_mismatch

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    client,
    make_mock_db,
)


def test_type_mismatch_warning_survives_join_response_boundary() -> None:
    left_column = SimpleNamespace(data_type="INTEGER")
    right_column = SimpleNamespace(data_type="VARCHAR")
    left_table = SimpleNamespace(physical_name="fact_sales")
    right_table = SimpleNamespace(physical_name="dim_customer")

    warnings = _check_join_type_mismatch(
        left_column,
        right_column,
        left_table,
        right_table,
        "customer_id",
        "customer_key",
    )

    response = JoinResponse(
        id=uuid4(),
        model_id=uuid4(),
        left_table_id=uuid4(),
        right_table_id=uuid4(),
        join_type="left",
        left_column_id=uuid4(),
        right_column_id=uuid4(),
        left_column_name="customer_id",
        right_column_name="customer_key",
        created_at=datetime.now(UTC),
        warnings=warnings,
    )

    assert len(response.warnings or []) == 1
    assert "INTEGER" in response.warnings[0]
    assert "VARCHAR" in response.warnings[0]
    assert "CAST" in response.warnings[0]


def test_compatible_types_do_not_emit_join_warning() -> None:
    left_column = SimpleNamespace(data_type="INTEGER")
    right_column = SimpleNamespace(data_type="INTEGER")
    table = SimpleNamespace(physical_name="fact_sales")

    warnings = _check_join_type_mismatch(
        left_column,
        right_column,
        table,
        table,
        "customer_id",
        "customer_key",
    )

    assert warnings == []


@pytest.mark.asyncio
async def test_update_endpoint_returns_type_mismatch_warning(client) -> None:
    join_id = uuid4()
    left_table_id = uuid4()
    right_table_id = uuid4()
    left_column_id = uuid4()
    right_column_id = uuid4()
    join = SimpleNamespace(
        id=join_id,
        model_id=TEST_MODEL_ID,
        left_table_id=left_table_id,
        right_table_id=right_table_id,
        join_type="inner",
        # The stub stands in for a real ``Join`` row, so it has to carry every
        # column the response builder reads. ``cardinality`` (join-orientation
        # contract) and ``population_participation`` (Bug-8615 governance G1)
        # are both NOT-NULL-with-default columns on the ORM; omitting them here
        # made the PATCH route raise AttributeError rather than 200.
        cardinality=None,
        population_participation="preserve_base_rows",
        left_column_id=left_column_id,
        right_column_id=right_column_id,
        created_at=datetime.now(UTC),
    )
    rows = {
        (Model, TEST_MODEL_ID): SimpleNamespace(
            id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID,
        ),
        (Join, join_id): join,
        (ModelColumn, left_column_id): SimpleNamespace(
            id=left_column_id,
            column_name="customer_id",
            data_type="INTEGER",
        ),
        (ModelColumn, right_column_id): SimpleNamespace(
            id=right_column_id,
            column_name="customer_key",
            data_type="VARCHAR",
        ),
        (ModelTable, left_table_id): SimpleNamespace(
            id=left_table_id,
            physical_name="fact_sales",
        ),
        (ModelTable, right_table_id): SimpleNamespace(
            id=right_table_id,
            physical_name="dim_customer",
        ),
    }
    db = make_mock_db()
    db.get = AsyncMock(side_effect=lambda model_type, row_id: rows.get((model_type, row_id)))

    with patch("src.api.joins.get_tenant_db", async_gen_from(db)):
        response = await client.patch(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/"
            f"{TEST_MODEL_ID}/joins/{join_id}",
            json={"join_type": "left"},
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["join_type"] == "left"
    assert len(payload["warnings"]) == 1
    assert "INTEGER" in payload["warnings"][0]
    assert "VARCHAR" in payload["warnings"][0]
