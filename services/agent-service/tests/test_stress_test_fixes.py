"""Tests for stress-test root-cause fixes (Phases 1-7)."""
from __future__ import annotations

import pytest


# ── Phase 1: HAVING clause aggregate wrapping ────────────────────────────────

class TestHavingClause:
    def test_having_wraps_with_aggregate(self):
        from src.exec.query import _filter_to_sql
        pred = _filter_to_sql(
            {"name": "base_amount", "op": "gt", "value": 100000},
            agg_wrap="SUM",
        )
        assert pred == 'SUM("base_amount") > 100000'

    def test_having_wraps_count_agg(self):
        from src.exec.query import _filter_to_sql
        pred = _filter_to_sql(
            {"name": "transaction_count", "op": "gte", "value": 1000},
            agg_wrap="COUNT",
        )
        assert pred == 'COUNT("transaction_count") >= 1000'

    def test_where_no_wrap(self):
        from src.exec.query import _filter_to_sql
        pred = _filter_to_sql(
            {"name": "country_code", "op": "eq", "value": "GB"},
        )
        assert pred == "\"country_code\" = 'GB'"

    def test_build_sql_having_uses_measure_agg(self):
        from src.exec.query import build_sql
        from src.tools.spec import QueryToolCall
        call = QueryToolCall(
            model_id="test",
            measures=["base_amount"],
            dimensions=["country_code"],
            where=[],
            having=[{"name": "base_amount", "op": "gt", "value": 100000000}],
            sort=[],
            limit=100,
        )
        sql = build_sql("model_slug", call, measure_aggs={"base_amount": "SUM"})
        assert 'HAVING SUM("base_amount") > 100000000' in sql
        assert 'GROUP BY "country_code"' in sql


# ── Phase 1: Error humanization ──────────────────────────────────────────────

class TestErrorHumanization:
    def test_having_error_humanized(self):
        from src.pipeline import _humanize_query_error
        msg = _humanize_query_error(
            "HAVING clause expression references base_amount "
            "which is neither grouped nor aggregated"
        )
        assert "threshold filter" in msg.lower() or "rephras" in msg.lower()
        assert "HAVING" not in msg

    def test_column_not_found_humanized(self):
        from src.pipeline import _humanize_query_error
        msg = _humanize_query_error(
            "column gross_margin does not exist in model"
        )
        assert "field" in msg.lower() or "model" in msg.lower()

    def test_missing_source_relation_not_humanized_as_field_error(self):
        # Bug-5356: a missing source TABLE is a data-availability fault, not a
        # model-field error. It must NOT tell the user to rephrase, and must not
        # be mislabelled as "one of the fields ... does not exist".
        from src.pipeline import (
            _humanize_query_error,
            _SOURCE_TABLE_MISSING_MESSAGE,
        )
        msg = _humanize_query_error(
            'relation "cmb_digital_banking.fct_digital_onboarding_nps" '
            "does not exist"
        )
        assert msg == _SOURCE_TABLE_MISSING_MESSAGE
        # Names the real cause (data availability), not a phrasing problem.
        assert "data-availability" in msg.lower() or "unavailable" in msg.lower()
        assert "will not help" in msg.lower()
        assert "field" not in msg.lower()

    def test_undefined_table_error_class_humanized_as_source_missing(self):
        # The asyncpg class name can surface in the propagated detail too.
        from src.pipeline import (
            _humanize_query_error,
            _SOURCE_TABLE_MISSING_MESSAGE,
        )
        msg = _humanize_query_error(
            "asyncpg.exceptions.UndefinedTableError: relation \"x\" "
            "does not exist"
        )
        assert msg == _SOURCE_TABLE_MISSING_MESSAGE

    def test_missing_column_still_humanized_as_field_error(self):
        # Regression guard: the column case must keep the field-error message,
        # not get swept into the source-missing branch.
        from src.pipeline import _humanize_query_error
        msg = _humanize_query_error(
            'column "acquisition_source" does not exist'
        )
        assert "field" in msg.lower() or "model" in msg.lower()
        assert "source connection" not in msg.lower()

    def test_unknown_error_fallback(self):
        from src.pipeline import _humanize_query_error
        msg = _humanize_query_error("something unexpected happened")
        assert "rephras" in msg.lower()

    # Bug-3587: router-side security denials must narrate as an access
    # restriction, NOT the generic "rephrase your metric" fallback.
    def test_persona_gate_rejection_humanized_as_security(self):
        from src.pipeline import _humanize_query_error, _QUERY_ERROR_FALLBACK
        msg = _humanize_query_error(
            "Query rejected by router (HTTP 403): Measure 'revenue' is not "
            "included in persona 'Regional Analyst'."
        )
        assert "security restriction" in msg.lower()
        assert "persona" in msg.lower() or "access" in msg.lower()
        assert msg != _QUERY_ERROR_FALLBACK

    def test_column_restricted_humanized_as_security(self):
        from src.pipeline import _humanize_query_error, _QUERY_ERROR_FALLBACK
        msg = _humanize_query_error(
            "Query rejected by router (HTTP 403): COLUMN_RESTRICTED — salary "
            "is not visible under your column-level security tags."
        )
        assert "security restriction" in msg.lower()
        assert msg != _QUERY_ERROR_FALLBACK

    def test_row_security_humanized_as_security(self):
        from src.pipeline import _humanize_query_error, _QUERY_ERROR_FALLBACK
        msg = _humanize_query_error(
            "Query rejected by router (HTTP 403): row security filter not "
            "permitted to be removed for this principal."
        )
        assert "security restriction" in msg.lower()
        assert msg != _QUERY_ERROR_FALLBACK

    def test_non_security_error_still_not_flagged_as_security(self):
        # A plain field/syntax error must NOT be mislabelled as a security denial.
        from src.pipeline import _humanize_query_error
        msg = _humanize_query_error("column gross_margin does not exist in model")
        assert "security restriction" not in msg.lower()

    # Bug-3587 / F-P7-01: a field whose NAME contains "restricted" must narrate
    # as a field-not-found error, NOT a security denial (no bare-substring match).
    def test_field_named_restricted_not_flagged_as_security(self):
        from src.pipeline import _humanize_query_error
        msg = _humanize_query_error(
            "column restricted_sales does not exist in model"
        )
        assert "security restriction" not in msg.lower()
        assert "field" in msg.lower() or "model" in msg.lower()

    def test_field_compatibility_message_is_preserved(self):
        from src.pipeline import _humanize_query_error

        msg = _humanize_query_error(
            "Query rejected by router (HTTP 422): There is no aggregation path "
            "between Average Student Age and Teacher Name. Average Student Age "
            "can be used with: Student Grade, School."
        )

        assert "aggregation path between Average Student Age and Teacher Name" in msg
        assert "Student Grade, School" in msg


# ── Phase 2: Chart selector coverage ─────────────────────────────────────────

class TestChartSelectorCoverage:
    def _r(self, columns, rows):
        return {"columns": columns, "rows": rows}

    def test_single_row_multi_measure_is_kpi(self):
        from src.charts.selector import select_chart_type
        rows = [[100, 200, 50]]
        assert select_chart_type(self._r(["count", "amount", "net"], rows)) == "kpi"

    def test_two_cat_dims_small_is_bar(self):
        from src.charts.selector import select_chart_type
        rows = [["US", "Credit", 100], ["US", "Debit", 200], ["UK", "Credit", 150]]
        assert select_chart_type(self._r(["country", "method", "amount"], rows)) == "bar"

    def test_two_cat_dims_medium_is_h_bar(self):
        from src.charts.selector import select_chart_type
        rows = [[f"cat{i}", f"sub{i}", i * 10] for i in range(20)]
        assert select_chart_type(self._r(["cat", "sub", "val"], rows)) == "h_bar"

    def test_two_cat_dims_60_is_h_bar(self):
        from src.charts.selector import select_chart_type
        rows = [[f"cat{i}", f"sub{i}", i * 10] for i in range(60)]
        assert select_chart_type(self._r(["cat", "sub", "val"], rows)) == "h_bar"

    def test_two_cat_dims_large_is_none(self):
        from src.charts.selector import select_chart_type
        rows = [[f"cat{i}", f"sub{i}", i * 10] for i in range(101)]
        assert select_chart_type(self._r(["cat", "sub", "val"], rows)) is None

    def test_sorted_data_gets_bar_not_pie(self):
        from src.charts.selector import select_chart_type
        rows = [[f"Cat{i}", i * 10] for i in range(5)]
        assert select_chart_type(
            self._r(["category", "amount"], rows),
            has_sort=True,
        ) == "bar"

    def test_unsorted_small_positive_gets_pie(self):
        from src.charts.selector import select_chart_type
        rows = [[f"Cat{i}", i * 10 + 1] for i in range(5)]
        assert select_chart_type(
            self._r(["category", "amount"], rows),
            has_sort=False,
        ) == "pie"


# ── Phase 3: User chart type extraction ──────────────────────────────────────

class TestUserChartTypeExtraction:
    def test_bar_chart_detected(self):
        from src.pipeline import _extract_user_chart_type
        assert _extract_user_chart_type("Draw a bar chart of revenue") == "bar"

    def test_horizontal_bar_detected(self):
        from src.pipeline import _extract_user_chart_type
        assert _extract_user_chart_type("Show horizontal bar of costs") == "h_bar"

    def test_stacked_bar_detected(self):
        from src.pipeline import _extract_user_chart_type
        assert _extract_user_chart_type("Show a stacked bar chart") == "stacked_bar"

    def test_line_chart_detected(self):
        from src.pipeline import _extract_user_chart_type
        assert _extract_user_chart_type("Show a line chart of trends") == "line"

    def test_pie_chart_detected(self):
        from src.pipeline import _extract_user_chart_type
        assert _extract_user_chart_type("Draw a pie chart of segments") == "pie"

    def test_no_chart_type(self):
        from src.pipeline import _extract_user_chart_type
        assert _extract_user_chart_type("Show me the total revenue") is None

    def test_case_insensitive(self):
        from src.pipeline import _extract_user_chart_type
        assert _extract_user_chart_type("Draw a BAR CHART") == "bar"


# ── Phase 5: Number formatting ───────────────────────────────────────────────

class TestNumberFormatting:
    def test_count_measure_no_decimals(self):
        from src.narrate.formatting import format_number
        result = format_number(410289, None, measure_name="transaction_count")
        assert result == "410,289"

    def test_count_measure_qty_suffix(self):
        from src.narrate.formatting import format_number
        result = format_number(1000, None, measure_name="order_qty")
        assert result == "1,000"

    def test_non_count_measure_keeps_decimals(self):
        from src.narrate.formatting import format_number
        result = format_number(410289.5, None, measure_name="avg_score")
        assert result == "410,289.50"

    def test_explicit_format_overrides_name_heuristic(self):
        from src.narrate.formatting import format_number
        result = format_number(410289, "decimal_2", measure_name="transaction_count")
        assert result == "410,289.00"


class TestRoundForDisplay:
    """F-023-14 — display rounding must keep its 2-decimal behaviour for
    magnitudes >= 1 but preserve significant figures for small ratios that
    the old data-layer ``_round_computed(value, 2)`` collapsed to 0.0."""

    def test_round_float_magnitude_ge_one(self):
        from src.pipeline import _round_for_display
        assert _round_for_display(-156381.23999997973) == -156381.24

    def test_round_dict(self):
        from src.pipeline import _round_for_display
        result = _round_for_display({"a": 1.23456, "b": "text"})
        assert result == {"a": 1.23, "b": "text"}

    def test_round_list(self):
        from src.pipeline import _round_for_display
        result = _round_for_display([1.111, 2.999])
        assert result == [1.11, 3.0]

    def test_round_int_unchanged(self):
        from src.pipeline import _round_for_display
        assert _round_for_display(42) == 42

    def test_small_ratio_preserved(self):
        # 0.42% (0.0042) must NOT collapse to 0.0 — the F-023-14 bug shape.
        from src.pipeline import _round_for_display
        assert _round_for_display(0.0042) == 0.0042
        assert _round_for_display(0.00481) != 0.0

    def test_bool_passthrough(self):
        from src.pipeline import _round_for_display
        assert _round_for_display(True) is True


# ── Phase 7: Multi-KPI rendering ─────────────────────────────────────────────

class TestMultiKpiRendering:
    def test_single_value_kpi(self):
        from src.charts.renderer import render_chart
        html = render_chart("kpi", ["total"], [[5000]], include_table=False)
        assert "5,000" in html

    def test_multi_value_kpi(self):
        from src.charts.renderer import render_chart
        html = render_chart(
            "kpi",
            ["transaction_count", "transaction_amount", "net_amount"],
            [[100, 5000, 4500]],
            include_table=False,
        )
        assert "100" in html
        assert "5,000" in html
        assert "4,500" in html
        assert "Transaction Count" in html
        assert "Transaction Amount" in html
        assert "Net Amount" in html


# ── Phase 9.1: User chart override applies to all selector modes ───────────

class TestUserChartOverrideAllModes:
    def test_pie_override_with_auto_selector(self):
        """User asks for pie chart — auto selector should not override it."""
        from src.charts.selector import select_chart_type
        from src.pipeline import _extract_user_chart_type
        rows = [[f"Cat{i}", i * 10] for i in range(5)]
        auto_type = select_chart_type(
            {"columns": ["category", "amount"], "rows": rows},
            has_sort=True,
        )
        assert auto_type == "bar"
        user_chart = _extract_user_chart_type("Create a pie chart of fee distribution")
        assert user_chart == "pie"

    def test_stacked_bar_override(self):
        from src.pipeline import _extract_user_chart_type
        assert _extract_user_chart_type("Show a stacked bar chart of amounts") == "stacked_bar"


# ── Phase 9.4: Raised selector boundary ────────────────────────────────────

class TestRaisedSelectorBoundary:
    def _r(self, columns, rows):
        return {"columns": columns, "rows": rows}

    def test_single_dim_80_rows_is_h_bar(self):
        from src.charts.selector import select_chart_type
        rows = [[f"Cat{i}", i * 10] for i in range(80)]
        assert select_chart_type(self._r(["cat", "val"], rows)) == "h_bar"

    def test_single_dim_101_rows_is_none(self):
        from src.charts.selector import select_chart_type
        rows = [[f"Cat{i}", i * 10] for i in range(101)]
        assert select_chart_type(self._r(["cat", "val"], rows)) is None

    def test_multi_dim_54_rows_is_h_bar(self):
        from src.charts.selector import select_chart_type
        rows = [[f"a{i}", f"b{i}", i * 10] for i in range(54)]
        assert select_chart_type(self._r(["dim1", "dim2", "val"], rows)) == "h_bar"


# ── Phase 9.5: Date detection for datetime objects ─────────────────────────

class TestDateDetection:
    def _r(self, columns, rows):
        return {"columns": columns, "rows": rows}

    def test_datetime_object_detected_as_date(self):
        import datetime
        from src.charts.selector import select_chart_type
        rows = [[datetime.date(2026, 3, d), d * 100] for d in range(1, 11)]
        assert select_chart_type(self._r(["business_date", "count"], rows)) == "line"

    def test_datetime_with_time_detected_as_date(self):
        import datetime
        from src.charts.selector import select_chart_type
        rows = [[datetime.datetime(2026, 3, d, 0, 0, 0), d * 100] for d in range(1, 11)]
        assert select_chart_type(self._r(["business_date", "count"], rows)) == "line"

    def test_datetime_string_with_time_detected_as_date(self):
        from src.charts.selector import select_chart_type
        rows = [[f"2026-03-{d:02d} 00:00:00", d * 100] for d in range(1, 11)]
        assert select_chart_type(self._r(["business_date", "count"], rows)) == "line"

    def test_90_day_daily_data_is_line(self):
        import datetime
        from src.charts.selector import select_chart_type
        base = datetime.date(2026, 3, 12)
        rows = [[base + datetime.timedelta(days=i), 1000 + i] for i in range(90)]
        assert select_chart_type(self._r(["business_date", "transaction_count"], rows)) == "line"


# ── Phase 9.2: Compound narration includes result_rows ─────────────────────

class TestCompoundNarrationMultiRow:
    def test_multi_row_prompt_includes_result_rows(self):
        from src.narrate.narrate import _build_compound_narrate_prompt
        step_summaries = [
            {"name": "fees", "columns": ["pm", "fee"], "rows_returned": 6, "sample_rows": []},
            {"name": "txn", "columns": ["pm", "txn"], "rows_returned": 6, "sample_rows": []},
        ]
        computed = {
            "expression": "fees.fee / txn.txn * 100",
            "label": "Fee percentage (%)",
            "value": None,
            "is_multi_row": True,
            "result_rows": [
                {"payment_method": "Card", "Fee percentage (%)": 1.1},
                {"payment_method": "Bank Transfer", "Fee percentage (%)": 0.9},
            ],
            "result_columns": ["payment_method", "Fee percentage (%)"],
        }
        _sys, user_prompt = _build_compound_narrate_prompt(
            "You are helpful.", "fee % by method", step_summaries, computed
        )
        assert "result_rows" in user_prompt
        assert "1.1" in user_prompt
        assert "0.9" in user_prompt
        assert "2 rows" in user_prompt

    def test_scalar_prompt_no_result_rows(self):
        from src.narrate.narrate import _build_compound_narrate_prompt
        computed = {
            "expression": "a.x - b.x",
            "label": "Difference",
            "value": 42.5,
            "is_multi_row": False,
        }
        _sys, user_prompt = _build_compound_narrate_prompt(
            "You are helpful.", "compare", [], computed
        )
        assert "42.5" in user_prompt
        # No result_rows DATA for a scalar. The system guidance text may mention the
        # field name ("computed.result_rows"); assert the JSON data key is absent,
        # not the bare substring, so the guidance wording doesn't trip this.
        assert '"result_rows"' not in user_prompt
