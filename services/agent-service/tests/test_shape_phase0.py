from __future__ import annotations

import pytest

from shared.schemas.measure_formats import (
    CANONICAL_TIME_VARIANT_ORDER,
    TIME_VARIANT_ALIASES,
    TIME_VARIANT_NAMES,
)
from src.planning.contracts import ShapeContract, ShapeLimits
from src.planning.enums import AnalyticalShape, AxisRole, ValueRole
from src.planning.measure_metadata import (
    CompoundFormulaMetadata,
    MeasureRoleMetadata,
    TIME_VARIANT_TRACE,
)
from src.planning.quality import evaluate_shape_data_quality
from src.planning.roles import infer_field_roles
from src.tools.expressions import normalize_dimensions


def _role_by_name(roles, name):
    return next(role for role in roles if role.name == name)


def test_grained_expression_dimension_is_temporal_with_arbitrary_alias():
    refs = normalize_dimensions([
        {"expr": {"fn": "date_trunc", "args": [{"literal": "month"}, {"field": "opened_at"}]}, "alias": "bucket"}
    ])

    roles = infer_field_roles(
        selected_measures=["accounts"],
        selected_dimensions=["bucket"],
        dimension_refs=refs,
        result_columns=["bucket", "accounts"],
        result_rows=[{"bucket": "2026-01-01", "accounts": 10}],
        model_profiles={},
        measure_metadata={"accounts": MeasureRoleMetadata(name="accounts")},
    )

    bucket = _role_by_name(roles, "bucket")
    assert bucket.role == AxisRole.TEMPORAL
    assert bucket.confidence == "expression"
    assert "expression_function=date_trunc" in bucket.notes
    assert "grain=month" in bucket.notes


def test_temporal_parts_are_cyclical_not_stable_trend_keys():
    roles = infer_field_roles(
        selected_measures=["revenue"],
        selected_dimensions=["month_no", "month_name"],
        dimension_refs=normalize_dimensions(["month_no", "month_name"]),
        result_columns=["month_no", "month_name", "revenue"],
        result_rows=[
            {"month_no": 1, "month_name": "January", "revenue": 100},
            {"month_no": 2, "month_name": "February", "revenue": 200},
        ],
        model_profiles={},
        measure_metadata={"revenue": MeasureRoleMetadata(name="revenue")},
    )

    month_no = _role_by_name(roles, "month_no")
    month_name = _role_by_name(roles, "month_name")
    assert month_no.role == AxisRole.TEMPORAL
    assert "month_part" in month_no.notes
    assert "cyclical_part_not_stable_trend_key" in month_no.notes
    assert month_name.role == AxisRole.TEMPORAL
    assert "calendar_order_required" in month_name.notes


def test_temporal_part_name_is_cyclical_without_result_values():
    roles = infer_field_roles(
        selected_measures=["revenue"],
        selected_dimensions=["month_no"],
        dimension_refs=normalize_dimensions(["month_no"]),
        result_columns=None,
        result_rows=None,
        model_profiles={},
        measure_metadata={"revenue": MeasureRoleMetadata(name="revenue")},
    )

    month_no = _role_by_name(roles, "month_no")
    assert month_no.role == AxisRole.TEMPORAL
    assert "month_part" in month_no.notes
    assert "cyclical_part_not_stable_trend_key" in month_no.notes


def test_profile_time_dimension_keeps_cyclical_part_notes():
    roles = infer_field_roles(
        selected_measures=["revenue"],
        selected_dimensions=["business_date_month"],
        dimension_refs=normalize_dimensions(["business_date_month"]),
        result_columns=None,
        result_rows=None,
        model_profiles={
            "dimensions": {
                "business_date_month": {"kind": "time"},
            },
        },
        measure_metadata={"revenue": MeasureRoleMetadata(name="revenue")},
    )

    month = _role_by_name(roles, "business_date_month")
    assert month.role == AxisRole.TEMPORAL
    assert "profile_kind=time" in month.notes
    assert "month_part" in month.notes
    assert "cyclical_part_not_stable_trend_key" in month.notes


def test_profile_time_dimension_without_date_name_is_temporal():
    roles = infer_field_roles(
        selected_measures=["revenue"],
        selected_dimensions=["created_at"],
        dimension_refs=normalize_dimensions(["created_at"]),
        result_columns=None,
        result_rows=None,
        model_profiles={
            "dimensions": {
                "created_at": {
                    "kind": "time",
                    "is_time_dim": True,
                    "time_grain": "day",
                },
            },
        },
        measure_metadata={"revenue": MeasureRoleMetadata(name="revenue")},
    )

    created_at = _role_by_name(roles, "created_at")
    assert created_at.role == AxisRole.TEMPORAL
    assert created_at.confidence == "metadata"
    assert "profile_kind=time" in created_at.notes
    assert "time_grain=day" in created_at.notes


def test_profile_non_time_dimension_overrides_temporal_name_pattern():
    roles = infer_field_roles(
        selected_measures=["revenue"],
        selected_dimensions=["date_sk"],
        dimension_refs=normalize_dimensions(["date_sk"]),
        result_columns=None,
        result_rows=None,
        model_profiles={
            "dimensions": {
                "date_sk": {
                    "kind": None,
                    "is_time_dim": False,
                    "time_grain": None,
                },
            },
        },
        measure_metadata={"revenue": MeasureRoleMetadata(name="revenue")},
    )

    date_sk = _role_by_name(roles, "date_sk")
    assert date_sk.role == AxisRole.CATEGORY
    assert date_sk.confidence == "metadata"
    assert "profile_dimension_category" in date_sk.notes


def test_profile_year_dimension_keeps_year_note():
    roles = infer_field_roles(
        selected_measures=["revenue"],
        selected_dimensions=["business_date_year"],
        dimension_refs=normalize_dimensions(["business_date_year"]),
        result_columns=None,
        result_rows=None,
        model_profiles={
            "dimensions": {
                "business_date_year": {"kind": "time"},
            },
        },
        measure_metadata={"revenue": MeasureRoleMetadata(name="revenue")},
    )

    year = _role_by_name(roles, "business_date_year")
    assert year.role == AxisRole.TEMPORAL
    assert "profile_kind=time" in year.notes
    assert "year" in year.notes


def test_low_cardinality_string_dimension_defaults_to_category():
    roles = infer_field_roles(
        selected_measures=["revenue"],
        selected_dimensions=["region"],
        dimension_refs=normalize_dimensions(["region"]),
        result_columns=["region", "revenue"],
        result_rows=[{"region": "North", "revenue": 100}, {"region": "South", "revenue": 80}],
        model_profiles={},
        measure_metadata={"revenue": MeasureRoleMetadata(name="revenue")},
    )

    assert _role_by_name(roles, "region").role == AxisRole.CATEGORY


def test_uda_and_calculated_measures_remain_model_measures():
    metadata = {
        "customer_score": MeasureRoleMetadata(
            name="customer_score",
            source_kind="user_defined_attribute",
            default_agg="avg",
            format="decimal_2dp",
            user_defined_attribute_name="customer score",
            is_additive=False,
        ),
        "margin_pct": MeasureRoleMetadata(
            name="margin_pct",
            measure_type="calculated",
            source_kind="calculated_expression",
            expression="profit / revenue",
            calc_agg_mode="post_aggregate",
            format="percent",
            is_additive=False,
        ),
    }

    roles = infer_field_roles(
        selected_measures=["customer_score", "margin_pct"],
        selected_dimensions=[],
        dimension_refs=[],
        result_columns=["customer_score", "margin_pct"],
        result_rows=[{"customer_score": 4.5, "margin_pct": 0.32}],
        model_profiles={},
        measure_metadata=metadata,
    )

    score = _role_by_name(roles, "customer_score")
    margin = _role_by_name(roles, "margin_pct")
    assert score.role == ValueRole.SINGLE_METRIC
    assert score.source == "measure"
    assert "source_kind=user_defined_attribute" in score.notes
    assert margin.role == ValueRole.SINGLE_METRIC
    assert "measure_type=calculated" in margin.notes
    assert "calculated_measure_treated_non_additive" in margin.notes


@pytest.mark.parametrize("variant", sorted(TIME_VARIANT_NAMES))
def test_time_variant_measure_catalog_entries_are_value_measures(variant):
    metadata = MeasureRoleMetadata(
        name=f"revenue_{variant}",
        variant_kind=variant,
        variant_of_measure="revenue",
        window_size=3 if variant in {"trailing_n", "moving_avg_n"} else None,
    )

    roles = infer_field_roles(
        selected_measures=[metadata.name],
        selected_dimensions=[],
        dimension_refs=[],
        result_columns=[metadata.name],
        result_rows=[{metadata.name: 100}],
        model_profiles={},
        measure_metadata={metadata.name: metadata},
    )

    role = _role_by_name(roles, metadata.name)
    assert role.role == ValueRole.TIME_VARIANT_MEASURE
    assert "variant_kind=" + variant in role.notes
    assert metadata.as_trace()["canonical_variant_kind"]


@pytest.mark.parametrize("alias, canonical", sorted(TIME_VARIANT_ALIASES.items()))
def test_time_variant_aliases_canonicalize(alias, canonical):
    metadata = MeasureRoleMetadata(name="m", variant_kind=alias)
    assert metadata.canonical_variant_kind == canonical


@pytest.mark.parametrize("variant", CANONICAL_TIME_VARIANT_ORDER)
def test_ui_admitted_time_variants_are_in_phase0_trace(variant):
    assert variant in TIME_VARIANT_TRACE["canonical_order"]
    assert variant in TIME_VARIANT_TRACE["validation_names"]


def test_compound_result_label_is_computed_value_with_formula_trace():
    formula = CompoundFormulaMetadata(
        result_label="fee_ratio",
        expression_tree={"op": "div", "left": {"ref": "fee.revenue"}, "right": {"ref": "base.revenue"}},
        referenced_steps=["fee", "base"],
        referenced_measures=["fee_amount", "base_amount"],
        operation_kinds=["div"],
        denominator_refs=["base.revenue"],
        null_on_zero_denominator=True,
        alignment_mode="row_aligned",
        is_multi_row=True,
    )

    roles = infer_field_roles(
        selected_measures=[],
        selected_dimensions=[],
        dimension_refs=[],
        result_columns=["fee_ratio"],
        result_rows=[{"fee_ratio": 0.12}],
        model_profiles={},
        measure_metadata={},
        computed_label="fee_ratio",
        compound_formula=formula,
    )

    role = _role_by_name(roles, "fee_ratio")
    assert role.role == ValueRole.COMPOUND_METRIC
    assert role.source == "computed"
    assert "operations=div" in role.notes
    assert "denominators=base.revenue" in role.notes


def test_shape_limits_load_from_mapping_and_trace_defaults():
    limits = ShapeLimits.from_mapping({"max_line_series": 8})
    assert limits.max_line_series == 8
    assert limits.max_pie_segments == 7
    assert limits.as_trace()["max_grouped_measures"] == 6


def test_pie_quality_blocks_negative_zero_sum_and_too_many_segments():
    contract = ShapeContract(
        shape=AnalyticalShape.BREAKDOWN,
        required_axes=[AxisRole.CATEGORY],
        optional_axes=[],
        value_role=ValueRole.SINGLE_METRIC,
        chart_preference="pie",
        table_required=False,
        renderer_binding={"segment": "category", "value": "amount"},
        data_quality_checks=["pie_suitable"],
        narration_fact_contract="composition",
    )

    findings = evaluate_shape_data_quality(
        contract=contract,
        columns=["category", "amount"],
        rows=[
            {"category": "A", "amount": 0},
            {"category": "B", "amount": -1},
            {"category": "C", "amount": 1},
        ],
        limits=ShapeLimits(max_pie_segments=2),
    )

    codes = {finding.code for finding in findings}
    assert {"pie_negative_values", "pie_too_many_segments"}.issubset(codes)
    assert all(finding.severity != "block_answer" for finding in findings)


def test_multi_series_quality_enforces_series_limit_and_coverage_warning():
    contract = ShapeContract(
        shape=AnalyticalShape.MULTI_SERIES_TIME,
        required_axes=[AxisRole.TEMPORAL, AxisRole.SERIES],
        optional_axes=[],
        value_role=ValueRole.SINGLE_METRIC,
        chart_preference="multi_line",
        table_required=False,
        renderer_binding={"x": "period", "series": "region", "value": "amount"},
        data_quality_checks=["time_series", "multi_series"],
        narration_fact_contract="multi_series_time",
    )

    findings = evaluate_shape_data_quality(
        contract=contract,
        columns=["period", "region", "amount"],
        rows=[
            {"period": "2026-01", "region": "A", "amount": 10},
            {"period": "2026-02", "region": "A", "amount": 20},
            {"period": "2026-01", "region": "B", "amount": 30},
            {"period": "2026-01", "region": "C", "amount": None},
        ],
        limits=ShapeLimits(max_line_series=2),
    )

    codes = {finding.code for finding in findings}
    assert "too_many_line_series" in codes
    assert "uneven_series_coverage" in codes
    assert "null_measure_values" in codes
