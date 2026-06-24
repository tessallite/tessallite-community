"""Bug-5346 — the combine expression is a semantic tree of data, never parsed
as code, so a step named after a Python keyword (``global``) can no longer
crash evaluation.

Covers the tree evaluator, semantic validation against step definitions, and
structural shape checks.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.recipes.eval import (
    CombineEvalError,
    check_node_shape,
    evaluate_combine,
    evaluate_combine_aligned,
    validate_expression,
)


def _step(name, measures):
    return SimpleNamespace(name=name, measures=measures)


def _ref(step, measure):
    return {"ref": {"step": step, "measure": measure}}


# ---------------------------------------------------------------------------
# Bug-5346 regression — a step named like a Python keyword must work
# ---------------------------------------------------------------------------

# round(max_city.base_amount / global.base_amount * 100, 2)
PERCENT_OF_TOTAL = {
    "op": "round",
    "args": [
        {"op": "mul", "args": [
            {"op": "div", "args": [_ref("max_city", "base_amount"),
                                   _ref("global", "base_amount")]},
            {"const": 100},
        ]},
        {"const": 2},
    ],
}


@pytest.mark.parametrize("keyword", ["global", "class", "for", "lambda", "import", "if"])
def test_step_named_like_python_keyword_evaluates(keyword):
    expr = {"op": "div", "args": [_ref("part", "amount"), _ref(keyword, "amount")]}
    ctx = {"part": {"amount": 25.0}, keyword: {"amount": 100.0}}
    assert evaluate_combine(expr, ctx) == 0.25


def test_step_named_global_validates_clean():
    steps = [_step("max_city", ["base_amount"]), _step("global", ["base_amount"])]
    assert validate_expression(PERCENT_OF_TOTAL, steps) == []


def test_percent_of_total_evaluates():
    ctx = {"max_city": {"base_amount": 30.0}, "global": {"base_amount": 120.0}}
    assert evaluate_combine(PERCENT_OF_TOTAL, ctx) == 25.0


# ---------------------------------------------------------------------------
# Numeric / null semantics carried over from the prior evaluator
# ---------------------------------------------------------------------------

def test_div_by_zero_returns_none():
    expr = {"op": "div", "args": [_ref("a", "x"), _ref("b", "y")]}
    assert evaluate_combine(expr, {"a": {"x": 5}, "b": {"y": 0}}) is None


def test_round_of_none_returns_none():
    expr = {"op": "round", "args": [
        {"op": "div", "args": [_ref("a", "x"), _ref("b", "y")]}, {"const": 2}]}
    assert evaluate_combine(expr, {"a": {"x": 5}, "b": {"y": 0}}) is None


def test_string_values_coerced_to_numeric():
    expr = {"op": "div", "args": [_ref("a", "x"), _ref("b", "y")]}
    assert evaluate_combine(expr, {"a": {"x": "100"}, "b": {"y": "4"}}) == 25.0


def test_if_else_branch():
    expr = {"op": "if", "args": [
        {"op": "gt", "args": [_ref("a", "x"), {"const": 0}]},
        _ref("a", "x"), {"const": 0}]}
    assert evaluate_combine(expr, {"a": {"x": 7}}) == 7
    assert evaluate_combine(expr, {"a": {"x": -7}}) == 0


# ---------------------------------------------------------------------------
# Validation — semantic and structural
# ---------------------------------------------------------------------------

def test_unknown_step_rejected():
    steps = [_step("sales", ["revenue"]), _step("units", ["qty"])]
    expr = {"op": "div", "args": [_ref("ghosts", "revenue"), _ref("units", "qty")]}
    errors = validate_expression(expr, steps)
    assert any("ghosts" in e for e in errors)


def test_unknown_measure_rejected():
    steps = [_step("sales", ["revenue"]), _step("units", ["qty"])]
    expr = {"op": "div", "args": [_ref("sales", "profit"), _ref("units", "qty")]}
    errors = validate_expression(expr, steps)
    assert any("profit" in e for e in errors)


def test_bad_op_rejected():
    steps = [_step("sales", ["revenue"])]
    expr = {"op": "system", "args": [_ref("sales", "revenue")]}
    errors = validate_expression(expr, steps)
    assert errors and "system" in errors[0]


def test_bad_arity_rejected():
    steps = [_step("sales", ["revenue"])]
    expr = {"op": "div", "args": [_ref("sales", "revenue")]}  # div needs 2
    errors = validate_expression(expr, steps)
    assert errors


def test_none_expression_is_valid():
    assert validate_expression(None, []) == []


def test_check_node_shape_rejects_multiple_keys():
    with pytest.raises(ValueError):
        check_node_shape({"const": 1, "ref": {"step": "a", "measure": "b"}})


def test_check_node_shape_rejects_non_object():
    with pytest.raises(ValueError):
        check_node_shape("max_city.base_amount")


def test_missing_ref_raises_at_eval():
    expr = _ref("missing", "x")
    with pytest.raises(CombineEvalError):
        evaluate_combine(expr, {"present": {"x": 1}})


# ---------------------------------------------------------------------------
# Row-aligned evaluation
# ---------------------------------------------------------------------------

def test_aligned_scalar_broadcast():
    # part per city / global total, broadcast scalar across rows
    expr = {"op": "round", "args": [
        {"op": "mul", "args": [
            {"op": "div", "args": [_ref("city", "amount"), _ref("total", "amount")]},
            {"const": 100}]},
        {"const": 1}]}
    step_rows = {
        "city": [{"city_name": "A", "amount": 30.0}, {"city_name": "B", "amount": 10.0}],
        "total": [{"amount": 100.0}],
    }
    step_dims = {"city": ["city_name"], "total": []}
    rows, cols, multi, mode = evaluate_combine_aligned(
        expr, step_rows, step_dims, "Share (%)")
    assert mode == "evaluated"
    assert multi is True
    by_city = {r["city_name"]: r["Share (%)"] for r in rows}
    assert by_city == {"A": 30.0, "B": 10.0}
