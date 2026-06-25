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


# ---------------------------------------------------------------------------
# Bug-5488: dimensions referenced ONLY inside a function-wrapped / OR-compound
# WHERE predicate must be collected so the source rewriter loads their physical
# columns and joins their tables (otherwise the bare semantic name leaks to the
# source DB and it raises "column does not exist").
# ---------------------------------------------------------------------------

def _where_query(raw_sql: str) -> LogicalQuery:
    """A LogicalQuery carrying a real raw WHERE clause and the unresolvable
    flag, but NO extracted LogicalFilters (mirrors what the parser produces for
    a function-wrapped or OR-compound predicate)."""
    lq = LogicalQuery(
        model_id="model-1",
        protocol="jdbc",
        raw_query=raw_sql,
        requested_measures=[],
        requested_dimensions=["payment_reference"],
        filters=[],
        grain=["payment_reference"],
        order_by=[],
        limit=None,
        offset=None,
        query_fingerprint="fp",
    )
    lq.has_unresolvable_where = True
    lq.from_tables = ["testmodel"]
    return lq


async def test_function_wrapped_where_dim_collected():
    """A dimension referenced only inside ``UPPER(TRIM(col))`` in an unresolvable
    WHERE is collected into ``where_referenced_dimensions`` even though it
    produces no LogicalFilter and is absent from SELECT/ORDER BY."""
    raw = (
        "SELECT payment_reference FROM modely "
        "WHERE UPPER(TRIM(payment_method)) <> UPPER(TRIM(payment_method_code)) "
        "LIMIT 1"
    )
    dims = [_dim("payment_reference"), _dim("payment_method"), _dim("payment_method_code")]
    with _patches(dims, [_meas("revenue")]):
        bound = await bind_query_to_model(_where_query(raw), AsyncMock())
    # Both function-wrapped columns are collected; the SELECT column is not the
    # concern of this set but is harmless if present.
    assert "payment_method" in bound.where_referenced_dimensions
    assert "payment_method_code" in bound.where_referenced_dimensions


async def test_or_compound_where_dim_collected():
    """Both branches of an OR-compound predicate are walked, so a dimension that
    appears only inside the function-wrapped branch is still collected."""
    raw = (
        "SELECT payment_reference FROM modely "
        "WHERE payment_method = 'CARD' "
        "OR UPPER(TRIM(payment_method)) <> UPPER(TRIM(payment_method_code)) "
        "LIMIT 1"
    )
    dims = [_dim("payment_reference"), _dim("payment_method"), _dim("payment_method_code")]
    with _patches(dims, [_meas("revenue")]):
        bound = await bind_query_to_model(_where_query(raw), AsyncMock())
    assert "payment_method_code" in bound.where_referenced_dimensions


async def test_where_collection_canonicalises_case():
    """A WHERE column typed in a different case resolves to the canonical model
    dimension name (case-insensitive), not the raw typed token."""
    raw = (
        "SELECT payment_reference FROM modely "
        "WHERE UPPER(PAYMENT_METHOD_CODE) = 'CARD' LIMIT 1"
    )
    dims = [_dim("payment_reference"), _dim("payment_method_code")]
    with _patches(dims, [_meas("revenue")]):
        bound = await bind_query_to_model(_where_query(raw), AsyncMock())
    assert "payment_method_code" in bound.where_referenced_dimensions
    assert "PAYMENT_METHOD_CODE" not in bound.where_referenced_dimensions


async def test_where_collection_ignores_non_model_tokens():
    """Literals, aliases, and unknown identifiers inside the WHERE are NOT
    captured — only names present in the model maps are collected."""
    raw = (
        "SELECT payment_reference FROM modely "
        "WHERE UPPER(payment_method_code) = 'CARD' "
        "AND LENGTH(not_a_model_column) > 0 LIMIT 1"
    )
    dims = [_dim("payment_reference"), _dim("payment_method_code")]
    with _patches(dims, [_meas("revenue")]):
        bound = await bind_query_to_model(_where_query(raw), AsyncMock())
    assert bound.where_referenced_dimensions == {"payment_method_code"}


async def test_where_collection_empty_for_resolvable_where():
    """When the WHERE is fully representable (no unresolvable flag), the set
    stays empty — the resolvable path already covers it via resolved_filters,
    so this fix is a strict no-op there."""
    filters = [LogicalFilter("payment_method_code", "eq", "CARD")]
    q = _query(
        dims=["payment_reference"], measures=["revenue"],
        filters=filters, has_unresolvable_where=False,
    )
    dims = [_dim("payment_reference"), _dim("payment_method_code")]
    with _patches(dims, [_meas("revenue")]):
        bound = await bind_query_to_model(q, AsyncMock())
    assert bound.where_referenced_dimensions == set()


async def test_subquery_wrapper_where_dim_collected():
    """Bug-457 / Codex-finding shape: when the unresolvable WHERE lives inside a
    subquery wrapper (``SELECT col FROM (SELECT * FROM model WHERE ...) q``), the
    collector must unwrap the wrapper using sqlglot's ``from_`` key (NOT
    ``from``) and walk the inner SELECT's WHERE, mirroring the rewriter's own
    subquery-WHERE extraction in source_sql. Otherwise the fallback is dead and
    a WHERE-only dimension still leaks."""
    raw = (
        "SELECT payment_reference FROM "
        "(SELECT * FROM modely "
        " WHERE UPPER(TRIM(payment_method)) <> UPPER(TRIM(payment_method_code))) q "
        "LIMIT 1"
    )
    dims = [_dim("payment_reference"), _dim("payment_method"), _dim("payment_method_code")]
    with _patches(dims, [_meas("revenue")]):
        bound = await bind_query_to_model(_where_query(raw), AsyncMock())
    assert "payment_method" in bound.where_referenced_dimensions
    assert "payment_method_code" in bound.where_referenced_dimensions


async def test_where_collection_excludes_measure_only_in_where():
    """Deep-review scope guard (Bug-5488): a MEASURE referenced only inside the
    WHERE is deliberately NOT collected — the source rewriter's filter backfill
    loads dimensions only and ``_get_phys_expr`` resolves a measure only when it
    is in resolved/order measures, so adding a filter-only measure name to
    ``filter_dim_names`` could not be satisfied. Only dimensions are collected."""
    raw = (
        "SELECT payment_reference FROM modely "
        "WHERE UPPER(payment_method_code) = 'CARD' AND revenue > 0 LIMIT 1"
    )
    dims = [_dim("payment_reference"), _dim("payment_method_code")]
    with _patches(dims, [_meas("revenue")]):
        bound = await bind_query_to_model(_where_query(raw), AsyncMock())
    assert bound.where_referenced_dimensions == {"payment_method_code"}
    assert "revenue" not in bound.where_referenced_dimensions


async def test_where_collection_empty_for_complex_passthrough():
    """Complex passthrough queries skip semantic resolution and go to source
    raw, so no WHERE column collection happens for them."""
    raw = "SELECT payment_reference FROM modely WHERE UPPER(payment_method_code) = 'CARD'"
    q = _where_query(raw)
    q.has_complex_sql = True
    dims = [_dim("payment_reference"), _dim("payment_method_code")]
    with _patches(dims, [_meas("revenue")]):
        bound = await bind_query_to_model(q, AsyncMock())
    assert bound.where_referenced_dimensions == set()
