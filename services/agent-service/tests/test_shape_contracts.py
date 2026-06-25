from __future__ import annotations

from src.planning.contracts import FieldRole, ShapeContract, ShapeLimits
from src.planning.enums import AnalyticalShape, AxisRole, ValueRole
from src.planning.intent import AnalyticalIntent
from src.planning.shape import normalize_result_shape


def _role(name, role, notes=None, source="dimension"):
    return FieldRole(
        name=name,
        role=role,
        confidence="metadata",
        source=source,
        notes=notes or [],
    )


def _contract(shape, binding, checks=None, chart=None, axes=None, table_required=False):
    return ShapeContract(
        shape=shape,
        required_axes=axes or [],
        optional_axes=[],
        value_role=ValueRole.SINGLE_METRIC,
        chart_preference=chart,
        table_required=table_required,
        renderer_binding=binding,
        data_quality_checks=checks or [],
        narration_fact_contract=shape.value,
    )


def test_year_month_rows_build_stable_period_and_sort():
    contract = _contract(
        AnalyticalShape.TIME_SERIES,
        {"x": "month_no", "y": "ratio"},
        ["time_series"],
        chart="line",
        axes=[AxisRole.TEMPORAL],
    )
    rows = [
        {"year": 2026, "month_no": 2, "ratio": 0.2},
        {"year": 2026, "month_no": 1, "ratio": 0.1},
    ]

    shaped = normalize_result_shape(
        ["year", "month_no", "ratio"],
        rows,
        AnalyticalIntent(shape_hint=AnalyticalShape.TIME_SERIES, wants_trend=True),
        [
            _role("year", AxisRole.TEMPORAL, notes=["year"]),
            _role("month_no", AxisRole.TEMPORAL, notes=["month_part", "cyclical_part_not_stable_trend_key"]),
            _role("ratio", ValueRole.COMPOUND_METRIC, source="computed"),
        ],
        contract,
        ShapeLimits(),
    )

    assert shaped.columns == ["period", "ratio"]
    assert shaped.rows == [["2026-01", 0.1], ["2026-02", 0.2]]
    assert rows[0] == {"year": 2026, "month_no": 2, "ratio": 0.2}
    assert contract.renderer_binding["x"] == "month_no"
    assert shaped.contract.renderer_binding["x"] == "period"


def test_month_name_axis_sorts_by_calendar_order():
    contract = _contract(
        AnalyticalShape.TIME_SERIES,
        {"x": "month_name", "y": "amount"},
        ["time_series"],
        chart="line",
        axes=[AxisRole.TEMPORAL],
    )

    shaped = normalize_result_shape(
        ["month_name", "amount"],
        [
            {"month_name": "March", "amount": 3},
            {"month_name": "January", "amount": 1},
            {"month_name": "February", "amount": 2},
        ],
        AnalyticalIntent(shape_hint=AnalyticalShape.TIME_SERIES, wants_trend=True),
        [
            _role("month_name", AxisRole.TEMPORAL, notes=["month_name", "calendar_order_required"]),
            _role("amount", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        contract,
        ShapeLimits(),
    )

    assert [row[0] for row in shaped.rows] == ["January", "February", "March"]


def test_quality_block_chart_for_too_many_categories_keeps_table_output():
    contract = _contract(
        AnalyticalShape.BREAKDOWN,
        {"x": "category", "y": "amount"},
        ["category_limit"],
        chart="bar",
        axes=[AxisRole.CATEGORY],
    )

    shaped = normalize_result_shape(
        ["category", "amount"],
        [{"category": f"C{i}", "amount": i} for i in range(4)],
        AnalyticalIntent(shape_hint=AnalyticalShape.BREAKDOWN),
        [
            _role("category", AxisRole.CATEGORY),
            _role("amount", ValueRole.SINGLE_METRIC, source="measure"),
        ],
        contract,
        ShapeLimits(max_bar_categories=3),
    )

    assert shaped.chart_type is None
    assert shaped.output_mode == "table"
    assert "chart_blocked_by_quality" in shaped.notes


def test_kpi_result_gets_kpi_output_mode():
    contract = _contract(
        AnalyticalShape.KPI,
        {"value": "revenue"},
        chart="kpi",
    )

    shaped = normalize_result_shape(
        ["revenue"],
        [{"revenue": 100}],
        AnalyticalIntent(shape_hint=AnalyticalShape.KPI),
        [_role("revenue", ValueRole.SINGLE_METRIC, source="measure")],
        contract,
        ShapeLimits(),
    )

    assert shaped.output_mode == "kpi"
    assert shaped.chart_type == "kpi"


def test_multi_series_facts_include_series_coverage():
    contract = _contract(
        AnalyticalShape.MULTI_SERIES_TIME,
        {"x": "period", "series": "city", "y": "ratio"},
        ["time_series", "multi_series"],
        chart="multi_line",
        axes=[AxisRole.TEMPORAL, AxisRole.SERIES],
    )

    shaped = normalize_result_shape(
        ["period", "city", "ratio"],
        [
            {"period": "2026-01", "city": "London", "ratio": 0.1},
            {"period": "2026-02", "city": "London", "ratio": 0.2},
            {"period": "2026-01", "city": "Manchester", "ratio": 0.3},
        ],
        AnalyticalIntent(shape_hint=AnalyticalShape.MULTI_SERIES_TIME, wants_trend=True, wants_separate_series=True),
        [
            _role("period", AxisRole.TEMPORAL),
            _role("city", AxisRole.CATEGORY),
            _role("ratio", ValueRole.COMPOUND_METRIC, source="computed"),
        ],
        contract,
        ShapeLimits(),
    )

    assert shaped.narration_facts.series_coverage["London"]["row_count"] == 2
    assert shaped.narration_facts.series_coverage["Manchester"]["row_count"] == 1
    assert any(finding.code == "uneven_series_coverage" for finding in shaped.quality_findings)
