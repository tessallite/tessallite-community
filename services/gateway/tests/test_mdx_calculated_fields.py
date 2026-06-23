"""Tests for MDX Calculated Fields (Block D)."""
import pytest

from src.dax.ts_mdx_parser import parse_mdx
from src.dax.mdx_calc_members import CalcMember, parse_calc_members, evaluate_calc_members


# ---------------------------------------------------------------------------
# D.1 — WITH MEMBER expression parsing
# ---------------------------------------------------------------------------

def test_simple_addition():
    mdx = '''WITH
MEMBER [Measures].[Total] AS
  [Measures].[Revenue] + [Measures].[Profit]
SELECT {[Measures].[Total]} ON COLUMNS
FROM [demo]'''
    parsed = parse_mdx(mdx)
    calcs = parse_calc_members(parsed.with_members)
    assert len(calcs) == 1
    assert calcs[0].name == "Total"
    assert calcs[0].calc_type == "arithmetic"


def test_division_with_zero_denominator():
    mdx = '''WITH
MEMBER [Measures].[Margin] AS
  [Measures].[Profit] / [Measures].[Revenue]
SELECT {[Measures].[Margin]} ON COLUMNS
FROM [demo]'''
    parsed = parse_mdx(mdx)
    calcs = parse_calc_members(parsed.with_members)

    rows = [
        {"Revenue": 1000, "Profit": 200},
        {"Revenue": 0, "Profit": 50},
    ]
    evaluate_calc_members(calcs, rows, ["Revenue", "Profit"], [])
    assert rows[0]["Margin"] == pytest.approx(0.2)
    assert rows[1]["Margin"] is None


def test_iif_conditional():
    mdx = '''WITH
MEMBER [Measures].[Adjusted] AS
  IIF([Measures].[Amount] > 0, [Measures].[Amount], 0)
SELECT {[Measures].[Adjusted]} ON COLUMNS
FROM [demo]'''
    parsed = parse_mdx(mdx)
    calcs = parse_calc_members(parsed.with_members)
    assert calcs[0].calc_type == "arithmetic"

    rows = [
        {"Region": "A", "Amount": 100},
        {"Region": "B", "Amount": -50},
        {"Region": "C", "Amount": 0},
    ]
    evaluate_calc_members(calcs, rows, ["Amount"], ["Region"])
    assert rows[0]["Adjusted"] == pytest.approx(100.0)
    assert rows[1]["Adjusted"] == pytest.approx(0.0)
    assert rows[2]["Adjusted"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# D.2 — Expression evaluation
# ---------------------------------------------------------------------------

def test_three_measure_expression():
    mdx = '''WITH
MEMBER [Measures].[Net] AS
  [Measures].[Revenue] - [Measures].[Cost] - [Measures].[Tax]
SELECT {[Measures].[Net]} ON COLUMNS
FROM [demo]'''
    parsed = parse_mdx(mdx)
    calcs = parse_calc_members(parsed.with_members)

    rows = [
        {"Revenue": 1000, "Cost": 600, "Tax": 100},
        {"Revenue": 500, "Cost": 200, "Tax": 50},
    ]
    evaluate_calc_members(calcs, rows, ["Revenue", "Cost", "Tax"], [])
    assert rows[0]["Net"] == pytest.approx(300.0)
    assert rows[1]["Net"] == pytest.approx(250.0)


def test_numeric_literal_multiplication():
    mdx = '''WITH
MEMBER [Measures].[Projected] AS
  [Measures].[Amount] * 1.1
SELECT {[Measures].[Projected]} ON COLUMNS
FROM [demo]'''
    parsed = parse_mdx(mdx)
    calcs = parse_calc_members(parsed.with_members)

    rows = [
        {"Amount": 100},
        {"Amount": 200},
    ]
    evaluate_calc_members(calcs, rows, ["Amount"], [])
    assert rows[0]["Projected"] == pytest.approx(110.0)
    assert rows[1]["Projected"] == pytest.approx(220.0)


def test_null_propagation():
    mdx = '''WITH
MEMBER [Measures].[Sum] AS
  [Measures].[A] + [Measures].[B]
SELECT {[Measures].[Sum]} ON COLUMNS
FROM [demo]'''
    parsed = parse_mdx(mdx)
    calcs = parse_calc_members(parsed.with_members)

    rows = [
        {"A": 100, "B": 200},
        {"A": None, "B": 200},
        {"A": 100, "B": None},
    ]
    evaluate_calc_members(calcs, rows, ["A", "B"], [])
    assert rows[0]["Sum"] == pytest.approx(300.0)
    assert rows[1]["Sum"] is None
    assert rows[2]["Sum"] is None


# ---------------------------------------------------------------------------
# D.3 — Response builder integration (via calc member injection)
# ---------------------------------------------------------------------------

def test_calculated_field_combined_with_show_values_as():
    from src.dax.mdx_calc_members import CalcMember
    calcs = [
        CalcMember(
            name="Margin",
            expression="[Measures].[Profit] / [Measures].[Revenue]",
            calc_type="arithmetic",
            base_measure="Profit",
        ),
        CalcMember(
            name="Pct",
            expression="[Measures].[Revenue] / ([Measures].[Revenue], [R].[(All)])",
            calc_type="pct_grand_total",
            base_measure="Revenue",
        ),
    ]
    rows = [
        {"Region": "A", "Revenue": 600, "Profit": 120},
        {"Region": "B", "Revenue": 400, "Profit": 100},
    ]
    evaluate_calc_members(calcs, rows, ["Revenue", "Profit"], ["Region"])
    assert rows[0]["Margin"] == pytest.approx(0.2)
    assert rows[1]["Margin"] == pytest.approx(0.25)
    assert rows[0]["Pct"] == pytest.approx(0.6)
    assert rows[1]["Pct"] == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# D.4 — Edge cases
# ---------------------------------------------------------------------------

def test_format_string_on_calculated_field():
    mdx = '''WITH
MEMBER [Measures].[Margin Pct] AS
  [Measures].[Profit] / [Measures].[Revenue],
  FORMAT_STRING = "0.00%"
SELECT {[Measures].[Margin Pct]} ON COLUMNS
FROM [demo]'''
    parsed = parse_mdx(mdx)
    calcs = parse_calc_members(parsed.with_members)
    assert calcs[0].format_string == "0.00%"


def test_subtraction_expression():
    mdx = '''WITH
MEMBER [Measures].[Diff] AS
  [Measures].[Budget] - [Measures].[Actual]
SELECT {[Measures].[Diff]} ON COLUMNS
FROM [demo]'''
    parsed = parse_mdx(mdx)
    calcs = parse_calc_members(parsed.with_members)

    rows = [{"Budget": 500, "Actual": 300}]
    evaluate_calc_members(calcs, rows, ["Budget", "Actual"], [])
    assert rows[0]["Diff"] == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# D.4b — Parenthesized operands
# ---------------------------------------------------------------------------

def test_parenthesized_operands_addition():
    """([Measures].[A]) + ([Measures].[B]) must not strip mismatched parens."""
    calcs = [CalcMember(
        name="Total",
        expression="([Measures].[Revenue]) + ([Measures].[Cost])",
        calc_type="arithmetic",
    )]
    rows = [{"Revenue": 600, "Cost": 400}]
    evaluate_calc_members(calcs, rows, ["Revenue", "Cost"], [])
    assert rows[0]["Total"] == pytest.approx(1000.0)


def test_parenthesized_sub_expression_division():
    """([Measures].[A] + [Measures].[B]) / [Measures].[C] must evaluate."""
    calcs = [CalcMember(
        name="Ratio",
        expression="([Measures].[Revenue] + [Measures].[Cost]) / [Measures].[Units]",
        calc_type="arithmetic",
    )]
    rows = [{"Revenue": 600, "Cost": 400, "Units": 100}]
    evaluate_calc_members(calcs, rows, ["Revenue", "Cost", "Units"], [])
    assert rows[0]["Ratio"] == pytest.approx(10.0)


def test_true_outer_parens_still_stripped():
    """Balanced outer parens wrapping a full expression are still stripped."""
    calcs = [CalcMember(
        name="Doubled",
        expression="([Measures].[Revenue] * 2)",
        calc_type="arithmetic",
    )]
    rows = [{"Revenue": 500}]
    evaluate_calc_members(calcs, rows, ["Revenue"], [])
    assert rows[0]["Doubled"] == pytest.approx(1000.0)


def test_unary_negative_multiply():
    """[Measures].[Amount] * -1 must negate the value."""
    calcs = [CalcMember(
        name="Negated",
        expression="[Measures].[Revenue] * -1",
        calc_type="arithmetic",
    )]
    rows = [{"Revenue": 500}]
    evaluate_calc_members(calcs, rows, ["Revenue"], [])
    assert rows[0]["Negated"] == pytest.approx(-500.0)


def test_unary_negative_divide():
    """[Measures].[Amount] / -2 must divide by negative."""
    calcs = [CalcMember(
        name="Half",
        expression="[Measures].[Revenue] / -2",
        calc_type="arithmetic",
    )]
    rows = [{"Revenue": 600}]
    evaluate_calc_members(calcs, rows, ["Revenue"], [])
    assert rows[0]["Half"] == pytest.approx(-300.0)


def test_unary_negative_add():
    """[Measures].[Amount] + -10 must subtract 10."""
    calcs = [CalcMember(
        name="Adjusted",
        expression="[Measures].[Revenue] + -10",
        calc_type="arithmetic",
    )]
    rows = [{"Revenue": 500}]
    evaluate_calc_members(calcs, rows, ["Revenue"], [])
    assert rows[0]["Adjusted"] == pytest.approx(490.0)


# ---------------------------------------------------------------------------
# D.5 — SOLVE_ORDER
# ---------------------------------------------------------------------------

def test_solve_order_parsed_from_properties():
    mdx = '''WITH
MEMBER [Measures].[Calc1] AS
  [Measures].[Revenue] * 2,
  SOLVE_ORDER = 10
MEMBER [Measures].[Calc2] AS
  [Measures].[Cost] * 3,
  SOLVE_ORDER = 5
SELECT {[Measures].[Calc1], [Measures].[Calc2]} ON COLUMNS
FROM [demo]'''
    parsed = parse_mdx(mdx)
    calcs = parse_calc_members(parsed.with_members)
    by_name = {c.name: c for c in calcs}
    assert by_name["Calc1"].solve_order == 10
    assert by_name["Calc2"].solve_order == 5


def test_solve_order_default_is_zero():
    mdx = '''WITH
MEMBER [Measures].[NoOrder] AS
  [Measures].[Revenue] + 1
SELECT {[Measures].[NoOrder]} ON COLUMNS
FROM [demo]'''
    parsed = parse_mdx(mdx)
    calcs = parse_calc_members(parsed.with_members)
    assert calcs[0].solve_order == 0


def test_solve_order_determines_independent_member_evaluation_order():
    """Two independent calc members should evaluate in solve_order order."""
    from src.dax.mdx_calc_members import _check_circular_references

    calcs = [
        CalcMember(name="High", expression="[Measures].[Revenue] * 2",
                   calc_type="arithmetic", base_measure="Revenue", solve_order=100),
        CalcMember(name="Low", expression="[Measures].[Cost] + 1",
                   calc_type="arithmetic", base_measure="Cost", solve_order=1),
    ]
    ordered = _check_circular_references(calcs)
    names = [c.name for c in ordered]
    assert names.index("Low") < names.index("High")


def test_solve_order_does_not_override_dependency():
    """A member depending on another must evaluate after it, regardless of solve_order."""
    from src.dax.mdx_calc_members import _check_circular_references

    calcs = [
        CalcMember(name="Base", expression="[Measures].[Revenue] * 2",
                   calc_type="arithmetic", base_measure="Revenue", solve_order=999),
        CalcMember(name="Derived", expression="[Measures].[Base] + 1",
                   calc_type="arithmetic", base_measure="Base", solve_order=1),
    ]
    ordered = _check_circular_references(calcs)
    names = [c.name for c in ordered]
    assert names.index("Base") < names.index("Derived")
