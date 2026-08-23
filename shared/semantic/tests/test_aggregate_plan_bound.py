"""Bug-8664 — the aggregate plan UPPER BOUND must contain what the real
scheduler-side FROM-clause builder actually joins.

``shared/semantic/aggregate_plan_bound.aggregate_plan_upper_bound`` is what the
query-router uses to decide WHICH relations an aggregate materialised over, so
it can prove (``query-router/src/routing/aggregate_population.py``) that serving
the aggregate returns the query's own row population. If the bound is an
UNDER-estimate, a relation the CTAS really joined is never put through the
lossless-attachment proof and a lossy join is admitted — the exact wrong number
the gate exists to stop.

The bound is deliberately NOT a re-implementation of either builder's traversal
(that would be a third renderer of one join decision, the Bug-8628 pattern), so
the contract that keeps it honest is BEHAVIOURAL: run the real builder and
assert what it joined is inside the bound. This file pins the scheduler /
shared side (``sql_builder.build_from_clause``); the optimizer's initial-CTAS
builder is pinned by
``services/optimizer/tests/test_creator_plan_bound_contract_8664.py``.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

from shared.semantic.aggregate_plan_bound import (
    aggregate_plan_upper_bound,
    induced_connected,
    reachable_from,
)

# ---------------------------------------------------------------------------
# Pure bound behaviour
# ---------------------------------------------------------------------------

_A, _B, _C, _D, _E = "a", "b", "c", "d", "e"


def _bound(edges, anchor, needed, tables=None):
    return aggregate_plan_upper_bound(
        table_ids=tables or {_A, _B, _C, _D, _E},
        edges=edges,
        anchor_table_id=anchor,
        needed_table_ids=needed,
    )


def test_star_plan_is_exactly_the_anchor_plus_needed():
    """A star whose needed dims all hang directly off the anchor needs no
    stepping stone, so the bound must not inflate to the whole model — that is
    what keeps the gate a correctness check instead of an acceleration outage."""
    edges = [(_A, _B), (_A, _C), (_A, _D), (_A, _E)]
    assert _bound(edges, _A, {_B, _C}) == frozenset({_A, _B, _C})


def test_no_needed_relations_bounds_the_whole_component():
    """``build_from_clause`` receives ``needed_table_ids=None`` when the
    aggregate resolves no grain/measure relation and then joins EVERYTHING
    reachable, so the bound must too. Anything narrower would be an
    under-estimate."""
    edges = [(_A, _B), (_B, _C)]
    assert _bound(edges, _A, set()) == frozenset({_A, _B, _C})


def test_a_disconnected_needed_set_falls_back_to_the_component():
    """When the needed relations are not connected among themselves the
    optimizer's builder pulls in an intermediate whose choice is not bounded by
    shortest paths, so the bound widens to the whole reachable component."""
    edges = [(_A, _B), (_B, _C), (_C, _D)]
    # {_A, _D} share no direct edge: D attaches only through B and C.
    assert _bound(edges, _A, {_D}) == frozenset({_A, _B, _C, _D})


def test_tied_shortest_paths_are_both_inside_the_bound():
    """``_join_closure``'s BFS parent choice between two equal-length
    anchor->needed paths is a tie-break this module must not have to reproduce.
    Both candidate stepping stones are therefore in the bound."""
    edges = [(_A, _B), (_B, _D), (_A, _C), (_C, _D)]
    assert _bound(edges, _A, {_D}) == frozenset({_A, _B, _C, _D})


def test_a_longer_detour_is_not_pulled_in():
    """The bound stays tight: a relation that is on NO shortest anchor->needed
    path cannot be added by either builder, so it stays out."""
    edges = [(_A, _B), (_A, _C), (_C, _D), (_D, _B)]
    # Shortest A->B is the direct edge; the C-D detour is longer.
    assert _bound(edges, _A, {_B}) == frozenset({_A, _B})


def test_unknown_anchor_is_unproven():
    assert _bound([(_A, _B)], "missing", {_B}) is None
    assert _bound([(_A, _B)], None, {_B}) is None


def test_a_needed_relation_outside_the_graph_is_unproven():
    """The graph does not describe this aggregate, so it cannot bound its plan.
    Returning a partial set here would silently drop the unknown relation."""
    assert _bound([(_A, _B)], _A, {"not-in-graph"}) is None


def test_empty_universe_is_unproven():
    assert aggregate_plan_upper_bound(
        table_ids=[], edges=[], anchor_table_id=_A, needed_table_ids=[],
    ) is None


def test_edges_naming_unknown_relations_are_ignored_not_trusted():
    """An edge to a relation the graph does not declare must not widen the
    universe the bound reasons over."""
    edges = [(_A, _B), (_B, "ghost")]
    assert _bound(edges, _A, {_B}, tables={_A, _B}) == frozenset({_A, _B})


def test_self_edges_do_not_connect_anything():
    adj = {"x": set(), "y": set()}
    assert induced_connected(frozenset({"x"}), adj) is True
    assert induced_connected(frozenset({"x", "y"}), adj) is False
    assert reachable_from({"x"}, adj) == frozenset({"x"})


# ---------------------------------------------------------------------------
# Contract: the real scheduler-side builder stays inside the bound
# ---------------------------------------------------------------------------


@dataclass
class _T:
    id: UUID
    physical_name: str
    table_type: str = "dim"
    alias: Optional[str] = None
    calendar_table_id: Optional[UUID] = None


@dataclass
class _C_:
    id: UUID
    model_table_id: UUID
    column_name: str
    data_type: str = "text"


@dataclass
class _J:
    id: UUID
    left_table_id: UUID
    right_table_id: UUID
    left_column_id: UUID
    right_column_id: UUID
    join_type: str = "left"


@dataclass
class _Graph:
    tables: list = field(default_factory=list)
    columns: list = field(default_factory=list)
    joins: list = field(default_factory=list)

    def db(self):
        from shared.db.models import Join, ModelColumn, ModelTable

        def _result(items):
            r = MagicMock()
            r.scalars.return_value.all.return_value = items
            return r

        async def _execute(stmt):
            entity = None
            try:
                entity = stmt.column_descriptions[0]["entity"]
            except Exception:
                pass
            if entity is ModelTable:
                return _result(self.tables)
            if entity is Join:
                return _result(self.joins)
            if entity is ModelColumn:
                return _result(self.columns)
            return _result([])

        db = MagicMock()
        db.execute = AsyncMock(side_effect=_execute)
        return db


def _diamond():
    """anchor --(two equal-length paths)--> leaf.

    The shape where ``_join_closure``'s BFS tie-break decides which stepping
    stone lands in the FROM clause. Ids are chosen so the anchor is the fact
    table regardless of ordering.
    """
    fact = _T(UUID(int=1), "s.fact", table_type="fact", alias="f")
    mid_a = _T(UUID(int=2), "s.mid_a", alias="ma")
    mid_b = _T(UUID(int=3), "s.mid_b", alias="mb")
    leaf = _T(UUID(int=4), "s.leaf", alias="lf")
    cols = [
        _C_(UUID(int=11), fact.id, "k1"),
        _C_(UUID(int=12), mid_a.id, "k1"),
        _C_(UUID(int=13), fact.id, "k2"),
        _C_(UUID(int=14), mid_b.id, "k2"),
        _C_(UUID(int=15), mid_a.id, "k3"),
        _C_(UUID(int=16), leaf.id, "k3"),
        _C_(UUID(int=17), mid_b.id, "k4"),
        _C_(UUID(int=18), leaf.id, "k4"),
    ]
    joins = [
        _J(UUID(int=21), fact.id, mid_a.id, UUID(int=11), UUID(int=12)),
        _J(UUID(int=22), fact.id, mid_b.id, UUID(int=13), UUID(int=14)),
        _J(UUID(int=23), mid_a.id, leaf.id, UUID(int=15), UUID(int=16)),
        _J(UUID(int=24), mid_b.id, leaf.id, UUID(int=17), UUID(int=18)),
    ]
    return _Graph([fact, mid_a, mid_b, leaf], cols, joins), fact, leaf


def test_build_from_clause_joins_only_relations_inside_the_bound():
    """The behavioural contract. Whatever ``_join_closure`` picks between the
    two tied stepping stones, it must be a relation the bound already covers —
    otherwise the aggregate population proof would never test that relation's
    join for row loss.
    """
    from shared.semantic.sql_builder import build_from_clause

    graph, fact, leaf = _diamond()
    _sql, alias_by_table_id = asyncio.run(
        build_from_clause(
            graph.db(), uuid4(), needed_table_ids={leaf.id},
        )
    )
    joined = {str(tid) for tid in alias_by_table_id}

    bound = aggregate_plan_upper_bound(
        table_ids={str(t.id) for t in graph.tables},
        edges=[(str(j.left_table_id), str(j.right_table_id)) for j in graph.joins],
        anchor_table_id=str(fact.id),
        needed_table_ids={str(leaf.id)},
    )
    assert bound is not None
    assert joined <= bound, (
        f"build_from_clause joined {joined - bound} outside the plan bound; "
        "the aggregate population proof would never test those joins"
    )


def test_build_from_clause_stays_inside_the_TIGHT_bound():
    """R1 finding 6. The diamond case above exercises the LOOSE branch (needed is
    not adjacent to the anchor, so the bound widens to the whole component and
    containment cannot fail). This pins the TIGHT branch — the one that is exact,
    the one an under-estimate would make a silent wrong number, and the branch
    every real acme-demo aggregate takes.

    ``needed = {mid_a}`` is adjacent to the anchor, so ``R = {fact, mid_a}`` is
    induced-connected and the bound must be exactly those two. ``mid_b`` and
    ``leaf`` are decoys the builder must not join.
    """
    from shared.semantic.sql_builder import build_from_clause

    graph, fact, _leaf = _diamond()
    mid_a = graph.tables[1]
    _sql, alias_by_table_id = asyncio.run(
        build_from_clause(graph.db(), uuid4(), needed_table_ids={mid_a.id})
    )
    joined = {str(tid) for tid in alias_by_table_id}

    bound = aggregate_plan_upper_bound(
        table_ids={str(t.id) for t in graph.tables},
        edges=[(str(j.left_table_id), str(j.right_table_id)) for j in graph.joins],
        anchor_table_id=str(fact.id),
        needed_table_ids={str(mid_a.id)},
    )
    assert bound == frozenset({str(fact.id), str(mid_a.id)}), (
        "the tight branch must not widen; if it does, the containment assertion "
        "below passes for free and the gate refuses far more than it should"
    )
    assert joined <= bound


def test_build_from_clause_unpruned_stays_inside_the_no_needed_bound():
    """With no pruning the builder joins the whole component, which is exactly
    the branch the bound takes when an aggregate resolves no relation."""
    from shared.semantic.sql_builder import build_from_clause

    graph, fact, _leaf = _diamond()
    _sql, alias_by_table_id = asyncio.run(
        build_from_clause(graph.db(), uuid4(), needed_table_ids=None)
    )
    joined = {str(tid) for tid in alias_by_table_id}

    bound = aggregate_plan_upper_bound(
        table_ids={str(t.id) for t in graph.tables},
        edges=[(str(j.left_table_id), str(j.right_table_id)) for j in graph.joins],
        anchor_table_id=str(fact.id),
        needed_table_ids=set(),
    )
    assert joined <= bound
    assert joined == {str(t.id) for t in graph.tables}
