from __future__ import annotations

from src.planning.contracts import ShapeContract, ShapeLimits
from src.planning.enums import AnalyticalShape, AxisRole, ValueRole
from src.planning.quality import evaluate_shape_data_quality


def _contract(shape, binding, checks, axes=None):
    return ShapeContract(
        shape=shape,
        required_axes=axes or [],
        optional_axes=[],
        value_role=ValueRole.SINGLE_METRIC,
        chart_preference=binding.get("chart"),
        table_required=False,
        renderer_binding={k: v for k, v in binding.items() if k != "chart"},
        data_quality_checks=checks,
        narration_fact_contract=shape.value,
    )


def test_category_limit_blocks_chart_not_answer():
    contract = _contract(
        AnalyticalShape.BREAKDOWN,
        {"chart": "bar", "x": "category", "y": "amount"},
        ["category_limit"],
        [AxisRole.CATEGORY],
    )
    rows = [{"category": f"C{i}", "amount": i} for i in range(4)]

    findings = evaluate_shape_data_quality(
        contract=contract,
        columns=["category", "amount"],
        rows=rows,
        limits=ShapeLimits(max_bar_categories=3),
    )

    assert findings[0].code == "too_many_bar_categories"
    assert findings[0].severity == "block_chart"


def test_monthly_time_series_gap_is_reported():
    contract = _contract(
        AnalyticalShape.TIME_SERIES,
        {"chart": "line", "x": "period", "y": "amount"},
        ["time_series"],
        [AxisRole.TEMPORAL],
    )

    findings = evaluate_shape_data_quality(
        contract=contract,
        columns=["period", "amount"],
        rows=[
            {"period": "2026-01", "amount": 10},
            {"period": "2026-03", "amount": 30},
        ],
        limits=ShapeLimits(),
    )

    assert any(finding.code == "missing_monthly_periods" for finding in findings)


def test_large_matrix_result_gets_table_first_warning():
    contract = _contract(
        AnalyticalShape.MATRIX,
        {},
        ["matrix_size"],
        [AxisRole.CATEGORY, AxisRole.CATEGORY],
    )
    rows = [{"row": i, "col": i % 10, "amount": i} for i in range(30)]

    findings = evaluate_shape_data_quality(
        contract=contract,
        columns=["row", "col", "amount"],
        rows=rows,
        limits=ShapeLimits(max_bar_categories=5, max_grouped_measures=5),
    )

    assert any(finding.code == "large_matrix_table" for finding in findings)


def test_large_detail_table_gets_warning():
    contract = _contract(
        AnalyticalShape.DETAIL_TABLE,
        {},
        ["table_size"],
        [AxisRole.DETAIL],
    )
    rows = [{"name": f"R{i}"} for i in range(4)]

    findings = evaluate_shape_data_quality(
        contract=contract,
        columns=["name"],
        rows=rows,
        limits=ShapeLimits(max_bar_categories=3),
    )

    assert any(finding.code == "large_table_result" for finding in findings)


def test_percentage_share_composition_can_render_as_pie():
    contract = _contract(
        AnalyticalShape.STACKED_COMPOSITION,
        {"chart": "pie", "segment": "category", "value": "share"},
        ["pie_suitable"],
        [AxisRole.CATEGORY],
    )

    findings = evaluate_shape_data_quality(
        contract=contract,
        columns=["category", "share"],
        rows=[
            {"category": "CARD", "share": 60},
            {"category": "CASH", "share": 40},
        ],
        limits=ShapeLimits(),
    )

    assert not any(finding.severity == "block_chart" for finding in findings)


def test_chart_max_rows_blocks_chart_output():
    contract = _contract(
        AnalyticalShape.BREAKDOWN,
        {"chart": "bar", "x": "category", "y": "amount"},
        [],
        [AxisRole.CATEGORY],
    )

    findings = evaluate_shape_data_quality(
        contract=contract,
        columns=["category", "amount"],
        rows=[
            {"category": "A", "amount": 1},
            {"category": "B", "amount": 2},
        ],
        limits=ShapeLimits(max_chart_rows=1),
    )

    assert [finding.code for finding in findings] == ["too_many_chart_rows"]
    assert findings[0].severity == "block_chart"
