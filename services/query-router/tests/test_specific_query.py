"""
Regression test: COUNT(100) inside a query routes to aggregate correctly.

Covers the specific scenario:
  SELECT account_type_code, sum(base_amount), count(100) FROM modelx
  GROUP BY account_type_code

This must parse COUNT(100) as __row_count, match an aggregate that has
__row_count__count, and produce a rewritten query referencing the physical
column.

Run from tessallite/services/query-router/:
    pytest tests/test_specific_query.py
"""
from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, patch

import pytest

from src.parsing.sql_parser import parse_sql_to_ir
from src.rewrite.query_rewriter import rewrite_for_aggregate
from src.ir.logical_query import BoundQuery, LogicalQuery

from conftest import make_measure, make_dimension, make_agg_col, make_aggregate

_PATCH_LOAD = "src.routing.aggregate_matcher.load_active_aggregates"


def _make_row_count_col():
    return types.SimpleNamespace(
        physical_col_name="__row_count__count",
        stat_type="count",
        measure=None,
    )


def test_count_100_parse():
    """COUNT(100) should produce __row_count measure and literal classification."""
    ir = parse_sql_to_ir(
        "SELECT account_type_code, sum(base_amount), count(100) FROM modelx GROUP BY account_type_code",
        "model-1",
    )
    assert "base_amount" in ir.requested_measures
    assert "__row_count" in ir.requested_measures
    assert "account_type_code" in ir.grain

    literals = [e for e in ir.select_expressions if e.classification == "literal"]
    assert len(literals) == 1
    assert literals[0].agg_function == "count"
    assert literals[0].inner_literal == "100"


async def test_count_100_aggregate_matching():
    """Aggregate with __row_count__count column matches COUNT(100) query."""
    from src.routing.aggregate_matcher import find_best_aggregate

    m_base = make_measure("base_amount", "sum")
    m_rc = make_measure("__row_count", "count")
    d = make_dimension("account_type_code")
    agg = make_aggregate(
        ["account_type_code"],
        [make_agg_col(m_base), _make_row_count_col()],
    )

    model = types.SimpleNamespace(id="model-1", slug="modelx")
    lq = parse_sql_to_ir(
        "SELECT account_type_code, sum(base_amount), count(100) FROM modelx GROUP BY account_type_code",
        "model-1",
    )
    bq = BoundQuery(
        logical_query=lq,
        model=model,
        resolved_measures=[m_base, m_rc],
        resolved_dimensions=[d],
        resolved_filters=[],
    )

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as load:
        load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is agg


def test_count_100_rewrite():
    """Rewritten SQL for COUNT(100) references __row_count__count column."""
    m_base = make_measure("base_amount", "sum")
    m_rc = make_measure("__row_count", "count")
    d = make_dimension("account_type_code")
    agg = make_aggregate(
        ["account_type_code"],
        [make_agg_col(m_base), _make_row_count_col()],
    )

    model = types.SimpleNamespace(id="model-1", slug="modelx")
    lq = parse_sql_to_ir(
        "SELECT account_type_code, sum(base_amount), count(100) FROM modelx GROUP BY account_type_code",
        "model-1",
    )
    bq = BoundQuery(
        logical_query=lq,
        model=model,
        resolved_measures=[m_base, m_rc],
        resolved_dimensions=[d],
        resolved_filters=[],
    )

    sql = rewrite_for_aggregate(bq, agg)
    assert '"base_amount__sum"' in sql
    assert '"__row_count__count"' in sql
    assert "SUM(" not in sql  # exact grain, no re-aggregation
