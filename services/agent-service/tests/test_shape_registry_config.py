from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.planning.contracts import FieldRole, ShapeLimits
from src.planning.enums import AnalyticalShape, AxisRole, ValueRole
from src.planning.intent import AnalyticalIntent, detect_analytical_intent
from src.planning.matcher import load_shape_registry, match_shape_contract


def _role(name, role, source="dimension"):
    return FieldRole(
        name=name,
        role=role,
        confidence="metadata",
        source=source,
    )


REGISTRY_ID_COVERAGE = {
    "ambiguous_role_shape": "catalogue_guard",
    "chart_disabled": "catalogue_guard",
    "comparison_two_filtered_kpis": "catalogue_variant",
    "composition_single_category": "active_contract+live_f9",
    "composition_single_category_many": "catalogue_variant",
    "compound_multi_metric_by_time": "catalogue_compound",
    "compound_ratio_by_category": "catalogue_compound",
    "compound_ratio_by_time": "catalogue_compound",
    "compound_ratio_by_time_series": "catalogue_compound",
    "compound_ratio_kpi": "catalogue_compound",
    "correlation_candidate": "catalogue_unsupported",
    "count_by_category": "catalogue_count",
    "count_by_time": "catalogue_count",
    "count_by_time_and_category": "catalogue_count",
    "detail_representable_table": "active_contract",
    "distribution_existing_bucket": "active_contract+live_f10",
    "distribution_generated_bucket": "catalogue_unsupported",
    "empty_result": "catalogue_guard",
    "filtered_breakdown": "catalogue_filtered",
    "filtered_kpi": "catalogue_filtered",
    "filtered_time_series": "catalogue_filtered",
    "grouped_comparison_multi_value": "catalogue_variant",
    "kpi_count": "catalogue_count",
    "kpi_multi_measure_set": "active_contract+live_f2",
    "kpi_multi_measure_too_many": "catalogue_limit",
    "kpi_single_computed_metric": "catalogue_compound",
    "kpi_single_measure": "active_contract+live_f1",
    "list_distinct_values": "catalogue_detail",
    "matrix_temporal_category": "catalogue_matrix",
    "matrix_two_categories": "active_contract+live_f11",
    "matrix_two_categories_multi_measure": "catalogue_matrix",
    "moving_average_time": "catalogue_unsupported",
    "multi_series_time": "active_contract+live_f8",
    "multi_series_time_many_series": "catalogue_limit",
    "multi_series_time_multi_measure": "catalogue_unsupported",
    "multi_series_time_year_month_parts": "catalogue_temporal_variant",
    "ordinal_bucket_breakdown": "catalogue_distribution",
    "percent_of_total_by_category": "catalogue_composition",
    "percent_of_total_by_time_category": "catalogue_composition",
    "period_over_period_kpi": "catalogue_period_comparison",
    "period_over_period_time": "catalogue_period_comparison",
    "ranking_bottom": "catalogue_ranking",
    "ranking_multi_measure": "catalogue_ranking",
    "ranking_top": "active_contract+live_f5",
    "raw_record_request": "active_contract+live_f13",
    "running_total_time": "catalogue_unsupported",
    "scatter_candidate": "active_contract",
    "single_category_breakdown": "active_contract+live_f3",
    "single_category_breakdown_many_rows": "catalogue_limit",
    "single_category_calculated_measure": "catalogue_variant",
    "single_category_many_measures": "catalogue_limit",
    "single_category_multi_measure": "active_contract+live_f4",
    "single_category_non_additive_calculated": "catalogue_variant",
    "single_row_with_category": "catalogue_variant",
    "small_table_requested": "catalogue_table_preference",
    "stacked_composition_multi_value": "catalogue_composition",
    "temporal_category_breakdown": "catalogue_temporal_variant",
    "temporal_category_no_trend": "catalogue_temporal_variant",
    "temporal_part_hour_profile": "catalogue_temporal_part",
    "temporal_part_month_profile": "catalogue_temporal_part",
    "temporal_part_week_profile": "catalogue_temporal_part",
    "temporal_part_weekday_profile": "catalogue_temporal_part",
    "temporal_two_categories": "catalogue_matrix",
    "temporal_two_categories_rollup": "catalogue_temporal_variant",
    "temporal_two_category_matrix": "active_contract",
    "temporal_two_part_two_category_matrix": "active_contract+live_f12",
    "three_axis_temporal_cube": "catalogue_matrix",
    "three_category_cube": "catalogue_matrix",
    "time_series_by_category": "active_contract",
    "time_series_date_hour_parts": "catalogue_temporal_part",
    "time_series_many_measures": "catalogue_limit",
    "time_series_month_part_only": "catalogue_temporal_part",
    "time_series_multi_measure": "active_contract+live_f7",
    "time_series_single_measure": "active_contract+live_f6",
    "time_series_year_month_parts": "catalogue_temporal_variant",
    "time_series_year_quarter_parts": "catalogue_temporal_variant",
    "time_series_year_week_parts": "catalogue_temporal_variant",
    "time_variant_measure_breakdown": "catalogue_time_variant",
    "time_variant_measure_kpi": "catalogue_time_variant",
    "time_variant_measure_trend": "catalogue_time_variant",
    "unsupported_many_axes_many_values": "catalogue_guard",
    "unsupported_no_values": "active_contract",
    "variance_by_category": "catalogue_period_comparison",
    "variance_by_time": "catalogue_period_comparison",
    "variance_kpi": "catalogue_period_comparison",
    "year_part_multi_metric_time": "active_contract+live_f7",
    "year_part_multi_series_time": "active_contract+live_f8",
    "year_part_time_series": "active_contract+live_f6",
    "year_part_time_series_by_category": "active_contract+live_f8",
}


PROMPT_VARIANT_GROUPS = {
    "ambiguous": {
        "simple": [
            "Show revenue by the ambiguous customer field",
            "Chart sales by the unclear region label",
        ],
        "compound": [
            "Compare revenue and margin by the ambiguous customer field",
            "Show contribution percentage by the unclear region label versus total",
        ],
    },
    "breakdown": {
        "simple": [
            "Show total revenue by payment method",
            "Break down base amount by city",
        ],
        "compound": [
            "Show fee-to-base ratio by payment method",
            "Compare Cairo contribution percentage by city against the global total",
        ],
    },
    "chart_disabled": {
        "simple": [
            "Show total revenue by payment method with charts disabled",
            "Return revenue by month using table output only",
        ],
        "compound": [
            "Show Cairo contribution percentage with charts disabled",
            "Compare current and prior revenue by month using table output only",
        ],
    },
    "composition": {
        "simple": [
            "Show revenue share by payment method as a pie chart",
            "Show the contribution mix of base amount by city",
        ],
        "compound": [
            "Show as a pie chart the contribution of Cairo base amount compared to global base amount",
            "Show percent of total revenue by payment method compared with the global total",
        ],
    },
    "compound": {
        "simple": [
            "Show the base amount to fee amount ratio",
            "Show Cairo base amount as a percent of global base amount",
        ],
        "compound": [
            "Show the fee-to-base ratio by city",
            "Trend Cairo share of global base amount by month",
        ],
    },
    "count": {
        "simple": [
            "Show total transaction count",
            "Count transactions by payment method",
        ],
        "compound": [
            "Show transaction count share by payment method compared to the total",
            "Trend transaction count contribution by month and payment method",
        ],
    },
    "detail": {
        "simple": [
            "List all payment methods",
            "Show a small table of cities and revenue",
        ],
        "compound": [
            "List payment methods with their revenue contribution percentages",
            "Show a table comparing Cairo base amount against global base amount",
        ],
    },
    "distribution": {
        "simple": [
            "Show distribution of total revenue by payment method",
            "Show base amount distribution by existing amount bucket",
        ],
        "compound": [
            "Show distribution of fee-to-base ratio by city",
            "Compare revenue share distribution by payment method against total revenue",
        ],
    },
    "empty": {
        "simple": [
            "Show revenue for city Atlantis",
            "Show transaction count for payment method NEVER_USED",
        ],
        "compound": [
            "Compare Atlantis revenue to global revenue",
            "Show Atlantis contribution percentage compared to the global total",
        ],
    },
    "filtered": {
        "simple": [
            "Show revenue for Cairo",
            "Show revenue by payment method for Cairo",
        ],
        "compound": [
            "Show Cairo revenue share compared to global revenue",
            "Trend Cairo revenue compared with global revenue by month",
        ],
    },
    "guard": {
        "simple": [
            "Show payment method only",
            "Chart country and city without a measure",
        ],
        "compound": [
            "Compare payment method and city without choosing a metric",
            "Show contribution percentage by country without a base measure",
        ],
    },
    "grouped_comparison": {
        "simple": [
            "Show revenue and transaction count by payment method",
            "Compare base amount and fee amount for each city",
        ],
        "compound": [
            "Compare revenue share and transaction share by payment method",
            "Show fee-to-base ratio and transaction count by city",
        ],
    },
    "kpi": {
        "simple": [
            "What is total revenue?",
            "Show total base amount as a KPI",
        ],
        "compound": [
            "What percentage of global base amount comes from Cairo?",
            "Show fee amount divided by base amount as a KPI",
        ],
    },
    "kpi_set": {
        "simple": [
            "Show total revenue and transaction count",
            "Show base amount, fee amount, and transaction count",
        ],
        "compound": [
            "Show revenue, transaction count, and fee-to-base ratio",
            "Show Cairo base amount, global base amount, and Cairo contribution percentage",
        ],
    },
    "limit": {
        "simple": [
            "Show every available measure as KPI cards",
            "Show revenue by every available category",
        ],
        "compound": [
            "Show every available ratio by month",
            "Compare all available metrics and percentages by payment method",
        ],
    },
    "matrix": {
        "simple": [
            "Show revenue by country and payment method",
            "Create a pivot table of base amount by city and payment method",
        ],
        "compound": [
            "Show fee-to-base ratio by country and payment method",
            "Show percent of total revenue by month, country, and payment method",
        ],
    },
    "period": {
        "simple": [
            "Compare this month's revenue to last month's revenue",
            "Show revenue variance versus the previous period",
        ],
        "compound": [
            "Trend current versus prior revenue difference by month",
            "Show percent change in revenue by payment method versus the previous period",
        ],
    },
    "ranking": {
        "simple": [
            "Show the top 5 countries by total revenue",
            "Show the bottom 5 cities by base amount",
        ],
        "compound": [
            "Rank cities by fee-to-base ratio",
            "Rank payment methods by contribution percentage of total revenue",
        ],
    },
    "raw_records": {
        "simple": [
            "Show raw transaction records",
            "List individual transaction rows",
        ],
        "compound": [
            "Show raw transactions and compare each one to global revenue",
            "List individual records with contribution percentage to total",
        ],
    },
    "scatter": {
        "simple": [
            "Show a scatter plot of revenue versus transaction count by payment method",
            "Plot base amount against fee amount by city as a scatter plot",
        ],
        "compound": [
            "Show a correlation plot of fee-to-base ratio versus revenue by city",
            "Scatter revenue contribution percentage against transaction share by payment method",
        ],
    },
    "table": {
        "simple": [
            "Show a table of revenue by payment method",
            "List monthly revenue in a table",
        ],
        "compound": [
            "Show a table of fee-to-base ratio by city",
            "List payment method contribution percentage compared to total revenue",
        ],
    },
    "temporal_part": {
        "simple": [
            "Show revenue by month of year",
            "Show transaction count by hour of day",
        ],
        "compound": [
            "Show fee-to-base ratio by month of year",
            "Compare revenue contribution by weekday against total revenue",
        ],
    },
    "time_series": {
        "simple": [
            "Show total revenue by month",
            "Trend base amount over time",
        ],
        "compound": [
            "Trend fee-to-base ratio by month",
            "Show Cairo contribution percentage by month compared to the global monthly total",
        ],
    },
    "time_variant": {
        "simple": [
            "Show year-to-date revenue total",
            "Trend trailing 3 month revenue by month",
        ],
        "compound": [
            "Compare year-to-date revenue against prior-year revenue",
            "Show trailing 3 month revenue share by payment method",
        ],
    },
    "unsupported": {
        "simple": [
            "Show a moving average of revenue over time",
            "Show running total revenue by month",
        ],
        "compound": [
            "Compare running total revenue to monthly revenue",
            "Show moving-average contribution percentage by payment method",
        ],
    },
}


REGISTRY_PROMPT_GROUP_BY_ID = {
    "ambiguous_role_shape": "ambiguous",
    "chart_disabled": "chart_disabled",
    "comparison_two_filtered_kpis": "period",
    "composition_single_category": "composition",
    "composition_single_category_many": "composition",
    "compound_multi_metric_by_time": "compound",
    "compound_ratio_by_category": "compound",
    "compound_ratio_by_time": "compound",
    "compound_ratio_by_time_series": "compound",
    "compound_ratio_kpi": "compound",
    "correlation_candidate": "scatter",
    "count_by_category": "count",
    "count_by_time": "count",
    "count_by_time_and_category": "count",
    "detail_representable_table": "detail",
    "distribution_existing_bucket": "distribution",
    "distribution_generated_bucket": "distribution",
    "empty_result": "empty",
    "filtered_breakdown": "filtered",
    "filtered_kpi": "filtered",
    "filtered_time_series": "filtered",
    "grouped_comparison_multi_value": "grouped_comparison",
    "kpi_count": "count",
    "kpi_multi_measure_set": "kpi_set",
    "kpi_multi_measure_too_many": "limit",
    "kpi_single_computed_metric": "compound",
    "kpi_single_measure": "kpi",
    "list_distinct_values": "detail",
    "matrix_temporal_category": "matrix",
    "matrix_two_categories": "matrix",
    "matrix_two_categories_multi_measure": "matrix",
    "moving_average_time": "unsupported",
    "multi_series_time": "time_series",
    "multi_series_time_many_series": "limit",
    "multi_series_time_multi_measure": "ambiguous",
    "multi_series_time_year_month_parts": "time_series",
    "ordinal_bucket_breakdown": "distribution",
    "percent_of_total_by_category": "composition",
    "percent_of_total_by_time_category": "composition",
    "period_over_period_kpi": "period",
    "period_over_period_time": "period",
    "ranking_bottom": "ranking",
    "ranking_multi_measure": "ranking",
    "ranking_top": "ranking",
    "raw_record_request": "raw_records",
    "running_total_time": "unsupported",
    "scatter_candidate": "scatter",
    "single_category_breakdown": "breakdown",
    "single_category_breakdown_many_rows": "limit",
    "single_category_calculated_measure": "breakdown",
    "single_category_many_measures": "limit",
    "single_category_multi_measure": "grouped_comparison",
    "single_category_non_additive_calculated": "breakdown",
    "single_row_with_category": "filtered",
    "small_table_requested": "table",
    "stacked_composition_multi_value": "composition",
    "temporal_category_breakdown": "time_series",
    "temporal_category_no_trend": "table",
    "temporal_part_hour_profile": "temporal_part",
    "temporal_part_month_profile": "temporal_part",
    "temporal_part_week_profile": "temporal_part",
    "temporal_part_weekday_profile": "temporal_part",
    "temporal_two_categories": "matrix",
    "temporal_two_categories_rollup": "time_series",
    "temporal_two_category_matrix": "matrix",
    "temporal_two_part_two_category_matrix": "matrix",
    "three_axis_temporal_cube": "matrix",
    "three_category_cube": "matrix",
    "time_series_by_category": "time_series",
    "time_series_date_hour_parts": "time_series",
    "time_series_many_measures": "limit",
    "time_series_month_part_only": "temporal_part",
    "time_series_multi_measure": "time_series",
    "time_series_single_measure": "time_series",
    "time_series_year_month_parts": "time_series",
    "time_series_year_quarter_parts": "time_series",
    "time_series_year_week_parts": "time_series",
    "time_variant_measure_breakdown": "time_variant",
    "time_variant_measure_kpi": "time_variant",
    "time_variant_measure_trend": "time_variant",
    "unsupported_many_axes_many_values": "matrix",
    "unsupported_no_values": "guard",
    "variance_by_category": "period",
    "variance_by_time": "period",
    "variance_kpi": "period",
    "year_part_multi_metric_time": "time_series",
    "year_part_multi_series_time": "time_series",
    "year_part_time_series": "time_series",
    "year_part_time_series_by_category": "time_series",
}


ACTIVE_REGISTRY_CONTRACT_CASES = [
    pytest.param(
        "kpi_single_measure",
        AnalyticalIntent(shape_hint=AnalyticalShape.KPI),
        [_role("revenue", ValueRole.SINGLE_METRIC, source="measure")],
        AnalyticalShape.KPI,
        "kpi",
        {"value": "revenue"},
        id="kpi_single_measure",
    ),
    pytest.param(
        "kpi_multi_measure_set",
        AnalyticalIntent(shape_hint=AnalyticalShape.KPI_SET),
        [
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
            _role("transaction_count", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        AnalyticalShape.KPI_SET,
        "kpi",
        {"cards": "revenue,transaction_count"},
        id="kpi_multi_measure_set",
    ),
    pytest.param(
        "single_category_breakdown",
        AnalyticalIntent(shape_hint=AnalyticalShape.BREAKDOWN),
        [
            _role("payment_method", AxisRole.CATEGORY),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        AnalyticalShape.BREAKDOWN,
        "bar",
        {"x": "payment_method", "y": "revenue"},
        id="single_category_breakdown",
    ),
    pytest.param(
        "single_category_multi_measure",
        AnalyticalIntent(shape_hint=AnalyticalShape.GROUPED_COMPARISON),
        [
            _role("payment_method", AxisRole.CATEGORY),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
            _role("transaction_count", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        AnalyticalShape.GROUPED_COMPARISON,
        "grouped_bar",
        {"x": "payment_method", "y": "revenue,transaction_count"},
        id="single_category_multi_measure",
    ),
    pytest.param(
        "ranking_top",
        AnalyticalIntent(shape_hint=AnalyticalShape.RANKING, wants_ranking=True),
        [
            _role("country_code", AxisRole.CATEGORY),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        AnalyticalShape.RANKING,
        "h_bar",
        {"y": "country_code", "x": "revenue"},
        id="ranking_top",
    ),
    pytest.param(
        "composition_single_category",
        AnalyticalIntent(
            shape_hint=AnalyticalShape.STACKED_COMPOSITION,
            wants_composition=True,
        ),
        [
            _role("payment_method", AxisRole.CATEGORY),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        AnalyticalShape.STACKED_COMPOSITION,
        "pie",
        {"segment": "payment_method", "value": "revenue"},
        id="composition_single_category",
    ),
    pytest.param(
        "time_series_single_measure",
        AnalyticalIntent(shape_hint=AnalyticalShape.TIME_SERIES, wants_trend=True),
        [
            _role("period", AxisRole.TEMPORAL),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        AnalyticalShape.TIME_SERIES,
        "line",
        {"x": "period", "y": "revenue"},
        id="time_series_single_measure",
    ),
    pytest.param(
        "time_series_multi_measure",
        AnalyticalIntent(shape_hint=AnalyticalShape.MULTI_METRIC_TIME, wants_trend=True),
        [
            _role("period", AxisRole.TEMPORAL),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
            _role("transaction_count", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        AnalyticalShape.MULTI_METRIC_TIME,
        "multi_line_wide",
        {"x": "period", "lines": "revenue,transaction_count"},
        id="time_series_multi_measure",
    ),
    pytest.param(
        "multi_series_time",
        AnalyticalIntent(
            shape_hint=AnalyticalShape.MULTI_SERIES_TIME,
            wants_trend=True,
            wants_separate_series=True,
        ),
        [
            _role("period", AxisRole.TEMPORAL),
            _role("payment_method", AxisRole.CATEGORY),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        AnalyticalShape.MULTI_SERIES_TIME,
        "multi_line",
        {"x": "period", "series": "payment_method", "y": "revenue"},
        id="multi_series_time",
    ),
    pytest.param(
        "year_part_time_series",
        AnalyticalIntent(shape_hint=AnalyticalShape.TIME_SERIES, wants_trend=True),
        [
            _role("business_date_year", AxisRole.TEMPORAL),
            _role("business_date_month", AxisRole.TEMPORAL),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        AnalyticalShape.TIME_SERIES,
        "line",
        {"x": "period", "y": "revenue"},
        id="year_part_time_series",
    ),
    pytest.param(
        "year_part_multi_series_time",
        AnalyticalIntent(
            shape_hint=AnalyticalShape.MULTI_SERIES_TIME,
            wants_trend=True,
            wants_separate_series=True,
        ),
        [
            _role("business_date_year", AxisRole.TEMPORAL),
            _role("business_date_month", AxisRole.TEMPORAL),
            _role("payment_method", AxisRole.CATEGORY),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        AnalyticalShape.MULTI_SERIES_TIME,
        "multi_line",
        {"x": "period", "series": "payment_method", "y": "revenue"},
        id="year_part_multi_series_time",
    ),
    pytest.param(
        "year_part_time_series_by_category",
        AnalyticalIntent(shape_hint=AnalyticalShape.TIME_SERIES, wants_trend=True),
        [
            _role("business_date_year", AxisRole.TEMPORAL),
            _role("business_date_month", AxisRole.TEMPORAL),
            _role("payment_method", AxisRole.CATEGORY),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        AnalyticalShape.MULTI_SERIES_TIME,
        "multi_line",
        {"x": "period", "series": "payment_method", "y": "revenue"},
        id="year_part_time_series_by_category",
    ),
    pytest.param(
        "year_part_multi_metric_time",
        AnalyticalIntent(shape_hint=AnalyticalShape.MULTI_METRIC_TIME, wants_trend=True),
        [
            _role("business_date_year", AxisRole.TEMPORAL),
            _role("business_date_month", AxisRole.TEMPORAL),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
            _role("transaction_count", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        AnalyticalShape.MULTI_METRIC_TIME,
        "multi_line_wide",
        {"x": "period", "lines": "revenue,transaction_count"},
        id="year_part_multi_metric_time",
    ),
    pytest.param(
        "time_series_by_category",
        AnalyticalIntent(shape_hint=AnalyticalShape.TIME_SERIES, wants_trend=True),
        [
            _role("period", AxisRole.TEMPORAL),
            _role("payment_method", AxisRole.CATEGORY),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        AnalyticalShape.MULTI_SERIES_TIME,
        "multi_line",
        {"x": "period", "series": "payment_method", "y": "revenue"},
        id="time_series_by_category",
    ),
    pytest.param(
        "temporal_two_category_matrix",
        AnalyticalIntent(shape_hint=AnalyticalShape.MATRIX),
        [
            _role("period", AxisRole.TEMPORAL),
            _role("country_code", AxisRole.CATEGORY),
            _role("payment_method", AxisRole.CATEGORY),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        AnalyticalShape.MATRIX,
        None,
        {},
        id="temporal_two_category_matrix",
    ),
    pytest.param(
        "temporal_two_part_two_category_matrix",
        AnalyticalIntent(shape_hint=AnalyticalShape.MATRIX),
        [
            _role("business_date_year", AxisRole.TEMPORAL),
            _role("business_date_month", AxisRole.TEMPORAL),
            _role("country_code", AxisRole.CATEGORY),
            _role("payment_method", AxisRole.CATEGORY),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        AnalyticalShape.MATRIX,
        None,
        {},
        id="temporal_two_part_two_category_matrix",
    ),
    pytest.param(
        "matrix_two_categories",
        AnalyticalIntent(shape_hint=AnalyticalShape.MATRIX),
        [
            _role("country_code", AxisRole.CATEGORY),
            _role("payment_method", AxisRole.CATEGORY),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        AnalyticalShape.MATRIX,
        "stacked_bar",
        {"x": "country_code", "series": "payment_method", "y": "revenue"},
        id="matrix_two_categories",
    ),
    pytest.param(
        "detail_representable_table",
        AnalyticalIntent(
            shape_hint=AnalyticalShape.DETAIL_TABLE,
            wants_detail_rows=True,
        ),
        [_role("country_code", AxisRole.CATEGORY)],
        AnalyticalShape.DETAIL_TABLE,
        None,
        {},
        id="detail_representable_table",
    ),
    pytest.param(
        "distribution_existing_bucket",
        AnalyticalIntent(
            shape_hint=AnalyticalShape.DISTRIBUTION,
            wants_distribution=True,
        ),
        [
            _role("payment_method", AxisRole.CATEGORY),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        AnalyticalShape.DISTRIBUTION,
        "bar",
        {"x": "payment_method", "y": "revenue"},
        id="distribution_existing_bucket",
    ),
    pytest.param(
        "unsupported_no_values",
        AnalyticalIntent(shape_hint=None),
        [_role("payment_method", AxisRole.CATEGORY)],
        AnalyticalShape.UNSUPPORTED,
        None,
        {},
        id="unsupported_no_values",
    ),
    pytest.param(
        "raw_record_request",
        AnalyticalIntent(
            shape_hint=AnalyticalShape.DETAIL_TABLE,
            wants_detail_rows=True,
            notes=["raw_records_requested"],
        ),
        [_role("transaction_id", AxisRole.CATEGORY)],
        AnalyticalShape.UNSUPPORTED,
        None,
        {},
        id="raw_record_request",
    ),
    pytest.param(
        "scatter_candidate",
        AnalyticalIntent(
            shape_hint=AnalyticalShape.UNSUPPORTED,
            wants_scatter=True,
        ),
        [
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
            _role("transaction_count", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        AnalyticalShape.UNSUPPORTED,
        None,
        {},
        id="scatter_candidate",
    ),
]


def test_every_registry_id_has_explicit_coverage_mode():
    registry_ids = {entry.id for entry in load_shape_registry()}

    assert set(REGISTRY_ID_COVERAGE) == registry_ids
    assert all(REGISTRY_ID_COVERAGE.values())


def test_every_registry_id_has_multiple_simple_and_compound_prompt_variants():
    registry_ids = {entry.id for entry in load_shape_registry()}

    assert set(REGISTRY_PROMPT_GROUP_BY_ID) == registry_ids
    for registry_id, group_name in REGISTRY_PROMPT_GROUP_BY_ID.items():
        prompts = PROMPT_VARIANT_GROUPS[group_name]
        simple = prompts["simple"]
        compound = prompts["compound"]
        assert len(simple) >= 2, registry_id
        assert len(compound) >= 2, registry_id
        assert len(set(simple)) == len(simple), registry_id
        assert len(set(compound)) == len(compound), registry_id
        assert all(isinstance(prompt, str) and len(prompt.split()) >= 4 for prompt in simple)
        assert all(isinstance(prompt, str) and len(prompt.split()) >= 4 for prompt in compound)


@pytest.mark.parametrize("registry_id", sorted(REGISTRY_PROMPT_GROUP_BY_ID))
def test_registry_prompt_variants_include_simple_and_compound_intent_words(registry_id):
    prompts = PROMPT_VARIANT_GROUPS[REGISTRY_PROMPT_GROUP_BY_ID[registry_id]]

    simple_text = " ".join(prompts["simple"]).lower()
    compound_text = " ".join(prompts["compound"]).lower()
    assert any(
        word in simple_text
        for word in ("show", "list", "chart", "plot", "trend", "count", "compare")
    ), registry_id
    assert any(
        word in compound_text
        for word in ("compare", "ratio", "percentage", "percent", "share", "contribution", "versus")
    ), registry_id


def test_active_runtime_contract_cases_are_declared_in_coverage_manifest():
    active_case_ids = {case.values[0] for case in ACTIVE_REGISTRY_CONTRACT_CASES}
    manifest_active_ids = {
        registry_id
        for registry_id, mode in REGISTRY_ID_COVERAGE.items()
        if mode.startswith("active_contract")
    }

    assert active_case_ids == manifest_active_ids


def test_review_table_coverage_rows_are_explicit_catalogue_or_future_rows():
    entries = load_shape_registry()
    catalogue_entries = [
        entry for entry in entries
        if entry.family == "review_table_coverage"
    ]

    assert catalogue_entries
    for entry in catalogue_entries:
        assert entry.match == {"intent": "coverage_only"}
        assert REGISTRY_ID_COVERAGE[entry.id].startswith("catalogue_")


def test_runtime_registry_families_do_not_use_catalogue_only_intent():
    for entry in load_shape_registry():
        if entry.family != "review_table_coverage":
            assert entry.match.get("intent") != "coverage_only"


@pytest.mark.parametrize(
    "registry_id,intent,field_roles,expected_shape,expected_chart,expected_binding",
    ACTIVE_REGISTRY_CONTRACT_CASES,
)
def test_active_runtime_registry_entries_select_exact_contract(
    registry_id,
    intent,
    field_roles,
    expected_shape,
    expected_chart,
    expected_binding,
):
    contract = match_shape_contract(
        intent=intent,
        field_roles=field_roles,
        call=object(),
        chart_type_selector="auto",
        limits=ShapeLimits(),
    )

    assert contract.shape == expected_shape
    assert contract.chart_preference == expected_chart
    assert contract.renderer_binding == expected_binding
    assert f"registry_entry={registry_id}" in contract.notes


def test_shape_registry_loads_grouped_families_and_entries_compile():
    entries = load_shape_registry()

    assert entries
    families = {entry.family for entry in entries}
    assert {"kpi", "temporal", "ranking_composition", "guards"}.issubset(families)
    assert all(entry.renderer_binding is not None for entry in entries)
    assert all(entry.fallback in {"table", "clarify", "refuse"} for entry in entries)
    assert all(entry.data_quality_checks is not None for entry in entries)
    assert all(entry.narration_fact_contract for entry in entries)


def test_agent_service_dockerfile_packages_config_directory():
    root = Path(__file__).resolve().parents[4]
    dockerfile = root / "tessallite" / "services" / "agent-service" / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")

    assert "COPY services/agent-service/config/ /app/config/" in text
    assert "COPY --from=builder /app/config /app/config" in text


def test_registry_covers_review_table_ids():
    root = Path(__file__).resolve().parents[4]
    table = root / "work" / "agent-shape-registry-review-table.md"
    ids = set()
    for line in table.read_text(encoding="utf-8").splitlines():
        if line.startswith("| `"):
            first_cell = line.split("|", 2)[1].strip()
            ids.add(first_cell.strip("`"))

    registry_ids = {entry.id for entry in load_shape_registry()}
    missing = sorted(ids - registry_ids)
    assert missing == []


def test_kpi_contract_matches_zero_axes_one_value():
    contract = match_shape_contract(
        intent=AnalyticalIntent(shape_hint=AnalyticalShape.KPI),
        field_roles=[_role("revenue", ValueRole.SINGLE_METRIC, source="measure")],
        call=object(),
        chart_type_selector="auto",
        limits=ShapeLimits(),
    )

    assert contract.shape == AnalyticalShape.KPI
    assert contract.chart_preference == "kpi"
    assert contract.value_role == ValueRole.SINGLE_METRIC


def test_multi_series_trend_maps_to_multi_line_contract():
    contract = match_shape_contract(
        intent=AnalyticalIntent(
            shape_hint=AnalyticalShape.MULTI_SERIES_TIME,
            wants_trend=True,
            wants_separate_series=True,
        ),
        field_roles=[
            _role("period", AxisRole.TEMPORAL),
            _role("city", AxisRole.CATEGORY),
            _role("ratio", ValueRole.COMPOUND_METRIC, source="computed"),
        ],
        call=object(),
        chart_type_selector="auto",
        limits=ShapeLimits(max_line_series=8),
    )

    assert contract.shape == AnalyticalShape.MULTI_SERIES_TIME
    assert contract.chart_preference == "multi_line"
    assert contract.renderer_binding == {"x": "period", "series": "city", "y": "ratio"}
    assert contract.max_series == 8


def test_generic_line_request_is_compatible_with_multi_series_renderer():
    contract = match_shape_contract(
        intent=AnalyticalIntent(
            shape_hint=AnalyticalShape.MULTI_SERIES_TIME,
            wants_trend=True,
            wants_separate_series=True,
        ),
        field_roles=[
            _role("period", AxisRole.TEMPORAL),
            _role("city", AxisRole.CATEGORY),
            _role("ratio", ValueRole.COMPOUND_METRIC, source="computed"),
        ],
        call=object(),
        chart_type_selector="line",
        limits=ShapeLimits(max_line_series=8),
    )

    assert contract.shape == AnalyticalShape.MULTI_SERIES_TIME
    assert contract.chart_preference == "multi_line"
    assert contract.table_required is False
    assert "chart_request_rejected=line" not in contract.notes


def test_generic_line_request_is_compatible_with_multi_metric_time_renderer():
    contract = match_shape_contract(
        intent=AnalyticalIntent(
            shape_hint=AnalyticalShape.MULTI_METRIC_TIME,
            wants_trend=True,
        ),
        field_roles=[
            _role("period", AxisRole.TEMPORAL),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
            _role("cost", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        call=object(),
        chart_type_selector="line",
        limits=ShapeLimits(max_line_series=8),
    )

    assert contract.shape == AnalyticalShape.MULTI_METRIC_TIME
    assert contract.chart_preference == "multi_line_wide"
    assert contract.table_required is False


def test_year_month_multi_metric_pair_maps_to_stable_period_multi_line_wide():
    contract = match_shape_contract(
        intent=detect_analytical_intent("Show revenue and transaction count by month"),
        field_roles=[
            _role("business_date_year", AxisRole.TEMPORAL),
            _role("business_date_month", AxisRole.TEMPORAL),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
            _role("transaction_count", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        call=object(),
        chart_type_selector="auto",
        limits=ShapeLimits(max_line_series=8),
    )

    assert contract.shape == AnalyticalShape.MULTI_METRIC_TIME
    assert contract.chart_preference == "multi_line_wide"
    assert contract.table_required is False
    assert contract.renderer_binding == {
        "x": "period",
        "lines": "revenue,transaction_count",
    }


def test_natural_temporal_category_phrase_maps_to_multi_series():
    contract = match_shape_contract(
        intent=detect_analytical_intent("revenue by month and region"),
        field_roles=[
            _role("month", AxisRole.TEMPORAL),
            _role("region", AxisRole.CATEGORY),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        call=object(),
        chart_type_selector="auto",
        limits=ShapeLimits(max_line_series=8),
    )

    assert contract.shape == AnalyticalShape.MULTI_SERIES_TIME
    assert contract.chart_preference == "multi_line"
    assert contract.renderer_binding == {"x": "month", "series": "region", "y": "revenue"}


def test_year_month_temporal_pair_maps_to_stable_period_line():
    contract = match_shape_contract(
        intent=detect_analytical_intent("Show total revenue by month"),
        field_roles=[
            _role("business_date_year", AxisRole.TEMPORAL),
            _role("business_date_month", AxisRole.TEMPORAL),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        call=object(),
        chart_type_selector="auto",
        limits=ShapeLimits(),
    )

    assert contract.shape == AnalyticalShape.TIME_SERIES
    assert contract.chart_preference == "line"
    assert contract.renderer_binding == {"x": "period", "y": "revenue"}


def test_year_month_category_pair_maps_to_stable_period_multi_line():
    contract = match_shape_contract(
        intent=detect_analytical_intent(
            "Show a separate line chart of total revenue by month for each payment method"
        ),
        field_roles=[
            _role("business_date_year", AxisRole.TEMPORAL),
            _role("business_date_month", AxisRole.TEMPORAL),
            _role("payment_method", AxisRole.CATEGORY),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        call=object(),
        chart_type_selector="auto",
        limits=ShapeLimits(),
    )

    assert contract.shape == AnalyticalShape.MULTI_SERIES_TIME
    assert contract.chart_preference == "multi_line"
    assert contract.renderer_binding == {
        "x": "period",
        "series": "payment_method",
        "y": "revenue",
    }


def test_year_month_category_trend_phrase_maps_to_stable_period_multi_line():
    contract = match_shape_contract(
        intent=detect_analytical_intent("Show payment-method revenue trends by month"),
        field_roles=[
            _role("business_date_year", AxisRole.TEMPORAL),
            _role("business_date_month", AxisRole.TEMPORAL),
            _role("payment_method", AxisRole.CATEGORY),
            _role("Revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        call=object(),
        chart_type_selector="auto",
        limits=ShapeLimits(),
    )

    assert contract.shape == AnalyticalShape.MULTI_SERIES_TIME
    assert contract.chart_preference == "multi_line"
    assert contract.renderer_binding == {
        "x": "period",
        "series": "payment_method",
        "y": "Revenue",
    }


def test_temporal_two_categories_does_not_drop_category_axes():
    contract = match_shape_contract(
        intent=detect_analytical_intent("revenue by month, region and product"),
        field_roles=[
            _role("month", AxisRole.TEMPORAL),
            _role("region", AxisRole.CATEGORY),
            _role("product", AxisRole.CATEGORY),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        call=object(),
        chart_type_selector="auto",
        limits=ShapeLimits(),
    )

    assert contract.shape == AnalyticalShape.MATRIX
    assert contract.chart_preference is None
    assert contract.table_required is True


def test_year_month_two_categories_maps_to_matrix_table_without_dropping_axes():
    contract = match_shape_contract(
        intent=detect_analytical_intent("revenue by month, country and payment method"),
        field_roles=[
            _role("business_date_year", AxisRole.TEMPORAL),
            _role("business_date_month", AxisRole.TEMPORAL),
            _role("country_code", AxisRole.CATEGORY),
            _role("payment_method", AxisRole.CATEGORY),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        call=object(),
        chart_type_selector="auto",
        limits=ShapeLimits(),
    )

    assert contract.shape == AnalyticalShape.MATRIX
    assert contract.chart_preference is None
    assert contract.table_required is True
    assert contract.required_axes == [
        AxisRole.TEMPORAL,
        AxisRole.CATEGORY,
        AxisRole.CATEGORY,
    ]


def test_matrix_two_categories_maps_to_two_axis_stacked_contract():
    contract = match_shape_contract(
        intent=AnalyticalIntent(shape_hint=AnalyticalShape.MATRIX),
        field_roles=[
            _role("region", AxisRole.CATEGORY),
            _role("product", AxisRole.CATEGORY),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        call=object(),
        chart_type_selector="auto",
        limits=ShapeLimits(),
    )

    assert contract.shape == AnalyticalShape.MATRIX
    assert contract.chart_preference == "stacked_bar"
    assert contract.table_required is False
    assert contract.renderer_binding == {
        "x": "region",
        "series": "product",
        "y": "revenue",
    }


def test_composition_allows_pie_only_from_composition_intent():
    contract = match_shape_contract(
        intent=AnalyticalIntent(
            shape_hint=AnalyticalShape.STACKED_COMPOSITION,
            wants_composition=True,
        ),
        field_roles=[
            _role("channel", AxisRole.CATEGORY),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        call=object(),
        chart_type_selector="auto",
        limits=ShapeLimits(),
    )

    assert contract.shape == AnalyticalShape.STACKED_COMPOSITION
    assert contract.chart_preference == "pie"
    assert "pie_suitable" in contract.data_quality_checks


def test_user_forced_pie_on_trend_is_rejected_to_table():
    contract = match_shape_contract(
        intent=AnalyticalIntent(shape_hint=AnalyticalShape.TIME_SERIES, wants_trend=True),
        field_roles=[
            _role("period", AxisRole.TEMPORAL),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        call=object(),
        chart_type_selector="pie",
        limits=ShapeLimits(),
    )

    assert contract.shape == AnalyticalShape.TIME_SERIES
    assert contract.chart_preference is None
    assert contract.table_required is True
    assert "chart_request_rejected=pie" in contract.notes


def test_invalid_registry_entry_missing_required_fields_fails_fast(tmp_path):
    path = tmp_path / "bad_registry.json"
    path.write_text(json.dumps({"families": {"bad": [{"id": "x"}]}}), encoding="utf-8")

    with pytest.raises(ValueError, match="missing keys"):
        load_shape_registry(path)


def test_raw_record_request_uses_unsupported_guard_contract():
    intent = AnalyticalIntent(
        shape_hint=AnalyticalShape.DETAIL_TABLE,
        wants_detail_rows=True,
        notes=["raw_records_requested"],
    )

    contract = match_shape_contract(
        intent=intent,
        field_roles=[_role("city", AxisRole.CATEGORY)],
        call=object(),
        chart_type_selector="auto",
        limits=ShapeLimits(),
    )

    assert contract.shape == AnalyticalShape.UNSUPPORTED
    assert contract.table_required is True
    assert "registry_entry=raw_record_request" in contract.notes
