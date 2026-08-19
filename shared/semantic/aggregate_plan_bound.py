"""Provable upper bound on the relations an aggregate's CTAS FROM clause joins
(Bug-8664).

Why a BOUND and not the plan itself
-----------------------------------
Two different builders emit an aggregate's FROM clause:

* ``optimizer/src/lifecycle/creator._build_source_from_clause`` — the initial
  CTAS. Grows greedily from the anchor over the REQUIRED tables, and only pulls
  in a non-required "intermediate" when no required table is adjacent to an
  already-joined one.
* ``shared/semantic/sql_builder.build_from_clause`` — the scheduler's full and
  incremental refresh. Prunes with ``_join_closure(anchor, needed)``: the anchor
  plus every table on a BFS shortest anchor->needed path.

The query-router needs to know WHICH relations an aggregate materialised over
so it can decide whether serving a query from that aggregate returns the same
row population the query's own compiled plan would (see
``query-router/src/routing/pocket_population.py`` for the proof itself). It must
not do that by re-implementing either traversal: three independent renderers of
one join decision is exactly what produced Bug-8628, and CLAUDE.md's
shared-primitive discipline forbids adding a fourth.

So this module computes an UPPER BOUND that contains whatever either builder can
emit. Over-estimating the plan is the SAFE direction for the proof — a larger
plan grows the ``extra`` set every table of which must then be justified, and
adds edges to the order-freeness test. It can only ever add refusals, never
admit a plan the real builder would not have produced.

The bound
---------
``needed`` is the grain-dimension / measure-source / calculated-reference table
set both builders start from (``layout.grain_cols`` + ``layout.measure_cols`` in
the refresh; the same three sources in the creator). ``anchor`` is
``graph_order.pick_anchor_table``'s verdict, which both builders use.

    R = needed | {anchor}

    * ``needed`` EMPTY          -> reachable({anchor})
      The two builders differ MOST here, which is why the bound takes the
      superset: the refresh passes ``needed_table_ids=None`` and
      ``build_from_clause`` joins the anchor's whole connected component, while
      the creator short-circuits (``required_table_ids <= {anchor.id}``) to the
      anchor ALONE. That divergence is a real build-side defect (Bug-8691, a
      concrete case of Bug-8680); the bound covers both, so serving stays sound
      whichever one produced the rows.
    * R induced-CONNECTED       -> R | shortest_path_tables(anchor -> R)
      The creator provably never leaves R: at every step some pending table in
      R is adjacent to an already-joined one, so the intermediates leg never
      runs. ``_join_closure`` can still route through a non-required table when
      two anchor->r paths tie in length, and every table it can add lies on a
      BFS shortest path by construction, so the shortest-path set covers it.
    * otherwise                 -> reachable(R)
      The creator's intermediates leg is reached and its choice is not bounded
      by shortest paths, but it can only ever follow declared edges outward from
      the anchor, so the connected component is a genuine bound.

The bound is EXACT wherever it is accepted (do not weaken this by accident)
----------------------------------------------------------------------------
``aggregate_population.aggregate_population_proven`` refuses on
:func:`component_is_acyclic` BEFORE it can return True, so any bound that is
ever ACCEPTED lives on a tree component. On a tree, paths are unique — the
tight branch's ``R | shortest_path_tables(anchor -> R)`` collapses to exactly
``R``, which is exactly what ``_join_closure`` returns and exactly what the
creator's greedy loop joins. The bound is therefore not merely an upper bound
on an accepted plan; it is the plan.

That makes :func:`component_is_acyclic` LOAD-BEARING for the bound's soundness,
not only for Bug-8637's spanning-tree divergence. Bug-8681 proposes narrowing
the component-wide acyclicity refusal to the relations actually joined; that is
safe ONLY once the joined set is persisted by the builders as ground truth, and
unsafe while the plan is still derived here. Narrow one without the other and
the tight branch silently stops being exact.

``induced_connected`` is likewise necessary rather than merely cautious: on a
graph where ``R`` is NOT connected among itself, the creator's intermediates leg
(``creator.py``'s ``all_table_ids - joined`` fallback) picks the first adjacent
unjoined relation in canonical join order, which can lie outside any Steiner
tree over ``R``. Do not "tighten" the bound by dropping that test.

Purity
------
No DB, no ORM, no I/O — plain ids and edge pairs, so the optimizer, the
scheduler and the query-router can all exercise it and the contract test can
pin both real builders inside the bound it returns.
"""
from __future__ import annotations

from collections import deque
from typing import Iterable, Mapping

__all__ = [
    "aggregate_plan_upper_bound",
    "component_is_acyclic",
    "induced_connected",
    "reachable_from",
]


def _adjacency(
    table_ids: frozenset[str], edges: Iterable[tuple[str, str]]
) -> dict[str, set[str]]:
    """Undirected adjacency restricted to relations the graph declares.

    An edge naming a relation outside ``table_ids`` describes a graph this
    bound does not model, so it is dropped rather than silently widening the
    universe. ``aggregate_plan_upper_bound`` refuses outright when a REQUIRED
    relation is missing, so a dropped edge can never hide a needed table.
    """
    adj: dict[str, set[str]] = {t: set() for t in table_ids}
    for left, right in edges:
        left, right = str(left), str(right)
        if left in adj and right in adj and left != right:
            adj[left].add(right)
            adj[right].add(left)
    return adj


def reachable_from(
    seeds: Iterable[str], adj: Mapping[str, set[str]]
) -> frozenset[str]:
    """Every relation reachable from ``seeds`` over declared edges."""
    seen = {s for s in seeds if s in adj}
    queue = deque(seen)
    while queue:
        for neighbour in adj.get(queue.popleft(), ()):
            if neighbour not in seen:
                seen.add(neighbour)
                queue.append(neighbour)
    return frozenset(seen)


def induced_connected(nodes: frozenset[str], adj: Mapping[str, set[str]]) -> bool:
    """True when ``nodes`` is connected using ONLY edges between ``nodes``."""
    if not nodes:
        return True
    start = next(iter(nodes))
    seen = {start}
    queue = deque([start])
    while queue:
        for neighbour in adj.get(queue.popleft(), ()):
            if neighbour in nodes and neighbour not in seen:
                seen.add(neighbour)
                queue.append(neighbour)
    return seen == set(nodes)


def _bfs_levels(start: str, adj: Mapping[str, set[str]]) -> dict[str, int]:
    """Hop distance from ``start`` to every relation it can reach."""
    dist = {start: 0}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for neighbour in adj.get(node, ()):
            if neighbour not in dist:
                dist[neighbour] = dist[node] + 1
                queue.append(neighbour)
    return dist


def _shortest_path_tables(
    anchor: str, targets: frozenset[str], adj: Mapping[str, set[str]]
) -> frozenset[str]:
    """Every relation lying on SOME shortest ``anchor`` -> ``target`` path.

    ``t`` is on a shortest path to ``r`` exactly when
    ``d(anchor, t) + d(t, r) == d(anchor, r)``. ``_join_closure`` walks a BFS
    parent chain, and every such chain is one of these paths, so whatever it can
    add is in here — without this module having to reproduce its tie-break.
    """
    from_anchor = _bfs_levels(anchor, adj)
    on_path: set[str] = {anchor}
    for target in targets:
        total = from_anchor.get(target)
        if total is None:
            continue
        from_target = _bfs_levels(target, adj)
        for node, d_anchor in from_anchor.items():
            d_target = from_target.get(node)
            if d_target is not None and d_anchor + d_target == total:
                on_path.add(node)
    return frozenset(on_path)


def component_is_acyclic(
    *,
    seeds: Iterable[str],
    table_ids: Iterable[str],
    edges: Iterable[tuple[str, str]],
) -> bool:
    """True when the connected component holding ``seeds`` has no cycle.

    Bug-8637: the served SOURCE SQL and the aggregate CTAS expand a model's join
    graph with two DIFFERENT traversals
    (``query-router/rewrite/joins._build_joined_from_clause`` extends from a set
    of already-joined relations; ``sql_builder.build_from_clause`` sweeps the
    canonically ordered join list). On a graph where two paths connect the same
    pair of relations, the FROM clause is a spanning-tree CHOICE, the two
    traversals make DIFFERENT choices, and the aggregate then answers the same
    question over a different row population than source — measured on 192 of
    300 random id assignments for one legal snowflake diamond, permanently and
    with no staleness signal.

    ``pocket_population._plan_is_comparable`` already refuses a non-tree
    component for pockets. This is the same refusal for the aggregate route,
    applied to the whole COMPONENT rather than only to the relations inside the
    plan bound: a second path can leave the bound and come back, and the source
    route can traverse it, so bounding the check to the plan would leave exactly
    the divergence this refuses.

    Edges are counted as a MULTISET. Two distinct ``Join`` rows over the same
    pair of relations are two edges and therefore a cycle — the FROM clause has
    to pick one of them — even though an adjacency SET would collapse them into
    one. A self-join is counted for the same reason.
    """
    universe = frozenset(str(t) for t in table_ids if str(t))
    edge_list = [(str(left), str(right)) for left, right in edges]
    in_graph = [
        (left, right) for left, right in edge_list
        if left in universe and right in universe
    ]
    adj = _adjacency(universe, in_graph)
    component = reachable_from((str(s) for s in seeds), adj)
    if not component:
        return False
    component_edges = [
        (left, right) for left, right in in_graph
        if left in component and right in component
    ]
    return len(component_edges) == len(component) - 1


def aggregate_plan_upper_bound(
    *,
    table_ids: Iterable[str],
    edges: Iterable[tuple[str, str]],
    anchor_table_id: str | None,
    needed_table_ids: Iterable[str],
) -> frozenset[str] | None:
    """Upper bound on the relations an aggregate CTAS over this graph joins.

    Returns ``None`` when the bound cannot be established — no anchor, an anchor
    the graph does not contain, or a required relation the graph does not
    contain. ``None`` means UNPROVEN to every caller and must fail closed; it is
    never "no constraint".
    """
    universe = frozenset(str(t) for t in table_ids if str(t))
    if not universe:
        return None
    anchor = str(anchor_table_id) if anchor_table_id else ""
    if anchor not in universe:
        return None

    needed = frozenset(str(t) for t in needed_table_ids if str(t))
    if not needed.issubset(universe):
        # The aggregate needs a relation this graph does not describe, so the
        # graph cannot bound its plan.
        return None

    adj = _adjacency(universe, edges)

    if not needed:
        # ``build_from_clause`` receives ``needed_table_ids=None`` and joins the
        # anchor's whole connected component.
        return reachable_from({anchor}, adj)

    required = needed | {anchor}
    if induced_connected(required, adj):
        return required | _shortest_path_tables(anchor, required, adj)
    return reachable_from(required, adj)
