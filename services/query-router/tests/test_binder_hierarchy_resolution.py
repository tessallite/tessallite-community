"""
Unit tests for hierarchy-level fallback binding in semantic binder.
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

import pytest

from src.ir.logical_query import LogicalFilter, LogicalQuery
from src.semantic.binder import bind_query_to_model


def _query(*, dims: list[str], measures: list[str], filters: list[LogicalFilter] | None = None) -> LogicalQuery:
    return LogicalQuery(
        model_id="model-1",
        protocol="jdbc",
        raw_query="SELECT 1",
        requested_measures=measures,
        requested_dimensions=dims,
        filters=filters or [],
        grain=list(dims),
        order_by=[],
        limit=None,
        offset=None,
        query_fingerprint="fp",
    )


async def test_bind_query_resolves_hierarchy_level_when_dimension_missing():
    from src.semantic.snapshot_resolver import DeployedShape

    model = types.SimpleNamespace(id="model-1", slug="m", deployed_version_id="v1")
    measure = types.SimpleNamespace(name="Amount", default_agg="sum", is_additive=True)
    shape = DeployedShape(
        measures=[measure], dimensions=[],
        hidden_column_ids=set(),
        physical_columns_all=set(), physical_columns_visible=set(),
        hierarchy_rows=[{
            "id": "h1", "name": "GeoHierarchy", "dimension_kind": None,
            "levels": [{
                "id": "lv1", "name": "Region", "ordinal": 0,
                "key_attribute_source": "physical_column",
                "key_attribute_id": "col-region",
            }],
        }],
    )
    db = AsyncMock()

    with (
        patch("src.semantic.binder._load_model", new=AsyncMock(return_value=model)),
        patch("src.semantic.binder.resolve_deployed_shape",
              new=AsyncMock(return_value=shape)),
    ):
        bound = await bind_query_to_model(
            _query(dims=["Region"], measures=["Amount"]),
            db,
        )

    assert len(bound.resolved_dimensions) == 1
    assert bound.resolved_dimensions[0].name == "Region"
    assert bound.resolved_dimensions[0].source_column_id == "col-region"
    assert "Region" in bound.resolved_dimensions_by_name


async def test_bind_query_keeps_filter_only_hierarchy_level_in_dimension_map():
    from src.semantic.snapshot_resolver import DeployedShape

    model = types.SimpleNamespace(id="model-1", slug="m", deployed_version_id="v1")
    shape = DeployedShape(
        measures=[], dimensions=[],
        hidden_column_ids=set(),
        physical_columns_all=set(), physical_columns_visible=set(),
        hierarchy_rows=[{
            "id": "h2", "name": "FxHierarchy", "dimension_kind": None,
            "levels": [{
                "id": "lv2", "name": "fx_segment", "ordinal": 0,
                "key_attribute_source": "user_defined_attribute",
                "key_attribute_id": "uda-segment",
            }],
        }],
    )
    db = AsyncMock()

    with (
        patch("src.semantic.binder._load_model", new=AsyncMock(return_value=model)),
        patch("src.semantic.binder.resolve_deployed_shape",
              new=AsyncMock(return_value=shape)),
    ):
        bound = await bind_query_to_model(
            _query(
                dims=[],
                measures=[],
                filters=[LogicalFilter("fx_segment", "eq", "A")],
            ),
            db,
        )

    assert bound.resolved_dimensions == []
    assert bound.resolved_dimensions_by_name["fx_segment"].user_defined_attribute_id == "uda-segment"
    assert bound.resolved_filters[0].dimension_name == "fx_segment"
