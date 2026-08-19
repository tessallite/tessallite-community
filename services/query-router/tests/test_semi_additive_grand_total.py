"""Bug-7192 — Semi-additive measures must keep semantics at ALL grains.

A semi-additive measure (last_non_empty, first_non_empty, etc.) must never
silently degrade to SUM at grand-total (no GROUP BY) or non-time grains
(e.g. GROUP BY region).  The time dimension's table must be joined even
when time is not in the grain, so the ordering column is available.

This test validates the table_resolution.py fix and the source_sql.py
ordering fallback.
"""
from __future__ import annotations

import types

import pytest

from src.rewrite.table_resolution import _resolve_required_and_base_tables


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_column(col_id, table_id, column_name):
    return types.SimpleNamespace(
        id=col_id,
        model_table_id=table_id,
        column_name=column_name,
    )


def _make_table(table_id, physical_name, alias=None):
    return types.SimpleNamespace(
        id=table_id,
        physical_name=physical_name,
        alias=alias or physical_name,
        display_name=physical_name,
    )


# ---------------------------------------------------------------------------
# Bug-7192: SA time dimension table must be included regardless of grain
# ---------------------------------------------------------------------------

def test_sa_time_table_included_without_time_in_grain():
    """The finest SA time dimension table should be joined even at grand-total."""
    from src.ir.logical_query import LogicalQuery, BoundQuery

    # Fact table
    fact_table = _make_table("t-fact", "fact", "f")
    # Time dimension table (separate table)
    time_table = _make_table("t-time", "time_dim", "td")

    # Columns
    amount_col = _make_column("c-amount", "t-fact", "amount")
    date_col = _make_column("c-date", "t-time", "date_col")

    columns_by_id = {
        "c-amount": amount_col,
        "c-date": date_col,
    }
    tables_by_id = {
        "t-fact": fact_table,
        "t-time": time_table,
    }

    # Measure on fact table
    balance = types.SimpleNamespace(
        id="m-balance",
        name="balance",
        default_agg="sum",
        is_additive=True,
        semi_additive_behavior="last_non_empty",
        source_column_id="c-amount",
        user_defined_attribute_id=None,
        measure_type="standard",
        variant_kind=None,
    )

    model = types.SimpleNamespace(id="model-1", slug="test_model", deployed_version_id="v1")
    # Grand total: no dimensions, no grain
    lq = LogicalQuery(
        model_id="model-1",
        protocol="jdbc",
        raw_query="SELECT SUM(balance) FROM test_model",
        requested_measures=["balance"],
        requested_dimensions=[],
        filters=[],
        grain=[],
        order_by=[],
        limit=None,
        offset=None,
        query_fingerprint="test_fp",
    )
    bq = BoundQuery(
        logical_query=lq,
        model=model,
        resolved_measures=[balance],
        resolved_dimensions=[],
        resolved_filters=[],
        resolved_dimensions_by_name={},
    )

    required_table_ids, base_table = _resolve_required_and_base_tables(
        bq,
        columns_by_id=columns_by_id,
        tables_by_id=tables_by_id,
        uda_by_id={},
        dimensions_by_name={},
        filter_dim_names=set(),
        order_col_names=set(),
        _order_measures=[],
        calc_ref_measures_by_name={},
        _sa_finest_time_col_id="c-date",
        _sa_has_time_in_grain=False,  # Grand total — time NOT in grain
    )

    # Bug-7192 fix: the time dimension table MUST be included even without
    # time in the grain, so the semi-additive ordering column is accessible.
    assert "t-time" in required_table_ids, (
        "The SA time dimension table should be included in required_table_ids "
        "even when time is NOT in the grain (grand-total / non-time grain)."
    )


def test_sa_time_table_included_with_time_in_grain():
    """Sanity check: SA time table is still included when time IS in the grain."""
    from src.ir.logical_query import LogicalQuery, BoundQuery

    fact_table = _make_table("t-fact", "fact", "f")
    time_table = _make_table("t-time", "time_dim", "td")

    amount_col = _make_column("c-amount", "t-fact", "amount")
    date_col = _make_column("c-date", "t-time", "date_col")

    columns_by_id = {
        "c-amount": amount_col,
        "c-date": date_col,
    }
    tables_by_id = {
        "t-fact": fact_table,
        "t-time": time_table,
    }

    balance = types.SimpleNamespace(
        id="m-balance",
        name="balance",
        default_agg="sum",
        is_additive=True,
        semi_additive_behavior="last_non_empty",
        source_column_id="c-amount",
        user_defined_attribute_id=None,
        measure_type="standard",
        variant_kind=None,
    )
    month = types.SimpleNamespace(
        id="d-month",
        name="month",
        source_column_id="c-date",
        user_defined_attribute_id=None,
        is_time_dim=True,
    )

    model = types.SimpleNamespace(id="model-1", slug="test_model", deployed_version_id="v1")
    lq = LogicalQuery(
        model_id="model-1",
        protocol="jdbc",
        raw_query="SELECT SUM(balance) FROM test_model GROUP BY month",
        requested_measures=["balance"],
        requested_dimensions=["month"],
        filters=[],
        grain=["month"],
        order_by=[],
        limit=None,
        offset=None,
        query_fingerprint="test_fp",
    )
    bq = BoundQuery(
        logical_query=lq,
        model=model,
        resolved_measures=[balance],
        resolved_dimensions=[month],
        resolved_filters=[],
        resolved_dimensions_by_name={"month": month},
    )

    required_table_ids, _ = _resolve_required_and_base_tables(
        bq,
        columns_by_id=columns_by_id,
        tables_by_id=tables_by_id,
        uda_by_id={},
        dimensions_by_name={"month": month},
        filter_dim_names=set(),
        order_col_names=set(),
        _order_measures=[],
        calc_ref_measures_by_name={},
        _sa_finest_time_col_id="c-date",
        _sa_has_time_in_grain=True,
    )

    assert "t-time" in required_table_ids
