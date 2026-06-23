"""E1 enhancement fixes — fail-loud rewrite fallbacks.

Covers:
  * F-006-13 (Bug-2741): an unknown filter operator must raise rather than
    silently render as equality.
  * F-006-12 (Bug-2740): an ORDER BY column that resolves to no selected
    expression, SELECT alias, or model dimension/measure must raise a clean
    SemanticBindingError instead of guessing the base-table alias.
"""
from __future__ import annotations

import pytest

from src.ir.logical_query import SemanticBindingError
from src.rewrite.conditions import _render_condition


# --------------------------------------------------------------------------
# F-006-13 — unknown operator must not become "="
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "operator,expected",
    [
        ("eq", '"x" = 5'),
        ("neq", '"x" != 5'),
        ("gt", '"x" > 5'),
        ("gte", '"x" >= 5'),
        ("lt", '"x" < 5'),
        ("lte", '"x" <= 5'),
    ],
)
def test_known_operators_still_render(operator, expected):
    assert _render_condition('"x"', operator, 5, "INTEGER") == expected


@pytest.mark.parametrize(
    "bad_operator",
    ["startswith", "regex", "approx", "", "=", "EQ", "not_eq", "between_exclusive"],
)
def test_unknown_operator_raises_not_equality(bad_operator):
    with pytest.raises(ValueError) as exc:
        _render_condition('"x"', bad_operator, 5, "INTEGER")
    # The message must name the offending operator and refuse equality.
    assert bad_operator.__repr__() in str(exc.value) or "operator" in str(exc.value).lower()


def test_unknown_operator_does_not_silently_equal():
    # Regression for Bug-628 shape: a producer gap (e.g. "not_in" mistyped)
    # must NOT degrade to `= value`.
    with pytest.raises(ValueError):
        _render_condition('"region"', "notin", ["EU", "UK"])


# --------------------------------------------------------------------------
# F-006-12 — ORDER BY unknown column must fail, not guess base alias
# --------------------------------------------------------------------------
#
# The source-SQL ORDER BY path requires a fully-bound query against a live
# model (DB-backed), so the end-to-end assertion lives in the live validation
# suites (validate_rewrite_semantic.py) and the SQL e2e query sets. Here we
# pin the contract that the rewriter raises the SHARED SemanticBindingError
# (not a generic ValueError or a silent guess) so any refactor that swaps the
# exception type is caught at the unit level.

def test_order_by_fail_loud_uses_shared_semantic_binding_error():
    import inspect

    from src.rewrite import source_sql

    src = inspect.getsource(source_sql)
    # The unknown-ORDER-BY branch must raise SemanticBindingError and must NOT
    # fall back to qualifying the bare name with the base alias.
    assert "Cannot resolve ORDER BY column" in src
    assert "raise SemanticBindingError" in src
    # Guard against re-introduction of the guess: the extracted-order branch
    # must not contain a base-alias qualification fallback for ORDER BY columns.
    assert src.count("_qcol(base_alias, col)") == 0
