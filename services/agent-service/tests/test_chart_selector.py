from src.charts.selector import select_chart_type


def _r(columns, rows):
    return {"columns": columns, "rows": rows}


# ---------------------------------------------------------------------------
# Rule 1: KPI — single row, single numeric column
# ---------------------------------------------------------------------------

def test_single_row_single_measure_is_kpi():
    assert select_chart_type(_r(["revenue"], [[1_000_000]])) == "kpi"


# ---------------------------------------------------------------------------
# Rule 2: multi_line — date dim + category dim + one measure
# ---------------------------------------------------------------------------

def test_date_and_category_dim_with_measure_is_multi_line():
    rows = [
        ["2024-01", "North", 100],
        ["2024-01", "South", 200],
        ["2024-02", "North", 150],
    ]
    assert select_chart_type(_r(["month", "region", "revenue"], rows)) == "multi_line"


# ---------------------------------------------------------------------------
# Rule 3: line — date dim + measure(s), no category dim
# ---------------------------------------------------------------------------

def test_date_dim_measure_is_line():
    rows = [["2024-01", 100], ["2024-02", 200], ["2024-03", 150]]
    assert select_chart_type(_r(["month", "revenue"], rows)) == "line"


# ---------------------------------------------------------------------------
# Rule 4: grouped_bar — one category dim + multiple measures
# ---------------------------------------------------------------------------

def test_one_cat_dim_multiple_measures_is_grouped_bar():
    rows = [["North", 100, 50], ["South", 200, 80]]
    assert select_chart_type(_r(["region", "revenue", "cost"], rows)) == "grouped_bar"


# ---------------------------------------------------------------------------
# Rule 5: too many rows → None
# ---------------------------------------------------------------------------

def test_over_hundred_rows_returns_none():
    rows = [[f"Cat{i}", i * 10] for i in range(101)]
    assert select_chart_type(_r(["category", "count"], rows)) is None


def test_fifty_to_hundred_rows_is_h_bar():
    rows = [[f"Cat{i}", i * 10] for i in range(60)]
    assert select_chart_type(_r(["category", "count"], rows)) == "h_bar"


# ---------------------------------------------------------------------------
# Rule 6: h_bar — 9–50 rows with one cat dim and one measure
# ---------------------------------------------------------------------------

def test_one_dim_many_values_is_h_bar():
    rows = [[f"Cat{i}", i * 10] for i in range(20)]
    assert select_chart_type(_r(["category", "count"], rows)) == "h_bar"


# ---------------------------------------------------------------------------
# Rules 7/8: pie vs bar based on sign
# ---------------------------------------------------------------------------

def test_one_dim_few_positive_values_is_pie():
    rows = [["North", 100], ["South", 200], ["East", 150]]
    assert select_chart_type(_r(["region", "revenue"], rows)) == "pie"


def test_one_dim_values_with_negatives_is_bar():
    rows = [["North", 100], ["South", -50]]
    assert select_chart_type(_r(["region", "revenue"], rows)) == "bar"


# ---------------------------------------------------------------------------
# Rule 9: bar — multiple measures, no dims
# ---------------------------------------------------------------------------

def test_measures_only_no_dims_single_row_is_kpi():
    rows = [[100, 200, 50]]
    assert select_chart_type(_r(["revenue", "cost", "profit"], rows)) == "kpi"


def test_measures_only_no_dims_multi_row_is_bar():
    rows = [[100, 200, 50], [150, 180, 60]]
    assert select_chart_type(_r(["revenue", "cost", "profit"], rows)) == "bar"


# ---------------------------------------------------------------------------
# Rule 10: no match → None
# ---------------------------------------------------------------------------

def test_multi_dim_small_returns_bar():
    rows = [["a", "b", 1]]
    assert select_chart_type(_r(["dim1", "dim2", "measure"], rows)) == "bar"


def test_multi_dim_medium_returns_h_bar():
    rows = [[f"a{i}", f"b{i}", i] for i in range(60)]
    assert select_chart_type(_r(["dim1", "dim2", "measure"], rows)) == "h_bar"


def test_multi_dim_large_returns_none():
    rows = [[f"a{i}", f"b{i}", i] for i in range(101)]
    assert select_chart_type(_r(["dim1", "dim2", "measure"], rows)) is None


# ---------------------------------------------------------------------------
# max_rows param respected
# ---------------------------------------------------------------------------

def test_max_rows_limit_overrides_chart():
    rows = [[f"Cat{i}", i] for i in range(10)]
    # With limit=5 the 10 rows exceed it → None
    assert select_chart_type(_r(["cat", "val"], rows), max_rows=5) is None


def test_empty_result_returns_none():
    assert select_chart_type(_r(["revenue"], [])) is None


def test_empty_columns_returns_none():
    assert select_chart_type({"columns": [], "rows": [[1]]}) is None


# ---------------------------------------------------------------------------
# Temporal integer column recognition (month_no, year, etc.)
# ---------------------------------------------------------------------------

def test_month_no_integer_with_category_is_multi_line():
    rows = [
        [4, "GB", 100],
        [4, "DE", 80],
        [5, "GB", 120],
        [5, "DE", 90],
    ]
    assert select_chart_type(_r(["month_no", "country_code", "amount"], rows)) == "multi_line"


def test_year_integer_with_measure_is_line():
    rows = [[2022, 100], [2023, 150], [2024, 200]]
    assert select_chart_type(_r(["year", "revenue"], rows)) == "line"


def test_fiscal_quarter_integer_is_date_dim():
    rows = [[1, 100], [2, 150], [3, 200], [4, 250]]
    assert select_chart_type(_r(["quarter", "sales"], rows)) == "line"


def test_non_temporal_integer_column_stays_measure():
    rows = [["North", 100, 50], ["South", 200, 80]]
    assert select_chart_type(_r(["region", "revenue", "cost"], rows)) == "grouped_bar"


# ---------------------------------------------------------------------------
# Rule 3b: multi_line_wide — date dim + multiple measures, no category dim
# ---------------------------------------------------------------------------

def test_date_dim_multiple_measures_is_multi_line_wide():
    rows = [
        ["2024-01", 100, 60],
        ["2024-02", 150, 80],
        ["2024-03", 200, 90],
    ]
    assert select_chart_type(_r(["month", "revenue", "cost"], rows)) == "multi_line_wide"


def test_date_dim_three_measures_is_multi_line_wide():
    rows = [
        ["2024-01", 100, 60, 40],
        ["2024-02", 150, 80, 70],
    ]
    assert select_chart_type(_r(["month", "revenue", "cost", "profit"], rows)) == "multi_line_wide"


def test_integer_year_multiple_measures_is_multi_line_wide():
    rows = [[2022, 100, 50], [2023, 150, 60], [2024, 200, 80]]
    assert select_chart_type(_r(["year", "revenue", "cost"], rows)) == "multi_line_wide"


def test_decimal_month_no_recognized_as_date_dim():
    from decimal import Decimal
    rows = [
        [Decimal(1), "AE", Decimal("2254676.08")],
        [Decimal(1), "DE", Decimal("2334940.63")],
        [Decimal(2), "AE", Decimal("2385319.73")],
        [Decimal(2), "DE", Decimal("2301510.55")],
    ]
    assert select_chart_type(_r(["month_no", "country_code", "transaction_amount"], rows)) == "multi_line"
