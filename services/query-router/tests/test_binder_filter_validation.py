"""
Unit tests for filter column validation in the semantic binder.

Covers:
- Unknown filter columns must raise SemanticBindingError (Bug-452)
- Case-insensitive filter resolution normalises to canonical name
- Measure names in filters are accepted
- Technical passthrough queries still allow unknown filters
- Business-relation complex SQL is blocked because it cannot audit hidden columns
"""
from __future__ import annotations

import types
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from src.ir.logical_query import LogicalFilter, LogicalQuery, SemanticBindingError
from src.semantic.binder import bind_query_to_model

_P = "src.semantic.binder"


def _query(
    *,
    dims: list[str],
    measures: list[str],
    filters: list[LogicalFilter] | None = None,
    has_complex_sql: bool = False,
    has_unresolvable_where: bool = False,
) -> LogicalQuery:
    lq = LogicalQuery(
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
    lq.has_complex_sql = has_complex_sql
    lq.has_unresolvable_where = has_unresolvable_where
    return lq


def _patches(dimensions, measures):
    model = types.SimpleNamespace(id="model-1", slug="testmodel", deployed_version_id="v1")
    stack = ExitStack()
    stack.enter_context(patch(f"{_P}._load_model", new=AsyncMock(return_value=model)))
    stack.enter_context(patch(f"{_P}._load_measures", new=AsyncMock(return_value=measures)))
    stack.enter_context(patch(f"{_P}._load_dimensions", new=AsyncMock(return_value=dimensions)))
    stack.enter_context(patch(f"{_P}._load_hidden_column_ids", new=AsyncMock(return_value=set())))
    stack.enter_context(patch(f"{_P}._load_hierarchy_level_dimensions", new=AsyncMock(return_value=[])))
    return stack


def _dim(name: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=f"d-{name}", name=name,
        source_column_id=f"col-{name}", user_defined_attribute_id=None,
    )


def _meas(name: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=f"m-{name}", name=name,
        default_agg="sum", is_additive=True,
        source_column_id=f"col-{name}", user_defined_attribute_id=None,
        measure_type="standard", expression=None, calc_agg_mode=None,
        semi_additive_behavior=None, variant_kind=None,
        variant_of_measure_id=None,
    )


async def test_unknown_filter_column_raises():
    filters = [LogicalFilter("nonexistent_col", "eq", "X")]
    with _patches([_dim("city_name")], [_meas("revenue")]):
        with pytest.raises(SemanticBindingError, match="Unknown filter column.*nonexistent_col"):
            await bind_query_to_model(
                _query(dims=["city_name"], measures=["revenue"], filters=filters),
                AsyncMock(),
            )


async def test_case_insensitive_filter_resolves():
    filters = [LogicalFilter("Account_Type", "eq", "Credit")]
    with _patches([_dim("city_name"), _dim("account_type")], [_meas("revenue")]):
        bound = await bind_query_to_model(
            _query(dims=["city_name"], measures=["revenue"], filters=filters),
            AsyncMock(),
        )
    assert len(bound.resolved_filters) == 1
    assert bound.resolved_filters[0].dimension_name == "account_type"


async def test_case_insensitive_measure_in_select_resolves():
    # F-003-06: SELECT-list measure binding must fold case like the filter
    # path. ``SUM(REVENUE)`` against a model measure ``revenue`` must bind to
    # the canonical measure, not raise Unknown column.
    with _patches([_dim("region")], [_meas("revenue")]):
        bound = await bind_query_to_model(
            _query(dims=[], measures=["REVENUE"]),
            AsyncMock(),
        )
    assert len(bound.resolved_measures) == 1
    assert bound.resolved_measures[0].name == "revenue"


async def test_case_insensitive_dimension_in_select_resolves():
    # F-003-06: SELECT-list dimension binding folds case too.
    with _patches([_dim("region")], [_meas("revenue")]):
        bound = await bind_query_to_model(
            _query(dims=["REGION"], measures=["revenue"]),
            AsyncMock(),
        )
    assert len(bound.resolved_dimensions) == 1
    assert bound.resolved_dimensions[0].name == "region"


async def test_exact_match_preferred():
    filters = [LogicalFilter("city_name", "eq", "Cairo")]
    with _patches([_dim("city_name")], [_meas("revenue")]):
        bound = await bind_query_to_model(
            _query(dims=["city_name"], measures=["revenue"], filters=filters),
            AsyncMock(),
        )
    assert bound.resolved_filters[0].dimension_name == "city_name"


async def test_measure_name_in_filter_accepted():
    filters = [LogicalFilter("revenue", "gt", 100)]
    with _patches([_dim("city_name")], [_meas("revenue")]):
        bound = await bind_query_to_model(
            _query(dims=["city_name"], measures=["revenue"], filters=filters),
            AsyncMock(),
        )
    assert len(bound.resolved_filters) == 1
    assert bound.resolved_filters[0].dimension_name == "revenue"


async def test_technical_complex_sql_allows_unknown_filter():
    filters = [LogicalFilter("unknown_col", "eq", "X")]
    with _patches([_dim("city_name")], []):
        bound = await bind_query_to_model(
            _query(dims=[], measures=[], filters=filters, has_complex_sql=True),
            AsyncMock(),
            include_hidden=True,
        )
    assert len(bound.resolved_filters) == 1
    assert bound.resolved_filters[0].dimension_name == "unknown_col"


async def test_business_complex_sql_accepted_as_passthrough():
    """Complex SQL on business view is allowed (passthrough with table-name
    substitution).  The binder skips dimension/measure resolution and sets
    has_passthrough_expressions=True so the rewriter preserves the raw SQL."""
    with _patches([_dim("city_name")], []):
        bound = await bind_query_to_model(
            _query(dims=[], measures=[], has_complex_sql=True),
            AsyncMock(),
        )
    assert bound.has_passthrough_expressions is True


async def test_unresolvable_where_allows_unknown_filter():
    filters = [LogicalFilter("unknown_col", "eq", "X")]
    with _patches([_dim("city_name")], [_meas("revenue")]):
        bound = await bind_query_to_model(
            _query(dims=["city_name"], measures=["revenue"],
                   filters=filters, has_unresolvable_where=True),
            AsyncMock(),
        )
    assert len(bound.resolved_filters) == 1


async def test_mixed_valid_invalid_filters_raises_on_invalid():
    filters = [
        LogicalFilter("city_name", "eq", "Cairo"),
        LogicalFilter("Channel_type", "eq", "CC"),
    ]
    with _patches([_dim("city_name"), _dim("account_type")], [_meas("revenue")]):
        with pytest.raises(SemanticBindingError, match="Channel_type"):
            await bind_query_to_model(
                _query(dims=["city_name"], measures=["revenue"], filters=filters),
                AsyncMock(),
            )


async def test_unknown_from_table_raises():
    """Shape #70: a FROM clause referencing a table that is not the model slug,
    display name, or a persona-suffixed name is rejected here rather than being
    forwarded to the source DB with silent table substitution."""
    q = _query(dims=["city_name"], measures=["revenue"])
    q.from_tables = ["does_not_exist"]
    with _patches([_dim("city_name")], [_meas("revenue")]):
        with pytest.raises(SemanticBindingError, match="Unknown table.*does_not_exist"):
            await bind_query_to_model(q, AsyncMock())


async def test_known_from_table_slug_accepted():
    """The model slug itself (and slug-prefixed names) are valid FROM tables."""
    q = _query(dims=["city_name"], measures=["revenue"])
    q.from_tables = ["testmodel"]
    with _patches([_dim("city_name")], [_meas("revenue")]):
        bound = await bind_query_to_model(q, AsyncMock())
    assert bound is not None
