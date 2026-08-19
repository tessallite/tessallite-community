"""Drill predicate compiler tests (B16 / F-019-05).

Verifies that drill filters compile via sqlglot expression nodes into
type-safe, dialect-correct canonical SQL — operators are honoured (not
silently rewritten to equality), literals are escaped, identifiers are
quote-escaped, and malformed operands raise structured errors.
"""
from __future__ import annotations

import pytest

from src.drill.predicate import (
    DrillPredicateError,
    compile_predicate,
    compile_where,
    quote_ident,
)


def test_eq_string_literal_escaped():
    assert compile_where([{"column": "n", "op": "eq", "value": "O'Brien"}]) == "\"n\" = 'O''Brien'"


def test_eq_numeric_literal():
    assert compile_where([{"column": "y", "op": "eq", "value": 2025}]) == '"y" = 2025'


def test_eq_boolean_literal():
    assert compile_where([{"column": "a", "op": "eq", "value": True}]) == '"a" = TRUE'


def test_eq_null_value_becomes_is_null():
    # Cell-coordinate contract: eq + null means "this dimension is null".
    assert compile_where([{"column": "r", "op": "eq", "value": None}]) == '"r" IS NULL'


@pytest.mark.parametrize(
    "op,sql_op",
    [("gt", ">"), ("gte", ">="), ("lt", "<"), ("lte", "<="), ("neq", "<>")],
)
def test_comparison_operators_not_forced_to_equality(op, sql_op):
    out = compile_where([{"column": "a", "op": op, "value": 5}])
    assert sql_op in out
    assert out != '"a" = 5'


def test_in_operator():
    assert compile_where([{"column": "s", "op": "in", "value": ["a", "b"]}]) == "\"s\" IN ('a', 'b')"


def test_between_operator():
    assert compile_where([{"column": "d", "op": "between", "value": [1, 10]}]) == '"d" BETWEEN 1 AND 10'


def test_like_and_ilike():
    assert compile_where([{"column": "n", "op": "like", "value": "A%"}]) == "\"n\" LIKE 'A%'"
    assert compile_where([{"column": "n", "op": "ilike", "value": "a%"}]) == "\"n\" ILIKE 'a%'"


def test_is_null_and_is_not_null():
    assert compile_where([{"column": "r", "op": "is_null"}]) == '"r" IS NULL'
    assert "IS NULL" in compile_where([{"column": "r", "op": "is_not_null"}])
    assert compile_where([{"column": "r", "op": "is_not_null"}]).startswith("NOT")


def test_multiple_predicates_anded():
    out = compile_where([
        {"column": "a", "op": "eq", "value": "x"},
        {"column": "b", "op": "gt", "value": 3},
    ])
    assert out == "\"a\" = 'x' AND \"b\" > 3"


def test_empty_specs_yield_empty_string():
    assert compile_where([]) == ""
    assert compile_where([{"column": None}]) == ""


def test_identifier_with_embedded_quote_is_escaped():
    # F-019-12: embedded double-quote must be doubled, not break out.
    assert quote_ident('a"; DROP TABLE x; --') == '"a""; DROP TABLE x; --"'


def test_unsupported_operator_raises():
    with pytest.raises(DrillPredicateError) as exc:
        compile_predicate({"column": "x", "op": "regex", "value": ".*"})
    assert exc.value.error_code == "DrillThroughUnsupportedOperator"


def test_in_requires_non_empty_list():
    with pytest.raises(DrillPredicateError) as exc:
        compile_predicate({"column": "x", "op": "in", "value": "notalist"})
    assert exc.value.error_code == "DrillThroughBadOperand"


def test_between_requires_two_bounds():
    with pytest.raises(DrillPredicateError) as exc:
        compile_predicate({"column": "x", "op": "between", "value": [1]})
    assert exc.value.error_code == "DrillThroughBadOperand"


def test_like_requires_string():
    with pytest.raises(DrillPredicateError) as exc:
        compile_predicate({"column": "x", "op": "like", "value": 5})
    assert exc.value.error_code == "DrillThroughBadOperand"


# ---------------------------------------------------------------------------
# Bug-7285: compile_where_expression returns sqlglot AST nodes (not strings)
# ---------------------------------------------------------------------------

def test_compile_where_expression_returns_ast_node():
    """Bug-7285: compile_where_expression must return a sqlglot expression
    tree, not a rendered string. This ensures typed literal nodes survive
    through to the downstream dialect transpiler."""
    from sqlglot import expressions as exp
    from src.drill.predicate import compile_where_expression

    result = compile_where_expression([{"column": "x", "op": "eq", "value": 42}])
    assert isinstance(result, exp.Expression)
    # The expression can be rendered for any dialect — verify that
    # the canonical postgres rendering matches compile_where.
    assert result.sql(dialect="postgres") == compile_where([{"column": "x", "op": "eq", "value": 42}])


def test_compile_where_expression_empty_returns_none():
    """Bug-7285: empty specs produce None (not an empty string)."""
    from src.drill.predicate import compile_where_expression

    assert compile_where_expression([]) is None
    assert compile_where_expression([{"column": None}]) is None


def test_compile_where_expression_multi_predicate_and():
    """Bug-7285: multiple predicates are AND-chained as sqlglot nodes."""
    from sqlglot import expressions as exp
    from src.drill.predicate import compile_where_expression

    result = compile_where_expression([
        {"column": "a", "op": "eq", "value": "x"},
        {"column": "b", "op": "gt", "value": 3},
    ])
    assert isinstance(result, exp.And)
    # The rendered output must match compile_where.
    assert result.sql(dialect="postgres") == compile_where([
        {"column": "a", "op": "eq", "value": "x"},
        {"column": "b", "op": "gt", "value": 3},
    ])
