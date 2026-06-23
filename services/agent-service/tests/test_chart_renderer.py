import json

from src.charts.renderer import render_chart, render_table, render_visual_artifact


# ---------------------------------------------------------------------------
# Edge cases / empty returns
# ---------------------------------------------------------------------------

def test_none_chart_type_returns_empty():
    result = render_chart(None, ["region", "revenue"], [["North", 100]], "default", "md", False)
    assert result == ""


def test_empty_rows_returns_empty():
    result = render_chart("bar", ["region", "revenue"], [], "default", "md", False)
    assert result == ""


def test_empty_columns_returns_empty():
    result = render_chart("bar", [], [["North", 100]], "default", "md", False)
    assert result == ""


def test_unknown_chart_type_returns_empty():
    result = render_chart("radar", ["x", "y"], [[1, 2]], "default", "md", False)
    assert result == ""


def test_render_table_returns_escaped_table_html():
    html = render_table(["name"], [["<script>"]])

    assert "<table" in html
    assert "rendered-data-table" in html
    assert "padding:8px 10px" in html
    assert "border:1px solid #c8d6cf" in html
    assert "&lt;script&gt;" in html


def test_visual_artifact_returns_structured_echarts_payload():
    payload = render_visual_artifact(
        "bar",
        ["region", "revenue"],
        [["North", 100], ["South", 200]],
        "default",
        "md",
        True,
    )

    data = json.loads(payload)
    assert data["kind"] == "tessallite.visual.v1"
    assert data["renderer"] == "echarts"
    assert data["chart_type"] == "bar"
    assert data["columns"] == ["region", "revenue"]
    assert data["rows"] == [
        {"region": "North", "revenue": 100},
        {"region": "South", "revenue": 200},
    ]
    assert data["include_table"] is True
    assert "charts-css" not in payload


# ---------------------------------------------------------------------------
# KPI
# ---------------------------------------------------------------------------

def test_kpi_returns_html_with_value():
    h = render_chart("kpi", ["revenue"], [[1_234_567]], "default", "md", False)
    assert "1" in h
    assert "<div" in h.lower()


def test_kpi_integer_formatted_with_commas():
    h = render_chart("kpi", ["count"], [[1000000]], "default", "md", False)
    assert "1,000,000" in h


def test_kpi_table_formats_scientific_decimal_without_scientific_notation():
    from decimal import Decimal
    h = render_chart("kpi", ["count"], [[Decimal("1.0E+5")]], "default", "md", True)
    assert "100,000" in h
    assert "1.0E+5" not in h


# ---------------------------------------------------------------------------
# Bar (vertical) — Charts.css "column"
# ---------------------------------------------------------------------------

def test_bar_returns_charts_css_column():
    h = render_chart(
        "bar",
        ["region", "revenue"],
        [["North", 100], ["South", 200]],
        "default", "md", False,
    )
    assert "charts-css" in h
    assert "column" in h
    assert "show-data-on-hover" in h
    assert "data-spacing-5" in h
    assert "North" in h
    assert "South" in h


def test_bar_normalizes_values():
    h = render_chart(
        "bar",
        ["region", "revenue"],
        [["North", 100], ["South", 200]],
        "default", "md", False,
    )
    assert "--size: 0.5" in h
    assert "--size: 1.0" in h


# ---------------------------------------------------------------------------
# Horizontal bar — Charts.css "bar"
# ---------------------------------------------------------------------------

def test_h_bar_returns_charts_css_bar():
    h = render_chart(
        "h_bar",
        ["category", "count"],
        [[f"Cat{i}", i * 10] for i in range(1, 16)],
        "default", "md", False,
    )
    assert "charts-css" in h
    assert "charts-css bar " in h
    assert "show-data-on-hover" in h
    assert "data-spacing-5" in h
    assert "Cat1" in h


# ---------------------------------------------------------------------------
# Line chart — Charts.css "line"
# ---------------------------------------------------------------------------

def test_line_chart_returns_charts_css_line():
    h = render_chart(
        "line",
        ["month", "revenue"],
        [["2024-01", 100], ["2024-02", 150], ["2024-03", 200]],
        "default", "md", False,
    )
    assert "charts-css" in h
    assert "line" in h
    assert "--start" in h
    assert "--end" in h


def test_line_chart_has_labels():
    h = render_chart(
        "line",
        ["month", "revenue"],
        [["2024-01", 100], ["2024-02", 150]],
        "default", "md", False,
    )
    assert "2024-01" in h
    assert "2024-02" in h


# ---------------------------------------------------------------------------
# Multi-line chart — Charts.css "line multiple"
# ---------------------------------------------------------------------------

def test_multi_line_returns_charts_css_line_multiple():
    rows = [
        ["2024-01", "North", 100],
        ["2024-01", "South", 80],
        ["2024-02", "North", 120],
        ["2024-02", "South", 90],
    ]
    h = render_chart("multi_line", ["month", "region", "revenue"], rows, "default", "md", False)
    assert "charts-css" in h
    assert "line" in h
    assert "multiple" in h


# ---------------------------------------------------------------------------
# Multi-line-wide chart — Charts.css "line multiple" from wide-format data
# ---------------------------------------------------------------------------

def test_multi_line_wide_returns_charts_css_line_multiple():
    rows = [
        ["2024-01", 100, 60],
        ["2024-02", 150, 80],
        ["2024-03", 200, 90],
    ]
    h = render_chart("multi_line_wide", ["month", "revenue", "cost"], rows, "default", "md", False)
    assert "charts-css" in h
    assert "line" in h
    assert "multiple" in h


def test_multi_line_wide_has_legend_with_series_names():
    rows = [
        ["2024-01", 100, 60],
        ["2024-02", 150, 80],
    ]
    h = render_chart("multi_line_wide", ["month", "revenue", "cost"], rows, "default", "md", False)
    assert "revenue" in h
    assert "cost" in h


def test_multi_line_wide_has_hover_data():
    rows = [["2024-01", 10, 5], ["2024-02", 20, 10]]
    h = render_chart("multi_line_wide", ["month", "a", "b"], rows, "default", "md", False)
    assert "show-data-on-hover" in h


def test_multi_line_wide_has_labels():
    rows = [["Jan", 10, 5], ["Feb", 20, 10]]
    h = render_chart("multi_line_wide", ["month", "a", "b"], rows, "default", "md", False)
    assert "Jan" in h
    assert "Feb" in h


def test_multi_line_wide_keeps_incompatible_measure_scales_in_one_chart():
    rows = [["2025-06", 7_159_629.56, 2_871], ["2025-07", 21_194_459.53, 8_530]]

    h = render_chart(
        "multi_line_wide",
        ["period", "Revenue", "transaction_count"],
        rows,
        "default",
        "md",
        False,
    )

    assert 'data-renderer="multi_metric_line"' in h
    assert "multi-metric-line-chart" in h
    assert "Dual-axis scale" in h
    assert "axis " in h
    assert "multi-measure-split-lines" not in h
    assert "width:100%;max-width:none" in h
    assert h.count("<svg") == 1
    assert "7,159,629.56" in h
    assert "8,530" in h
    assert 'data-series="Revenue"' in h
    assert 'data-label="2025-06"' in h
    assert 'data-value="7,159,629.56"' in h
    assert 'data-series="transaction_count"' in h
    assert 'data-value="8,530"' in h
    assert "Revenue" in h
    assert "transaction_count" in h


# ---------------------------------------------------------------------------
# Pie chart — Charts.css "pie"
# ---------------------------------------------------------------------------

def test_pie_chart_returns_charts_css_pie():
    h = render_chart(
        "pie",
        ["segment", "share"],
        [["A", 40], ["B", 35], ["C", 25]],
        "default", "md", False,
    )
    assert "charts-css" in h
    assert "pie" in h


def test_pie_chart_slices_sum_to_one():
    h = render_chart(
        "pie",
        ["segment", "share"],
        [["A", 50], ["B", 50]],
        "default", "md", False,
    )
    assert "--end: 1" in h or "--end: 1.0" in h


# ---------------------------------------------------------------------------
# Grouped bar — Charts.css "column multiple"
# ---------------------------------------------------------------------------

def test_grouped_bar_returns_charts_css_column_multiple():
    h = render_chart(
        "grouped_bar",
        ["region", "revenue", "cost"],
        [["North", 100, 60], ["South", 200, 80]],
        "default", "md", False,
    )
    assert "charts-css" in h
    assert "column" in h
    assert "multiple" in h
    assert "North" in h


def test_grouped_bar_has_legend():
    h = render_chart(
        "grouped_bar",
        ["region", "revenue", "cost"],
        [["North", 100, 60], ["South", 200, 80]],
        "default", "md", False,
    )
    assert "revenue" in h
    assert "cost" in h


def test_grouped_bar_splits_unlike_measure_scales_into_readable_small_multiples():
    h = render_chart(
        "grouped_bar",
        ["payment_method", "Revenue", "transaction_count"],
        [
            ["BANK_TRANSFER", "41392449.19", 16504],
            ["CARD", "41984019.78", 16751],
        ],
        "default",
        "md",
        True,
    )

    assert 'data-renderer="split_grouped_bar"' in h
    assert h.count('class="charts-css column show-labels') == 2
    assert "Revenue" in h
    assert "transaction_count" in h
    assert "BANK_TRANSFER" in h
    assert "rendered-data-table" in h
    assert "payment_method" in h


# ---------------------------------------------------------------------------
# Stacked bar — Charts.css "column multiple stacked"
# ---------------------------------------------------------------------------

def test_stacked_bar_returns_charts_css_column_stacked():
    h = render_chart(
        "stacked_bar",
        ["region", "revenue", "cost"],
        [["North", 100, 60], ["South", 200, 80]],
        "default", "md", False,
    )
    assert "charts-css" in h
    assert "column" in h
    assert "stacked" in h


def test_stacked_bar_has_multiple_tds_per_row():
    h = render_chart(
        "stacked_bar",
        ["region", "revenue", "cost"],
        [["North", 100, 60], ["South", 200, 80]],
        "default", "md", False,
    )
    assert h.count("--size") >= 4


def test_stacked_bar_supports_long_form_two_axis_rows():
    h = render_chart(
        "stacked_bar",
        ["country", "payment_method", "Revenue"],
        [
            ["AE", "BANK_TRANSFER", 10],
            ["AE", "CARD", 5],
            ["US", "BANK_TRANSFER", 7],
            ["US", "CARD", 3],
        ],
        "default", "md", True,
    )

    assert "charts-css column multiple stacked" in h
    assert 'data-series="BANK_TRANSFER"' in h
    assert 'data-series="CARD"' in h
    assert 'class="matrix-pivot-table"' in h
    assert "<th>BANK_TRANSFER</th>" in h
    assert "AE - BANK_TRANSFER" not in h
    assert "country / payment_method" not in h


# ---------------------------------------------------------------------------
# Data table
# ---------------------------------------------------------------------------

def test_include_table_appends_html_table():
    h = render_chart(
        "bar",
        ["region", "revenue"],
        [["North", 100], ["South", 200]],
        "default", "md", True,
    )
    assert h.count("<table") == 2
    assert "North" in h


def test_exclude_table_no_data_table():
    h = render_chart(
        "bar",
        ["region", "revenue"],
        [["North", 100], ["South", 200]],
        "default", "md", False,
    )
    assert h.count("<table") == 1


# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------

def test_tessallite_palette_colors_present():
    h = render_chart("bar", ["x", "y"], [["a", 1]], "tessallite", "md", False)
    assert "#006C35" in h


def test_colorblind_safe_palette():
    h = render_chart("bar", ["x", "y"], [["a", 1]], "colorblind_safe", "md", False)
    assert "#0072B2" in h


# ---------------------------------------------------------------------------
# Size
# ---------------------------------------------------------------------------

def test_size_sm_renders():
    h = render_chart("bar", ["x", "y"], [["a", 1]], "default", "sm", False)
    assert "max-width:400px" in h


def test_size_lg_renders():
    h = render_chart("bar", ["x", "y"], [["a", 1]], "default", "lg", False)
    assert "max-width:800px" in h


# ---------------------------------------------------------------------------
# Chart polish — title, labels-size, hover
# ---------------------------------------------------------------------------

def test_chart_has_visible_title():
    h = render_chart("bar", ["region", "revenue"], [["A", 1]], "default", "md", False)
    assert "font-weight:600" in h
    assert ">revenue<" in h


def test_labels_size_in_scoped_style():
    h = render_chart("bar", ["x", "y"], [["a", 1]], "default", "md", False)
    assert "--labels-size: 3rem" in h


def test_line_chart_has_hover_data():
    h = render_chart(
        "line", ["month", "val"],
        [["2024-01", 10], ["2024-02", 20]],
        "default", "md", False,
    )
    assert "show-data-on-hover" in h


def test_pie_chart_no_hover_data():
    h = render_chart(
        "pie", ["seg", "val"],
        [["A", 50], ["B", 50]],
        "default", "md", False,
    )
    assert "show-data-on-hover" not in h


def test_grouped_bar_has_spacing_and_hover():
    h = render_chart(
        "grouped_bar",
        ["region", "revenue", "cost"],
        [["N", 100, 60]],
        "default", "md", False,
    )
    assert "data-spacing-5" in h
    assert "show-data-on-hover" in h


# ---------------------------------------------------------------------------
# Decimal / non-float numeric type coercion
# ---------------------------------------------------------------------------

def test_decimal_values_render_correctly():
    from decimal import Decimal
    h = render_chart(
        "bar",
        ["country", "amount"],
        [["GB", Decimal("62170308.91")], ["DE", Decimal("20144911.01")]],
        "default", "md", False,
    )
    assert "--size: 1.0" in h
    assert "62,170,308.91" in h
    assert "20,144,911.01" in h


def test_decimal_pie_chart():
    from decimal import Decimal
    h = render_chart(
        "pie",
        ["country", "amount"],
        [["A", Decimal("50")], ["B", Decimal("50")]],
        "default", "md", False,
    )
    assert "--end: 1" in h or "--end: 1.0" in h


def test_decimal_kpi_formatted():
    from decimal import Decimal
    h = render_chart("kpi", ["total"], [[Decimal("1234567.89")]], "default", "md", False)
    assert "1,234,567.89" in h


def test_selector_handles_decimal():
    from decimal import Decimal
    from src.charts.selector import select_chart_type
    result = {
        "columns": ["country", "amount"],
        "rows": [["GB", Decimal("100")], ["DE", Decimal("80")]],
    }
    assert select_chart_type(result) is not None


# ---------------------------------------------------------------------------
# Grouped bar skips non-numeric columns
# ---------------------------------------------------------------------------

def test_grouped_bar_skips_non_numeric_columns():
    h = render_chart(
        "grouped_bar",
        ["month", "country", "amount"],
        [[4, "GB", 100], [5, "DE", 200]],
        "default", "md", False,
    )
    assert "--size: 0.5" in h
    assert "--size: 1.0" in h
    assert h.count("--size") == 2
