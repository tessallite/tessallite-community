"""Bug-8758: nested named-set references must not survive inlining as bare tokens.

``_inline_named_sets`` applies ONE regex substitution per named set, in list
order, over the MDX. A set whose expression references another set therefore only
resolves if the parent is substituted before the child — correctness depends on
the order the model-service happened to return the rows in. When it does not
resolve, a bare ``[Set]`` token reaches the axis extractors, which do not
recognise it, and the axis renders EMPTY: the deceptive-empty-result failure
Bug-7254 declared unacceptable, arrived at silently.

Written during the Task #110 review (Bug-8384). Promoted rather than discarded
because it is the executable statement of Bug-8758.
"""
from __future__ import annotations

import pytest

from src.dax.xmla_server import _inline_named_sets

CHILD = {
    "name": "Top10",
    "list_type": "advanced_mdx",
    "expression": "TopCount([Customer].[Customer].Members, 10, [Measures].[Revenue])",
}
PARENT = {
    "name": "combined",
    "list_type": "advanced_mdx",
    "expression": "Union([Top10], [Measures].[Revenue])",
}
MDX = "SELECT {[Measures].[Revenue]} ON COLUMNS, {[combined]} ON ROWS FROM [modelx]"


def test_nested_reference_resolves_when_the_parent_is_substituted_first():
    """The order the DB currently returns ('combined' < 'Top10') happens to work."""
    out = _inline_named_sets(MDX, [PARENT, CHILD])
    assert "[Top10]" not in out
    assert "TopCount([Customer].[Customer].Members, 10," in out


def test_nested_reference_resolves_regardless_of_list_order():
    """Bug-8713/8758: the xfail is removed because inlining now iterates.

    In the child-first order a single ordered pass left a bare ``[Top10]``: the
    parent's expression reintroduced the child token AFTER the child's turn had
    passed, the axis extractors did not recognise it, and the row axis rendered
    EMPTY — a deceptive empty result rather than an error. Inlining now runs to
    a fixed point, so the outcome no longer depends on the order the
    model-service happened to return the rows in.
    """
    out = _inline_named_sets(MDX, [CHILD, PARENT])
    assert "[Top10]" not in out, (
        "A bare [Top10] survives inlining: the axis extractors cannot resolve "
        "it and the row axis renders EMPTY. Inlining must reach a fixed point "
        "(or fail loud) rather than depend on list order."
    )
    assert "TopCount([Customer].[Customer].Members, 10," in out


def test_both_list_orders_produce_the_same_mdx():
    """The whole point of a fixed point: order must stop mattering at all."""
    assert _inline_named_sets(MDX, [CHILD, PARENT]) == _inline_named_sets(
        MDX, [PARENT, CHILD]
    )


def test_a_definition_cycle_fails_loud_instead_of_spinning():
    """A self-referencing set must raise, never loop or emit a bare token.

    The depth cap is what converts a cycle into a diagnosable error. Without it
    a fixed-point loop would either not terminate or silently return a partially
    expanded axis — the same deceptive-empty failure the fix exists to remove.
    """
    a = {"name": "A", "list_type": "advanced_mdx", "expression": "Union([B], [X])"}
    b = {"name": "B", "list_type": "advanced_mdx", "expression": "Union([A], [Y])"}
    with pytest.raises(ValueError, match="did not settle"):
        _inline_named_sets(
            "SELECT {[Measures].[R]} ON COLUMNS, {[A]} ON ROWS FROM [modelx]",
            [a, b],
        )


def test_bug8713_ten_level_acyclic_chain_settles():
    """Bug-8713: the depth guard must not reject a ten-pass acyclic chain."""
    named_sets = [
        {
            "name": f"S{i}",
            "list_type": "advanced_mdx",
            "expression": f"[S{i + 1}]" if i < 9 else "[Measures].[Revenue]",
        }
        for i in range(9, -1, -1)
    ]
    out = _inline_named_sets(
        "SELECT {[S0]} ON COLUMNS FROM [modelx]", named_sets,
    )
    assert "[S" not in out
    assert "[Measures].[Revenue]" in out
