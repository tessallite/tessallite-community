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
    model = types.SimpleNamespace(id="model-1", slug="m", deployed_version_id="v1")
    measure = types.SimpleNamespace(name="Amount", default_agg="sum", is_additive=True)
    hierarchy_level = types.SimpleNamespace(
        name="Region",
        source_column_id="col-region",
        user_defined_attribute_id=None,
    )
    db = AsyncMock()

    with (
        patch("src.semantic.binder._load_model", new=AsyncMock(return_value=model)),
        patch("src.semantic.binder._load_measures", new=AsyncMock(return_value=[measure])),
        patch("src.semantic.binder._load_dimensions", new=AsyncMock(return_value=[])),
        patch(
            "src.semantic.binder._load_hidden_column_ids",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "src.semantic.binder._load_hierarchy_level_dimensions",
            new=AsyncMock(return_value=[hierarchy_level]),
        ),
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
    model = types.SimpleNamespace(id="model-1", slug="m", deployed_version_id="v1")
    hierarchy_level = types.SimpleNamespace(
        name="fx_segment",
        source_column_id=None,
        user_defined_attribute_id="uda-segment",
    )
    db = AsyncMock()

    with (
        patch("src.semantic.binder._load_model", new=AsyncMock(return_value=model)),
        patch("src.semantic.binder._load_measures", new=AsyncMock(return_value=[])),
        patch("src.semantic.binder._load_dimensions", new=AsyncMock(return_value=[])),
        patch(
            "src.semantic.binder._load_hidden_column_ids",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "src.semantic.binder._load_hierarchy_level_dimensions",
            new=AsyncMock(return_value=[hierarchy_level]),
        ),
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
