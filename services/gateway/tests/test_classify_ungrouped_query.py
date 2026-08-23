"""Tests for _classify_ungrouped_query gateway function.

Validates that the classifier correctly distinguishes:
  * Ungrouped queries with non-aggregate columns -> "raw"
  * Ungrouped queries with ONLY aggregate columns -> "source"
  * Grouped queries -> None (normal flow)
  * SELECT * -> "raw"
  * Unparseable SQL -> None (safe fallback)
  * Known-measure columns treated as aggregates -> "source"
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_GATEWAY_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _GATEWAY_SRC not in sys.path:
    sys.path.insert(0, _GATEWAY_SRC)

from jdbc.server import _classify_ungrouped_query, _UNREACHABLE_MEASURE_RE


class TestClassifyUngroupedQuery:

    def test_ungrouped_with_dimension_returns_raw(self):
        sql = 'SELECT "region_code", "amount" FROM "project1"."modelx"'
        assert _classify_ungrouped_query(sql) == "raw"

    def test_grouped_returns_none(self):
        sql = (
            'SELECT "region_code", SUM("amount") '
            'FROM "project1"."modelx" GROUP BY "region_code"'
        )
        assert _classify_ungrouped_query(sql) is None

    def test_aggregate_only_returns_source(self):
        sql = 'SELECT COUNT(*) FROM "project1"."modelx"'
        assert _classify_ungrouped_query(sql) == "source"

    def test_aggregate_only_sum_returns_source(self):
        sql = 'SELECT SUM("amount"), AVG("price") FROM "project1"."modelx"'
        assert _classify_ungrouped_query(sql) == "source"

    def test_select_star_returns_raw(self):
        sql = 'SELECT * FROM "project1"."modelx"'
        assert _classify_ungrouped_query(sql) == "raw"

    def test_mixed_agg_and_dimension_returns_none(self):
        # Bug-5879: an explicit aggregate function mixed with a plain column
        # is an aggregation request — it needs GROUP BY injection (normal
        # flow), not the raw route, which strips aggregation wrappers and
        # would silently return per-row values for SUM("amount").
        sql = (
            'SELECT "region_code", SUM("amount") '
            'FROM "project1"."modelx"'
        )
        assert _classify_ungrouped_query(sql) is None

    def test_mixed_measure_column_and_dimension_returns_raw(self):
        # A flat measure COLUMN (no aggregate function) next to a dimension
        # is a detail-row browse — stays on the raw route.
        sql = (
            'SELECT "region_code", "amount" '
            'FROM "project1"."modelx"'
        )
        table_columns = {
            "modelx": [
                {"name": "region_code", "kind": "dimension"},
                {"name": "amount", "kind": "measure"},
            ]
        }
        assert _classify_ungrouped_query(sql, table_columns) == "raw"

    def test_window_function_returns_none(self):
        # Bug-5879: the raw builder cannot render window functions.
        sql = (
            'SELECT "region_code", ROW_NUMBER() OVER (ORDER BY "amount") '
            'FROM "project1"."modelx"'
        )
        assert _classify_ungrouped_query(sql) is None

    def test_scalar_subquery_returns_none(self):
        # Bug-5879: the raw builder cannot render subqueries.
        sql = (
            'SELECT "region_code", (SELECT MAX("amount") FROM "project1"."modelx") '
            'FROM "project1"."modelx"'
        )
        assert _classify_ungrouped_query(sql) is None

    def test_exists_subquery_returns_none(self):
        # Bug-5879: correlated EXISTS predicates cannot be represented on
        # the raw route even when the SELECT list is plain columns.
        sql = (
            'SELECT "region_code" FROM "project1"."modelx" m1 '
            'WHERE EXISTS (SELECT 1 FROM "project1"."modelx" m2 '
            'WHERE m2."region_code" = m1."region_code")'
        )
        assert _classify_ungrouped_query(sql) is None

    def test_distinct_returns_none(self):
        # Bug-5879: the raw builder does not emit DISTINCT.
        sql = 'SELECT DISTINCT "region_code" FROM "project1"."modelx"'
        assert _classify_ungrouped_query(sql) is None

    def test_expression_over_aggregates_returns_none(self):
        # Bug-5879: COALESCE/arithmetic over aggregates is aggregate-shaped;
        # the raw route cannot render it.
        sql = (
            'SELECT COALESCE(SUM("amount"), 0) '
            'FROM "project1"."modelx"'
        )
        assert _classify_ungrouped_query(sql) is None

    def test_empty_sql_returns_none(self):
        assert _classify_ungrouped_query("") is None

    def test_non_select_returns_none(self):
        sql = 'SET application_name = "power_bi"'
        assert _classify_ungrouped_query(sql) is None

    def test_no_table_returns_none(self):
        sql = "SELECT 1"
        assert _classify_ungrouped_query(sql) is None

    def test_known_measure_column_treated_as_aggregate(self):
        table_columns = {
            "modelx": [
                {"name": "amount", "kind": "measure"},
                {"name": "region_code", "kind": "dimension"},
            ]
        }
        sql = 'SELECT "amount" FROM "modelx"'
        assert _classify_ungrouped_query(sql, table_columns) == "source"

    def test_known_measure_with_dimension_returns_raw(self):
        table_columns = {
            "modelx": [
                {"name": "amount", "kind": "measure"},
                {"name": "region_code", "kind": "dimension"},
            ]
        }
        sql = 'SELECT "region_code", "amount" FROM "modelx"'
        assert _classify_ungrouped_query(sql, table_columns) == "raw"

    def test_min_max_aggregate_returns_source(self):
        sql = 'SELECT MIN("price"), MAX("price") FROM "project1"."modelx"'
        assert _classify_ungrouped_query(sql) == "source"

    def test_top_n_without_group_by_returns_raw(self):
        sql = (
            'SELECT "region_code", "amount" '
            'FROM "project1"."modelx" LIMIT 100'
        )
        assert _classify_ungrouped_query(sql) == "raw"

    def test_where_clause_no_group_by_returns_raw(self):
        sql = (
            'SELECT "region_code", "amount" '
            'FROM "project1"."modelx" '
            'WHERE "region_code" = \'NORTH\''
        )
        assert _classify_ungrouped_query(sql) == "raw"


class TestUnreachableMeasurePattern:

    def test_depends_on_unreachable_fields(self):
        msg = "revenue depends on fields that are not reachable"
        assert _UNREACHABLE_MEASURE_RE.search(msg) is not None

    def test_no_compatible_dimensions(self):
        msg = "cost has no compatible dimensions"
        assert _UNREACHABLE_MEASURE_RE.search(msg) is not None

    def test_no_aggregation_path_between(self):
        msg = "There is no aggregation path between revenue and region"
        assert _UNREACHABLE_MEASURE_RE.search(msg) is not None

    def test_hierarchy_path_resolution_failure(self):
        msg = "Cannot resolve hierarchy path. Missing join between 'fact_sales' and 'dim_product'."
        assert _UNREACHABLE_MEASURE_RE.search(msg) is not None

    def test_hierarchy_path_generic(self):
        msg = "Cannot resolve hierarchy path. Missing join between required hierarchy tables."
        assert _UNREACHABLE_MEASURE_RE.search(msg) is not None
