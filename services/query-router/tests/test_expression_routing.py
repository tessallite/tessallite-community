"""
Tests for expression-aware query routing.

Covers: SQL parser classification, aggregate rewrite (exact/coarser grain),
scalar wrapper preservation, GROUP BY emission, passthrough detection,
and fallback path.
"""
from __future__ import annotations

import types
from unittest.mock import MagicMock

import pytest

from src.parsing.sql_parser import parse_sql_to_ir
from src.rewrite.query_rewriter import rewrite_for_aggregate
from src.ir.logical_query import BoundQuery, LogicalFilter, LogicalQuery, SelectExpression


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agg(grain, columns, *, target_schema="public", physical_table_name="agg_table_123"):
    """Build a lightweight aggregate definition namespace."""
    return types.SimpleNamespace(
        id="agg_1",
        physical_table_name=physical_table_name,
        target_schema=target_schema,
        grain=grain,
        columns=columns,
    )


def _make_col(measure_name, stat_type, *, physical_col_name=None):
    """Build a lightweight aggregate column namespace."""
    return types.SimpleNamespace(
        physical_col_name=physical_col_name or f"{measure_name}__{stat_type}",
        stat_type=stat_type,
        measure=types.SimpleNamespace(name=measure_name) if measure_name else None,
    )


def _make_row_count_col():
    """Build the __row_count__count synthetic column."""
    return types.SimpleNamespace(
        physical_col_name="__row_count__count",
        stat_type="count",
        measure=None,
    )


def _bind(ir, measures, dimensions, *, filters=None):
    """Build a BoundQuery from an IR + list of (name, default_agg) tuples."""
    return BoundQuery(
        logical_query=ir,
        model=MagicMock(),
        resolved_measures=[
            types.SimpleNamespace(name=n, default_agg=a, is_additive=True)
            for n, a in measures
        ],
        resolved_dimensions=[types.SimpleNamespace(name=n) for n in dimensions],
        resolved_filters=filters or [],
    )


# ===========================================================================
# 1. Parser — SELECT expression classification
# ===========================================================================

class TestParserClassification:

    def test_count_1_classified_as_literal(self):
        ir = parse_sql_to_ir("SELECT dim1, COUNT(1) FROM t GROUP BY dim1", "m1")
        assert "__row_count" in ir.requested_measures
        assert "dim1" in ir.requested_dimensions
        literals = [e for e in ir.select_expressions if e.classification == "literal"]
        assert len(literals) == 1
        assert literals[0].agg_function == "count"
        assert literals[0].inner_literal == "1"

    def test_count_star_classified_as_literal(self):
        ir = parse_sql_to_ir("SELECT dim1, COUNT(*) FROM t GROUP BY dim1", "m1")
        assert "__row_count" in ir.requested_measures
        literals = [e for e in ir.select_expressions if e.classification == "literal"]
        assert len(literals) == 1
        assert literals[0].agg_function == "count"
        assert literals[0].inner_literal == "*"

    def test_count_numeric_literal_classified_as_literal(self):
        """COUNT(100), COUNT(42), etc. are semantically COUNT(*)."""
        for lit, expected_val in [("100", "100"), ("42", "42"), ("0", "0"), ("3.14", "3.14")]:
            ir = parse_sql_to_ir(f"SELECT dim1, COUNT({lit}) FROM t GROUP BY dim1", "m1")
            assert "__row_count" in ir.requested_measures, f"COUNT({lit}) should add __row_count"
            literals = [e for e in ir.select_expressions if e.classification == "literal"]
            assert len(literals) == 1, f"COUNT({lit}) should classify as literal"
            assert literals[0].agg_function == "count"
            assert literals[0].inner_literal == expected_val

    def test_count_string_literal_classified_as_literal(self):
        """COUNT('x'), COUNT('hello') are semantically COUNT(*)."""
        ir = parse_sql_to_ir("SELECT dim1, COUNT('x') FROM t GROUP BY dim1", "m1")
        assert "__row_count" in ir.requested_measures
        literals = [e for e in ir.select_expressions if e.classification == "literal"]
        assert len(literals) == 1
        assert literals[0].agg_function == "count"
        assert literals[0].inner_literal == "x"

    def test_count_boolean_literal_classified_as_literal(self):
        """COUNT(TRUE) is semantically COUNT(*)."""
        ir = parse_sql_to_ir("SELECT dim1, COUNT(TRUE) FROM t GROUP BY dim1", "m1")
        assert "__row_count" in ir.requested_measures
        literals = [e for e in ir.select_expressions if e.classification == "literal"]
        assert len(literals) == 1
        assert literals[0].agg_function == "count"

    def test_count_negative_literal_classified_as_literal(self):
        """COUNT(-1) is semantically COUNT(*)."""
        ir = parse_sql_to_ir("SELECT dim1, COUNT(-1) FROM t GROUP BY dim1", "m1")
        assert "__row_count" in ir.requested_measures
        literals = [e for e in ir.select_expressions if e.classification == "literal"]
        assert len(literals) == 1
        assert literals[0].agg_function == "count"

    def test_sum_column_classified_as_analytical(self):
        ir = parse_sql_to_ir("SELECT SUM(revenue) FROM t", "m1")
        assert "revenue" in ir.requested_measures
        analytics = [e for e in ir.select_expressions if e.classification == "analytical"]
        assert len(analytics) == 1
        assert analytics[0].agg_function == "sum"
        assert analytics[0].inner_column == "revenue"

    def test_avg_column_classified_as_analytical(self):
        ir = parse_sql_to_ir("SELECT AVG(price) FROM t", "m1")
        assert "price" in ir.requested_measures
        analytics = [e for e in ir.select_expressions if e.classification == "analytical"]
        assert len(analytics) == 1
        assert analytics[0].agg_function == "avg"
        assert analytics[0].inner_column == "price"

    def test_min_max_classified_as_analytical(self):
        ir = parse_sql_to_ir("SELECT MIN(price), MAX(price) FROM t", "m1")
        assert "price" in ir.requested_measures
        analytics = [e for e in ir.select_expressions if e.classification == "analytical"]
        assert len(analytics) == 2
        funcs = {e.agg_function for e in analytics}
        assert funcs == {"min", "max"}

    def test_count_distinct_classified_as_analytical(self):
        ir = parse_sql_to_ir("SELECT COUNT(DISTINCT customer_id) FROM t", "m1")
        assert "customer_id" in ir.requested_measures
        analytics = [e for e in ir.select_expressions if e.classification == "analytical"]
        assert len(analytics) == 1
        assert analytics[0].agg_function == "count_distinct"
        assert analytics[0].inner_column == "customer_id"

    def test_bare_column_classified_as_passthrough_with_inner_column(self):
        ir = parse_sql_to_ir("SELECT dim1 FROM t", "m1")
        assert "dim1" in ir.requested_dimensions
        pts = [e for e in ir.select_expressions if e.classification == "passthrough"]
        assert len(pts) == 1
        assert pts[0].inner_column == "dim1"
        assert pts[0].agg_function is None

    def test_scalar_wrapper_around_aggregate_classified_as_analytical(self):
        ir = parse_sql_to_ir(
            "SELECT ROUND(SUM(revenue), 2) AS rounded_rev FROM t", "m1"
        )
        assert "revenue" in ir.requested_measures
        analytics = [e for e in ir.select_expressions if e.classification == "analytical"]
        assert len(analytics) == 1
        assert analytics[0].agg_function == "sum"
        assert analytics[0].inner_column == "revenue"
        assert analytics[0].alias == "rounded_rev"

    def test_complex_aggregate_expr_classified_as_passthrough(self):
        ir = parse_sql_to_ir("SELECT SUM(price * qty) AS total FROM t", "m1")
        # price and qty should NOT be extracted as measures
        assert "price" not in ir.requested_measures
        assert "qty" not in ir.requested_measures
        pts = [e for e in ir.select_expressions if e.classification == "passthrough"]
        assert any(e.agg_function is not None for e in pts)

    def test_mixed_select_list(self):
        sql = "SELECT dim1, SUM(revenue), COUNT(*), AVG(price) FROM t GROUP BY dim1"
        ir = parse_sql_to_ir(sql, "m1")

        assert "dim1" in ir.requested_dimensions
        assert "revenue" in ir.requested_measures
        assert "__row_count" in ir.requested_measures
        assert "price" in ir.requested_measures

        classifications = [e.classification for e in ir.select_expressions]
        assert "passthrough" in classifications  # dim1
        assert "analytical" in classifications   # SUM(revenue), AVG(price)
        assert "literal" in classifications      # COUNT(*)

    def test_alias_preserved(self):
        ir = parse_sql_to_ir("SELECT SUM(revenue) AS total_rev FROM t", "m1")
        analytics = [e for e in ir.select_expressions if e.classification == "analytical"]
        assert analytics[0].alias == "total_rev"

    def test_no_alias_is_none(self):
        ir = parse_sql_to_ir("SELECT SUM(revenue) FROM t", "m1")
        analytics = [e for e in ir.select_expressions if e.classification == "analytical"]
        assert analytics[0].alias is None


# ===========================================================================
# 2. Parser — grain and dimensions
# ===========================================================================

class TestParserGrainAndDimensions:

    def test_group_by_sets_grain(self):
        ir = parse_sql_to_ir("SELECT dim1, SUM(x) FROM t GROUP BY dim1", "m1")
        assert ir.grain == ["dim1"]

    def test_multiple_group_by(self):
        ir = parse_sql_to_ir("SELECT a, b, SUM(x) FROM t GROUP BY a, b", "m1")
        assert ir.grain == ["a", "b"]

    def test_no_group_by_empty_grain(self):
        ir = parse_sql_to_ir("SELECT SUM(x) FROM t", "m1")
        assert ir.grain == []

    def test_dimensions_include_bare_selects(self):
        ir = parse_sql_to_ir("SELECT a, b FROM t", "m1")
        assert set(ir.requested_dimensions) == {"a", "b"}

    def test_order_by_extracted(self):
        ir = parse_sql_to_ir(
            "SELECT a, SUM(x) FROM t GROUP BY a ORDER BY a DESC", "m1"
        )
        assert ir.order_by == [("a", "desc")]

    def test_limit_and_offset_extracted(self):
        ir = parse_sql_to_ir("SELECT a FROM t LIMIT 10 OFFSET 5", "m1")
        assert ir.limit == 10
        assert ir.offset == 5


# ===========================================================================
# 3. Parser — filters
# ===========================================================================

class TestParserFilters:

    def test_eq_filter(self):
        ir = parse_sql_to_ir("SELECT a FROM t WHERE a = 'x'", "m1")
        assert len(ir.filters) == 1
        assert ir.filters[0].operator == "eq"
        assert ir.filters[0].value == "x"

    def test_in_filter(self):
        ir = parse_sql_to_ir("SELECT a FROM t WHERE a IN ('x', 'y')", "m1")
        assert len(ir.filters) == 1
        assert ir.filters[0].operator == "in"
        assert ir.filters[0].value == ["x", "y"]

    def test_between_filter(self):
        ir = parse_sql_to_ir("SELECT a FROM t WHERE a BETWEEN 1 AND 10", "m1")
        assert len(ir.filters) == 1
        assert ir.filters[0].operator == "between"
        assert ir.filters[0].value == (1, 10)

    def test_is_null_filter(self):
        ir = parse_sql_to_ir("SELECT a FROM t WHERE a IS NULL", "m1")
        assert any(f.operator == "is_null" for f in ir.filters)


# ===========================================================================
# 4. Rewriter — aggregate path, exact grain
# ===========================================================================

class TestRewriterExactGrain:

    def test_count_1_exact_grain_no_re_agg(self):
        """COUNT(1) at exact grain -> direct column reference, no SUM()."""
        ir = parse_sql_to_ir(
            "SELECT dim1, COUNT(1) AS my_count FROM t GROUP BY dim1", "m1"
        )
        agg = _make_agg(
            ["dim1"], [_make_col("revenue", "sum"), _make_row_count_col()]
        )
        bq = _bind(ir, [("__row_count", "count")], ["dim1"])
        sql = rewrite_for_aggregate(bq, agg)

        assert '"__row_count__count"' in sql
        assert "SUM(" not in sql

    def test_count_star_exact_grain_no_re_agg(self):
        """COUNT(*) at exact grain -> direct column reference."""
        ir = parse_sql_to_ir(
            "SELECT dim1, COUNT(*) AS cnt FROM t GROUP BY dim1", "m1"
        )
        agg = _make_agg(["dim1"], [_make_row_count_col()])
        bq = _bind(ir, [("__row_count", "count")], ["dim1"])
        sql = rewrite_for_aggregate(bq, agg)

        assert '"__row_count__count"' in sql
        assert "SUM(" not in sql

    @pytest.mark.parametrize("lit", ["100", "'x'", "TRUE", "-1"])
    def test_count_arbitrary_literal_exact_grain(self, lit):
        """COUNT(<any literal>) at exact grain -> __row_count__count column."""
        ir = parse_sql_to_ir(
            f"SELECT dim1, COUNT({lit}) FROM t GROUP BY dim1", "m1"
        )
        agg = _make_agg(["dim1"], [_make_row_count_col()])
        bq = _bind(ir, [("__row_count", "count")], ["dim1"])
        sql = rewrite_for_aggregate(bq, agg)

        assert '"__row_count__count"' in sql, f"COUNT({lit}) should rewrite to __row_count__count"
        assert "SUM(" not in sql

    @pytest.mark.parametrize("lit", ["100", "'x'", "TRUE", "-1"])
    def test_count_arbitrary_literal_coarser_grain(self, lit):
        """COUNT(<any literal>) at coarser grain -> SUM(__row_count__count)."""
        ir = parse_sql_to_ir(f"SELECT COUNT({lit}) AS total FROM t", "m1")
        agg = _make_agg(["dim1"], [_make_row_count_col()])
        bq = _bind(ir, [("__row_count", "count")], [])
        sql = rewrite_for_aggregate(bq, agg)

        assert 'SUM("__row_count__count")' in sql, f"COUNT({lit}) coarser grain should SUM"

    def test_sum_exact_grain_direct_column(self):
        """SUM(revenue) at exact grain -> direct column reference."""
        ir = parse_sql_to_ir(
            "SELECT dim1, SUM(revenue) AS rev FROM t GROUP BY dim1", "m1"
        )
        agg = _make_agg(
            ["dim1"], [_make_col("revenue", "sum"), _make_row_count_col()]
        )
        bq = _bind(ir, [("revenue", "sum")], ["dim1"])
        sql = rewrite_for_aggregate(bq, agg)

        assert '"revenue__sum"' in sql
        assert "SUM(" not in sql

    def test_avg_exact_grain_derived_from_sum_count(self):
        """AVG(revenue) at exact grain -> sum/count derivation."""
        ir = parse_sql_to_ir(
            "SELECT dim1, AVG(revenue) AS avg_rev FROM t GROUP BY dim1", "m1"
        )
        agg = _make_agg(
            ["dim1"],
            [
                _make_col("revenue", "sum"),
                _make_col("revenue", "count"),
                _make_row_count_col(),
            ],
        )
        bq = _bind(ir, [("revenue", "avg")], ["dim1"])
        sql = rewrite_for_aggregate(bq, agg)

        assert '"revenue__sum"' in sql
        assert '"revenue__count"' in sql
        assert "NULLIF" in sql

    def test_min_exact_grain_direct_column(self):
        """MIN at exact grain -> direct column reference."""
        ir = parse_sql_to_ir("SELECT MIN(price) FROM t", "m1")
        agg = _make_agg(
            [], [_make_col("price", "min"), _make_row_count_col()]
        )
        bq = _bind(ir, [("price", "min")], [])
        sql = rewrite_for_aggregate(bq, agg)

        assert '"price__min"' in sql


# ===========================================================================
# 5. Rewriter — aggregate path, coarser grain (re-aggregation)
# ===========================================================================

class TestRewriterCoarserGrain:

    def test_count_star_coarser_grain_sums_row_count(self):
        """COUNT(*) with no dims against aggregate with dim1 -> SUM(__row_count__count)."""
        ir = parse_sql_to_ir("SELECT COUNT(*) AS total FROM t", "m1")
        agg = _make_agg(["dim1"], [_make_row_count_col()])
        bq = _bind(ir, [("__row_count", "count")], [])
        sql = rewrite_for_aggregate(bq, agg)

        assert 'SUM("__row_count__count")' in sql

    def test_sum_coarser_grain_re_sums(self):
        """SUM(revenue) at coarser grain -> SUM(revenue__sum)."""
        ir = parse_sql_to_ir(
            "SELECT dim1, SUM(revenue) AS rev FROM t GROUP BY dim1", "m1"
        )
        agg = _make_agg(
            ["dim1", "dim2"],
            [_make_col("revenue", "sum"), _make_row_count_col()],
        )
        bq = _bind(ir, [("revenue", "sum")], ["dim1"])
        sql = rewrite_for_aggregate(bq, agg)

        assert 'SUM("revenue__sum")' in sql

    def test_coarser_grain_includes_group_by(self):
        """Re-aggregation queries must include GROUP BY for dimensions."""
        ir = parse_sql_to_ir(
            "SELECT dim1, SUM(revenue) FROM t GROUP BY dim1", "m1"
        )
        agg = _make_agg(
            ["dim1", "dim2"],
            [_make_col("revenue", "sum"), _make_row_count_col()],
        )
        bq = _bind(ir, [("revenue", "sum")], ["dim1"])
        sql = rewrite_for_aggregate(bq, agg)

        assert "GROUP BY" in sql
        assert '"dim1"' in sql

    def test_exact_grain_no_group_by(self):
        """Exact grain queries should NOT add GROUP BY (no re-aggregation)."""
        ir = parse_sql_to_ir(
            "SELECT dim1, SUM(revenue) FROM t GROUP BY dim1", "m1"
        )
        agg = _make_agg(
            ["dim1"], [_make_col("revenue", "sum"), _make_row_count_col()]
        )
        bq = _bind(ir, [("revenue", "sum")], ["dim1"])
        sql = rewrite_for_aggregate(bq, agg)

        assert "GROUP BY" not in sql

    def test_min_coarser_grain_re_aggregates_with_min(self):
        """MIN at coarser grain -> MIN(price__min), not SUM."""
        ir = parse_sql_to_ir("SELECT MIN(price) FROM t", "m1")
        agg = _make_agg(
            ["dim1"], [_make_col("price", "min"), _make_row_count_col()]
        )
        bq = _bind(ir, [("price", "min")], [])
        sql = rewrite_for_aggregate(bq, agg)

        assert 'MIN("price__min")' in sql

    def test_max_coarser_grain_re_aggregates_with_max(self):
        """MAX at coarser grain -> MAX(price__max)."""
        ir = parse_sql_to_ir("SELECT MAX(price) FROM t", "m1")
        agg = _make_agg(
            ["dim1"], [_make_col("price", "max"), _make_row_count_col()]
        )
        bq = _bind(ir, [("price", "max")], [])
        sql = rewrite_for_aggregate(bq, agg)

        assert 'MAX("price__max")' in sql

    def test_avg_coarser_grain_derived_from_sum_count(self):
        """AVG at coarser grain -> SUM(sum_col) / NULLIF(SUM(count_col), 0)."""
        ir = parse_sql_to_ir("SELECT AVG(revenue) FROM t", "m1")
        agg = _make_agg(
            ["dim1"],
            [
                _make_col("revenue", "sum"),
                _make_col("revenue", "count"),
                _make_row_count_col(),
            ],
        )
        bq = _bind(ir, [("revenue", "avg")], [])
        sql = rewrite_for_aggregate(bq, agg)

        assert 'SUM("revenue__sum")' in sql
        assert 'SUM("revenue__count")' in sql
        assert "NULLIF" in sql

    def test_grand_total_no_group_by(self):
        """Grand total (no dimensions) should NOT have GROUP BY."""
        ir = parse_sql_to_ir("SELECT COUNT(*) AS total FROM t", "m1")
        agg = _make_agg(["dim1"], [_make_row_count_col()])
        bq = _bind(ir, [("__row_count", "count")], [])
        sql = rewrite_for_aggregate(bq, agg)

        assert "GROUP BY" not in sql


# ===========================================================================
# 5b. Rewriter — stored avg column correctness (F-004-01 / F-004-05)
# ===========================================================================

def _avg_default_cols(name):
    """The physical column set an avg-default measure materialises:
    primary avg + multi-stat sum/count/min/max (MULTI_STAT_MAP['avg'])."""
    return [
        _make_col(name, "avg"),
        _make_col(name, "sum"),
        _make_col(name, "count"),
        _make_col(name, "min"),
        _make_col(name, "max"),
        _make_row_count_col(),
    ]


class TestAvgStoredColumnCorrectness:
    """F-004-01: a stored avg column must NEVER be re-aggregated at coarser
    grain — SUM of stored averages is not an average. The coarser-grain
    answer is always the count-weighted SUM(sum)/SUM(count) decomposition;
    the stored avg column is readable at exact grain only."""

    def test_avg_coarser_grain_with_stored_avg_col_decomposes(self):
        """The critical mode from the report: avg-default measure, aggregate
        holds score__avg AND sum/count, query at coarser grain. Must emit the
        decomposition, never SUM(score__avg)."""
        ir = parse_sql_to_ir("SELECT AVG(score) FROM t", "m1")
        agg = _make_agg(["country", "region"], _avg_default_cols("score"))
        bq = _bind(ir, [("score", "avg")], [])
        sql = rewrite_for_aggregate(bq, agg)

        assert 'SUM("score__avg")' not in sql
        assert '"score__avg"' not in sql
        assert 'SUM("score__sum")' in sql
        assert 'SUM("score__count")' in sql
        assert "NULLIF" in sql

    def test_avg_exact_grain_with_stored_avg_col_reads_it(self):
        """At exact grain the stored avg column IS the pre-computed answer."""
        ir = parse_sql_to_ir(
            "SELECT country, AVG(score) AS s FROM t GROUP BY country", "m1"
        )
        agg = _make_agg(["country"], _avg_default_cols("score"))
        bq = _bind(ir, [("score", "avg")], ["country"])
        sql = rewrite_for_aggregate(bq, agg)

        assert '"score__avg"' in sql
        assert "NULLIF" not in sql

    def test_avg_coarser_grain_avg_only_aggregate_never_sums_averages(self):
        """Defence-in-depth: a legacy avg-only aggregate (no sum/count) at
        coarser grain must emit NULL, never a re-aggregation of the stored
        averages. (The matcher gates this shape to exact grain, so the
        rewriter should not normally see it.)"""
        ir = parse_sql_to_ir("SELECT AVG(score) FROM t", "m1")
        agg = _make_agg(
            ["country"], [_make_col("score", "avg"), _make_row_count_col()]
        )
        bq = _bind(ir, [("score", "avg")], [])
        sql = rewrite_for_aggregate(bq, agg)

        assert 'SUM("score__avg")' not in sql
        assert "NULL" in sql

    def test_avg_decomposition_avoids_integer_division(self):
        """F-004-05: the derivation must not truncate on integer-typed
        sum/count columns — the emitted expression carries the * 1.0
        numeric promotion on both the coarser- and exact-grain forms."""
        ir = parse_sql_to_ir("SELECT AVG(days) FROM t", "m1")
        agg = _make_agg(
            ["d"],
            [
                _make_col("days", "sum"),
                _make_col("days", "count"),
                _make_row_count_col(),
            ],
        )
        bq = _bind(ir, [("days", "avg")], [])
        sql = rewrite_for_aggregate(bq, agg)
        assert 'SUM("days__sum") * 1.0 / NULLIF(SUM("days__count"), 0)' in sql

        ir2 = parse_sql_to_ir("SELECT d, AVG(days) FROM t GROUP BY d", "m1")
        agg2 = _make_agg(
            ["d"],
            [
                _make_col("days", "sum"),
                _make_col("days", "count"),
                _make_row_count_col(),
            ],
        )
        bq2 = _bind(ir2, [("days", "avg")], ["d"])
        sql2 = rewrite_for_aggregate(bq2, agg2)
        assert '"days__sum" * 1.0 / NULLIF("days__count", 0)' in sql2

    def test_having_avg_decomposition_avoids_integer_division(self):
        """The HAVING avg mapper shares the * 1.0 promotion."""
        ir = parse_sql_to_ir(
            "SELECT d, SUM(days) FROM t GROUP BY d HAVING AVG(days) > 3",
            "m1",
        )
        agg = _make_agg(
            ["d", "e"],
            [
                _make_col("days", "sum"),
                _make_col("days", "count"),
                _make_row_count_col(),
            ],
        )
        bq = _bind(ir, [("days", "sum")], ["d"])
        sql = rewrite_for_aggregate(bq, agg)
        assert "HAVING" in sql.upper()
        assert 'SUM("days__sum") * 1.0 / NULLIF(SUM("days__count"), 0)' in sql


# ===========================================================================
# 6. Rewriter — scalar wrapper preservation
# ===========================================================================

class TestRewriterScalarWrappers:

    def test_round_sum_preserves_round(self):
        """ROUND(SUM(revenue), 2) -> ROUND(<physical_expr>, 2)."""
        ir = parse_sql_to_ir(
            "SELECT ROUND(SUM(revenue), 2) AS rounded FROM t", "m1"
        )
        agg = _make_agg(
            [], [_make_col("revenue", "sum"), _make_row_count_col()]
        )
        bq = _bind(ir, [("revenue", "sum")], [])
        sql = rewrite_for_aggregate(bq, agg)

        assert "ROUND(" in sql.upper()


# ===========================================================================
# 6b. Rewriter — COUNT(DISTINCT) exact grain (F-004-02)
# ===========================================================================

class TestRewriterCountDistinct:

    def test_count_distinct_exact_grain_reads_stored_column(self):
        """F-004-02: COUNT(DISTINCT user_id) at exact grain reads the stored
        ``user_id__count_distinct`` column, NOT the raw ``COUNT(DISTINCT
        user_id)`` against a table that has no ``user_id`` column."""
        ir = parse_sql_to_ir(
            "SELECT country, COUNT(DISTINCT user_id) AS uu "
            "FROM t GROUP BY country",
            "m1",
        )
        agg = _make_agg(
            ["country"],
            [
                _make_col("user_id", "count_distinct"),
                _make_row_count_col(),
            ],
        )
        bq = _bind(ir, [("user_id", "count_distinct")], ["country"])
        sql = rewrite_for_aggregate(bq, agg)

        assert '"user_id__count_distinct"' in sql
        # The raw COUNT(DISTINCT ...) function call must NOT survive — the bare
        # ``user_id`` column does not exist in the aggregate table. The stored
        # column name (count_distinct suffix) is fine; a COUNT( function call
        # is not.
        assert "COUNT(" not in sql.upper()


# ===========================================================================
# 6c. Rewriter — HAVING stat coverage / DISTINCT (F-004-04)
# ===========================================================================

class TestRewriterHaving:

    def test_having_count_distinct_uses_count_distinct_column(self):
        """F-004-04: HAVING COUNT(DISTINCT c) reads the stored
        ``c__count_distinct`` column, NOT the row-count column (which counts
        rows, not distinct values)."""
        ir = parse_sql_to_ir(
            "SELECT country, SUM(revenue) FROM t GROUP BY country "
            "HAVING COUNT(DISTINCT customer) > 10",
            "m1",
        )
        agg = _make_agg(
            ["country"],
            [
                _make_col("revenue", "sum"),
                _make_col("customer", "count_distinct"),
                _make_row_count_col(),
            ],
        )
        bq = _bind(ir, [("revenue", "sum")], ["country"])
        sql = rewrite_for_aggregate(bq, agg)

        assert '"customer__count_distinct"' in sql
        # Must NOT substitute the row-count column for the distinct count.
        assert '"__row_count__count" > 10' not in sql
        assert 'SUM("__row_count__count") > 10' not in sql

    def test_having_missing_stat_fails_loud(self):
        """F-004-04: HAVING MAX(x) against an aggregate that stores only
        x__sum must NOT silently substitute the sum column — the rewriter
        raises so the router falls back to source."""
        from src.rewrite.query_rewriter import AggregateRewriteUnsupported

        ir = parse_sql_to_ir(
            "SELECT country, SUM(x) FROM t GROUP BY country HAVING MAX(x) > 5",
            "m1",
        )
        agg = _make_agg(
            ["country"],
            [_make_col("x", "sum"), _make_row_count_col()],
        )
        bq = _bind(ir, [("x", "sum")], ["country"])
        with pytest.raises(AggregateRewriteUnsupported):
            rewrite_for_aggregate(bq, agg)


# ===========================================================================
# 7. Rewriter — WHERE / ORDER BY / LIMIT / OFFSET
# ===========================================================================

class TestRewriterClauses:

    def test_where_rendered(self):
        ir = parse_sql_to_ir(
            "SELECT dim1, SUM(revenue) FROM t WHERE dim1 = 'US' GROUP BY dim1",
            "m1",
        )
        agg = _make_agg(
            ["dim1"], [_make_col("revenue", "sum"), _make_row_count_col()]
        )
        bq = _bind(
            ir,
            [("revenue", "sum")],
            ["dim1"],
            filters=[LogicalFilter("dim1", "eq", "US")],
        )
        sql = rewrite_for_aggregate(bq, agg)

        assert "WHERE" in sql
        assert "'US'" in sql

    def test_limit_rendered(self):
        ir = parse_sql_to_ir("SELECT dim1 FROM t LIMIT 50", "m1")
        agg = _make_agg(["dim1"], [_make_row_count_col()])
        bq = _bind(ir, [], ["dim1"])
        sql = rewrite_for_aggregate(bq, agg)

        assert "LIMIT 50" in sql

    def test_order_by_rendered(self):
        ir = parse_sql_to_ir(
            "SELECT dim1, SUM(revenue) FROM t GROUP BY dim1 ORDER BY dim1 DESC",
            "m1",
        )
        agg = _make_agg(
            ["dim1"], [_make_col("revenue", "sum"), _make_row_count_col()]
        )
        bq = _bind(ir, [("revenue", "sum")], ["dim1"])
        sql = rewrite_for_aggregate(bq, agg)

        assert "ORDER BY" in sql
        assert "DESC" in sql


# ===========================================================================
# 8. Rewriter — table reference
# ===========================================================================

class TestRewriterTableRef:

    def test_schema_qualified_table(self):
        ir = parse_sql_to_ir("SELECT dim1 FROM t", "m1")
        agg = _make_agg(
            ["dim1"], [_make_row_count_col()], target_schema="trgt"
        )
        bq = _bind(ir, [], ["dim1"])
        sql = rewrite_for_aggregate(bq, agg)

        assert '"trgt"."agg_table_123"' in sql

    def test_no_schema_just_table(self):
        ir = parse_sql_to_ir("SELECT dim1 FROM t", "m1")
        agg = _make_agg(
            ["dim1"], [_make_row_count_col()], target_schema=""
        )
        bq = _bind(ir, [], ["dim1"])
        sql = rewrite_for_aggregate(bq, agg)

        assert '"agg_table_123"' in sql
        assert '""."agg_table_123"' not in sql


# ===========================================================================
# 9. Passthrough detection
# ===========================================================================

class TestPassthroughDetection:

    def test_bare_dimensions_do_not_set_passthrough(self):
        """Bare dimension columns should NOT trigger has_passthrough_expressions."""
        ir = parse_sql_to_ir(
            "SELECT dim1, SUM(revenue) FROM t GROUP BY dim1", "m1"
        )
        has_passthrough = any(
            expr.classification == "passthrough" and expr.inner_column is None
            for expr in ir.select_expressions
        )
        assert has_passthrough is False

    def test_complex_aggregate_sets_passthrough(self):
        """SUM(price * qty) should trigger has_passthrough_expressions."""
        ir = parse_sql_to_ir("SELECT SUM(price * qty) FROM t", "m1")
        has_passthrough = any(
            expr.classification == "passthrough" and expr.inner_column is None
            for expr in ir.select_expressions
        )
        assert has_passthrough is True

    def test_stddev_classified_as_analytical_exact_grain(self):
        """Phase E: STDDEV_POP is now a routable analytical expression whose
        agg_function is the canonical stat type (exact-grain materialised col),
        not a passthrough. The sqlglot key 'stddevpop' normalises to
        'stddev_pop'."""
        ir = parse_sql_to_ir("SELECT STDDEV_POP(revenue) FROM t", "m1")
        analytics = [e for e in ir.select_expressions if e.classification == "analytical"]
        assert len(analytics) == 1
        assert analytics[0].agg_function == "stddev_pop"
        assert analytics[0].inner_column == "revenue"
        assert "revenue" in ir.requested_measures


# ===========================================================================
# 10. Fallback path (no select_expressions)
# ===========================================================================

class TestRewriterFallback:

    def test_fallback_when_no_select_expressions(self):
        """V1 queries without select_expressions should still produce valid SQL."""
        lq = LogicalQuery(
            model_id="m1",
            protocol="jdbc",
            raw_query="SELECT 1",
            requested_measures=["revenue"],
            requested_dimensions=["dim1"],
            filters=[],
            grain=["dim1"],
            order_by=[],
            limit=None,
            offset=None,
            query_fingerprint="test",
            select_expressions=[],
        )
        bq = BoundQuery(
            logical_query=lq,
            model=MagicMock(),
            resolved_measures=[
                types.SimpleNamespace(name="revenue", default_agg="sum")
            ],
            resolved_dimensions=[types.SimpleNamespace(name="dim1")],
            resolved_filters=[],
        )
        agg = _make_agg(
            ["dim1"], [_make_col("revenue", "sum"), _make_row_count_col()]
        )
        sql = rewrite_for_aggregate(bq, agg)

        assert '"dim1"' in sql
        assert '"revenue__sum"' in sql


# ===========================================================================
# 11. Fingerprint
# ===========================================================================

class TestFingerprint:

    def test_same_query_same_fingerprint(self):
        ir1 = parse_sql_to_ir("SELECT SUM(x) FROM t GROUP BY a", "m1")
        ir2 = parse_sql_to_ir("SELECT SUM(x) FROM t GROUP BY a", "m1")
        assert ir1.query_fingerprint == ir2.query_fingerprint

    def test_different_measures_different_fingerprint(self):
        ir1 = parse_sql_to_ir("SELECT SUM(x) FROM t GROUP BY a", "m1")
        ir2 = parse_sql_to_ir("SELECT SUM(y) FROM t GROUP BY a", "m1")
        assert ir1.query_fingerprint != ir2.query_fingerprint


# ===========================================================================
# 12. Registry integration
# ===========================================================================

class TestRegistryIntegration:

    def test_known_aggregate_funcs(self):
        from src.registry import get_aggregate_func
        assert get_aggregate_func("count") is not None
        assert get_aggregate_func("sum") is not None
        assert get_aggregate_func("avg") is not None
        assert get_aggregate_func("min") is not None
        assert get_aggregate_func("max") is not None
        assert get_aggregate_func("count_distinct") is not None

    def test_unknown_func_returns_none(self):
        from src.registry import get_aggregate_func
        assert get_aggregate_func("nonexistent_func") is None

    def test_scalar_transparent(self):
        from src.registry import get_scalar_func
        assert get_scalar_func("round") == "transparent"
        assert get_scalar_func("coalesce") == "transparent"
        assert get_scalar_func("cast") == "transparent"

    def test_pseudo_literals(self):
        from src.registry import is_pseudo_literal
        assert is_pseudo_literal("1") == "row_marker"
        assert is_pseudo_literal("*") == "row_marker"
        assert is_pseudo_literal("42") == "row_marker"
        assert is_pseudo_literal("foo") == "constant"

    def test_routing_types(self):
        from src.registry import get_aggregate_func
        assert get_aggregate_func("sum")["routing"] == "mappable"
        assert get_aggregate_func("avg")["routing"] == "derivable"
        # MIN/MAX are re-aggregatable (MIN(MIN)=MIN, MAX(MAX)=MAX) — mappable,
        # not exact_grain (Q8 exact-grain was only for COUNT DISTINCT / median).
        assert get_aggregate_func("min")["routing"] == "mappable"
        assert get_aggregate_func("max")["routing"] == "mappable"
        assert get_aggregate_func("count_distinct")["routing"] == "exact_grain"
        # Phase E: STDDEV/VAR are materialisable (opt-in include_stats) and
        # routable at exact grain — no longer passthrough.
        assert get_aggregate_func("stddev_pop")["routing"] == "exact_grain"
        assert get_aggregate_func("stddev_samp")["routing"] == "exact_grain"
        assert get_aggregate_func("var_pop")["routing"] == "exact_grain"
        assert get_aggregate_func("var_samp")["routing"] == "exact_grain"


# ===========================================================================
# Cause A — Paren normalization (routing classification must be invariant
# under semantically-transparent grouping; `(SUM(x))` == `SUM(x)`).
# ===========================================================================

class TestParenNormalization:

    def test_parenthesized_sum_classified_as_analytical(self):
        ir = parse_sql_to_ir("SELECT (SUM(revenue)) AS value FROM t", "m1")
        assert "revenue" in ir.requested_measures
        analytics = [e for e in ir.select_expressions if e.classification == "analytical"]
        assert len(analytics) == 1
        assert analytics[0].agg_function == "sum"
        assert analytics[0].inner_column == "revenue"
        assert not any(e.classification == "passthrough" for e in ir.select_expressions)

    def test_nested_parens_classified_as_analytical(self):
        ir = parse_sql_to_ir("SELECT ((SUM(revenue))) FROM t", "m1")
        analytics = [e for e in ir.select_expressions if e.classification == "analytical"]
        assert len(analytics) == 1
        assert analytics[0].agg_function == "sum"
        assert analytics[0].inner_column == "revenue"
        assert not any(e.classification == "passthrough" for e in ir.select_expressions)

    def test_classification_invariant_to_redundant_parens(self):
        bare = parse_sql_to_ir("SELECT SUM(revenue) FROM t", "m1")
        paren = parse_sql_to_ir("SELECT (SUM(revenue)) FROM t", "m1")
        bare_cls = [e.classification for e in bare.select_expressions]
        paren_cls = [e.classification for e in paren.select_expressions]
        assert bare_cls == paren_cls == ["analytical"]
        assert sorted(bare.requested_measures) == sorted(paren.requested_measures)

    def test_parenthesized_min_classified_as_analytical(self):
        ir = parse_sql_to_ir("SELECT (MIN(price)) FROM t", "m1")
        analytics = [e for e in ir.select_expressions if e.classification == "analytical"]
        assert len(analytics) == 1
        assert analytics[0].agg_function == "min"
        assert analytics[0].inner_column == "price"

    def test_parenthesized_sum_rewrites_like_bare(self):
        ir = parse_sql_to_ir("SELECT (SUM(revenue)) FROM t", "m1")
        agg = _make_agg(["dim1"], [_make_col("revenue", "sum"), _make_row_count_col()])
        bq = _bind(ir, [("revenue", "sum")], [])
        sql = rewrite_for_aggregate(bq, agg)
        assert 'SUM("revenue__sum")' in sql


# ===========================================================================
# Cause B — Composed-aggregate routing (scalar compositions of re-aggregatable
# aggregates: ratios, CASE). Each aggregate node maps to its OWN column.
# ===========================================================================

class TestComposedAggregateRouting:

    def test_ratio_two_sums_marked_composable(self):
        # Composable stays classification="passthrough" (source path unchanged)
        # but carries composable=True so the matcher does not bail.
        ir = parse_sql_to_ir('SELECT SUM(kyc)/SUM(login) AS v FROM t', "m1")
        assert "kyc" in ir.requested_measures
        assert "login" in ir.requested_measures
        comp = [e for e in ir.select_expressions if e.composable]
        assert len(comp) == 1
        assert sorted(comp[0].agg_functions) == ["sum", "sum"]

    def test_case_ratio_marked_composable(self):
        ir = parse_sql_to_ir(
            'SELECT CASE WHEN SUM(login)=0 THEN NULL ELSE SUM(kyc)*1.0/SUM(login) END AS v FROM t', "m1")
        comp = [e for e in ir.select_expressions if e.composable]
        assert len(comp) == 1

    def test_sum_of_product_not_composable(self):
        ir = parse_sql_to_ir('SELECT SUM(qty * price) AS v FROM t', "m1")
        assert not any(e.composable for e in ir.select_expressions)

    def test_stddev_in_ratio_not_composable(self):
        ir = parse_sql_to_ir('SELECT SUM(a)/STDDEV(b) AS v FROM t', "m1")
        assert not any(e.composable for e in ir.select_expressions)

    def test_bare_column_in_expression_not_composable(self):
        ir = parse_sql_to_ir('SELECT SUM(a) + b AS v FROM t', "m1")
        assert not any(e.composable for e in ir.select_expressions)

    def test_ratio_rewrites_each_sum_to_own_column(self):
        ir = parse_sql_to_ir('SELECT SUM(kyc)/SUM(login) AS v FROM t', "m1")
        agg = _make_agg(["dim1"], [_make_col("kyc", "sum"), _make_col("login", "sum"), _make_row_count_col()])
        bq = _bind(ir, [("kyc", "sum"), ("login", "sum")], [])
        sql = rewrite_for_aggregate(bq, agg)
        assert 'SUM("kyc__sum")' in sql
        assert 'SUM("login__sum")' in sql

    def test_case_ratio_preserves_case_and_maps_both(self):
        ir = parse_sql_to_ir(
            'SELECT CASE WHEN SUM(login)=0 THEN NULL ELSE SUM(kyc)*1.0/SUM(login) END AS v FROM t', "m1")
        agg = _make_agg(["dim1"], [_make_col("kyc", "sum"), _make_col("login", "sum"), _make_row_count_col()])
        bq = _bind(ir, [("kyc", "sum"), ("login", "sum")], [])
        sql = rewrite_for_aggregate(bq, agg)
        assert 'SUM("kyc__sum")' in sql
        assert 'SUM("login__sum")' in sql
        assert "CASE" in sql.upper()

    def test_sum_over_count_ratio_maps_both(self):
        ir = parse_sql_to_ir('SELECT SUM(amount)/COUNT(*) AS v FROM t', "m1")
        agg = _make_agg(["dim1"], [_make_col("amount", "sum"), _make_row_count_col()])
        bq = _bind(ir, [("amount", "sum"), ("__row_count", "count")], [])
        sql = rewrite_for_aggregate(bq, agg)
        assert 'SUM("amount__sum")' in sql
        assert 'SUM("__row_count__count")' in sql

    def test_avg_ratio_derives_each_avg_from_sum_count(self):
        ir = parse_sql_to_ir('SELECT AVG(a)/AVG(b) AS v FROM t', "m1")
        agg = _make_agg(["dim1"], [
            _make_col("a", "sum"), _make_col("a", "count"),
            _make_col("b", "sum"), _make_col("b", "count"),
            _make_row_count_col(),
        ])
        bq = _bind(ir, [("a", "avg"), ("b", "avg")], [])
        sql = rewrite_for_aggregate(bq, agg)
        assert 'SUM("a__sum")' in sql and 'NULLIF(SUM("a__count")' in sql
        assert 'SUM("b__sum")' in sql and 'NULLIF(SUM("b__count")' in sql

    def test_composable_carries_inner_aggregate_column_pairs(self):
        # HIGH-1: the parser must record each composable node's (column, func)
        # pair so the matcher can require the exact stat column, not just the
        # function name.
        ir = parse_sql_to_ir('SELECT MAX(a)/MAX(b) AS v FROM t', "m1")
        comp = [e for e in ir.select_expressions if e.composable]
        assert len(comp) == 1
        assert sorted(comp[0].inner_aggregates) == [("a", "max"), ("b", "max")]

    def test_composable_count_star_pair_uses_row_count(self):
        ir = parse_sql_to_ir('SELECT SUM(a)/COUNT(*) AS v FROM t', "m1")
        comp = [e for e in ir.select_expressions if e.composable]
        assert len(comp) == 1
        assert sorted(comp[0].inner_aggregates) == [("__row_count", "count"), ("a", "sum")]

    def test_count_only_composite_rewrites_each_node_to_row_count(self):
        # MEDIUM-1: a pure count-only composite (COUNT(*)/COUNT(*)) must rewrite
        # each node to the aggregate row-count column so the result is exact:
        # SUM("__row_count__count") / SUM("__row_count__count").
        ir = parse_sql_to_ir('SELECT COUNT(*)/COUNT(*) AS v FROM t', "m1")
        agg = _make_agg(["dim1"], [_make_col("a", "sum"), _make_row_count_col()])
        bq = _bind(ir, [], [])
        sql = rewrite_for_aggregate(bq, agg)
        assert sql.upper().count('SUM("__ROW_COUNT__COUNT")') == 2

    def test_composable_node_emits_null_when_exact_stat_column_absent(self):
        # Defence-in-depth: the rewriter must NOT substitute a different stat
        # for an explicit composable node. With only sum columns, MAX(a) has no
        # a__max column -> NULL, never MAX(a__sum).
        ir = parse_sql_to_ir('SELECT MAX(a)/MAX(b) AS v FROM t', "m1")
        agg = _make_agg(["dim1"], [_make_col("a", "sum"), _make_col("b", "sum"), _make_row_count_col()])
        bq = _bind(ir, [("a", "sum"), ("b", "sum")], [])
        sql = rewrite_for_aggregate(bq, agg)
        assert 'a__sum' not in sql and 'b__sum' not in sql
        assert "NULL" in sql.upper()


# ===========================================================================
# Phase D — median / percentile recognised as a materialised quantile column
# (pNN). Routed exact-grain only; non-canonical fractions stay passthrough.
# ===========================================================================

class TestPercentileParsing:

    def test_median_classified_as_p50(self):
        ir = parse_sql_to_ir("SELECT MEDIAN(amount) AS m FROM t", "m1")
        a = [e for e in ir.select_expressions if e.classification == "analytical"]
        assert len(a) == 1
        assert a[0].agg_function == "p50"
        assert a[0].inner_column == "amount"
        assert "amount" in ir.requested_measures

    def test_percentile_cont_within_group_is_passthrough(self):
        # F-003-07: ordered-set aggregates (WITHIN GROUP) are source-passthrough
        # by design (query-shape catalog shape #86). The parser must flag them
        # complex — NOT classify them as a routable pNN, which produced dead,
        # self-contradicting machinery (the binder discards complex-SQL
        # measures so the pNN classification could never route).
        ir = parse_sql_to_ir(
            "SELECT PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY amount) AS p FROM t", "m1")
        assert ir.has_complex_sql is True
        assert not any(e.classification == "analytical" for e in ir.select_expressions)

    def test_percentile_disc_within_group_is_passthrough(self):
        ir = parse_sql_to_ir(
            "SELECT PERCENTILE_DISC(0.25) WITHIN GROUP (ORDER BY amount) FROM t", "m1")
        assert ir.has_complex_sql is True
        assert not any(e.classification == "analytical" for e in ir.select_expressions)

    def test_non_canonical_percentile_stays_passthrough(self):
        ir = parse_sql_to_ir(
            "SELECT PERCENTILE_CONT(0.37) WITHIN GROUP (ORDER BY amount) FROM t", "m1")
        assert ir.has_complex_sql is True
        assert not any(e.classification == "analytical" for e in ir.select_expressions)

    def test_median_exact_grain_reads_p50_column(self):
        ir = parse_sql_to_ir("SELECT dim1, MEDIAN(amount) AS m FROM t GROUP BY dim1", "m1")
        agg = _make_agg(["dim1"], [_make_col("amount", "p50"), _make_col("amount", "sum"), _make_row_count_col()])
        bq = _bind(ir, [("amount", "sum")], ["dim1"])
        sql = rewrite_for_aggregate(bq, agg)
        assert '"amount__p50"' in sql
        assert "MEDIAN" not in sql.upper()

# NOTE (F-003-07): the former ``test_percentile_cont_exact_grain_reads_p90_column``
# was removed. It hand-built a BoundQuery via the ``_bind`` helper with
# ``has_passthrough_expressions``/``has_complex_sql`` defaulted, asserting an
# aggregate-routed PERCENTILE_CONT WITHIN GROUP — a pipeline state the
# production binder never produces (WITHIN GROUP is complex SQL → passthrough,
# per shape catalog #86). The parser-level passthrough behaviour is now pinned
# by ``test_percentile_cont_within_group_is_passthrough`` above. MEDIAN (p50)
# remains the routable percentile path — see
# ``test_median_exact_grain_reads_p50_column`` below.


# ===========================================================================
# Phase E — dispersion stats (STDDEV/VAR) as exact-grain materialised columns
# ===========================================================================

class TestStatParsingAndRewrite:

    def test_stddev_samp_classified(self):
        ir = parse_sql_to_ir("SELECT STDDEV(amount) AS s FROM t", "m1")
        a = [e for e in ir.select_expressions if e.classification == "analytical"]
        assert len(a) == 1
        assert a[0].agg_function == "stddev_samp"  # bare STDDEV == sample
        assert a[0].inner_column == "amount"

    def test_var_pop_classified(self):
        ir = parse_sql_to_ir("SELECT VAR_POP(amount) FROM t", "m1")
        a = [e for e in ir.select_expressions if e.classification == "analytical"]
        assert len(a) == 1
        assert a[0].agg_function == "var_pop"

    def test_stddev_does_not_flag_complex_sql(self):
        """REGRESSION: STDDEV/VAR sqlglot keys (stddevsamp/variance) differ from
        the registry's canonical names; _detect_complex_sql must normalise them
        so the query is NOT forced to passthrough (which would skip routing)."""
        for fn in ("STDDEV(amount)", "STDDEV_POP(amount)", "VAR_SAMP(amount)", "VARIANCE(amount)"):
            ir = parse_sql_to_ir(f"SELECT dim1, {fn} FROM t GROUP BY dim1", "m1")
            assert ir.has_complex_sql is False, fn

    def test_stddev_exact_grain_reads_stat_column(self):
        ir = parse_sql_to_ir("SELECT dim1, STDDEV_SAMP(amount) AS s FROM t GROUP BY dim1", "m1")
        agg = _make_agg(["dim1"], [_make_col("amount", "stddev_samp"), _make_col("amount", "sum"), _make_row_count_col()])
        bq = _bind(ir, [("amount", "sum")], ["dim1"])
        sql = rewrite_for_aggregate(bq, agg)
        assert '"amount__stddev_samp"' in sql
        # the stored column is read directly — no STDDEV function CALL remains
        assert "STDDEV(" not in sql.upper() and "STDDEV_SAMP(" not in sql.upper()

    def test_var_samp_exact_grain_reads_stat_column(self):
        ir = parse_sql_to_ir("SELECT dim1, VARIANCE(amount) AS v FROM t GROUP BY dim1", "m1")
        agg = _make_agg(["dim1"], [_make_col("amount", "var_samp"), _make_row_count_col()])
        bq = _bind(ir, [("amount", "sum")], ["dim1"])
        sql = rewrite_for_aggregate(bq, agg)
        assert '"amount__var_samp"' in sql
        assert "VARIANCE(" not in sql.upper() and "VAR_SAMP(" not in sql.upper()


# ---------------------------------------------------------------------------
# Bug-5349 contract — the exact SQL the agent-service now emits for expression
# dimensions must be flagged as function grain so the binder forces the
# passthrough/source path (binder.has_function_grain). One date/time function
# and one non-date scalar function.
# ---------------------------------------------------------------------------
class TestAgentExpressionDimensionContract:
    def test_date_trunc_grain_sets_function_grain(self):
        sql = (
            'SELECT DATE_TRUNC(\'month\', "business_date") AS "business_date_month", '
            'SUM("amount") AS "amount" FROM "modelx" '
            'GROUP BY DATE_TRUNC(\'month\', "business_date") '
            'ORDER BY DATE_TRUNC(\'month\', "business_date") ASC LIMIT 1001'
        )
        ir = parse_sql_to_ir(sql, "m1")
        assert ir.has_function_grain is True

    def test_scalar_text_function_grain_sets_function_grain(self):
        sql = (
            'SELECT LOWER("city") AS "city_lower", SUM("amount") AS "amount" '
            'FROM "modelx" GROUP BY LOWER("city") ORDER BY LOWER("city") ASC LIMIT 101'
        )
        ir = parse_sql_to_ir(sql, "m1")
        assert ir.has_function_grain is True

    def test_bare_dimension_does_not_set_function_grain(self):
        sql = (
            'SELECT "country_code", SUM("amount") AS "amount" FROM "modelx" '
            'GROUP BY "country_code" ORDER BY "country_code" ASC LIMIT 101'
        )
        ir = parse_sql_to_ir(sql, "m1")
        assert ir.has_function_grain is False

    # --- Bug-5349 Phase 2/3 — WHERE / HAVING / projection / CASE expressions ---
    # The exact SQL the agent-service now emits for these clauses must parse
    # cleanly and bind to the model. None of these clauses are subqueries / CTEs
    # / windows, so the parser must NOT flag them complex-SQL-only-via-those
    # constructs; the function-on-column filters and computed projections route
    # through the source path. (Engine read-only: no parser/binder/router edit.)

    def test_where_function_on_column_parses(self):
        # EXTRACT(MONTH FROM d) = 6 in WHERE — function-on-column filter.
        sql = (
            'SELECT "country_code", SUM("amount") AS "amount" FROM "modelx" '
            'WHERE EXTRACT(MONTH FROM "business_date") = 6 '
            'GROUP BY "country_code" ORDER BY "country_code" ASC LIMIT 101'
        )
        ir = parse_sql_to_ir(sql, "m1")
        assert "country_code" in ir.requested_dimensions
        assert "amount" in ir.requested_measures

    def test_where_column_to_column_parses(self):
        sql = (
            'SELECT SUM("amount") AS "amount" FROM "modelx" '
            'WHERE "settlement_date" > "transaction_date" LIMIT 101'
        )
        ir = parse_sql_to_ir(sql, "m1")
        assert "amount" in ir.requested_measures

    def test_where_or_not_grouped_predicate_parses(self):
        sql = (
            'SELECT SUM("amount") AS "amount" FROM "modelx" '
            'WHERE ("city" = \'cairo\' OR (NOT "city" IS NULL)) LIMIT 101'
        )
        ir = parse_sql_to_ir(sql, "m1")
        # The OR/NOT grouped predicate parses and the measure binds; the grouped
        # boolean is not a subquery/CTE/window so it is not engine-complex SQL.
        assert "amount" in ir.requested_measures
        assert ir.has_complex_sql is False

    def test_having_ratio_of_aggregates_parses(self):
        sql = (
            'SELECT "category", SUM("fees") AS "fees", SUM("amount") AS "amount" '
            'FROM "modelx" GROUP BY "category" '
            'HAVING (SUM("fees") / SUM("amount")) > 0.5 '
            'ORDER BY "category" ASC LIMIT 101'
        )
        ir = parse_sql_to_ir(sql, "m1")
        assert "fees" in ir.requested_measures
        assert "amount" in ir.requested_measures

    def test_projection_case_bucket_parses(self):
        # A CASE computed projection alongside a grouping dimension.
        sql = (
            'SELECT "category", '
            'CASE WHEN "amount" > 1000 THEN \'high\' ELSE \'low\' END AS "band" '
            'FROM "modelx" GROUP BY "category" ORDER BY "category" ASC LIMIT 101'
        )
        ir = parse_sql_to_ir(sql, "m1")
        assert "category" in ir.requested_dimensions

    def test_projection_round_of_sum_parses_as_analytical(self):
        # ROUND(SUM(amount), 2) — the engine already classifies the scalar
        # wrapper around an aggregate as analytical (existing contract above).
        sql = (
            'SELECT "category", ROUND(SUM("amount"), 2) AS "amt" FROM "modelx" '
            'GROUP BY "category" ORDER BY "category" ASC LIMIT 101'
        )
        ir = parse_sql_to_ir(sql, "m1")
        assert "amount" in ir.requested_measures
        analytics = [e for e in ir.select_expressions if e.classification == "analytical"]
        assert any(e.agg_function == "sum" and e.inner_column == "amount" for e in analytics)
