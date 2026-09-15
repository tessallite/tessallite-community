from __future__ import annotations

from src.planning.contracts import FieldRole, ShapeContract, ShapeLimits
from src.planning.enums import AnalyticalShape, AxisRole, ValueRole
from src.planning.intent import AnalyticalIntent, detect_analytical_intent
from src.planning.shape import add_average_comparison_fact, normalize_result_shape


def _role(name, role, source="dimension"):
    return FieldRole(name=name, role=role, confidence="metadata", source=source)


def _contract(shape, binding, chart=None, table_required=False, checks=None):
    return ShapeContract(
        shape=shape,
        required_axes=[],
        optional_axes=[],
        value_role=ValueRole.SINGLE_METRIC,
        chart_preference=chart,
        table_required=table_required,
        renderer_binding=binding,
        data_quality_checks=checks or [],
        narration_fact_contract=shape.value,
    )


def test_multi_series_facts_include_full_period_coverage_and_extrema():
    shaped = normalize_result_shape(
        ["period", "country", "amount"],
        [
            {"period": "2026-01", "country": "GB", "amount": 10},
            {"period": "2026-02", "country": "GB", "amount": 30},
            {"period": "2026-01", "country": "DE", "amount": 20},
        ],
        AnalyticalIntent(
            shape_hint=AnalyticalShape.MULTI_SERIES_TIME,
            wants_trend=True,
            wants_separate_series=True,
        ),
        [
            _role("period", AxisRole.TEMPORAL),
            _role("country", AxisRole.CATEGORY),
            _role("amount", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        _contract(
            AnalyticalShape.MULTI_SERIES_TIME,
            {"x": "period", "series": "country", "y": "amount"},
            chart="multi_line",
        ),
        ShapeLimits(),
    )

    facts = shaped.narration_facts
    assert facts.output_mode == "chart_table"
    assert facts.separate_series is True
    assert facts.date_range["period"] == ("2026-01", "2026-02")
    assert facts.series_coverage["GB"]["row_count"] == 2
    assert facts.series_coverage["GB"]["first_period"] == "2026-01"
    assert facts.series_coverage["GB"]["last_period"] == "2026-02"
    assert facts.series_coverage["GB"]["max_period"] == "2026-02"
    assert facts.extrema["amount"]["max_period"] == "2026-02"


def test_numeric_period_labels_use_numeric_date_range_bounds():
    shaped = normalize_result_shape(
        ["month", "method", "amount"],
        [
            {"month": "1", "method": "CARD", "amount": 10},
            {"month": "9", "method": "CARD", "amount": 20},
            {"month": "12", "method": "CARD", "amount": 30},
        ],
        AnalyticalIntent(
            shape_hint=AnalyticalShape.MULTI_SERIES_TIME,
            wants_trend=True,
            wants_separate_series=True,
        ),
        [
            _role("month", AxisRole.TEMPORAL),
            _role("method", AxisRole.CATEGORY),
            _role("amount", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        _contract(
            AnalyticalShape.MULTI_SERIES_TIME,
            {"x": "month", "series": "method", "y": "amount"},
            chart="multi_line",
        ),
        ShapeLimits(),
    )

    assert shaped.narration_facts.date_range["month"] == ("1", "12")


def test_ranking_facts_are_present_only_for_ranking_intent():
    shaped = normalize_result_shape(
        ["country", "amount"],
        [
            {"country": "GB", "amount": 30},
            {"country": "DE", "amount": 10},
        ],
        AnalyticalIntent(
            shape_hint=AnalyticalShape.RANKING,
            wants_ranking=True,
            ranking_direction="desc",
            requested_limit=2,
        ),
        [
            _role("country", AxisRole.CATEGORY),
            _role("amount", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        _contract(AnalyticalShape.RANKING, {"y": "country", "x": "amount"}, chart="h_bar"),
        ShapeLimits(),
    )

    assert shaped.narration_facts.ranking == {
        "sort_metric": "amount",
        "direction": "desc",
        "limit": 2,
        "limit_explicit": True,
        "top_boundary": {"country": "GB", "amount": 30},
        "bottom_boundary": {"country": "DE", "amount": 10},
        "row_count": 2,
    }


def test_Bug_9971_most_result_has_explicit_top_boundary_facts():
    intent = detect_analytical_intent(
        "Which account type has processed the most money in 2026?"
    )
    shaped = normalize_result_shape(
        ["account_type", "transaction_amount"],
        [{"account_type": "WALLET", "transaction_amount": 32829598.09}],
        intent,
        [
            _role("account_type", AxisRole.CATEGORY),
            _role("transaction_amount", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        _contract(
            AnalyticalShape.RANKING,
            {"x": "transaction_amount", "y": "account_type"},
            chart="h_bar",
        ),
        ShapeLimits(),
    )

    assert shaped.shape == AnalyticalShape.RANKING
    assert shaped.narration_facts.ranking == {
        "sort_metric": "transaction_amount",
        "direction": "desc",
        "limit": 1,
        "limit_explicit": True,
        "top_boundary": {"account_type": "WALLET", "transaction_amount": 32829598.09},
        "bottom_boundary": {"account_type": "WALLET", "transaction_amount": 32829598.09},
        "row_count": 1,
    }


def test_table_only_quality_facts_are_serialised_for_narration():
    shaped = normalize_result_shape(
        ["category", "amount"],
        [{"category": f"C{i}", "amount": i} for i in range(30)],
        AnalyticalIntent(shape_hint=AnalyticalShape.BREAKDOWN, wants_breakdown=True),
        [
            _role("category", AxisRole.CATEGORY),
            _role("amount", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        _contract(
            AnalyticalShape.BREAKDOWN,
            {"x": "category", "y": "amount"},
            chart="bar",
            checks=["category_limit"],
        ),
        ShapeLimits(max_bar_categories=25),
    )

    trace = shaped.as_trace()
    assert trace["output_mode"] == "table"
    assert trace["narration_facts"]["table"]["table_only_reason"] == "chart_rejected_or_disabled"
    assert trace["narration_facts"]["data_quality_findings"][0]["code"] == "too_many_bar_categories"


def test_matrix_facts_include_two_axis_visual_mode():
    shaped = normalize_result_shape(
        ["country", "payment_method", "Revenue"],
        [
            {"country": "AE", "payment_method": "BANK_TRANSFER", "Revenue": 10},
            {"country": "AE", "payment_method": "CARD", "Revenue": 5},
            {"country": "US", "payment_method": "BANK_TRANSFER", "Revenue": 7},
        ],
        AnalyticalIntent(shape_hint=AnalyticalShape.MATRIX, wants_breakdown=True),
        [
            _role("country", AxisRole.CATEGORY),
            _role("payment_method", AxisRole.CATEGORY),
            _role("Revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        _contract(
            AnalyticalShape.MATRIX,
            {"x": "country", "series": "payment_method", "y": "Revenue"},
            chart="stacked_bar",
            checks=["matrix_size", "additive_values"],
        ),
        ShapeLimits(),
    )

    matrix = shaped.narration_facts.matrix
    assert shaped.output_mode == "chart_table"
    assert matrix == {
        "row_axis": "country",
        "column_axis": "payment_method",
        "primary_axis": "country",
        "secondary_axis": "payment_method",
        "row_axis_count": 2,
        "column_axis_count": 2,
        "populated_cell_count": 3,
        "visual_mode": "stacked_bar",
    }


def test_composition_shape_derives_share_percentages_for_pie_output():
    shaped = normalize_result_shape(
        ["payment_method", "Revenue"],
        [
            {"payment_method": "BANK_TRANSFER", "Revenue": "41392449.19"},
            {"payment_method": "CHEQUE", "Revenue": "42439991.05"},
            {"payment_method": "CARD", "Revenue": "41892652.29"},
        ],
        AnalyticalIntent(
            shape_hint=AnalyticalShape.STACKED_COMPOSITION,
            wants_composition=True,
        ),
        [
            _role("payment_method", AxisRole.CATEGORY),
            _role("Revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        _contract(
            AnalyticalShape.STACKED_COMPOSITION,
            {"segment": "payment_method", "value": "Revenue"},
            chart="pie",
            checks=["pie_suitable"],
        ),
        ShapeLimits(),
    )

    assert shaped.columns == ["payment_method", "Revenue Share (%)"]
    assert shaped.rows[0] == ["BANK_TRANSFER", 32.92]
    assert shaped.contract.renderer_binding["value"] == "Revenue Share (%)"
    assert shaped.narration_facts.value_label == "Revenue Share (%)"


def test_Bug_9974_grouped_temporal_axis_uses_validated_source_filter_range():
    rows = [
        {"business_date_year": "2026-01-01T00:00:00Z", "amount": 10},
        {"business_date_year": "2026-01-01T00:00:00Z", "amount": 20},
    ]
    shaped = normalize_result_shape(
        ["business_date_year", "amount"],
        rows,
        AnalyticalIntent(shape_hint=AnalyticalShape.BREAKDOWN, wants_breakdown=True),
        [
            _role("business_date_year", AxisRole.TEMPORAL),
            _role("amount", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        _contract(
            AnalyticalShape.BREAKDOWN,
            {"x": "business_date_year", "y": "amount"},
            chart="bar",
        ),
        ShapeLimits(),
        filter_where=[{
            "name": "business_date",
            "op": "between",
            "value": ["2026-01-01", "2026-09-10"],
        }],
    )

    assert shaped.narration_facts.date_range["business_date_year"] == (
        "2026-01-01",
        "2026-09-10",
    )

    # A raw date axis continues to report observed rows rather than widening
    # them to the requested filter when the source has a narrower result.
    raw = normalize_result_shape(
        ["business_date", "amount"],
        [
            {"business_date": "2026-02-01", "amount": 10},
            {"business_date": "2026-03-01", "amount": 20},
        ],
        AnalyticalIntent(shape_hint=AnalyticalShape.BREAKDOWN, wants_breakdown=True),
        [
            _role("business_date", AxisRole.TEMPORAL),
            _role("amount", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        _contract(
            AnalyticalShape.BREAKDOWN,
            {"x": "business_date", "y": "amount"},
            chart="bar",
        ),
        ShapeLimits(),
        filter_where=[{
            "name": "business_date",
            "op": "between",
            "value": ["2026-01-01", "2026-09-10"],
        }],
    )
    assert raw.narration_facts.date_range["business_date"] == (
        "2026-02-01",
        "2026-03-01",
    )


def test_Bug_9973_average_fact_requires_complete_grouped_rows_and_target():
    rows = [
        {"account_type": "WALLET", "base_amount": 36_487_318.78},
        {"account_type": "LOAN", "base_amount": 36_245_683.67},
        {"account_type": "CREDIT", "base_amount": 36_179_774.10},
        {"account_type": "SAVINGS", "base_amount": 35_965_373.66},
        {"account_type": "CURRENT", "base_amount": 35_842_415.96},
    ]
    shaped = normalize_result_shape(
        ["account_type", "base_amount"],
        rows,
        AnalyticalIntent(shape_hint=AnalyticalShape.BREAKDOWN, wants_breakdown=True),
        [
            _role("account_type", AxisRole.CATEGORY),
            _role("base_amount", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        _contract(
            AnalyticalShape.BREAKDOWN,
            {"x": "account_type", "y": "base_amount"},
            chart="bar",
        ),
        ShapeLimits(),
    )
    question = (
        "What is the total base amount for each account type, and is CREDIT "
        "above or below the average?"
    )

    add_average_comparison_fact(
        shaped.narration_facts,
        rows,
        user_message=question,
        complete=True,
        requested=True,
    )
    fact = shaped.narration_facts.average_comparison
    assert fact is not None
    assert fact["status"] == "available"
    assert fact["target_category"] == "CREDIT"
    assert fact["target_relation"] == "above"
    assert fact["average_value"] == 36_144_113.234
    assert fact["comparisons"]["CREDIT"] == {
        "value": 36_179_774.10,
        "relation": "above",
    }

    incomplete = normalize_result_shape(
        ["account_type", "base_amount"],
        rows,
        AnalyticalIntent(shape_hint=AnalyticalShape.BREAKDOWN, wants_breakdown=True),
        [
            _role("account_type", AxisRole.CATEGORY),
            _role("base_amount", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        _contract(AnalyticalShape.BREAKDOWN, {"x": "account_type", "y": "base_amount"}),
        ShapeLimits(),
    )
    add_average_comparison_fact(
        incomplete.narration_facts,
        rows,
        user_message=question,
        complete=False,
        requested=True,
    )
    assert incomplete.narration_facts.average_comparison == {
        "status": "unavailable",
        "reason": "complete_grouped_rows_required",
    }

    # A different comparison question must not acquire a benchmark fact merely
    # because its result happens to be a complete grouped breakdown.
    control = normalize_result_shape(
        ["account_type", "base_amount"],
        rows,
        AnalyticalIntent(shape_hint=AnalyticalShape.BREAKDOWN, wants_breakdown=True),
        [
            _role("account_type", AxisRole.CATEGORY),
            _role("base_amount", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        _contract(AnalyticalShape.BREAKDOWN, {"x": "account_type", "y": "base_amount"}),
        ShapeLimits(),
    )
    add_average_comparison_fact(
        control.narration_facts,
        rows,
        user_message="What is the difference between WALLET and CREDIT?",
        complete=True,
        requested=False,
    )
    assert control.narration_facts.average_comparison is None
