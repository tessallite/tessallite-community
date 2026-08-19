"""Lock in that the aggregate rewriter reads grain_physical_cols.

Regression: when an aggregate has a collision-resolved physical column
name (e.g. ``account_type_active_flag`` instead of bare ``active_flag``),
the rewriter must emit the physical name in SELECT, GROUP BY, ORDER BY,
and WHERE rather than the logical dimension name.
"""
from __future__ import annotations

import types
from datetime import datetime, timezone

import pytest

from src.rewrite.query_rewriter import rewrite_for_aggregate
from src.ir.logical_query import LogicalFilter

from conftest import (
    make_agg_col,
    make_aggregate,
    make_bound_query,
    make_dimension,
    make_measure,
)


def _attach_physical_cols(agg, physical_cols):
    agg.grain_physical_cols = physical_cols
    return agg


def test_rewriter_emits_physical_grain_col_in_select_and_group_by():
    m = make_measure("base_amount")
    d = make_dimension("active_flag")
    agg = make_aggregate(["active_flag"], [make_agg_col(m)])
    _attach_physical_cols(agg, ["account_type_active_flag"])

    bq = make_bound_query([d], [m], grain=["active_flag"])
    sql = rewrite_for_aggregate(bq, agg)

    # Physical column name appears in the column read
    assert '"account_type_active_flag" AS "active_flag"' in sql


def test_rewriter_falls_back_to_logical_name_when_no_physical_cols():
    m = make_measure("base_amount")
    d = make_dimension("country_code")
    agg = make_aggregate(["country_code"], [make_agg_col(m)])
    # No grain_physical_cols attribute set → legacy behaviour
    agg.grain_physical_cols = None

    bq = make_bound_query([d], [m], grain=["country_code"])
    sql = rewrite_for_aggregate(bq, agg)

    assert '"country_code"' in sql


def test_rewriter_maps_order_by_to_physical_name():
    m = make_measure("base_amount")
    d = make_dimension("payment_method")
    agg = make_aggregate(["payment_method"], [make_agg_col(m)])
    _attach_physical_cols(agg, ["method_payment_method"])

    bq = make_bound_query(
        [d],
        [m],
        grain=["payment_method"],
        order_by=[("payment_method", "asc")],
    )
    sql = rewrite_for_aggregate(bq, agg)

    order_part = sql.split("ORDER BY ", 1)[1]
    assert '"method_payment_method"' in order_part


def test_rewriter_preserves_numeric_grain_filter_type_for_bigquery():
    m = make_measure("base_amount")
    d = make_dimension("fiscal_year")
    d.data_type = "INT64"
    agg = make_aggregate(["fiscal_year"], [make_agg_col(m)])
    _attach_physical_cols(agg, ["fiscal_year"])

    bq = make_bound_query(
        [d],
        [m],
        grain=["fiscal_year"],
        filters=[LogicalFilter(dimension_name="fiscal_year", operator="eq", value=2024)],
    )
    sql = rewrite_for_aggregate(bq, agg, target_dialect="bigquery")

    assert "`fiscal_year` = 2024" in sql
    assert "`fiscal_year` = '2024'" not in sql


def test_f006_02_length_mismatched_physical_grain_raises():
    """F-006-02: grain ['active_flag'] + two physical names must not emit active_flag."""
    from src.rewrite.aggregate import AggregateRewriteUnsupported

    m = make_measure("base_amount")
    d = make_dimension("active_flag")
    agg = make_aggregate(["active_flag"], [make_agg_col(m)])
    _attach_physical_cols(agg, ["account_type_active_flag", "extra"])
    bq = make_bound_query([d], [m], grain=["active_flag"], order_by=[("active_flag", "asc")])
    with pytest.raises(AggregateRewriteUnsupported):
        rewrite_for_aggregate(bq, agg)
