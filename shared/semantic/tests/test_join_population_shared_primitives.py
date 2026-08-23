"""The join-population classifier owns no private copy of a shared test.

Bug-8674. ``graph_order.is_fact_table`` exists as "ONE fact test for the whole
codebase" because two copies of that comparison drifted apart inside a single
commit and re-opened Bug-8600's fail-open. The classifier picks its
breadth-first root with it, and that root decides every edge's near/far
orientation and therefore every join's classification — the same correctness
role ``pick_anchor_table`` plays, so it is exactly the caller that must not
re-implement the test.

Asserted on the source rather than only through behaviour: today
``FACT_TABLE_TYPE`` IS the literal ``"fact"``, so a private copy is
behaviourally identical and no black-box test could tell them apart. The whole
point of the shared primitive is the day that stops being true.

The detector is itself under test (``test_the_guard_itself_catches_every_
reintroduction_shape``). Its first version inspected only
``ast.Compare.comparators``, so a Yoda comparison, an ``in`` tuple, a module
constant and a ``match``/``case`` all walked straight past it — a guard with an
enumeration blind spot is not a guard, and CLAUDE.md names that as a
first-class finding category rather than an incidental one.
"""
from __future__ import annotations

import ast
import inspect

import pytest

from shared.semantic import join_population_validator as module

pytestmark = pytest.mark.unit

_FACT_LITERAL = "fact"


def _string_constants(node: ast.AST) -> list[str]:
    """Every string constant directly readable from *node*.

    Recurses into the container literals a membership test uses
    (``x in ("fact",)``), which is where the first version of this walk lost
    the trail.
    """
    if isinstance(node, ast.Constant):
        return [node.value] if isinstance(node.value, str) else []
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        out: list[str] = []
        for element in node.elts:
            out.extend(_string_constants(element))
        return out
    return []


def _private_fact_comparisons(tree: ast.AST) -> list[int]:
    """Line numbers where the module tests for the fact literal on its own.

    Covers every shape a re-introduction can take:

    * ``t.table_type == "fact"`` and the Yoda form ``"fact" == t.table_type``
      (the constant can sit on EITHER side of a ``Compare``);
    * ``t.table_type in ("fact",)`` / ``[...]`` / ``{...}``;
    * ``match t.table_type: case "fact":``;
    * a module-level ``FACT = "fact"`` that a later comparison reads through a
      name, which no ``Compare`` walk can see.
    """
    hits: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if any(
                _FACT_LITERAL in _string_constants(operand)
                for operand in operands
            ):
                hits.append(node.lineno)
        elif isinstance(node, ast.MatchValue):
            if _FACT_LITERAL in _string_constants(node.value):
                hits.append(node.lineno)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            if node.value is not None and _FACT_LITERAL in _string_constants(
                node.value
            ):
                hits.append(node.lineno)
    return sorted(set(hits))


def test_the_fact_anchor_uses_the_shared_primitive():
    """Matched on the parsed AST, not on the raw text.

    A substring scan would also fire on the comment that EXPLAINS this rule,
    which is the classic way a guard like this gets weakened until it stops
    guarding anything. Only the literal in executable code counts.
    """
    source = inspect.getsource(module)
    assert "is_fact_table(" in source, (
        "the fact-table test must come from shared.semantic.graph_order"
    )
    offenders = _private_fact_comparisons(ast.parse(source))
    assert not offenders, (
        f"a private fact-table test reappeared at line(s) {offenders}; "
        "graph_order.is_fact_table owns this test for the whole codebase"
    )


@pytest.mark.parametrize(
    "snippet",
    [
        'if t.table_type == "fact": pass',
        'if "fact" == t.table_type: pass',
        'if t.table_type in ("fact",): pass',
        'if t.table_type in ["fact"]: pass',
        'if t.table_type in {"fact"}: pass',
        'FACT = "fact"\nif t.table_type == FACT: pass',
        'match t.table_type:\n    case "fact":\n        pass',
    ],
)
def test_the_guard_itself_catches_every_reintroduction_shape(snippet):
    """R5-2. The detector is the thing most likely to rot silently: a guard
    that stops detecting still reports green forever."""
    assert _private_fact_comparisons(ast.parse(snippet)), snippet


@pytest.mark.parametrize(
    "snippet",
    [
        'if t.table_type == "dim_detail": pass',
        'if is_fact_table(t): pass',
        '"""a docstring mentioning fact"""',
        '# a comment mentioning == "fact"',
    ],
)
def test_the_guard_does_not_fire_on_innocent_code(snippet):
    """Mutation partner: a detector that flags everything is as useless as one
    that flags nothing, and would push the next author to delete it."""
    assert not _private_fact_comparisons(ast.parse(snippet)), snippet


def test_the_shared_primitive_still_answers_for_an_orm_row():
    """Guards the import itself: if ``is_fact_table`` ever stopped accepting an
    ORM-shaped row, the classifier would resolve no anchor and silently fall
    back to worse-of-both-directions on every edge."""
    import types

    from shared.semantic.graph_order import is_fact_table

    assert is_fact_table(types.SimpleNamespace(table_type="fact")) is True
    assert is_fact_table(types.SimpleNamespace(table_type="dim_detail")) is False
