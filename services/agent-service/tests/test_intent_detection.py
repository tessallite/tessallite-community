from __future__ import annotations

from src.planning.enums import AnalyticalShape
from src.planning.intent import detect_analytical_intent


def test_compare_follow_up_inherits_trend_from_previous_plan():
    previous = {
        "query": {
            "dimensions": ["business_date_month"],
            "dimension_exprs": [{"name": "business_date", "grain": "month"}],
            "measures": ["revenue"],
            "chart_type": "line",
        }
    }

    intent = detect_analytical_intent(
        "Compare that to London and Manchester",
        previous_plan=previous,
    )

    assert intent.wants_comparison is True
    assert intent.wants_trend is True
    assert intent.shape_hint == AnalyticalShape.TIME_SERIES
    assert "inherited_trend_from_previous_plan" in intent.notes


def _Bug_9739_previous_breakdown_plan():
    return {
        "query": {
            "model_id": "model-1",
            "measures": ["base_amount"],
            "dimensions": ["account_type"],
        }
    }


def _assert_Bug_9739_replacement_cue_suppresses(message: str):
    intent = detect_analytical_intent(
        message,
        previous_plan=_Bug_9739_previous_breakdown_plan(),
    )

    assert intent.requested_grain == "year"
    assert intent.wants_trend is True
    assert intent.preserve_previous_breakdown_dimensions is False
    assert "preserve_previous_breakdown_dimensions" not in intent.notes


def test_Bug_9739_cue_free_temporal_breakdown_preserves_prior_categories():
    previous = {
        "query": {
            "model_id": "model-1",
            "measures": ["base_amount"],
            "dimensions": ["account_type"],
        }
    }

    intent = detect_analytical_intent(
        "Break that down by year",
        previous_plan=previous,
    )

    assert intent.preserve_previous_breakdown_dimensions is True
    assert intent.wants_trend is True
    assert intent.shape_hint == AnalyticalShape.TIME_SERIES
    assert "preserve_previous_breakdown_dimensions" in intent.notes


def test_Bug_9968_cue_free_temporal_breakdown_preserves_prior_where():
    previous = {
        "query": {
            "model_id": "model-1",
            "measures": ["base_amount"],
            "dimensions": ["account_type"],
            "where": [{
                "name": "business_date",
                "op": "between",
                "value": ["2026-01-01", "2026-09-10"],
            }],
        }
    }

    intent = detect_analytical_intent(
        "Break that down by year",
        previous_plan=previous,
    )

    assert intent.preserve_previous_breakdown_where is True
    assert "preserve_previous_breakdown_where" in intent.notes


def test_Bug_9968_explicit_period_change_suppresses_prior_where():
    previous = {
        "query": {
            "model_id": "model-1",
            "measures": ["base_amount"],
            "dimensions": ["account_type"],
            "where": [{
                "name": "business_date",
                "op": "between",
                "value": ["2026-01-01", "2026-09-10"],
            }],
        }
    }

    intent = detect_analytical_intent(
        "Break that down by year for 2025",
        previous_plan=previous,
    )

    assert intent.preserve_previous_breakdown_where is False
    assert "preserve_previous_breakdown_where" not in intent.notes


def test_Bug_9739_instead_cue_suppresses_previous_categories():
    _assert_Bug_9739_replacement_cue_suppresses(
        "Break that down by year instead",
    )


def test_Bug_9739_instead_of_cue_suppresses_previous_categories():
    _assert_Bug_9739_replacement_cue_suppresses(
        "Break that down by year instead of account type",
    )


def test_Bug_9739_rather_than_cue_suppresses_previous_categories():
    _assert_Bug_9739_replacement_cue_suppresses(
        "Break that down by year rather than account type",
    )


def test_Bug_9739_replace_cue_suppresses_previous_categories():
    _assert_Bug_9739_replacement_cue_suppresses(
        "Break that down by year and replace account type",
    )


def test_Bug_9739_not_by_cue_suppresses_previous_categories():
    _assert_Bug_9739_replacement_cue_suppresses(
        "Break that down by year, not by account type",
    )


def test_Bug_9739_only_by_cue_suppresses_previous_categories():
    _assert_Bug_9739_replacement_cue_suppresses(
        "Break that down only by year",
    )


def test_Bug_9739_drop_cue_suppresses_previous_categories():
    _assert_Bug_9739_replacement_cue_suppresses(
        "Break that down by year and drop account type",
    )


def test_Bug_9739_remove_cue_suppresses_previous_categories():
    _assert_Bug_9739_replacement_cue_suppresses(
        "Break that down by year and remove account type",
    )


def test_separate_city_trend_lines_detects_multi_series_without_field_lookup():
    intent = detect_analytical_intent("I want each city as a separate trend line")

    assert intent.wants_separate_series is True
    assert intent.wants_trend is True
    assert intent.shape_hint == AnalyticalShape.MULTI_SERIES_TIME


def test_last_15_months_marks_cross_year_risk_and_month_grain():
    intent = detect_analytical_intent("Show the monthly trend for the last 15 months")

    assert intent.requested_grain == "month"
    assert intent.requested_period_may_cross_year is True
    assert "period_may_cross_parent_cycle" in intent.notes


def test_top_n_request_detects_ranking():
    intent = detect_analytical_intent("top 10 customers by revenue")

    assert intent.wants_ranking is True
    assert intent.shape_hint == AnalyticalShape.RANKING


def test_Bug_9971_most_request_is_single_descending_ranking():
    intent = detect_analytical_intent(
        "Which account type has processed the most money in 2026?"
    )

    assert intent.wants_ranking is True
    assert intent.ranking_direction == "desc"
    assert intent.requested_limit == 1
    assert intent.shape_hint == AnalyticalShape.RANKING


def test_Bug_9971_most_of_composition_wording_is_not_ranking():
    intent = detect_analytical_intent("What is the most of the total revenue?")

    assert intent.wants_ranking is False
    assert intent.ranking_direction is None
    assert intent.requested_limit is None


def test_two_grouping_axes_detect_matrix_table_shape():
    intent = detect_analytical_intent("revenue by region and product")

    assert intent.shape_hint == AnalyticalShape.MATRIX
    assert "multiple_grouping_axes_requested" in intent.notes


def test_raw_record_request_detects_detail_table_shape():
    intent = detect_analytical_intent("show transactions for London")

    assert intent.wants_detail_rows is True
    assert intent.shape_hint == AnalyticalShape.DETAIL_TABLE


def test_transaction_amount_trend_is_not_raw_record_intent():
    intent = detect_analytical_intent("show transaction amount trend by month")

    assert intent.wants_detail_rows is False
    assert intent.wants_trend is True
    assert intent.shape_hint == AnalyticalShape.TIME_SERIES


def test_share_request_detects_composition_not_generic_breakdown():
    intent = detect_analytical_intent("share of revenue by channel")

    assert intent.wants_composition is True
    assert intent.shape_hint == AnalyticalShape.STACKED_COMPOSITION


def test_distributed_wording_detects_distribution_not_breakdown():
    intent = detect_analytical_intent("show how total revenue is distributed by payment method")

    assert intent.wants_distribution is True
    assert intent.shape_hint == AnalyticalShape.DISTRIBUTION


def test_scatter_wording_detects_unsupported_scatter_candidate():
    intent = detect_analytical_intent("show a scatter plot of revenue versus transaction count")

    assert intent.wants_scatter is True
    assert intent.shape_hint == AnalyticalShape.UNSUPPORTED


def test_vague_more_details_follow_up_prefers_detail_table_constraint():
    intent = detect_analytical_intent(
        "show more details",
        previous_plan={"query": {"measures": ["revenue"], "dimensions": ["region"]}},
    )

    assert intent.wants_detail_rows is True
    assert intent.shape_hint == AnalyticalShape.DETAIL_TABLE
