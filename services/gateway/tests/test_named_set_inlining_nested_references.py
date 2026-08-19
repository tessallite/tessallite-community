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


@pytest.mark.xfail(
    reason=(
        "Bug-8758: inlining is a single ordered pass, so the child-first order "
        "leaves a bare [Top10] and the row axis renders EMPTY. Reachable "
        "whenever the name ordering puts the child before the parent. Fix by "
        "iterating to a fixed point with a depth/cycle cap, or by detecting an "
        "unresolved bare set reference and failing loud. Remove this xfail with "
        "the fix."
    ),
    strict=True,
)
def test_nested_reference_resolves_regardless_of_list_order():
    out = _inline_named_sets(MDX, [CHILD, PARENT])
    assert "[Top10]" not in out, (
        "A bare [Top10] survives inlining: the axis extractors cannot resolve "
        "it and the row axis renders EMPTY. Inlining must reach a fixed point "
        "(or fail loud) rather than depend on list order."
    )
