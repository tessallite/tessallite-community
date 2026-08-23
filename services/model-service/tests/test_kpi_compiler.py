"""Unit tests for kpi_compiler.py (Phase 3 — KPI expression-to-SQL compiler)."""
from __future__ import annotations

import pytest

from src.kpi_compiler import (
    _CARRY_FORWARD_ISLAND_COL,
    CompilerContext,
    CompiledQuery,
    compile_expression,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Simple measure compilation
# ---------------------------------------------------------------------------

class TestSimpleMeasure:
    def test_single_measure_default_agg(self):
        result = compile_expression('measure("Revenue")')
        assert "SUM" in result.select_expr
        assert '"Revenue"' in result.select_expr
        assert result.measure_names == ["Revenue"]

    def test_single_measure_with_custom_agg(self):
        ctx = CompilerContext(measure_aggs={"Revenue": "avg"})
        result = compile_expression('measure("Revenue")', ctx)
        assert "AVG" in result.select_expr
        assert '"Revenue"' in result.select_expr

    def test_count_distinct_agg(self):
        ctx = CompilerContext(measure_aggs={"Customers": "count_distinct"})
        result = compile_expression('measure("Customers")', ctx)
        assert "COUNT(DISTINCT" in result.select_expr

    def test_model_slug_in_from(self):
        ctx = CompilerContext(model_slug="acme_sales")
        result = compile_expression('measure("Revenue")', ctx)
        assert '"acme_sales"' in result.sql

    def test_default_model_slug(self):
        result = compile_expression('measure("Revenue")')
        assert '"Model"' in result.sql

    def test_sql_has_value_alias(self):
        result = compile_expression('measure("Revenue")')
        assert "AS value" in result.sql


# ---------------------------------------------------------------------------
# Arithmetic expressions
# ---------------------------------------------------------------------------

class TestArithmetic:
    def test_addition(self):
        result = compile_expression('measure("A") + measure("B")')
        assert "+" in result.select_expr
        assert result.measure_names == ["A", "B"]

    def test_subtraction(self):
        result = compile_expression('measure("A") - measure("B")')
        assert "-" in result.select_expr

    def test_multiplication(self):
        result = compile_expression('measure("A") * 100')
        assert "*" in result.select_expr
        assert "100" in result.select_expr

    def test_unary_minus(self):
        result = compile_expression('-measure("A")')
        assert result.select_expr.startswith("(-")

    def test_number_literal_integer(self):
        result = compile_expression("literal(42)")
        assert "42" in result.select_expr

    def test_number_literal_float(self):
        result = compile_expression("literal(3.14)")
        assert "3.14" in result.select_expr


# ---------------------------------------------------------------------------
# Safe division
# ---------------------------------------------------------------------------

class TestSafeDivision:
    def test_safe_div(self):
        result = compile_expression('safe_div(measure("Revenue"), measure("Headcount"))')
        assert "CASE WHEN" in result.select_expr
        assert "= 0" in result.select_expr
        assert "IS NULL" in result.select_expr
        assert "THEN NULL" in result.select_expr
        assert "1.0" in result.select_expr  # precision cast
        assert result.measure_names == ["Revenue", "Headcount"]

    def test_safe_ratio(self):
        result = compile_expression('safe_ratio(measure("A"), measure("B"))')
        assert "CASE WHEN" in result.select_expr
        assert "THEN NULL" in result.select_expr

    def test_div_with_fallback(self):
        result = compile_expression('div(measure("A"), measure("B"), 0)')
        assert "CASE WHEN" in result.select_expr
        assert "THEN 0" in result.select_expr
        assert result.measure_names == ["A", "B"]


# ---------------------------------------------------------------------------
# Conditional functions
# ---------------------------------------------------------------------------

class TestConditional:
    def test_coalesce(self):
        result = compile_expression('coalesce(measure("A"), measure("B"))')
        assert "COALESCE(" in result.select_expr

    def test_coalesce_three_args(self):
        result = compile_expression('coalesce(measure("A"), measure("B"), literal(0))')
        assert result.select_expr.count(",") == 2

    def test_if_then_else(self):
        result = compile_expression('if_then_else(measure("A"), measure("B"), literal(0))')
        assert "CASE WHEN" in result.select_expr
        assert "<> 0" in result.select_expr


# ---------------------------------------------------------------------------
# Arithmetic functions
# ---------------------------------------------------------------------------

class TestArithmeticFunctions:
    def test_abs(self):
        result = compile_expression('abs(measure("A"))')
        assert "ABS(" in result.select_expr

    def test_round(self):
        result = compile_expression('round(measure("A"), literal(2))')
        assert "ROUND(" in result.select_expr

    def test_min_of(self):
        result = compile_expression('min_of(measure("A"), measure("B"))')
        assert "LEAST(" in result.select_expr

    def test_max_of(self):
        result = compile_expression('max_of(measure("A"), measure("B"))')
        assert "GREATEST(" in result.select_expr


# ---------------------------------------------------------------------------
# Aggregation modes
# ---------------------------------------------------------------------------

class TestAggregationModes:
    def test_aggregate_first_wraps_measures(self):
        ctx = CompilerContext(calc_agg_mode="aggregate_first")
        result = compile_expression('measure("Revenue")', ctx)
        assert "SUM(" in result.select_expr

    def test_row_first_bare_column(self):
        ctx = CompilerContext(calc_agg_mode="row_first")
        result = compile_expression('measure("Revenue")', ctx)
        # For row_first, measure should be bare column, outer agg applied to full expr
        assert '"Revenue"' in result.select_expr
        # The full expression should be wrapped in outer agg
        assert result.select_expr.startswith("SUM(")

    def test_pre_aggregated_applies_outer_agg(self):
        # F-017-24: pre_aggregated with measure() previously emitted a bare,
        # un-aggregated column (an arbitrary row). It must now apply the outer
        # aggregation across rows (default sum when outer_agg unset).
        ctx = CompilerContext(calc_agg_mode="pre_aggregated")
        result = compile_expression('measure("Revenue")', ctx)
        assert result.select_expr == 'SUM("Revenue")'

    def test_pre_aggregated_honours_outer_agg(self):
        # F-017-24: outer_agg drives the across-rows aggregation (avg per the
        # spec's average-per-product margin example).
        ctx = CompilerContext(calc_agg_mode="pre_aggregated", outer_agg="avg")
        result = compile_expression('measure("Margin")', ctx)
        assert result.select_expr == 'AVG("Margin")'

    def test_row_first_honours_outer_agg(self):
        # F-017-24: row_first outer aggregation was hard-coded to the context
        # default; outer_agg=avg must now produce AVG(...) so average-per-row
        # KPIs are expressible.
        ctx = CompilerContext(calc_agg_mode="row_first", outer_agg="avg")
        result = compile_expression('measure("Margin")', ctx)
        assert result.select_expr.startswith("AVG(")

    def test_automatic_wraps_measures(self):
        ctx = CompilerContext(calc_agg_mode="automatic")
        result = compile_expression('measure("Revenue")', ctx)
        assert "SUM(" in result.select_expr


# ---------------------------------------------------------------------------
# KPI and time intelligence references
# ---------------------------------------------------------------------------

class TestKPIReferences:
    def test_kpi_ref_returns_null(self):
        result = compile_expression('kpi("Conversion Rate")')
        assert "NULL" in result.select_expr
        assert result.kpi_names == ["Conversion Rate"]

    def test_kpi_ref_not_compilable(self):
        result = compile_expression('kpi("Conversion Rate")')
        # KPI refs can't be compiled to SQL
        assert result.kpi_names == ["Conversion Rate"]


class TestTimeIntelligence:
    def test_prior_period_sets_flag(self):
        ctx = CompilerContext(time_column="order_date")
        result = compile_expression('prior_period(measure("Revenue"), "month")', ctx)
        assert result.has_time_intelligence is True
        assert "LAG" in result.select_expr

    def test_prior_period_year(self):
        ctx = CompilerContext(time_column="order_date")
        result = compile_expression('prior_period(measure("Revenue"), "year")', ctx)
        assert result.has_time_intelligence is True
        assert "LAG" in result.select_expr

    def test_prior_period_quarter(self):
        ctx = CompilerContext(time_column="order_date")
        result = compile_expression('prior_period(measure("Revenue"), "quarter")', ctx)
        assert result.has_time_intelligence is True
        assert "LAG" in result.select_expr

    def test_prior_period_week(self):
        ctx = CompilerContext(time_column="order_date")
        result = compile_expression('prior_period(measure("Revenue"), "week")', ctx)
        assert result.has_time_intelligence is True
        assert "LAG" in result.select_expr

    def test_pct_change_compiles(self):
        ctx = CompilerContext(time_column="order_date")
        result = compile_expression('pct_change(measure("Revenue"), "month")', ctx)
        assert result.has_time_intelligence is True
        assert "LAG" in result.select_expr
        assert "NULLIF" in result.select_expr

    def test_moving_avg_compiles(self):
        ctx = CompilerContext(time_column="order_date")
        result = compile_expression('moving_avg(measure("Revenue"), literal(3), "month")', ctx)
        assert result.has_time_intelligence is True
        assert "AVG" in result.select_expr
        assert "ROWS BETWEEN" in result.select_expr

    def test_trailing_sum_compiles(self):
        ctx = CompilerContext(time_column="order_date")
        result = compile_expression('trailing_sum(measure("Revenue"), literal(6), "month")', ctx)
        assert result.has_time_intelligence is True
        assert "SUM" in result.select_expr
        assert "ROWS BETWEEN" in result.select_expr

    def test_lag_compiles(self):
        ctx = CompilerContext(time_column="order_date")
        result = compile_expression('lag(measure("Revenue"), literal(1), "month")', ctx)
        assert result.has_time_intelligence is True
        assert "LAG" in result.select_expr

    def test_lead_compiles(self):
        ctx = CompilerContext(time_column="order_date")
        result = compile_expression('lead(measure("Revenue"), literal(1), "month")', ctx)
        assert result.has_time_intelligence is True
        assert "LEAD" in result.select_expr

    def test_cagr_compiles_at_year_grain(self):
        # Bug-6645: CAGR requires month or coarser grain. When no grain is
        # derivable from the expression, the emitter rejects the variant
        # (VariantSqlError) and the compiler falls back to NULL (the
        # Python-side evaluator handles it instead). Supply an explicit year
        # grain via the third argument to prove the SQL path works.
        ctx = CompilerContext(time_column="order_date")
        result = compile_expression(
            'cagr(measure("Revenue"), literal(3), "year")', ctx
        )
        assert result.has_time_intelligence is True
        assert "POWER" in result.select_expr

    def test_cagr_without_grain_returns_null(self):
        # Bug-6645: CAGR with no derivable grain defaults to day, which is
        # rejected by the emitter (VariantSqlError). The compiler catches it
        # and returns "NULL" as the select expression. The compiled query
        # still carries has_time_intelligence=True so KPI metadata is correct,
        # but the SQL evaluates to NULL (no value displayed).
        ctx = CompilerContext(time_column="order_date")
        result = compile_expression('cagr(measure("Revenue"), literal(3))', ctx)
        assert result.has_time_intelligence is True
        assert result.select_expr == "NULL"

    def test_period_to_date_ytd(self):
        ctx = CompilerContext(time_column="order_date")
        result = compile_expression('period_to_date(measure("Revenue"), "year")', ctx)
        assert result.has_time_intelligence is True
        assert "SUM" in result.select_expr
        assert "UNBOUNDED PRECEDING" in result.select_expr

    def test_period_to_date_qtd(self):
        ctx = CompilerContext(time_column="order_date")
        result = compile_expression('period_to_date(measure("Revenue"), "quarter")', ctx)
        assert result.has_time_intelligence is True
        assert "SUM" in result.select_expr

    def test_period_to_date_mtd(self):
        ctx = CompilerContext(time_column="order_date")
        result = compile_expression('period_to_date(measure("Revenue"), "month")', ctx)
        assert result.has_time_intelligence is True

    def test_fiscal_period_to_date(self):
        ctx = CompilerContext(
            time_column="order_date",
            calendar_type="fiscal",
            fiscal_year_start_month=4,
        )
        result = compile_expression('fiscal_period_to_date(measure("Revenue"), "year")', ctx)
        assert result.has_time_intelligence is True
        assert "SUM" in result.select_expr

    def test_kpi_inside_time_function_falls_back(self):
        """kpi() refs inside time functions cannot be compiled to SQL."""
        ctx = CompilerContext(time_column="order_date")
        result = compile_expression('prior_period(kpi("Child KPI"), "month")', ctx)
        assert result.has_time_intelligence is True
        assert "NULL" in result.select_expr
        assert result.kpi_names == ["Child KPI"]

    def test_no_time_column_fails_closed(self):
        """Bug-8573/Bug-9233: without a time_column, time intelligence REFUSES.

        Superseded ``test_no_time_column_defaults``, which pinned the old
        ``ctx.time_column or "date"`` fallback as intended behaviour. That
        default anchored a LAG/YTD period boundary on a column named ``date``
        that is not the KPI's time dimension — on a model whose fact date is
        ``business_date`` the period offset was computed from the wrong column
        and the KPI reported a plausible, wrong number.

        Test escape: coverage ENSHRINED the defect. Guard: this assertion.
        Tier: T2.
        """
        from src.kpi_compiler import KPITimeContextError

        with pytest.raises(KPITimeContextError):
            compile_expression('lag(measure("Revenue"), literal(1), "month")')

    def test_time_column_is_used_verbatim_not_a_default(self):
        """The variant binds the KPI's OWN time column, never a literal 'date'."""
        ctx = CompilerContext(time_column="business_date")
        result = compile_expression(
            'lag(measure("Revenue"), literal(1), "month")', ctx
        )
        assert result.has_time_intelligence is True
        assert '"business_date"' in result.select_expr
        assert '"date"' not in result.select_expr

    def test_prior_period_default_grain(self):
        ctx = CompilerContext(time_column="order_date")
        result = compile_expression('prior_period(measure("Revenue"))', ctx)
        assert result.has_time_intelligence is True
        assert "LAG" in result.select_expr

    def test_ratio_with_prior_period(self):
        """Real-world: safe_div(measure, prior_period(measure)) for growth ratio."""
        ctx = CompilerContext(time_column="order_date")
        result = compile_expression(
            'safe_div(measure("Revenue"), prior_period(measure("Revenue"), "month"))', ctx
        )
        assert result.has_time_intelligence is True
        assert "CASE WHEN" in result.select_expr
        assert "LAG" in result.select_expr


# ---------------------------------------------------------------------------
# Complex expressions (real-world KPI patterns)
# ---------------------------------------------------------------------------

class TestComplexExpressions:
    def test_ratio_kpi(self):
        """Revenue per Headcount."""
        result = compile_expression(
            'safe_div(measure("Revenue"), measure("Headcount"))'
        )
        assert "CASE WHEN" in result.select_expr
        assert result.measure_names == ["Revenue", "Headcount"]
        assert not result.has_time_intelligence
        assert not result.kpi_names

    def test_margin_kpi(self):
        """Gross Margin = (Revenue - COGS) / Revenue."""
        result = compile_expression(
            'safe_div(measure("Revenue") - measure("COGS"), measure("Revenue"))'
        )
        assert "CASE WHEN" in result.select_expr
        assert "-" in result.select_expr
        assert set(result.measure_names) == {"Revenue", "COGS"}

    def test_variance_kpi(self):
        """Variance = Actual - Budget."""
        result = compile_expression('measure("Actual") - measure("Budget")')
        assert "-" in result.select_expr
        assert result.measure_names == ["Actual", "Budget"]

    def test_weighted_composite(self):
        """Weighted composite: 0.6 * A + 0.4 * B."""
        result = compile_expression(
            'literal(0.6) * measure("A") + literal(0.4) * measure("B")'
        )
        assert "0.6" in result.select_expr
        assert "0.4" in result.select_expr
        assert result.measure_names == ["A", "B"]


# ---------------------------------------------------------------------------
# NULL and edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_measure_name_with_spaces(self):
        result = compile_expression('measure("Total Revenue")')
        assert '"Total Revenue"' in result.select_expr

    def test_measure_name_with_special_chars(self):
        result = compile_expression('measure("Revenue (Net)")')
        # The identifier should be properly quoted
        assert "Revenue (Net)" in result.select_expr

    def test_nested_safe_div(self):
        result = compile_expression(
            'safe_div(safe_div(measure("A"), measure("B")), measure("C"))'
        )
        assert result.select_expr.count("CASE WHEN") == 2

    def test_deduplicate_measure_names(self):
        """Same measure referenced twice should appear once in measure_names."""
        result = compile_expression(
            'safe_div(measure("Revenue") - measure("COGS"), measure("Revenue"))'
        )
        assert result.measure_names.count("Revenue") == 1

    def test_empty_kpi_names_for_measure_only(self):
        result = compile_expression('measure("Revenue")')
        assert result.kpi_names == []

    def test_agg_mode_in_result(self):
        ctx = CompilerContext(calc_agg_mode="aggregate_first")
        result = compile_expression('measure("Revenue")', ctx)
        assert result.agg_mode == "aggregate_first"


# ---------------------------------------------------------------------------
# Semi-additive compilation
# ---------------------------------------------------------------------------

class TestSemiAdditive:
    @staticmethod
    def _inner_sql(sql: str) -> str:
        return sql.split("FROM (", 1)[1].split(") AS sub", 1)[0]

    def test_last_non_additive_uses_order_limit(self):
        ctx = CompilerContext(
            non_additive_agg="last",
            at_grain="month",
            time_column="order_date",
        )
        result = compile_expression('measure("Balance")', ctx)
        assert "ORDER BY" in result.sql
        assert "DESC" in result.sql
        assert "LIMIT 1" in result.sql
        # sqlglot may normalize "IS NOT NULL" to "NOT x IS NULL"
        assert "IS NOT NULL" in result.sql or "NOT" in result.sql and "IS NULL" in result.sql
        assert "GROUP BY" in result.sql
        inner_sql = self._inner_sql(result.sql)
        assert 'AS inner_val, "order_date"' in inner_sql
        assert 'GROUP BY DATE_TRUNC(\'MONTH\', "order_date"), "order_date"' in inner_sql

    def test_first_non_additive(self):
        ctx = CompilerContext(
            non_additive_agg="first",
            at_grain="day",
            time_column="order_date",
        )
        result = compile_expression('measure("Balance")', ctx)
        assert "ORDER BY" in result.sql
        assert "ASC" in result.sql
        assert "LIMIT 1" in result.sql

    def test_min_non_additive(self):
        ctx = CompilerContext(
            non_additive_agg="min",
            at_grain="day",
            time_column="order_date",
        )
        result = compile_expression('measure("Balance")', ctx)
        assert "MIN(" in result.sql
        assert "GROUP BY" in result.sql

    def test_max_non_additive(self):
        ctx = CompilerContext(
            non_additive_agg="max",
            at_grain="day",
            time_column="order_date",
        )
        result = compile_expression('measure("Balance")', ctx)
        assert "MAX(" in result.sql
        assert "GROUP BY" in result.sql

    def test_avg_non_additive_uses_outer_avg(self):
        """Bug-6252: avg must average per-grain values, not fall through to
        the unknown-aggregation SUM fallback."""
        ctx = CompilerContext(
            non_additive_agg="avg",
            at_grain="month",
            time_column="order_date",
        )
        result = compile_expression('measure("Balance")', ctx)
        outer_select = result.sql.upper().split(" FROM ", 1)[0]
        assert "AVG(" in outer_select
        assert "SUM(" not in outer_select

    @pytest.mark.parametrize("agg, outer_func", [
        ("avg", "AVG"),
        ("min", "MIN"),
        ("max", "MAX"),
    ])
    def test_reducing_non_additive_groups_by_bucket_not_raw_time(
        self, agg, outer_func
    ):
        """Bug-6252: reducing semi-additive aggregations must reduce one
        inner value per requested grain bucket. Grouping by the raw time column
        would average/min/max time-points inside the bucket instead."""
        ctx = CompilerContext(
            non_additive_agg=agg,
            at_grain="month",
            time_column="order_date",
        )
        result = compile_expression('measure("Balance")', ctx)
        outer_select = result.sql.upper().split(" FROM ", 1)[0]
        assert f"{outer_func}(INNER_VAL)" in outer_select
        inner_sql = self._inner_sql(result.sql)
        assert 'AS inner_val, "order_date"' not in inner_sql
        assert inner_sql.endswith('GROUP BY DATE_TRUNC(\'MONTH\', "order_date")')

    def test_at_grain_keyword_date_truncs_time_column(self):
        """Bug-6252: at_grain is a grain keyword, not a required physical
        column named "month"."""
        ctx = CompilerContext(
            non_additive_agg="avg",
            at_grain="month",
            time_column="business_date",
        )
        sql = compile_expression('measure("Balance")', ctx).sql.upper()
        assert "DATE_TRUNC('MONTH'" in sql
        assert '"MONTH"' not in sql

    def test_at_grain_raw_column_fails_closed(self):
        """Bug-8573: a non-keyword at_grain must REFUSE, not group by that column.

        Superseded ``test_at_grain_real_column_not_truncated``, which pinned the
        raw-column form as intended. Grouping the inner query by
        ``(reporting_bucket, business_date)`` makes the first/last outer
        ``ORDER BY business_date LIMIT 1`` pick ONE ARBITRARY row out of the
        whole table instead of reducing per period, and for avg/min/max it
        reduces over the wrong buckets entirely.

        Test escape: coverage ENSHRINED the defect. Guard: this assertion +
        ``_validate_kpi_at_grain`` at the API boundary. Tier: T2.
        """
        from src.kpi_compiler import KPITimeContextError

        ctx = CompilerContext(
            non_additive_agg="avg",
            at_grain="reporting_bucket",
            time_column="business_date",
        )
        with pytest.raises(KPITimeContextError):
            compile_expression('measure("Balance")', ctx)

    def test_at_grain_keyword_is_case_and_space_insensitive(self):
        """Bug-8573: a persisted " Month " still buckets, it does not fall through."""
        ctx = CompilerContext(
            non_additive_agg="avg",
            at_grain=" Month ",
            time_column="business_date",
        )
        sql = compile_expression('measure("Balance")', ctx).sql
        # sqlglot normalises the unit literal's case on transpile.
        assert "DATE_TRUNC('MONTH', \"business_date\")" in sql
        assert '"reporting' not in sql

    def test_semi_additive_has_subquery(self):
        ctx = CompilerContext(
            non_additive_agg="last",
            at_grain="month",
            time_column="order_date",
        )
        result = compile_expression('measure("Balance")', ctx)
        assert "sub" in result.sql
        assert "inner_val" in result.sql

    def test_semi_additive_without_time_column_fails_closed(self):
        """Bug-8573/Bug-9233: no time column => refuse, never a literal 'date'.

        Superseded ``test_semi_additive_default_time_column``. A closing-balance
        KPI reduced over a column named ``date`` that is not the KPI's time
        dimension returns a plausible, wrong number (or a cryptic source error
        when no such column exists).

        Test escape: coverage ENSHRINED the defect. Guard: this assertion.
        Tier: T2.
        """
        from src.kpi_compiler import KPITimeContextError

        ctx = CompilerContext(
            non_additive_agg="last",
            at_grain="month",
        )
        with pytest.raises(KPITimeContextError):
            compile_expression('measure("Balance")', ctx)

    def test_semi_additive_without_at_grain_buckets_by_the_time_column(self):
        """Half-configured KPI (agg only): "last value by date" uses the KPI's
        own time column, not a column literally named ``date`` (Bug-8573)."""
        ctx = CompilerContext(
            non_additive_agg="last",
            time_column="business_date",
        )
        sql = compile_expression('measure("Balance")', ctx).sql
        assert '"business_date"' in sql
        assert '"date"' not in sql.replace('"business_date"', "")

    def test_semi_additive_unknown_agg_fails_loud(self):
        """Bug-6252: an unknown non_additive_agg must FAIL, not fall back to SUM.

        This test previously asserted the opposite ("Unknown non_additive_agg
        falls back to SUM") and so pinned the defect as intended behaviour. The
        fallback was a silent wrong-numbers bug: summing a column of per-period
        balances reports the sum of every day's balance instead of the closing
        balance, which is the exact failure semi-additive support exists to
        prevent. ``non_additive_agg`` is now a closed enum at the API boundary
        and is re-checked here, so an unrecognised token cannot compile at all.

        Test escape (recorded per policy): the escape was not missing coverage —
        existing coverage ENSHRINED the defect. Guard: this assertion plus
        ``test_bug6252_kpi_semi_additive_vocabulary.py``. Tier: T1.
        """
        ctx = CompilerContext(
            non_additive_agg="unknown_agg",
            at_grain="month",
            time_column="order_date",
        )
        with pytest.raises(ValueError, match="not supported"):
            compile_expression('measure("Balance")', ctx)

    def test_semi_additive_explicit_sum_still_sums(self):
        """``sum`` remains a legitimate EXPLICIT choice — Bug-6252 removed the
        silent fallback, not the option."""
        ctx = CompilerContext(
            non_additive_agg="sum",
            at_grain="month",
            time_column="order_date",
        )
        result = compile_expression('measure("Balance")', ctx)
        assert "SUM(" in result.sql

    # -- carry-forward (Bug-9482) ------------------------------------------
    #
    # The two tests that used to live here asserted ``"COALESCE" in sql`` and
    # ``"ARRAY_AGG" in sql`` and never executed anything. Both passed against an
    # emission PostgreSQL rejects on EVERY version, so they ENSHRINED the defect
    # rather than missing it. They are replaced by:
    #   * the structural invariant below, which is what the defect actually
    #     violated (a carry-forward window whose argument is an AGGREGATE), and
    #   * KNOWN-VALUE tests that RUN the SQL on real Postgres, in
    #     tests/integration/test_kpi_period_and_semi_additive_values_db.py.
    # A shape assertion is not admissible evidence for this bug: only executing
    # it is.

    def _aggregates_inside_windows(self, sql: str):
        """Aggregates nested INSIDE a window construct.

        An aggregate used AS a window function (``MAX(x) OVER (...)``) is legal
        and expected. What PostgreSQL refuses is an aggregate anywhere INSIDE
        one: as the windowed function's argument, in its PARTITION BY, or in its
        ORDER BY. That is exactly the old emission, so it is exactly what this
        looks for.
        """
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(sql, read="postgres")
        found = []
        for window in tree.find_all(exp.Window):
            found.extend(
                node for node in window.find_all(exp.AggFunc)
                if node is not window.this
            )
        return found

    def test_carry_forward_never_puts_an_aggregate_inside_a_window(self):
        """Bug-9482: the exact construct PostgreSQL refuses, AND the fill is
        actually emitted.

        The old emission was
        ``COALESCE(SUM(x), LAST_VALUE(SUM(x) IGNORE NULLS) OVER (...))`` — an
        aggregate as the argument of a window function, which the PostgreSQL
        pre-pass then copied into ``FILTER (WHERE SUM(x) IS NOT NULL)``.
        PostgreSQL answers "aggregate functions are not allowed in FILTER" and
        "aggregate ORDER BY is not implemented for window functions". The fill
        must therefore operate one scope OUT, on the per-period column, so no
        window argument may contain an aggregate.

        L7R-01: those two assertions are both NEGATIVE, and a negative
        invariant is vacuously satisfied by emitting no fill at all. Replacing
        ``_carry_forward_scope`` with the identity function left 288 unit tests
        green, including every test in this class. The positive assertion below
        — the island column the fill scope introduces is present — is what
        makes this class notice a fill that silently stopped being applied.

        (The deleted ``test_carry_forward_with_semi_additive`` asserted only
        ``COALESCE`` and ``GROUP BY``, never ``ARRAY_AGG``, so it did NOT
        enshrine the broken shape and would still pass verbatim. It is not
        restored because the ``at_grain='month', non_additive_agg='last'``
        context below now asserts strictly more than it did.)
        """
        for ctx in (
            CompilerContext(carry_forward=True, time_column="order_date"),
            CompilerContext(
                carry_forward=True, time_column="order_date", at_grain="day",
            ),
            CompilerContext(
                carry_forward=True, time_column="order_date",
                at_grain="month", non_additive_agg="avg",
            ),
            CompilerContext(
                carry_forward=True, time_column="order_date",
                at_grain="month", non_additive_agg="last",
            ),
            CompilerContext(
                calc_agg_mode="row_first", carry_forward=True,
                time_column="order_date", inner_agg="sum",
                inner_grain="month", outer_agg="avg",
            ),
        ):
            sql = compile_expression('measure("Balance")', ctx).sql
            nested = self._aggregates_inside_windows(sql)
            assert not nested, (
                "a carry-forward window contains an AGGREGATE again "
                f"({[n.sql() for n in nested]}) — PostgreSQL rejects this "
                f"SQL: {sql}"
            )
            assert "IGNORE NULLS" not in sql.upper(), (
                "the emission depends on the PostgreSQL IGNORE NULLS pre-pass "
                f"again, which produces unexecutable SQL: {sql}"
            )
            assert _CARRY_FORWARD_ISLAND_COL in sql, (
                "carry_forward was authored but NO fill scope was emitted: the "
                f"island column {_CARRY_FORWARD_ISLAND_COL!r} is absent, so "
                "this KPI silently serves the un-filled number while the two "
                f"assertions above pass vacuously (L7R-01). SQL: {sql}"
            )

    def test_carry_forward_requires_a_time_column(self):
        """Bug-8573 / Bug-9233, the fourth ``_require_time_column`` caller.

        A gap-fill is ORDERED BY the KPI's time dimension. With none bound there
        is no correct default: the old hard-coded ``"date"`` either referenced a
        column that does not exist or filled in the order of a column that is
        not this KPI's time dimension. Fail closed.
        """
        from src.kpi_compiler import KPITimeContextError

        with pytest.raises(KPITimeContextError, match="carry-forward"):
            compile_expression(
                'measure("Balance")', CompilerContext(carry_forward=True),
            )

    def test_the_grain_vocabulary_has_one_owner(self):
        """L7-R5 — the compiler must IMPORT the gated ``at_grain`` vocabulary.

        ``_validate_kpi_at_grain`` gates the API against
        ``_KPI_AT_GRAIN_KEYWORDS``; the compiler decides, from the same word,
        whether to emit ``DATE_TRUNC('<grain>', <time col>)`` or to treat the
        value as a COLUMN NAME. A private duplicate lets the two disagree about
        which values are grains, and the compiler's answer is what the SQL
        actually GROUPS BY. Object identity, not equality — the Bug-6574 pattern
        — because two equal-today sets are exactly what drifts.

        Import direction is service -> shared. ``shared/`` importing from a
        service would invert the dependency and break every other consumer.
        """
        from shared.schemas.domains.governance_advanced import (
            _KPI_AT_GRAIN_KEYWORDS,
        )
        from src import kpi_compiler as compiler_mod

        assert compiler_mod._GRAIN_KEYWORDS is _KPI_AT_GRAIN_KEYWORDS, (
            "kpi_compiler holds its own copy of the grain vocabulary again; the "
            "API validator and the compiler then disagree about which at_grain "
            "values are grain keywords and which are column names (L7-R5)"
        )

    def test_carry_forward_transpiles_to_every_supported_dialect(self):
        """Bug-9482: the fill is ordinary ANSI SQL, so ONE emission serves every
        dialect and no per-connector branch is needed (SQL rule 1)."""
        for dialect in (
            "postgresql", "bigquery", "snowflake", "sqlserver",
            "hadoop_spark", "redshift",
        ):
            ctx = CompilerContext(
                dialect=dialect, carry_forward=True, time_column="order_date",
                at_grain="month", non_additive_agg="avg",
            )
            sql = compile_expression('measure("Balance")', ctx).sql
            assert sql, f"empty transpilation for {dialect}"
            assert "IGNORE NULLS" not in sql.upper(), dialect

    def test_no_semi_additive_when_fields_absent(self):
        """Without at_grain/non_additive_agg, no subquery wrapping."""
        result = compile_expression('measure("Revenue")')
        assert "sub" not in result.sql
        assert "inner_val" not in result.sql


# ---------------------------------------------------------------------------
# Aggregate-of-aggregate compilation
# ---------------------------------------------------------------------------

class TestAggregateOfAggregate:
    def test_inner_sum_outer_avg(self):
        ctx = CompilerContext(
            inner_agg="sum",
            inner_grain="month",
            outer_agg="avg",
            time_column="order_date",
        )
        result = compile_expression('measure("Revenue")', ctx)
        assert "AVG(" in result.sql
        assert "SUM(" in result.sql
        assert "GROUP BY" in result.sql
        assert "inner_val" in result.sql
        assert "sub" in result.sql

    def test_inner_avg_outer_max(self):
        ctx = CompilerContext(
            inner_agg="avg",
            inner_grain="quarter",
            outer_agg="max",
            time_column="business_date",
        )
        result = compile_expression('measure("Revenue")', ctx)
        assert "MAX(" in result.sql
        assert "AVG(" in result.sql
        assert "GROUP BY" in result.sql
        # F-017-21: a grain keyword ("quarter") is DATE_TRUNC'd against the time
        # column, not treated as a literal column name. (sqlglot uppercases the
        # grain literal during transpile.)
        assert "DATE_TRUNC('QUARTER'" in result.sql.upper()
        assert '"business_date"' in result.sql

    def test_inner_count_outer_sum(self):
        ctx = CompilerContext(
            inner_agg="count",
            inner_grain="day",
            outer_agg="sum",
            time_column="business_date",
        )
        result = compile_expression('measure("Orders")', ctx)
        assert "SUM(" in result.sql
        assert "COUNT(" in result.sql
        # F-017-21: grain keyword -> DATE_TRUNC, not a raw column reference.
        assert "DATE_TRUNC('DAY'" in result.sql.upper()

    def test_default_inner_grain_uses_time_column(self):
        """When inner_grain is None, falls back to time_column."""
        ctx = CompilerContext(
            inner_agg="sum",
            outer_agg="avg",
            time_column="order_date",
        )
        result = compile_expression('measure("Revenue")', ctx)
        assert '"order_date"' in result.sql

    def test_default_inner_grain_without_time_column_fails_closed(self):
        """Bug-8573/Bug-9233: no inner_grain AND no time column => refuse.

        Superseded ``test_default_inner_grain_uses_date``, which pinned the
        ``or "date"`` fallback. An aggregate-of-aggregate bucketed on a column
        that is not the KPI's time dimension changes the inner grouping and
        therefore the outer average — a plausible, wrong number.

        Test escape: coverage ENSHRINED the defect. Guard: this assertion.
        Tier: T2.
        """
        from src.kpi_compiler import KPITimeContextError

        ctx = CompilerContext(
            inner_agg="sum",
            outer_agg="avg",
        )
        with pytest.raises(KPITimeContextError):
            compile_expression('measure("Revenue")', ctx)

    def test_default_inner_grain_uses_the_kpi_time_column(self):
        ctx = CompilerContext(
            inner_agg="sum",
            outer_agg="avg",
            time_column="business_date",
        )
        sql = compile_expression('measure("Revenue")', ctx).sql
        assert '"business_date"' in sql
        assert "DATE_TRUNC" not in sql.upper()

    def test_agg_of_agg_takes_priority_over_semi_additive(self):
        """aggregate_of_aggregate should take priority when both are set."""
        ctx = CompilerContext(
            inner_agg="sum",
            inner_grain="month",
            outer_agg="avg",
            time_column="business_date",
            non_additive_agg="last",
            at_grain="day",
        )
        result = compile_expression('measure("Revenue")', ctx)
        # Should use agg-of-agg pattern, not semi-additive
        assert "inner_val" in result.sql
        assert "GROUP BY" in result.sql


# ---------------------------------------------------------------------------
# Dialect transpilation
# ---------------------------------------------------------------------------

class TestDialectTranspilation:
    def test_postgres_no_change(self):
        ctx = CompilerContext(dialect="postgresql")
        result = compile_expression('measure("Revenue")', ctx)
        assert '"Revenue"' in result.sql
        assert "SUM" in result.sql

    def test_bigquery_transpile(self):
        ctx = CompilerContext(dialect="bigquery")
        result = compile_expression('measure("Revenue")', ctx)
        # BigQuery uses backticks for identifiers
        assert "Revenue" in result.sql

    def test_spark_transpile(self):
        ctx = CompilerContext(dialect="hadoop_spark")
        result = compile_expression('measure("Revenue")', ctx)
        # Spark uses backticks for identifiers
        assert "Revenue" in result.sql

    def test_snowflake_transpile(self):
        ctx = CompilerContext(dialect="snowflake")
        result = compile_expression('measure("Revenue")', ctx)
        assert "Revenue" in result.sql

    def test_unknown_dialect_no_change(self):
        """Unknown dialect should not crash — returns unchanged SQL."""
        ctx = CompilerContext(dialect="unknown_db")
        result = compile_expression('measure("Revenue")', ctx)
        assert "Revenue" in result.sql

    def test_semi_additive_with_bigquery_dialect(self):
        """Semi-additive SQL should transpile for BigQuery."""
        ctx = CompilerContext(
            non_additive_agg="last",
            at_grain="month",
            time_column="order_date",
            dialect="bigquery",
        )
        result = compile_expression('measure("Balance")', ctx)
        # Should compile without error and contain key patterns
        assert "Balance" in result.sql
        assert "ORDER BY" in result.sql

    def test_agg_of_agg_with_spark_dialect(self):
        """Aggregate-of-aggregate SQL should transpile for Spark."""
        ctx = CompilerContext(
            inner_agg="sum",
            inner_grain="month",
            outer_agg="avg",
            time_column="business_date",
            dialect="hadoop_spark",
        )
        result = compile_expression('measure("Revenue")', ctx)
        assert "Revenue" in result.sql


# ---------------------------------------------------------------------------
# Time-intelligence subquery path (business builder)
# ---------------------------------------------------------------------------

class TestTimeIntelligenceSubquery:
    """Tests for the legacy enable_ti_subquery path (window-function subquery).

    When enable_ti_subquery is True (without CTE metadata), the compiler
    wraps time-intelligence expressions in a GROUP BY subquery.
    """

    def test_prior_period_generates_subquery(self):
        ctx = CompilerContext(
            time_column="order_date",
            enable_ti_subquery=True,
        )
        result = compile_expression('prior_period(measure("Revenue"), "month")', ctx)
        assert result.has_time_intelligence
        assert "GROUP BY" in result.sql
        assert "_ti" in result.sql
        assert "ORDER BY" in result.sql
        assert "LIMIT 1" in result.sql

    def test_no_subquery_without_flag(self):
        ctx = CompilerContext(
            time_column="order_date",
            enable_ti_subquery=False,
        )
        result = compile_expression('prior_period(measure("Revenue"), "month")', ctx)
        assert "GROUP BY" not in result.sql
        assert "_ti" not in result.sql

    def test_no_subquery_for_non_ti_expression(self):
        ctx = CompilerContext(
            time_column="order_date",
            enable_ti_subquery=True,
            filter_where_clause='"country" = \'Germany\'',
        )
        result = compile_expression('measure("Revenue")', ctx)
        assert not result.has_time_intelligence
        assert "Germany" in result.sql
        assert "_ti" not in result.sql

    def test_non_ti_combines_filter_and_time_where(self):
        ctx = CompilerContext(
            time_column="order_date",
            enable_ti_subquery=True,
            filter_where_clause='"country" = \'Germany\'',
            time_where_clause='"order_date" >= \'2025-01-01\'',
        )
        result = compile_expression('measure("Revenue")', ctx)
        assert "Germany" in result.sql
        assert "2025-01-01" in result.sql
        assert "AND" in result.sql


class TestCteScalarKpi:
    """Tests for CTE-based scalar KPI SQL (business builder path).

    When ti_type and base_expression are set on CompilerContext, the compiler
    generates CTE-based scalar SQL that always produces one row, one column.
    """

    def test_prior_period_cte(self):
        ctx = CompilerContext(
            time_column="order_date",
            ti_type="prior_period",
            ti_grain="month",
            base_expression='measure("Revenue")',
            time_window_start_sql="DATE_TRUNC('month', CURRENT_DATE)",
            time_window_end_sql="CURRENT_DATE",
        )
        result = compile_expression('prior_period(measure("Revenue"), "month")', ctx)
        assert "WITH" in result.sql
        assert "current_p" in result.sql
        assert "prior_p" in result.sql
        assert "INTERVAL" in result.sql
        assert "SELECT value FROM prior_p" in result.sql

    def test_growth_pct_cte(self):
        ctx = CompilerContext(
            time_column="order_date",
            ti_type="growth_pct",
            ti_grain="year",
            base_expression='measure("Revenue")',
            time_window_start_sql="DATE_TRUNC('year', CURRENT_DATE)",
            time_window_end_sql="CURRENT_DATE",
        )
        result = compile_expression(
            'safe_div(measure("Revenue") - prior_period(measure("Revenue"), "year"), '
            'abs(prior_period(measure("Revenue"), "year")))',
            ctx,
        )
        assert "current_p" in result.sql
        assert "prior_p" in result.sql
        assert "ABS" in result.sql
        assert "1 year" in result.sql.lower()

    def test_period_to_date_cte(self):
        ctx = CompilerContext(
            time_column="order_date",
            ti_type="period_to_date",
            ti_grain="year",
            base_expression='measure("Revenue")',
        )
        result = compile_expression('period_to_date(measure("Revenue"), "year")', ctx)
        assert "DATE_TRUNC" in result.sql
        assert "CURRENT_DATE" in result.sql
        assert "WITH" not in result.sql

    def test_moving_avg_cte(self):
        ctx = CompilerContext(
            time_column="order_date",
            ti_type="moving_avg",
            ti_grain="month",
            ti_n_periods=3,
            base_expression='measure("Revenue")',
            time_window_start_sql="DATE_TRUNC('month', CURRENT_DATE - INTERVAL '3 months')",
            time_window_end_sql="DATE_TRUNC('month', CURRENT_DATE) - INTERVAL '1 day'",
        )
        result = compile_expression('moving_avg(measure("Revenue"), "month", literal(3))', ctx)
        assert "period_values" in result.sql
        assert "AVG(value)" in result.sql
        assert "GROUP BY" in result.sql
        assert "LIMIT 3" in result.sql

    def test_trailing_sum_cte(self):
        ctx = CompilerContext(
            time_column="order_date",
            ti_type="trailing_sum",
            ti_grain="month",
            ti_n_periods=6,
            base_expression='measure("Revenue")',
            time_window_start_sql="DATE_TRUNC('month', CURRENT_DATE - INTERVAL '6 months')",
            time_window_end_sql="DATE_TRUNC('month', CURRENT_DATE) - INTERVAL '1 day'",
        )
        result = compile_expression('trailing_sum(measure("Revenue"), "month", literal(6))', ctx)
        assert "period_values" in result.sql
        assert "SUM(value)" in result.sql
        assert "LIMIT 6" in result.sql

    def test_filter_where_in_cte(self):
        ctx = CompilerContext(
            time_column="order_date",
            ti_type="prior_period",
            ti_grain="month",
            base_expression='measure("Revenue")',
            filter_where_clause='"country" = \'Germany\'',
            time_window_start_sql="DATE_TRUNC('month', CURRENT_DATE)",
            time_window_end_sql="CURRENT_DATE",
        )
        result = compile_expression('prior_period(measure("Revenue"), "month")', ctx)
        assert "Germany" in result.sql
        assert result.sql.count("Germany") == 2

    def test_cte_not_used_without_ti_metadata(self):
        ctx = CompilerContext(
            time_column="order_date",
        )
        result = compile_expression('measure("Revenue")', ctx)
        assert "WITH" not in result.sql
        assert "current_p" not in result.sql
        assert "AS value" in result.sql

    def test_cte_with_custom_range_bounds(self):
        ctx = CompilerContext(
            time_column="order_date",
            ti_type="prior_period",
            ti_grain="month",
            base_expression='measure("Revenue")',
            time_window_start_sql="'2025-01-01'",
            time_window_end_sql="'2025-01-31'",
        )
        result = compile_expression('prior_period(measure("Revenue"), "month")', ctx)
        assert "2025-01-01" in result.sql
        assert "2025-01-31" in result.sql
        assert "INTERVAL" in result.sql

    def test_cte_uses_exclusive_upper_bound(self):
        ctx = CompilerContext(
            time_column="order_date",
            ti_type="prior_period",
            ti_grain="month",
            base_expression='measure("Revenue")',
            time_window_start_sql="DATE_TRUNC('month', CURRENT_DATE)",
            time_window_end_sql="DATE_TRUNC('month', CURRENT_DATE) + INTERVAL '1 month'",
        )
        result = compile_expression('prior_period(measure("Revenue"), "month")', ctx)
        assert "< DATE_TRUNC" in result.sql
        assert "<= DATE_TRUNC" not in result.sql

    def test_share_rank_returns_rank_value(self):
        ctx = CompilerContext(
            model_slug="sales_model",
            share_type="rank",
            share_dimension="Country",
            base_expression='measure("Revenue")',
        )
        result = compile_expression('rank_over(measure("Revenue"))', ctx)
        assert "RANK()" in result.sql
        assert "ORDER BY dim_value DESC" in result.sql
        assert "MIN(rnk) AS value" in result.sql

    def test_share_rank_with_member_filter(self):
        ctx = CompilerContext(
            model_slug="sales_model",
            share_type="rank",
            share_dimension="Country",
            base_expression='measure("Revenue")',
            filter_where_clause='"Country" = \'Germany\'',
        )
        result = compile_expression('rank_over(measure("Revenue"))', ctx)
        assert "RANK()" in result.sql
        assert "Germany" in result.sql
        # Member filter must NOT be in the grouped CTE WHERE
        grouped_cte = result.sql.split("grouped AS (")[1].split("), ranked")[0]
        assert "Germany" not in grouped_cte
        # Member filter applied after ranking, returns single scalar via MIN
        assert "MIN(rnk) AS value" in result.sql
        after_ranked = result.sql.split("FROM ranked")[1]
        assert "Germany" in after_ranked

    def test_share_rank_multi_member_returns_best_rank(self):
        ctx = CompilerContext(
            model_slug="sales_model",
            share_type="rank",
            share_dimension="Country",
            base_expression='measure("Revenue")',
            filter_where_clause='"Country" IN (\'Germany\', \'France\')',
        )
        result = compile_expression('rank_over(measure("Revenue"))', ctx)
        # Multi-member IN filter still produces one scalar via MIN(rnk)
        assert "MIN(rnk) AS value" in result.sql
        assert "Germany" in result.sql
        assert "France" in result.sql

    def test_share_of_total_with_member_filter(self):
        ctx = CompilerContext(
            model_slug="sales_model",
            share_type="share_of_total",
            share_dimension="Country",
            base_expression='measure("Revenue")',
            filter_where_clause='"Country" = \'Germany\'',
        )
        result = compile_expression('share_of_total(measure("Revenue"))', ctx)
        # Full peer set preserved — member filter not in grouped CTE WHERE
        grouped_cte = result.sql.split("grouped AS (")[1].split("), total")[0]
        assert "Germany" not in grouped_cte
        # Combined share via SUM
        assert "SUM(grouped.dim_value)" in result.sql
        assert "Germany" in result.sql

    def test_share_of_total_multi_member_combined_share(self):
        ctx = CompilerContext(
            model_slug="sales_model",
            share_type="share_of_total",
            share_dimension="Country",
            base_expression='measure("Revenue")',
            filter_where_clause='"Country" IN (\'Germany\', \'France\')',
        )
        result = compile_expression('share_of_total(measure("Revenue"))', ctx)
        # Multi-member returns combined share, not one arbitrary member
        assert "SUM(grouped.dim_value)" in result.sql
        assert "Germany" in result.sql
        assert "France" in result.sql

    def test_share_rank_peer_filters_preserved(self):
        ctx = CompilerContext(
            model_slug="sales_model",
            share_type="rank",
            share_dimension="Country",
            base_expression='measure("Revenue")',
            filter_where_clause='"Region" = \'EMEA\' AND "Country" = \'Germany\'',
        )
        result = compile_expression('rank_over(measure("Revenue"))', ctx)
        # Non-share-dim filter stays in grouped CTE
        grouped_cte = result.sql.split("grouped AS (")[1].split("), ranked")[0]
        assert "EMEA" in grouped_cte
        assert "Germany" not in grouped_cte

    def test_share_rank_between_filter_not_corrupted(self):
        """BETWEEN contains AND — splitting on ' AND ' would corrupt it."""
        between_pred = "\"Country\" BETWEEN 'A' AND 'M'"
        ctx = CompilerContext(
            model_slug="sales_model",
            share_type="rank",
            share_dimension="Country",
            base_expression='measure("Revenue")',
            filter_where_clause=between_pred,
            filter_predicate_list=[between_pred],
        )
        result = compile_expression('rank_over(measure("Revenue"))', ctx)
        # The BETWEEN predicate must appear intact in the member WHERE
        assert "BETWEEN 'A' AND 'M'" in result.sql
        # It must NOT appear in the grouped CTE (it's a member filter)
        grouped_cte = result.sql.split("grouped AS (")[1].split("), ranked")[0]
        assert "BETWEEN" not in grouped_cte

    def test_share_rank_between_with_peer_filter(self):
        """BETWEEN on share dim + equality on peer dim — both preserved."""
        between_pred = "\"Country\" BETWEEN 'A' AND 'M'"
        peer_pred = "\"Region\" = 'EMEA'"
        ctx = CompilerContext(
            model_slug="sales_model",
            share_type="rank",
            share_dimension="Country",
            base_expression='measure("Revenue")',
            filter_where_clause=f"{peer_pred} AND {between_pred}",
            filter_predicate_list=[peer_pred, between_pred],
        )
        result = compile_expression('rank_over(measure("Revenue"))', ctx)
        # Peer filter in grouped CTE
        grouped_cte = result.sql.split("grouped AS (")[1].split("), ranked")[0]
        assert "EMEA" in grouped_cte
        # BETWEEN member filter after ranking, intact
        assert "BETWEEN 'A' AND 'M'" in result.sql
        after_ranked = result.sql.split("FROM ranked")[1]
        assert "BETWEEN" in after_ranked


# ---------------------------------------------------------------------------
# Bug-9385 / F-103-02: a period-to-date (YTD) KPI must compile to a
# YEAR-BOUNDED query, never an unrestricted all-time SUM. This pins the
# year-bounding so a "Revenue (YTD)" KPI can never silently regress to all-time.
# ---------------------------------------------------------------------------

class TestPeriodToDateIsYearBounded:
    def test_ytd_compiles_to_current_year_window(self):
        from src.kpi_compiler import compile_scalar_kpi_sql
        ctx = CompilerContext(model_slug="acme_sales", time_column="business_date")
        sql = compile_scalar_kpi_sql(
            'SUM("base_amount")', ctx,
            ti_type="period_to_date", ti_grain="year",
        )
        assert sql is not None
        # Year-bounded: filters from the start of the CURRENT year up to today.
        assert "DATE_TRUNC('year', CURRENT_DATE)" in sql
        assert "business_date" in sql
        # A WHERE time window is present — this is what makes YTD differ from
        # all-time (an all-time SUM has no time predicate at all).
        assert "WHERE" in sql

    def test_ytd_query_differs_from_all_time(self):
        from src.kpi_compiler import compile_scalar_kpi_sql, compile_expression
        ctx = CompilerContext(model_slug="acme_sales", time_column="business_date")
        ytd_sql = compile_scalar_kpi_sql(
            'SUM("base_amount")', ctx,
            ti_type="period_to_date", ti_grain="year",
        )
        # The all-time compile of the same measure has NO time window — the two
        # queries are structurally different, so their results differ whenever
        # data exists outside the current year (the F-103-02 regression: a YTD
        # KPI showing all-time).
        all_time = compile_expression('measure("base_amount")').select_expr
        assert "DATE_TRUNC('year', CURRENT_DATE)" in ytd_sql
        assert "DATE_TRUNC('year', CURRENT_DATE)" not in all_time
        assert "CURRENT_DATE" not in all_time

    def test_mtd_and_qtd_bound_to_their_grain(self):
        from src.kpi_compiler import compile_scalar_kpi_sql
        ctx = CompilerContext(model_slug="m", time_column="d")
        mtd = compile_scalar_kpi_sql('SUM("x")', ctx, ti_type="period_to_date", ti_grain="month")
        qtd = compile_scalar_kpi_sql('SUM("x")', ctx, ti_type="period_to_date", ti_grain="quarter")
        assert "DATE_TRUNC('month', CURRENT_DATE)" in mtd
        assert "DATE_TRUNC('quarter', CURRENT_DATE)" in qtd
