"""Contract guard for the shared SQLAlchemy Result fakes in conftest.

These fakes stand in for a real ``AsyncSession`` across the named-set route
tests, so a defect in them presents as a phantom route failure (or, worse, a
phantom route SUCCESS) rather than as a harness bug. Two properties are pinned:

1. ``FakeResult`` honours the ``Result`` contract, not the ``ScalarResult`` one.
   The fake it replaces returned ITSELF from ``scalars()`` and implemented only
   ``all()``, so ``rbac.caller_has_role``'s ``Result.scalar_one_or_none()`` call
   (rbac.py:326) raised ``AttributeError`` under test while working correctly
   against a real session -- four named-set tests went red for a harness reason
   when ``list_named_sets`` gained its Bug-8767 draft gate.

2. ``routed_execute`` routes on the FROM clause, NOT on a bare substring, and is
   therefore independent of keyword order. ``select(NamedSet)`` renders the
   column ``named_sets.dimensions``, so a bare-substring matcher answers the
   named-set query with the DIMENSION rows whenever ``dimensions=`` is passed
   first -- a silent wrong-answer, not a crash.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import MultipleResultsFound

from shared.db.models import Dimension, NamedSet, UserAccessBinding

from .conftest import FakeResult, FakeScalarResult, routed_execute

pytestmark = pytest.mark.unit


def test_bug_8924_fake_result_exposes_the_result_api_not_the_scalar_result_api():
    """Bug-9010 / P1-MS-TEST-INFRA-SELF-001: keep ScalarResult narrow."""
    result = FakeResult(["row"])
    # Result-level accessors: what rbac.caller_has_role actually calls.
    assert result.scalar_one_or_none() == "row"
    assert result.scalar() == "row"
    assert result.first() == "row"
    # scalars() must hand back a SEPARATE view, never self.
    scalars = result.scalars()
    assert isinstance(scalars, FakeScalarResult)
    assert scalars is not result
    assert scalars.all() == ["row"]
    assert not hasattr(scalars, "scalar_one_or_none")
    assert not hasattr(scalars, "fetchone")


def test_bug_8924_fake_result_scalar_projection_uses_the_requested_column():
    """Tuple projections must follow ``Result.scalars(index)`` semantics."""
    result = FakeResult([("id-a", "display-a"), ("id-b", "display-b")])
    assert result.scalars().all() == ["id-a", "id-b"]
    assert result.scalars(1).all() == ["display-a", "display-b"]


def test_fake_result_empty_and_multiple_match_sqlalchemy_semantics():
    assert FakeResult([]).scalar_one_or_none() is None
    assert FakeResult([]).first() is None
    assert FakeResult([]).scalars().all() == []
    with pytest.raises(MultipleResultsFound):
        FakeResult(["a", "b"]).scalar_one_or_none()
    with pytest.raises(MultipleResultsFound):
        FakeResult(["a", "b"]).scalars().one_or_none()


def test_bug_8924_no_model_service_fake_returns_itself_from_scalars():
    """The Result/ScalarResult boundary must stay real in every test fake.

    Bug-9011 / P1-MS-TEST-INFRA-SELF-003 is the guard-enumeration hardening
    captured by this test.

    This guard scans every Python file under the model-service tests tree,
    including helper modules such as ``scope_fake_db.py`` and nested helper
    classes. Parsing errors are deliberately allowed to fail the test instead
    of being ignored, and returning the receiver, or an alias of it, is the
    collapsed fake shape that caused Bug-8924.
    """
    tests_root = Path(__file__).parent
    collapsed = []

    class _CollapsedResultVisitor(ast.NodeVisitor):
        def __init__(self, receiver: str):
            self.aliases = {receiver}

        def _is_alias(self, node):
            return isinstance(node, ast.Name) and node.id in self.aliases

        def _record_target(self, node):
            if isinstance(node, ast.Name):
                self.aliases.add(node.id)
            elif isinstance(node, (ast.Tuple, ast.List)):
                for element in node.elts:
                    self._record_target(element)

        def visit_Assign(self, node: ast.Assign):
            if self._is_alias(node.value):
                for target in node.targets:
                    self._record_target(target)
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign):
            if self._is_alias(node.value):
                self._record_target(node.target)
            self.generic_visit(node)

        def visit_Return(self, node: ast.Return):
            if self._is_alias(node.value):
                collapsed.append((path, node.lineno))
            self.generic_visit(node)

        def visit_FunctionDef(self, _node: ast.FunctionDef):
            return None

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, _node: ast.ClassDef):
            return None

    for path in sorted(tests_root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name != "scalars" or not node.args.args:
                continue
            _CollapsedResultVisitor(node.args.args[0].arg).visit(
                ast.Module(body=node.body, type_ignores=[])
            )

    assert not collapsed, "Result fakes must return a separate ScalarResult: " + ", ".join(
        f"{path}:{line}" for path, line in collapsed
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reversed_kwargs", [False, True])
async def test_routed_execute_routes_on_the_from_clause_regardless_of_order(
    reversed_kwargs: bool,
) -> None:
    """A bare-substring matcher fails this: select(NamedSet) contains the text
    'dimensions' via its own column, so keyword order would decide the answer."""
    sets = ["named-set-row"]
    dims = [("dim-id", "Customer")]
    kwargs = (
        {"dimensions": dims, "named_sets": sets}
        if reversed_kwargs
        else {"named_sets": sets, "dimensions": dims}
    )
    execute = routed_execute(**kwargs)

    assert (await execute(select(NamedSet))).scalars().all() == sets
    assert (await execute(select(Dimension.id, Dimension.name))).all() == dims


@pytest.mark.asyncio
async def test_routed_execute_answers_an_unrouted_table_with_no_rows():
    """The RBAC binding lookups must see an EMPTY user_access_bindings table so
    caller_has_role takes its documented zero-bindings bootstrap path, rather
    than being handed whatever rows the route under test asked for."""
    execute = routed_execute(named_sets=["named-set-row"])
    binding_result = await execute(select(UserAccessBinding))
    assert binding_result.scalar_one_or_none() is None
    probe_result = await execute(select(UserAccessBinding.id).limit(1))
    assert probe_result.first() is None
