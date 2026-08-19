"""Phase 2 of the semantic-layer plan — visibility cascade in the binder.

The binder drops dimensions and measures whose underlying ModelColumn
is flagged `is_hidden` when `include_hidden=False` (business view), and
keeps them when `include_hidden=True` (the `<model>_technical` variant).
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

from src.ir.logical_query import LogicalQuery
from src.semantic.binder import bind_query_to_model


def _query(**overrides) -> LogicalQuery:
    base = dict(
        model_id="model-1",
        protocol="jdbc",
        raw_query="SELECT *",
        requested_measures=[],
        requested_dimensions=[],
        filters=[],
        grain=[],
        order_by=[],
        limit=None,
        offset=None,
        query_fingerprint="fp",
        select_star=True,
    )
    base.update(overrides)
    return LogicalQuery(**base)


async def test_business_view_hides_dims_and_measures_with_hidden_source_column():
    from src.semantic.snapshot_resolver import DeployedShape

    model = types.SimpleNamespace(id="model-1", slug="m", deployed_version_id="v1")
    visible_col_id = "col-visible"
    hidden_col_id = "col-hidden"

    visible_dim = types.SimpleNamespace(name="Region", source_column_id=visible_col_id)
    hidden_dim = types.SimpleNamespace(name="internal_code", source_column_id=hidden_col_id)
    visible_measure = types.SimpleNamespace(
        name="Revenue", default_agg="sum", is_additive=True, source_column_id=visible_col_id
    )
    hidden_measure = types.SimpleNamespace(
        name="debug_cost", default_agg="sum", is_additive=True, source_column_id=hidden_col_id
    )
    shape = DeployedShape(
        measures=[visible_measure, hidden_measure],
        dimensions=[visible_dim, hidden_dim],
        hidden_column_ids={hidden_col_id},
        physical_columns_all={"region", "internal_code"},
        physical_columns_visible={"region"},
        hierarchy_rows=[],
    )
    db = AsyncMock()

    with (
        patch("src.semantic.binder._load_model", new=AsyncMock(return_value=model)),
        patch("src.semantic.binder.resolve_deployed_shape",
              new=AsyncMock(return_value=shape)),
    ):
        bound = await bind_query_to_model(_query(), db, include_hidden=False)

    dim_names = {d.name for d in bound.resolved_dimensions}
    measure_names = {m.name for m in bound.resolved_measures}
    assert dim_names == {"Region"}
    assert measure_names == {"Revenue"}


async def test_technical_view_keeps_hidden_objects():
    from src.semantic.snapshot_resolver import DeployedShape

    model = types.SimpleNamespace(id="model-1", slug="m", deployed_version_id="v1")
    visible_dim = types.SimpleNamespace(name="Region", source_column_id="col-visible")
    hidden_dim = types.SimpleNamespace(name="internal_code", source_column_id="col-hidden")
    shape = DeployedShape(
        measures=[],
        dimensions=[visible_dim, hidden_dim],
        hidden_column_ids={"col-hidden"},
        physical_columns_all={"region", "internal_code"},
        physical_columns_visible={"region"},
        hierarchy_rows=[],
    )
    db = AsyncMock()

    with (
        patch("src.semantic.binder._load_model", new=AsyncMock(return_value=model)),
        patch("src.semantic.binder.resolve_deployed_shape",
              new=AsyncMock(return_value=shape)),
    ):
        bound = await bind_query_to_model(_query(), db, include_hidden=True)

    assert {d.name for d in bound.resolved_dimensions} == {"Region", "internal_code"}
