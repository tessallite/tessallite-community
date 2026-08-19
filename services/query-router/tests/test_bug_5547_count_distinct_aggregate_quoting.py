"""Bug-5547 regression: count_distinct served from an aggregate must emit a
connector-correct quoted IDENTIFIER, not a string literal.

Root cause: ``_phys_expr_for_node`` hardcoded ANSI double quotes (``"col"``)
for the non-additive direct-emit columns (count_distinct / quantile / stat).
Those expressions are concatenated into the final statement WITHOUT a
sqlglot transpile step, so on BigQuery ``"order_count__count_distinct"`` is a
STRING LITERAL and every row returned the column name instead of the value.

The fix threads the target ``connector`` into ``_phys_expr_for_node`` for the
direct-emit columns only (``quote_identifier`` -> backticks on BigQuery,
double quotes on PostgreSQL), while the additive SUM/MIN/MAX path keeps
PostgreSQL-canonical quoting because it is re-parsed with ``read="postgres"``
and transpiled downstream (Bug-921 contract).
"""
from __future__ import annotations

import types

import pytest
import sqlglot
from sqlglot import exp

from src.ir.logical_query import SelectExpression
from src.rewrite.aggregate import _phys_expr_for_node
from src.rewrite.query_rewriter import rewrite_for_aggregate

from conftest import (
    make_agg_col,
    make_aggregate,
    make_bound_query,
    make_dimension,
    make_measure,
)


def _col(name):
    return types.SimpleNamespace(physical_col_name=name)


# --- Unit: _phys_expr_for_node connector-aware quoting --------------------

@pytest.mark.parametrize(
    "connector,expected",
    [
        ("postgresql", '"cust__count_distinct"'),
        ("bigquery", "`cust__count_distinct`"),
        ("redshift", '"cust__count_distinct"'),
        ("hadoop_spark", "`cust__count_distinct`"),
    ],
)
def test_count_distinct_phys_expr_uses_connector_quoting(connector, expected):
    """At exact grain the stored count_distinct column must be quoted for the
    target connector (the value is emitted directly, no transpile follows)."""
    lookup = {("cust", "count_distinct"): _col("cust__count_distinct")}
    assert (
        _phys_expr_for_node("cust", "count_distinct", lookup, True, connector)
        == expected
    )


@pytest.mark.parametrize("connector", ["postgresql", "bigquery", "hadoop_spark"])
def test_count_distinct_phys_expr_null_at_coarser_grain(connector):
    """Coarser grain stays fail-obvious NULL regardless of connector."""
    lookup = {("cust", "count_distinct"): _col("cust__count_distinct")}
    assert _phys_expr_for_node("cust", "count_distinct", lookup, False, connector) == "NULL"


@pytest.mark.parametrize(
    "connector,expected",
    [
        ("postgresql", '"rev__p50"'),
        ("bigquery", "`rev__p50`"),
    ],
)
def test_quantile_phys_expr_uses_connector_quoting(connector, expected):
    """Quantile columns travel the same direct-emit path as count_distinct."""
    lookup = {("rev", "p50"): _col("rev__p50")}
    assert _phys_expr_for_node("rev", "p50", lookup, True, connector) == expected


def test_additive_path_stays_postgres_canonical_for_reparse():
    """The additive SUM path is re-parsed with read='postgres' then transpiled,
    so it MUST stay PostgreSQL double-quoted even when a target connector is
    requested — feeding backticks to the postgres parser corrupts it (Bug-921).
    The default connector is therefore postgresql for the additive callers."""
    lookup = {("rev", "sum"): _col("rev__sum")}
    # Default (postgresql) — what the additive caller passes.
    assert _phys_expr_for_node("rev", "sum", lookup, False) == 'SUM("rev__sum")'
    assert _phys_expr_for_node("rev", "sum", lookup, True) == '"rev__sum"'
    # A postgres-canonical SUM(...) re-parses cleanly and transpiles to backticks.
    reparsed = sqlglot.parse_one('SUM("rev__sum")', read="postgres").sql(dialect="bigquery")
    assert reparsed == "SUM(`rev__sum`)"


# --- End-to-end: rewrite_for_aggregate value correctness ------------------

def _count_distinct_bound_query():
    m = make_measure("order_count", default_agg="count_distinct", is_additive=False)
    d = make_dimension("region")
    agg = make_aggregate(["region"], [make_agg_col(m, "count_distinct")])
    agg.grain_physical_cols = ["region"]

    sel = SelectExpression(
        raw_text="COUNT(DISTINCT customer_id)",
        alias="order_count",
        classification="analytical",
        agg_function="count_distinct",
        inner_column="order_count",
        inner_literal=None,
    )
    bq = make_bound_query([d], [m], grain=["region"])
    bq.logical_query.select_expressions = [sel]
    return bq, agg


def test_count_distinct_emits_backtick_identifier_on_bigquery():
    """The reported bug: BigQuery rendered the column as a string literal,
    returning the column name for every row instead of the numeric value."""
    bq, agg = _count_distinct_bound_query()
    sql = rewrite_for_aggregate(bq, agg, target_dialect="bigquery")

    assert "`order_count__count_distinct`" in sql
    # Must NOT be a double-quoted (string-literal) read on BigQuery.
    assert '"order_count__count_distinct"' not in sql

    # Strongest check: the stored column is parsed as a COLUMN identifier,
    # never as a STRING LITERAL (which is what produced the garbage value).
    tree = sqlglot.parse_one(sql, read="bigquery")
    cols = {c.name for c in tree.find_all(exp.Column)}
    assert "order_count__count_distinct" in cols
    string_lits = {l.this for l in tree.find_all(exp.Literal) if l.is_string}
    assert "order_count__count_distinct" not in string_lits


def test_count_distinct_emits_double_quote_identifier_on_postgres():
    bq, agg = _count_distinct_bound_query()
    sql = rewrite_for_aggregate(bq, agg, target_dialect="postgres")

    assert '"order_count__count_distinct"' in sql
    tree = sqlglot.parse_one(sql, read="postgres")
    cols = {c.name for c in tree.find_all(exp.Column)}
    assert "order_count__count_distinct" in cols
    string_lits = {l.this for l in tree.find_all(exp.Literal) if l.is_string}
    assert "order_count__count_distinct" not in string_lits


def test_additive_sum_still_correct_on_bigquery():
    """Guard: the fix must not break the additive path that already worked."""
    m = make_measure("revenue", default_agg="sum")
    d = make_dimension("region")
    agg = make_aggregate(["region"], [make_agg_col(m, "sum")])
    agg.grain_physical_cols = ["region"]

    sel = SelectExpression(
        raw_text="SUM(revenue)",
        alias="revenue",
        classification="analytical",
        agg_function="sum",
        inner_column="revenue",
        inner_literal=None,
    )
    bq = make_bound_query([d], [m], grain=["region"])
    bq.logical_query.select_expressions = [sel]

    sql = rewrite_for_aggregate(bq, agg, target_dialect="bigquery")
    assert "`revenue__sum`" in sql
    assert '"revenue__sum"' not in sql
