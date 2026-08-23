from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from src.planning.contracts import FieldRole
from src.planning.enums import AnalyticalShape, AxisRole, ValueRole
from src.planning.intent import AnalyticalIntent, detect_analytical_intent
from src.planning.measure_metadata import MeasureRoleMetadata
from src.planning.roles import infer_field_roles
from src.planning.validation import (
    apply_shape_contract_repairs,
    validate_shape_contract_before_execution,
)
from src.tools.spec import QueryToolCall
from src.tools.expressions import normalize_dimensions


MODEL_ID = "00000000-0000-0000-0000-000000000001"


@dataclass
class _Profile:
    id: UUID
    dimension_names: list[str]


@dataclass
class _Bundle:
    model_profiles: list[_Profile]


def _role(name, role, notes=None, source="dimension"):
    return FieldRole(
        name=name,
        role=role,
        confidence="metadata",
        source=source,
        notes=notes or [],
    )


def _query(measures=None, dimensions=None, sort=None):
    return QueryToolCall(
        model_id=MODEL_ID,
        measures=measures or [],
        dimensions=dimensions or [],
        where=[],
        having=[],
        sort=sort or [],
        limit=100,
    )


def test_kpi_intent_rejects_grouping_axis_before_execution():
    issues = validate_shape_contract_before_execution(
        _query(measures=["revenue"], dimensions=["region"]),
        AnalyticalIntent(shape_hint=AnalyticalShape.KPI),
        [
            _role("region", AxisRole.CATEGORY),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
    )

    assert any(issue.reason == "kpi_requires_no_grouping_axes" for issue in issues)


def test_top_ranking_without_sort_is_repairable_and_adds_sort_limit():
    call = _query(measures=["revenue"], dimensions=["customer"])
    intent = detect_analytical_intent("top 10 customers by revenue")
    roles = [
        _role("customer", AxisRole.CATEGORY),
        _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
    ]

    issues = validate_shape_contract_before_execution(call, intent, roles)
    repaired = apply_shape_contract_repairs(call, intent, roles)

    assert any(issue.reason == "ranking_sort_missing" for issue in issues)
    assert repaired is True
    assert call.sort == [{"name": "revenue", "direction": "desc"}]
    assert call.limit == 10
    assert call.limit_explicit is True


def test_bottom_ranking_repair_sorts_ascending():
    call = _query(measures=["cost"], dimensions=["supplier"])
    intent = detect_analytical_intent("bottom 5 suppliers by cost")
    roles = [
        _role("supplier", AxisRole.CATEGORY),
        _role("cost", ValueRole.SINGLE_METRIC, source="measure"),
    ]

    assert apply_shape_contract_repairs(call, intent, roles) is True
    assert call.sort == [{"name": "cost", "direction": "asc"}]
    assert call.limit == 5


def test_scalar_superlative_is_allowed_as_kpi_value():
    call = _query(measures=["revenue"], dimensions=[])
    intent = detect_analytical_intent("What is the highest revenue?")
    roles = [_role("revenue", ValueRole.SINGLE_METRIC, source="measure")]

    issues = validate_shape_contract_before_execution(call, intent, roles)

    assert not any(issue.reason == "ranking_requires_category_axis" for issue in issues)
    assert apply_shape_contract_repairs(call, intent, roles) is False
    assert call.sort == []


def test_trend_requires_temporal_axis():
    issues = validate_shape_contract_before_execution(
        _query(measures=["revenue"], dimensions=["region"]),
        AnalyticalIntent(shape_hint=AnalyticalShape.TIME_SERIES, wants_trend=True),
        [
            _role("region", AxisRole.CATEGORY),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
    )

    assert any(issue.reason == "trend_requires_temporal_axis" for issue in issues)


def test_trend_rejects_lone_cyclical_month_part():
    issues = validate_shape_contract_before_execution(
        _query(measures=["revenue"], dimensions=["month_no"]),
        AnalyticalIntent(
            shape_hint=AnalyticalShape.TIME_SERIES,
            wants_trend=True,
        ),
        [
            _role(
                "month_no",
                AxisRole.TEMPORAL,
                notes=["month_part", "cyclical_part_not_stable_trend_key"],
            ),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
    )

    assert any(
        issue.reason == "cyclical_temporal_part_not_stable_trend_key"
        for issue in issues
    )


def test_trend_repair_replaces_lone_month_part_with_date_grain():
    call = _query(measures=["revenue"], dimensions=["business_date_month"])
    intent = detect_analytical_intent("Show total revenue by month")
    roles = [
        _role(
            "business_date_month",
            AxisRole.TEMPORAL,
            notes=["month_part", "cyclical_part_not_stable_trend_key"],
        ),
        _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
    ]
    bundle = _Bundle([
        _Profile(
            id=UUID(MODEL_ID),
            dimension_names=["business_date", "business_date_month"],
        ),
    ])

    repaired = apply_shape_contract_repairs(call, intent, roles, bundle)

    assert repaired is True
    assert call.dimensions == ["business_date_month"]
    assert call.dimension_refs
    assert call.dimension_refs[0].raw == {"name": "business_date", "grain": "month"}
    assert call.dimension_refs[0].is_bare is False


def test_trend_repair_prefers_semantic_year_plus_month_dimensions():
    call = _query(
        measures=["revenue"],
        dimensions=["business_date_month", "payment_method"],
    )
    intent = detect_analytical_intent(
        "Show a separate line chart of total revenue by month for each payment method"
    )
    roles = [
        _role(
            "business_date_month",
            AxisRole.TEMPORAL,
            notes=["month_part", "cyclical_part_not_stable_trend_key"],
        ),
        _role("payment_method", AxisRole.CATEGORY),
        _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
    ]
    bundle = _Bundle([
        _Profile(
            id=UUID(MODEL_ID),
            dimension_names=[
                "business_date",
                "business_date_year",
                "business_date_month",
                "payment_method",
            ],
        ),
    ])

    repaired = apply_shape_contract_repairs(call, intent, roles, bundle)

    assert repaired is True
    assert call.dimensions == [
        "business_date_year",
        "business_date_month",
        "payment_method",
    ]
    assert [ref.raw for ref in call.dimension_refs or []] == call.dimensions
    assert call.sort == [
        {"name": "business_date_year", "direction": "asc"},
        {"name": "business_date_month", "direction": "asc"},
    ]


def test_cross_year_trend_allows_year_plus_month_parts():
    issues = validate_shape_contract_before_execution(
        _query(measures=["revenue"], dimensions=["year", "month_no"]),
        AnalyticalIntent(
            shape_hint=AnalyticalShape.TIME_SERIES,
            wants_trend=True,
            requested_period_may_cross_year=True,
        ),
        [
            _role("year", AxisRole.TEMPORAL, notes=["year"]),
            _role(
                "month_no",
                AxisRole.TEMPORAL,
                notes=["month_part", "cyclical_part_not_stable_trend_key"],
            ),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
    )

    assert not any(
        issue.reason == "cyclical_temporal_part_not_stable_trend_key"
        for issue in issues
    )


def test_multi_series_requires_temporal_series_and_one_value():
    issues = validate_shape_contract_before_execution(
        _query(measures=["revenue", "cost"], dimensions=["period"]),
        AnalyticalIntent(
            shape_hint=AnalyticalShape.MULTI_SERIES_TIME,
            wants_trend=True,
            wants_separate_series=True,
        ),
        [
            _role("period", AxisRole.TEMPORAL),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
            _role("cost", ValueRole.SINGLE_METRIC, source="measure"),
        ],
    )

    reasons = {issue.reason for issue in issues}
    assert "multi_series_time_requires_series_axis" in reasons
    assert "multi_series_time_requires_one_value" in reasons


def test_matrix_shape_is_allowed_table_only_before_execution():
    issues = validate_shape_contract_before_execution(
        _query(measures=["revenue"], dimensions=["region", "product"]),
        AnalyticalIntent(shape_hint=AnalyticalShape.MATRIX),
        [
            _role("region", AxisRole.CATEGORY),
            _role("product", AxisRole.CATEGORY),
            _role("revenue", ValueRole.SINGLE_METRIC, source="measure"),
        ],
    )

    assert issues == []


def test_raw_record_request_is_not_silently_converted_to_aggregate_table():
    intent = detect_analytical_intent("show transactions for London")
    issues = validate_shape_contract_before_execution(
        _query(dimensions=["city"]),
        intent,
        [_role("city", AxisRole.CATEGORY)],
    )

    assert any(
        issue.reason == "raw_records_not_supported_by_aggregate_tools"
        for issue in issues
    )


def test_non_additive_measure_blocks_stacked_composition():
    issues = validate_shape_contract_before_execution(
        _query(measures=["margin_pct"], dimensions=["region"]),
        AnalyticalIntent(
            shape_hint=AnalyticalShape.STACKED_COMPOSITION,
            wants_composition=True,
        ),
        [
            _role("region", AxisRole.CATEGORY),
            _role(
                "margin_pct",
                ValueRole.SINGLE_METRIC,
                notes=["calculated_measure_treated_non_additive"],
                source="measure",
            ),
        ],
    )

    assert any(issue.reason == "composition_requires_additive_values" for issue in issues)


def test_semi_additive_measure_blocks_stacked_composition_end_to_end():
    """Bug-8843: stale additive persistence must not bypass shape validation."""
    metadata = MeasureRoleMetadata(
        name="account_balance",
        default_agg="sum",
        is_additive=True,
        semi_additive_behavior="last_non_empty",
    )
    roles = infer_field_roles(
        selected_measures=["account_balance"],
        selected_dimensions=["region"],
        dimension_refs=normalize_dimensions(["region"]),
        result_columns=["region", "account_balance"],
        result_rows=[{"region": "EU", "account_balance": 90}],
        model_profiles={},
        measure_metadata={"account_balance": metadata},
    )

    issues = validate_shape_contract_before_execution(
        _query(measures=["account_balance"], dimensions=["region"]),
        AnalyticalIntent(
            shape_hint=AnalyticalShape.STACKED_COMPOSITION,
            wants_composition=True,
        ),
        roles,
    )

    assert "semi_additive_treated_non_additive" in next(
        role for role in roles if role.name == "account_balance"
    ).notes
    assert any(issue.reason == "composition_requires_additive_values" for issue in issues)
