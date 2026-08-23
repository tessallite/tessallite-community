"""Row-population equivalence proof for pocket serving (Bug-8580).

WHY THIS EXISTS
---------------
A pocket caches ``SELECT * FROM <model> [WHERE ...]``.  ``SELECT *`` projects
every model column, so the query-router compiles it to the model's FULL star
join — every table, using the modeller's declared join types.  A consuming
query, however, is compiled with **join elision**: ``_resolve_required_and_base_tables``
only marks the tables that own a referenced column, and
``joins._build_joined_from_clause`` joins only those.  When any elided join is
INNER / RIGHT (or duplicates rows), the two plans produce DIFFERENT row
populations over the same model.

The pocket matcher's containment proof is about COLUMNS and PREDICATE VALUES.
Neither says anything about which rows survive the joins, so a pocket holding
``fact INNER JOIN 20 dimensions`` (16,722 rows on the acme-demo seed) was
admitted as a superset of a bare fact scan (100,000 rows) and every additive
aggregate served from it came back understated.

WHAT THIS MODULE PROVES
-----------------------
Given the model's join graph and the table set the QUERY's plan joins (``Q``),
decide whether the pocket's row population — restricted to ``Q`` — is the same
row multiset the query's own plan would produce.  ``P``, the table set the
pocket's plan joins, is estimated as everything reachable from ``Q`` in the join
graph.  It is a pure function: no DB, no I/O, no SQL.

``population_proven`` returns True ONLY when equivalence is provable.  Anything
unproven returns False and the caller must fall back to source.  A False result
never produces a wrong number; it only forgoes an acceleration.

THE RULE
--------
1. ``Q`` must be NON-EMPTY and name only relations this model's graph contains.
   (``Q ⊆ P`` needs no check: ``P`` is DERIVED from ``Q`` by reachability, so it
   holds by construction.)  Unproven otherwise.
2. Every join edge inside ``P`` must DECLARE which relation it preserves
   (``inner``/``left``/``right``/``full``).  A legacy/unrecognised token
   (``many_to_one`` WAS the storage default before the orientation/cardinality
   split moved it to ``inner``; rows written earlier still carry it) renders
   as an un-flipped ``LEFT JOIN`` onto whichever side the traversal
   accumulated first, and the two plans start from different base tables, so
   the same edge can preserve opposite relations in each.  Unproven.
3. Each connected component of ``P`` must be a TREE.  With a cycle (or a
   parallel edge pair) the FROM clause is a spanning-tree CHOICE and the two
   plans can join on different predicates over the same relations.  Unproven.
4. ``P`` must be ORDER-FREE: rooted at some relation, its INNER edges form one
   contiguous cluster containing that root and every outer edge preserves its
   core-ward endpoint.  The renderer emits a left-to-right chain grown from the
   plan's BASE, the two plans have different bases, and an outer join does not
   commute with an INNER join further along the same path — so per-edge
   preservation alone does not survive composition.  See ``_plan_is_comparable``
   for the worked 100-vs-600 counterexample.  Unproven otherwise.
5. ``extra = P \\ Q``    — empty means both plans render the same edges, with the
                          same orientation, in an order that does not matter.
                          PROVEN.
6. Otherwise every table in ``extra`` must attach LOSSLESSLY to the kept set:

   * Every model join edge incident to that table whose other endpoint is also
     in ``P`` must have its other endpoint in ``Q``.  An edge between two
     *extra* tables is refused outright — a DELIBERATE conservatism, not a
     necessity: the build recorded no traversal order, so this module does not
     attempt to reconstruct a multi-hop attachment chain (snowflake arm behind
     another elided arm).  Such a chain routes to source.
   * The edge must PRESERVE the kept endpoint's rows:
       - ``inner`` -> no (drops unmatched rows on both sides);
       - ``full``  -> no (it ADDS unmatched rows from the far side);
       - ``left``  -> only when the kept endpoint is the modeller's LEFT table;
       - ``right`` -> only when the kept endpoint is the modeller's RIGHT table.
     (Legacy tokens never reach this point — rule 2 refused the plan.)
   * The edge must NOT DUPLICATE: the join column on the EXTRA table's side must
     be that table's SOLE declared primary-key column.  A single-column unique
     key admits at most one match per kept row, so the kept side's cardinality
     is unchanged.  A COMPOSITE key joined on one half matches N rows and
     inflates every SUM/COUNT, and nothing in the product validates that a table
     flags at most one ``is_primary_key`` column.
   * The table must have at least one incident edge — a relation with no join
     path cannot be reasoned about.

SOUNDNESS OF THE CALLER'S APPROXIMATIONS
----------------------------------------
``P`` must be over-estimated and ``Q`` under-estimated; both directions only
ever ADD refusals:

* ``P`` is the reachability closure of ``Q``.  It is a genuine upper bound: the
  join traversal grows outward from the plan's base along declared edges only,
  so it can never reach outside that connected component.  It stays an
  OVER-estimate of what the pocket really joined — it also covers the
  stepping-stone tables that own no projected column
  (``_build_joined_from_clause``'s ``intermediates`` leg), which a
  bound-objects-only estimate would miss and which would hide a lossy edge.
* ``Q`` under-estimated (tables owning resolved dimensions / measures / filter
  dimensions; calculated-measure references are not chased).  Under-estimating
  ``Q`` can only grow ``extra``.

Consequently every table that genuinely differs between the two REAL plans is
in ``extra`` and is individually tested, and the edge the real traversal used is
always among the incident edges tested.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

# Join-type vocabulary.  IMPORTED, not restated: the renderer
# (``shared/semantic/join_keyword.py``) decides what SQL is EMITTED and this
# module decides what may be PROVEN, so a token the two classified differently
# would be a silent wrong-number hole.  These were previously two hand-kept
# "byte-identical" copies — the exact arrangement that let the CTAS builder
# drift from the source route in Bug-8628.
from shared.semantic.join_keyword import (  # noqa: E402
    FULL_TOKENS as _FULL_TOKENS,
    INNER_TOKENS as _INNER_TOKENS,
    LEFT_TOKENS as _LEFT_TOKENS,
    RIGHT_TOKENS as _RIGHT_TOKENS,
    is_orientation_declared as _token_orientation_is_declared,
)
from shared.semantic.join_population_serving import (
    DEFAULT_POPULATION_PARTICIPATION,
    population_defining_table_ids,
)


@dataclass(frozen=True)
class JoinEdge:
    """One model join edge, in the modeller's own left/right orientation."""

    left_table_id: str
    right_table_id: str
    left_column_id: str
    right_column_id: str
    join_type: str
    population_participation: str = DEFAULT_POPULATION_PARTICIPATION

    def other_endpoint(self, table_id: str) -> str | None:
        if table_id == self.left_table_id:
            return self.right_table_id
        if table_id == self.right_table_id:
            return self.left_table_id
        return None

    def column_on(self, table_id: str) -> str | None:
        """The join column belonging to ``table_id`` (None when not an endpoint).

        A self-join (both endpoints the same table) has two distinct columns and
        no unambiguous answer, so it returns None and the proof fails closed.
        """
        if self.left_table_id == self.right_table_id:
            return None
        if table_id == self.left_table_id:
            return self.left_column_id
        if table_id == self.right_table_id:
            return self.right_column_id
        return None


@dataclass(frozen=True)
class ModelJoinGraph:
    """The deployed physical graph the proof reasons over.

    ``table_ids`` is the model's whole relation universe; the POCKET plan
    estimate is derived from it by reachability (see ``population_proven``).
    ``pk_column_ids`` holds the ids of columns declared ``is_primary_key``.
    ``table_id_by_column_id`` lets the proof verify that a join column really
    belongs to the table it is claimed for, so a malformed graph cannot be read
    as a uniqueness proof.

    ``anchor_table_id`` is ``shared.semantic.graph_order.pick_anchor_table``'s
    verdict for this model — the relation every aggregate CTAS FROM clause is
    built around. The pocket proof never reads it (a pocket's ``SELECT *`` plan
    is the whole reachable component whatever the anchor is); the AGGREGATE plan
    bound does (Bug-8664), and it belongs on the graph so both routes read one
    resolution of the anchor rather than each deriving its own. ``None`` when
    the loader could not resolve one, which is UNPROVEN to the aggregate side.
    """

    table_ids: frozenset[str]
    edges: tuple[JoinEdge, ...] = ()
    pk_column_ids: frozenset[str] = frozenset()
    table_id_by_column_id: dict[str, str] = field(default_factory=dict)
    anchor_table_id: str | None = None


def _orientation_is_declared(edge: JoinEdge) -> bool:
    """True when the edge names WHICH RELATION it preserves, independently of
    the traversal that renders it.

    ``inner``, ``full``, ``left`` and ``right`` do: the shared renderer
    ``shared.semantic.join_keyword.join_keyword`` flips LEFT<->RIGHT when the traversal arrives from the modeller's right
    table precisely so the PRESERVED PHYSICAL TABLE stays the same (Bug-7775).

    An unrecognised / legacy token does NOT. ``many_to_one`` WAS the column
    default in ``shared/db/models.py``; the join-orientation contract moved
    that default to ``inner``, but rows written before it still carry the old
    token and must keep serving (invariant 4). ``join_keyword`` renders those as an
    un-flipped ``LEFT JOIN``, which preserves whichever side happens to be
    ALREADY ACCUMULATED — and that is decided by the plan's BASE table, which
    this module cannot see. The pocket's base is measure-driven (``SELECT *``
    resolves a measure first) while a dimension-only query's base is its first
    resolved dimension, so the very same legacy edge can render as
    ``sales LEFT JOIN country`` for the pocket and ``country LEFT JOIN sales``
    for the query. Those are different row multisets: countries with no sale
    vanish from one and a NULL group appears in the other.

    So a plan containing ANY legacy-tokened edge is not comparable here at all —
    not for an elided relation, and not even when the two table sets are
    identical. ``population_proven`` refuses the whole plan in that case.
    Lifting this needs the build to record its actual base and emitted edges
    (see docs/questions/questions_pocket-join-population.md option 1a).
    """
    return _token_orientation_is_declared(edge.join_type)


def _edge_preserves(edge: JoinEdge, kept_table_id: str) -> bool:
    """True when ``edge`` cannot drop or add rows on ``kept_table_id``'s side.

    Only meaningful for an orientation-declared edge; ``population_proven`` has
    already refused any plan containing a legacy-tokened one. The final
    ``return False`` keeps that a defence-in-depth invariant rather than an
    assumption: if this is ever called on an undeclared token it refuses.
    """
    token = (edge.join_type or "").strip().lower()
    if token in _INNER_TOKENS:
        # Drops rows on both sides that have no counterpart.
        return False
    if token in _FULL_TOKENS:
        # Preserves both sides but ADDS the far side's unmatched rows, so the
        # kept side's row multiset is not preserved either.
        return False
    if token in _LEFT_TOKENS:
        return kept_table_id == edge.left_table_id
    if token in _RIGHT_TOKENS:
        return kept_table_id == edge.right_table_id
    return False


def _attaches_losslessly(
    table_id: str,
    *,
    kept: frozenset[str],
    plan: frozenset[str],
    graph: ModelJoinGraph,
) -> bool:
    """True when joining ``table_id`` into ``kept`` provably changes no row."""
    incident = [
        edge for edge in graph.edges
        if edge.other_endpoint(table_id) is not None
        and (edge.other_endpoint(table_id) or "") in plan
    ]
    if not incident:
        # In the pocket's plan but unreachable in the declared graph: the plan
        # that produced this table is not describable here.
        return False
    for edge in incident:
        other = edge.other_endpoint(table_id) or ""
        if other not in kept:
            # An edge between two tables that are BOTH absent from the query's
            # plan: a multi-hop attachment chain. Refused conservatively — the
            # build records no traversal order, and reconstructing one here
            # would be a second inference layered on top of the one this proof
            # already makes. Costs a snowflake arm behind another elided arm;
            # that query routes to source.
            return False
        if not _edge_preserves(edge, other):
            return False
        far_column_id = edge.column_on(table_id)
        if not far_column_id:
            return False
        # The column must genuinely belong to the table it is claimed for, or a
        # malformed graph could borrow another relation's primary key.
        if graph.table_id_by_column_id.get(far_column_id) != table_id:
            return False
        if far_column_id not in graph.pk_column_ids:
            # Not provably unique -> the edge may fan the kept rows out.
            return False
        # ``is_primary_key`` is a PER-COLUMN modeller flag and nothing validates
        # that a table declares at most one. A COMPOSITE key legitimately flags
        # two columns, and joining on one half of it matches N rows per kept row
        # — fanning the fact out and INFLATING every SUM/COUNT (the Bug-8580
        # defect in the opposite direction). Only a single-column declared key
        # proves at-most-one-match.
        if _declared_key_columns(table_id, graph) != {far_column_id}:
            return False
    return True


def _declared_key_columns(table_id: str, graph: ModelJoinGraph) -> set[str]:
    """The columns ``table_id`` declares as primary key."""
    return {
        cid for cid in graph.pk_column_ids
        if graph.table_id_by_column_id.get(cid) == table_id
    }


def _reachable_from(seeds: frozenset[str], graph: ModelJoinGraph) -> frozenset[str]:
    """Every relation the join traversal could reach starting from ``seeds``.

    ``_build_joined_from_clause`` grows the FROM clause outward from the base
    table along declared join edges and returns ``None`` (compile failure) if it
    cannot reach a required relation. A relation in a DIFFERENT connected
    component than the plan's base can therefore never appear in any compiled
    plan, so it is not part of the pocket's population and must not be counted
    against it.
    """
    adjacency: dict[str, list[str]] = {}
    for edge in graph.edges:
        if edge.left_table_id in graph.table_ids and edge.right_table_id in graph.table_ids:
            adjacency.setdefault(edge.left_table_id, []).append(edge.right_table_id)
            adjacency.setdefault(edge.right_table_id, []).append(edge.left_table_id)
    seen = set(seeds)
    queue = list(seeds)
    while queue:
        current = queue.pop()
        for neighbour in adjacency.get(current, ()):
            if neighbour not in seen:
                seen.add(neighbour)
                queue.append(neighbour)
    return frozenset(seen)


def _plan_is_comparable(
    plan: frozenset[str], plan_edges: list[JoinEdge],
) -> bool:
    """True when the plan renders the SAME rows whichever relation roots it.

    ``_build_joined_from_clause`` emits a left-to-right chain grown outward from
    ``base_table_id``, and the two plans have DIFFERENT roots: a pocket's
    ``SELECT *`` binds every measure so the compiler roots it at the first
    measure's relation, while a measure-free projection query roots at its first
    dimension's relation. Per-edge preservation is NOT compositional across that
    difference — an outer join does not commute with a row-reducing INNER join
    further along the same path:

        fact --LEFT(preserves fact)--> dim_b --INNER--> dim_c

        rooted at fact  : fact LEFT JOIN dim_b INNER JOIN dim_c
                          -> the INNER hop discards the rows LEFT preserved
        rooted at dim_c : dim_c INNER JOIN dim_b RIGHT JOIN fact
                          -> every fact row survives

    Measured on that shape: SUM(amount) 100 one way, 600 the other, with every
    edge orientation-declared and the table sets identical.

    A plan is order-free when each connected component is a TREE (a cycle or a
    parallel edge pair makes the FROM clause a spanning-tree CHOICE, and which
    edge is dropped depends on set-iteration order) AND admits a CORE: a
    relation from which every INNER edge lies inside one contiguous inner
    cluster — inner joins are associative and commutative, so their order is
    free — and every outer edge preserves its CORE-WARD endpoint, so it is a
    pure attachment onto that cluster rather than a filter applied to it.
    Everything else routes to source.
    """
    adjacency: dict[str, list[tuple[str, JoinEdge]]] = {t: [] for t in plan}
    for edge in plan_edges:
        adjacency[edge.left_table_id].append((edge.right_table_id, edge))
        adjacency[edge.right_table_id].append((edge.left_table_id, edge))

    unvisited = set(plan)
    while unvisited:
        component = _component_of(next(iter(unvisited)), adjacency)
        unvisited -= component
        component_edges = [
            e for e in plan_edges
            if e.left_table_id in component and e.right_table_id in component
        ]
        # Tree test, per component: a forest bound over the whole plan would let
        # a cycle in one component hide behind another component's spare edge.
        if len(component_edges) != len(component) - 1:
            return False
        if len(component_edges) <= 1:
            # One edge renders identically from either end (A LEFT JOIN B and
            # B RIGHT JOIN A are the same rows), so there is nothing to order.
            continue
        if not any(
            _core_is_valid(core, adjacency, component) for core in sorted(component)
        ):
            return False
    return True


def _component_of(
    start: str, adjacency: dict[str, list[tuple[str, JoinEdge]]],
) -> set[str]:
    seen = {start}
    queue = [start]
    while queue:
        for neighbour, _edge in adjacency.get(queue.pop(), ()):
            if neighbour not in seen:
                seen.add(neighbour)
                queue.append(neighbour)
    return seen


def _core_is_valid(
    core: str,
    adjacency: dict[str, list[tuple[str, JoinEdge]]],
    component: set[str],
) -> bool:
    """True when every edge points its preserved side back toward ``core``.

    Walks outward from ``core``. A relation is IN THE INNER CLUSTER when it is
    the core itself or was reached from a cluster member by an INNER edge; an
    INNER edge reached from outside the cluster is refused, because an inner
    join applied after an outer one filters the rows that outer join preserved.
    An outer edge must preserve the core-ward endpoint it was reached from.
    """
    in_inner_cluster = {core: True}
    seen = {core}
    queue = [core]
    while queue:
        near = queue.pop(0)
        for far, edge in adjacency.get(near, ()):
            if far in seen or far not in component:
                continue
            token = (edge.join_type or "").strip().lower()
            if token in _INNER_TOKENS:
                if not in_inner_cluster[near]:
                    return False
                in_inner_cluster[far] = True
            else:
                if not _edge_preserves(edge, near):
                    return False
                in_inner_cluster[far] = False
            seen.add(far)
            queue.append(far)
    return True


def population_proven(
    *,
    graph: ModelJoinGraph | None,
    query_table_ids: Iterable[str] | None,
    plan_table_ids: Iterable[str] | None = None,
) -> bool:
    """True when an artifact built over ``graph`` serves ``query_table_ids`` exactly.

    ``graph`` is None when the model's physical graph could not be resolved, and
    ``query_table_ids`` is None (or empty) when the query's plan could not be
    determined.  Both are UNPROVEN, not "unconstrained": they return False.

    ``plan_table_ids`` is the ARTIFACT's own plan ``P``.

    * Omitted (the POCKET caller): ``P`` is estimated as everything REACHABLE
      from the query's own relations. That is an upper bound on what the
      pocket's ``SELECT *`` could have joined — the traversal starts at the
      plan's base and can only follow declared edges, and the pocket must
      contain the query's relations to be rewritable onto at all, so the two
      plans live in the same connected component. Measured on the acme-demo
      ``modely`` snapshot: the model declares 29 relations but only 23 are
      reachable from the fact, and the live compiled ``SELECT *`` joins exactly
      those 23. Counting the 6 unreachable ones would refuse even a query whose
      plan is identical to the pocket's, for no correctness gain.
    * Supplied (the AGGREGATE caller, Bug-8664): an aggregate CTAS joins only
      ``{anchor} ∪ grain tables ∪ measure tables`` plus any stepping stone its
      traversal needed — far less than the whole reachable component — so the
      reachability estimate would refuse almost every aggregate on a model that
      declares a single lossy join anywhere. The caller supplies a provable
      upper bound on its own plan instead
      (``shared.semantic.aggregate_plan_bound``); ``Q ⊆ P`` is no longer free
      and IS checked below.

    Every rule from this module's header applies unchanged to a supplied plan:
    the caller only decides WHICH relations the artifact joined, never whether
    joining them was lossless.
    """
    if graph is None or query_table_ids is None:
        return False
    if not graph.table_ids:
        return False
    if plan_table_ids is not None:
        supplied_plan = frozenset(str(t) for t in plan_table_ids if str(t))
        if not supplied_plan or not supplied_plan.issubset(graph.table_ids):
            # An empty plan, or one naming a relation this graph does not
            # describe, is not a plan this proof can reason about.
            return False
    else:
        supplied_plan = None
    kept = frozenset(str(t) for t in query_table_ids if str(t))
    if not kept:
        # e.g. ``SELECT count(*) FROM model`` with no filters: nothing resolves
        # to a physical relation, so the query's plan is unknown here even
        # though the compiler would pick the fact table. Unproven.
        #
        # Today the multi-hop rule below would refuse an empty ``kept`` anyway
        # (no extra table has an incident edge into the kept set). This stays as
        # the EXPLICIT contract — "unknown is not unconstrained" — so relaxing
        # that conservatism later cannot silently turn an unknown plan into an
        # accepted one.
        return False
    if not kept.issubset(graph.table_ids):
        # The query names a relation this model's graph does not contain, so the
        # graph does not describe the query. Unproven.
        return False

    # Bug-8615 / G3: the query and artifact plans both include every endpoint
    # of a deployed population-defining edge.  Resolve this through the same
    # mapping/ORM/namespace normalizer used by source and aggregate builders.
    # Unknown participation is elidable; malformed mandatory endpoints or an
    # endpoint outside the deployed graph are unproven and therefore refuse.
    population_tables = population_defining_table_ids(graph.edges)
    if population_tables is None or not population_tables.issubset(graph.table_ids):
        return False
    if not population_tables.issubset(_reachable_from(kept, graph)):
        return False
    kept = frozenset(set(kept) | set(population_tables))
    if supplied_plan is None:
        plan = _reachable_from(kept, graph)
    else:
        plan = supplied_plan
        if not kept.issubset(plan):
            # The query's plan joins a relation the ARTIFACT did not. With a
            # reachability-derived plan this holds by construction; with a
            # supplied one it is a real possibility and the two populations
            # cannot be compared by the ``extra`` rule below. Unproven.
            return False
    plan_edges = [
        edge for edge in graph.edges
        if edge.left_table_id in plan and edge.right_table_id in plan
    ]

    # Every edge the two plans could render must name the relation it preserves
    # independently of traversal direction. A legacy-tokened edge does not, and
    # the two plans have different BASE tables, so it can render preserving
    # opposite sides in each. See ``_orientation_is_declared``. This must be
    # checked over the WHOLE plan, not only the elided part: with identical
    # table sets there is no elided part at all, yet the orientations can still
    # differ.
    if any(not _orientation_is_declared(edge) for edge in plan_edges):
        return False

    # The plan must be a FOREST (rule 3) and ORDER-FREE (rule 4). Both are
    # per-connected-component properties, so they are checked together.
    if not _plan_is_comparable(plan, plan_edges):
        return False

    extra = plan - kept
    if not extra:
        return True
    return all(
        _attaches_losslessly(table_id, kept=kept, plan=plan, graph=graph)
        for table_id in extra
    )
