from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.db.models import LineageMapping

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    make_mock_db,
    make_model,
)

pytestmark = pytest.mark.unit


def _result(*, scalars=None, all_rows=None, scalar=None):
    result = MagicMock()
    result.scalars.return_value.all.return_value = scalars or []
    result.all.return_value = all_rows or []
    result.scalar.return_value = scalar
    return result


@pytest.mark.anyio
async def test_lineage_graph_emits_consumable_field_nodes_from_real_lineage_mapping(client):
    source_id = uuid.uuid4()
    table_id = uuid.uuid4()
    column_id = uuid.uuid4()
    lineage = LineageMapping(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        semantic_field_name="gross_revenue",
        semantic_field_type="measure",
        aggregate_col_id=None,
        source_column_id=column_id,
    )
    source = types.SimpleNamespace(
        id=source_id,
        display_name="Warehouse",
        source_type="postgres",
        config={"schema": "public"},
    )
    table = types.SimpleNamespace(
        id=table_id,
        model_id=TEST_MODEL_ID,
        source_id=source_id,
        physical_name="orders",
        alias="orders",
        display_name="Orders",
    )
    column = types.SimpleNamespace(
        id=column_id,
        model_table_id=table_id,
        column_name="revenue",
        display_name="Revenue",
        data_type="numeric",
        is_hidden=False,
    )

    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())
    db.execute = AsyncMock(
        side_effect=[
            _result(scalars=[source]),
            _result(),
            _result(),
            _result(scalars=[lineage]),
            _result(all_rows=[(source_id, 1)]),
            _result(scalar=0),
            _result(all_rows=[(column, table)]),
        ]
    )

    with patch("src.api.lineage.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/lineage"
        )

    assert resp.status_code == 200
    body = resp.json()
    nodes = {node["id"]: node for node in body["nodes"]}
    edges = {(edge["source"], edge["target"], edge["label"]) for edge in body["edges"]}

    column_node_id = f"col:{column_id}"
    field_node_id = "field:measure:gross_revenue"
    assert nodes[column_node_id]["type"] == "column"
    assert nodes[column_node_id]["label"] == "Revenue"
    assert nodes[field_node_id]["type"] == "field"
    assert nodes[field_node_id]["label"] == "gross_revenue"
    assert (column_node_id, field_node_id, "feeds") in edges
    assert (field_node_id, str(TEST_MODEL_ID), "defined by") in edges
