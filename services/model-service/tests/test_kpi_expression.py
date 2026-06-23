"""Unit tests for shared.semantic.kpi_expression (Phase 1 — KPI v2 DSL)."""
from __future__ import annotations

import pytest

from shared.semantic.kpi_expression import (
    FUNCTION_REGISTRY,
    ASTNode,
    BinaryOp,
    FunctionCall,
    KPIExpressionError,
    NumberLiteral,
    StringLiteral,
    UnaryMinus,
    ValidationResult,
    parse_kpi_expression,
    tokenize,
    validate_expression,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

class TestTokenize:
    def test_simple_function_call(self):
        tokens = tokenize('measure("Revenue")')
        types = [t.type for t in tokens]
        assert types == ["IDENT", "LPAREN", "STRING", "RPAREN", "EOF"]

    def test_arithmetic(self):
        tokens = tokenize("1 + 2 * 3")
        values = [t.value for t in tokens if t.type != "EOF"]
        assert values == ["1", "+", "2", "*", "3"]

    def test_nested_parens(self):
        tokens = tokenize("(1 + 2) * 3")
        types = [t.type for t in tokens if t.type != "EOF"]
        assert types == ["LPAREN", "NUMBER", "PLUS", "NUMBER", "RPAREN", "STAR", "NUMBER"]

    def test_unexpected_character(self):
        with pytest.raises(KPIExpressionError, match="Unexpected character"):
            tokenize("measure(@)")

    def test_decimal_number(self):
        tokens = tokenize("3.14")
        assert tokens[0].value == "3.14"

    def test_whitespace_handling(self):
        tokens = tokenize("  measure ( \"x\" )  ")
        non_eof = [t for t in tokens if t.type != "EOF"]
        assert len(non_eof) == 4


# ---------------------------------------------------------------------------
# Parser — valid expressions
# ---------------------------------------------------------------------------

class TestParserValid:
    def test_simple_measure(self):
        ast = parse_kpi_expression('measure("Revenue")')
        assert isinstance(ast, FunctionCall)
        assert ast.name == "measure"
        assert len(ast.args) == 1
        assert isinstance(ast.args[0], StringLiteral)
        assert ast.args[0].value == "Revenue"

    def test_kpi_reference(self):
        ast = parse_kpi_expression('kpi("Conversion Rate")')
        assert isinstance(ast, FunctionCall)
        assert ast.name == "kpi"
        assert ast.args[0].value == "Conversion Rate"

    def test_safe_div(self):
        ast = parse_kpi_expression('safe_div(measure("Profit"), measure("Revenue"))')
        assert isinstance(ast, FunctionCall)
        assert ast.name == "safe_div"
        assert len(ast.args) == 2

    def test_div_with_fallback(self):
        ast = parse_kpi_expression('div(measure("a"), measure("b"), 0)')
        assert isinstance(ast, FunctionCall)
        assert ast.name == "div"
        assert len(ast.args) == 3

    def test_nested_functions(self):
        ast = parse_kpi_expression(
            'round(safe_div(measure("Profit"), measure("Revenue")), 2)'
        )
        assert isinstance(ast, FunctionCall)
        assert ast.name == "round"

    def test_arithmetic_addition(self):
        ast = parse_kpi_expression('measure("a") + measure("b")')
        assert isinstance(ast, BinaryOp)
        assert ast.op == "+"

    def test_arithmetic_subtraction(self):
        ast = parse_kpi_expression('measure("a") - measure("b")')
        assert isinstance(ast, BinaryOp)
        assert ast.op == "-"

    def test_arithmetic_multiplication(self):
        ast = parse_kpi_expression('measure("a") * 100')
        assert isinstance(ast, BinaryOp)
        assert ast.op == "*"

    def test_unary_minus(self):
        ast = parse_kpi_expression('-measure("a")')
        assert isinstance(ast, UnaryMinus)

    def test_parenthesized_expression(self):
        ast = parse_kpi_expression('(measure("a") + measure("b")) * 100')
        assert isinstance(ast, BinaryOp)
        assert ast.op == "*"

    def test_number_literal(self):
        ast = parse_kpi_expression("literal(42)")
        assert isinstance(ast, FunctionCall)
        assert ast.name == "literal"

    def test_coalesce(self):
        ast = parse_kpi_expression('coalesce(measure("a"), measure("b"), 0)')
        assert isinstance(ast, FunctionCall)
        assert ast.name == "coalesce"
        assert len(ast.args) == 3

    def test_if_then_else(self):
        ast = parse_kpi_expression(
            'if_then_else(measure("flag"), measure("a"), measure("b"))'
        )
        assert isinstance(ast, FunctionCall)
        assert ast.name == "if_then_else"
        assert len(ast.args) == 3

    def test_time_intelligence_prior_period(self):
        ast = parse_kpi_expression('prior_period(measure("Revenue"), "month")')
        assert isinstance(ast, FunctionCall)
        assert ast.name == "prior_period"

    def test_time_intelligence_moving_avg(self):
        ast = parse_kpi_expression('moving_avg(measure("Revenue"), 3, "month")')
        assert isinstance(ast, FunctionCall)
        assert ast.name == "moving_avg"
        assert len(ast.args) == 3

    def test_time_intelligence_pct_change(self):
        ast = parse_kpi_expression('pct_change(measure("Revenue"), "quarter")')
        assert isinstance(ast, FunctionCall)
        assert ast.name == "pct_change"

    def test_time_intelligence_cagr(self):
        ast = parse_kpi_expression('cagr(measure("Revenue"), 3, "year")')
        assert isinstance(ast, FunctionCall)
        assert ast.name == "cagr"

    def test_dimension_reference(self):
        ast = parse_kpi_expression('dimension("Region")')
        assert isinstance(ast, FunctionCall)
        assert ast.name == "dimension"

    def test_complex_ratio_kpi(self):
        """Type 2: Ratio — safe_div(measure("GM"), measure("Revenue"))"""
        ast = parse_kpi_expression(
            'safe_div(measure("Gross Margin"), measure("Revenue"))'
        )
        assert isinstance(ast, FunctionCall)
        assert ast.name == "safe_div"

    def test_complex_variance_kpi(self):
        """Type 3: Variance — measure("Actual") - measure("Budget")"""
        ast = parse_kpi_expression('measure("Actual") - measure("Budget")')
        assert isinstance(ast, BinaryOp)
        assert ast.op == "-"

    def test_complex_growth_rate_kpi(self):
        """Type 4: Growth Rate — pct_change(measure("Revenue"), "quarter")"""
        ast = parse_kpi_expression('pct_change(measure("Revenue"), "quarter")')
        assert isinstance(ast, FunctionCall)

    def test_complex_moving_window_kpi(self):
        """Type 5: Moving Window — moving_avg(measure("Revenue"), 3, "month")"""
        ast = parse_kpi_expression('moving_avg(measure("Revenue"), 3, "month")')
        assert isinstance(ast, FunctionCall)

    def test_complex_composite_kpi(self):
        """Type 6: Composite — kpi("A") * 0.4 + kpi("B") * 0.6"""
        ast = parse_kpi_expression('kpi("Score A") * 0.4 + kpi("Score B") * 0.6')
        assert isinstance(ast, BinaryOp)
        assert ast.op == "+"

    def test_position_tracking(self):
        ast = parse_kpi_expression('measure("x")')
        assert ast.pos_start == 0
        assert ast.pos_end == 12  # len('measure("x")') == 12


# ---------------------------------------------------------------------------
# Parser — error cases
# ---------------------------------------------------------------------------

class TestParserErrors:
    def test_bare_division_blocked(self):
        with pytest.raises(KPIExpressionError) as exc_info:
            parse_kpi_expression('measure("a") / measure("b")')
        err = exc_info.value
        assert err.code == "BARE_DIVISION"
        assert "safe_div" in str(err)
        assert err.suggestion is not None

    def test_bare_identifier_blocked(self):
        with pytest.raises(KPIExpressionError) as exc_info:
            parse_kpi_expression("Revenue")
        err = exc_info.value
        assert err.code == "BARE_IDENTIFIER"
        assert err.suggestion is not None

    def test_empty_expression(self):
        with pytest.raises(KPIExpressionError, match="empty"):
            parse_kpi_expression("")

    def test_whitespace_only(self):
        with pytest.raises(KPIExpressionError, match="empty"):
            parse_kpi_expression("   ")

    def test_unexpected_token(self):
        with pytest.raises(KPIExpressionError) as exc_info:
            parse_kpi_expression('measure("a") measure("b")')
        assert exc_info.value.code == "SYNTAX_ERROR"

    def test_unclosed_parenthesis(self):
        with pytest.raises(KPIExpressionError):
            parse_kpi_expression('(measure("a") + measure("b")')

    def test_missing_argument(self):
        """measure() with no args parses fine but fails validation (ARGUMENT_COUNT)."""
        result = validate_expression("measure()")
        assert not result.valid
        assert any(e.code == "ARGUMENT_COUNT" for e in result.errors)


# ---------------------------------------------------------------------------
# Validator — validate_expression()
# ---------------------------------------------------------------------------

class TestValidateExpression:
    def test_valid_simple_measure(self):
        result = validate_expression(
            'measure("Revenue")',
            model_measures={"Revenue", "Profit"},
        )
        assert result.valid
        assert result.referenced_measures == ["Revenue"]
        assert result.referenced_kpis == []
        assert result.has_time_intelligence is False

    def test_valid_with_kpi_ref(self):
        result = validate_expression(
            'kpi("Margin") * 100',
            model_kpis={"Margin", "Growth"},
        )
        assert result.valid
        assert result.referenced_kpis == ["Margin"]

    def test_unknown_measure(self):
        result = validate_expression(
            'measure("Revnue")',
            model_measures={"Revenue", "Profit"},
        )
        assert not result.valid
        assert any(e.code == "UNKNOWN_MEASURE" for e in result.errors)
        # Should suggest "Revenue" via fuzzy match
        measure_error = next(e for e in result.errors if e.code == "UNKNOWN_MEASURE")
        assert measure_error.suggestion is not None
        assert "Revenue" in measure_error.suggestion

    def test_unknown_kpi(self):
        result = validate_expression(
            'kpi("NonExistent")',
            model_kpis={"Margin"},
        )
        assert not result.valid
        assert any(e.code == "UNKNOWN_KPI" for e in result.errors)

    def test_unknown_dimension(self):
        result = validate_expression(
            'dimension("Regoin")',
            model_dimensions={"Region", "Category"},
        )
        assert not result.valid
        assert any(e.code == "UNKNOWN_DIMENSION" for e in result.errors)

    def test_unknown_function(self):
        result = validate_expression('mesure("Revenue")')
        assert not result.valid
        assert any(e.code == "UNKNOWN_FUNCTION" for e in result.errors)
        # Should suggest "measure" via fuzzy match
        fn_error = next(e for e in result.errors if e.code == "UNKNOWN_FUNCTION")
        assert fn_error.suggestion is not None

    def test_wrong_arg_count(self):
        result = validate_expression('safe_div(measure("a"))')
        assert not result.valid
        assert any(e.code == "ARGUMENT_COUNT" for e in result.errors)

    def test_type_mismatch_measure_needs_string(self):
        result = validate_expression("measure(42)")
        assert not result.valid
        assert any(e.code == "TYPE_MISMATCH" for e in result.errors)

    def test_time_intelligence_detected(self):
        result = validate_expression(
            'prior_period(measure("Revenue"), "month")',
            model_measures={"Revenue"},
        )
        assert result.valid
        assert result.has_time_intelligence
        assert result.requires_time_dimension

    def test_time_dimension_required_but_missing(self):
        result = validate_expression(
            'prior_period(measure("Revenue"), "month")',
            model_measures={"Revenue"},
            has_time_dimension=False,
        )
        assert not result.valid
        assert any(e.code == "TIME_DIMENSION_REQUIRED" for e in result.errors)

    def test_invalid_grain(self):
        result = validate_expression(
            'moving_avg(measure("Revenue"), 3, "biweekly")',
            model_measures={"Revenue"},
        )
        assert not result.valid
        assert any(e.code == "INVALID_GRAIN" for e in result.errors)

    def test_valid_grains_accepted(self):
        for grain in ("day", "week", "month", "quarter", "year"):
            result = validate_expression(
                f'prior_period(measure("Revenue"), "{grain}")',
                model_measures={"Revenue"},
            )
            assert result.valid, f"Grain '{grain}' should be valid"

    def test_detected_agg_mode_pre_aggregated(self):
        """Expressions referencing only kpi() should detect pre_aggregated."""
        result = validate_expression(
            'kpi("A") * 0.4 + kpi("B") * 0.6',
            model_kpis={"A", "B"},
        )
        assert result.valid
        assert result.detected_agg_mode == "pre_aggregated"

    def test_detected_agg_mode_aggregate_first_for_ratio(self):
        """safe_div of two measures should detect aggregate_first."""
        result = validate_expression(
            'safe_div(measure("GM"), measure("Revenue"))',
            model_measures={"GM", "Revenue"},
        )
        assert result.valid
        assert result.detected_agg_mode == "aggregate_first"

    def test_expression_tree_populated(self):
        result = validate_expression('measure("Revenue")')
        assert result.expression_tree is not None
        assert result.expression_tree["type"] == "call"
        assert result.expression_tree["fn"] == "measure"

    def test_empty_expression(self):
        result = validate_expression("")
        assert not result.valid
        assert any(e.code == "EMPTY_EXPRESSION" for e in result.errors)

    def test_bare_division_reported(self):
        result = validate_expression('measure("a") / measure("b")')
        assert not result.valid
        assert any(e.code == "BARE_DIVISION" for e in result.errors)

    def test_deduplicates_references(self):
        result = validate_expression(
            'measure("a") + measure("a")',
            model_measures={"a"},
        )
        assert result.valid
        assert result.referenced_measures == ["a"]

    def test_skip_model_checks_when_none(self):
        """When model_measures/kpis/dimensions are None, name checks are skipped."""
        result = validate_expression(
            'measure("AnyName") + kpi("AnyKPI")',
        )
        assert result.valid


# ---------------------------------------------------------------------------
# Function registry completeness
# ---------------------------------------------------------------------------

class TestFunctionRegistry:
    def test_core_functions_registered(self):
        expected = {
            "measure", "kpi", "literal", "dimension",
            "safe_div", "safe_ratio", "div",
            "coalesce", "if_then_else",
            "abs", "round", "min_of", "max_of",
        }
        assert expected.issubset(set(FUNCTION_REGISTRY.keys()))

    def test_time_intelligence_functions_registered(self):
        expected = {
            "prior_period", "period_to_date", "moving_avg",
            "trailing_sum", "lag", "lead", "cagr",
            "pct_change", "fiscal_period_to_date",
        }
        assert expected.issubset(set(FUNCTION_REGISTRY.keys()))

    def test_time_intelligence_functions_flagged(self):
        for name in (
            "prior_period", "period_to_date", "moving_avg",
            "trailing_sum", "lag", "lead", "cagr",
            "pct_change", "fiscal_period_to_date",
        ):
            assert FUNCTION_REGISTRY[name].is_time_intelligence, (
                f"{name} should have is_time_intelligence=True"
            )

    def test_core_functions_not_flagged_as_time(self):
        for name in ("measure", "kpi", "safe_div", "abs", "round"):
            assert not FUNCTION_REGISTRY[name].is_time_intelligence, (
                f"{name} should not have is_time_intelligence=True"
            )
