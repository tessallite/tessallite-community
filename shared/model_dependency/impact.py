"""Mutation simulation, traversal, and severity aggregation (spec §7.2-§7.6).

Given a built ``DependencyGraph`` and a target, compute the impacted set for an
``inspect`` or ``delete`` operation with deterministic shortest witness paths,
hard/soft/informational severity, cascade closure, and cycle handling.

Traversal runs over the SCC condensation so cycles never cause hangs or
duplicates. Counts always describe the FULL reachable set; only display paths are
truncated (spec §7.5). Change simulation (§7.3) rebuilds a proposed graph and
diffs; that orchestration lives in the model-service layer, which owns the
snapshot copy + delta apply and calls ``simulate_delete``/``inspect`` here.
"""

from __future__ import annotations

import heapq
from collections import deque
from typing import Optional

from .edge_catalogue import SECURITY_OBJECT_TYPES, reason_key
from .graph import DependencyGraph
from .types import (
    DependencyEdge,
    EdgeKind,
    ImpactPath,
    ImpactResult,
    ImpactSummary,
    ImpactedObject,
    NodeKey,
    ObjectType,
    Operation,
    Severity,
)

# Default display caps (spec §7.5). The model-service passes settings-driven
# values; these are safe fallbacks so the pure engine is usable standalone.
DEFAULT_MAX_PATHS_PER_OBJECT = 3
DEFAULT_MAX_DISPLAY_IMPACTS = 2000


def inspect(graph: DependencyGraph, target: NodeKey, **kw) -> ImpactResult:
    """Forward traversal from ``target`` (spec §7.2). inspect previews the target's
    removal read-only, so it computes the SAME owned cascade closure as delete —
    this keeps inspect/delete severity parity (§13.6) and keeps ``cascade_deleted``
    effects consistent with ``cascade_closure``. It simply does not mutate anything."""
    closure = _cascade_closure(graph, target)
    return _traverse(graph, target, operation="inspect", removed=closure, **kw)


def simulate_delete(
    graph: DependencyGraph,
    target: NodeKey,
    *,
    max_paths_per_object: int = DEFAULT_MAX_PATHS_PER_OBJECT,
    max_display_impacts: int = DEFAULT_MAX_DISPLAY_IMPACTS,
) -> ImpactResult:
    """Delete simulation (spec §7.2): compute owned cascade closure, remove it,
    then classify surviving dependents as hard/soft."""
    closure = _cascade_closure(graph, target)
    return _traverse(
        graph,
        target,
        operation="delete",
        removed=closure,
        max_paths_per_object=max_paths_per_object,
        max_display_impacts=max_display_impacts,
    )


def _cascade_closure(graph: DependencyGraph, target: NodeKey) -> frozenset[NodeKey]:
    """Owned closure from containment + explicit ``cascade`` edges (spec §7.2.1)."""
    closure: set[NodeKey] = {target}
    queue: deque[NodeKey] = deque([target])
    while queue:
        node = queue.popleft()
        for edge in graph.dependents_of(node):
            if edge.delete_policy == "cascade" and edge.dependent not in closure:
                closure.add(edge.dependent)
                queue.append(edge.dependent)
    return frozenset(closure)


def _traverse(
    graph: DependencyGraph,
    target: NodeKey,
    *,
    operation: Operation,
    removed: frozenset[NodeKey],
    max_paths_per_object: int = DEFAULT_MAX_PATHS_PER_OBJECT,
    max_display_impacts: int = DEFAULT_MAX_DISPLAY_IMPACTS,
) -> ImpactResult:
    target_node = graph.node(target)
    if target_node is None:
        raise KeyError(f"target node not in graph: {target.token()}")

    # BFS by min-depth over forward adjacency. For delete, cascade-closure
    # members are reported as cascade_deleted, not as broken survivors.
    min_depth: dict[NodeKey, int] = {target: 0}
    best_path: dict[NodeKey, tuple[list[str], list[DependencyEdge]]] = {
        target: ([target.token()], [])
    }
    # Track the reaching edge for severity per object (highest severity wins).
    reaching_edges: dict[NodeKey, list[DependencyEdge]] = {}

    queue: deque[NodeKey] = deque([target])
    while queue:
        node = queue.popleft()
        depth = min_depth[node]
        path_nodes, path_edges = best_path[node]
        for edge in graph.dependents_of(node):
            dep = edge.dependent
            reaching_edges.setdefault(dep, []).append(edge)
            nd = depth + 1
            if dep not in min_depth or nd < min_depth[dep]:
                min_depth[dep] = nd
                best_path[dep] = (path_nodes + [dep.token()], path_edges + [edge])
                queue.append(dep)

    impacted_keys = [k for k in min_depth if k != target]

    # Resulting-state propagation (spec §7.6): an object's severity depends on the
    # RESULTING STATE of each edge's dependency endpoint, not on the edge alone.
    # A hard edge from a dependency that was only soft-degraded (not removed/broken)
    # degrades the dependent at most softly; a cascade edge from a NON-removed
    # parent contributes nothing. Resolved in min_depth order (dependencies before
    # dependents) so each endpoint's state is known when its dependents classify.
    #   removed  = in the cascade closure (deleted with the target)
    #   broken   = a surviving object whose required dependency vanished (hard)
    #   degraded = a surviving object that lost optional coverage/visibility (soft)
    #   intact   = present and unaffected (only appears as an edge source, e.g. the
    #              target itself)
    # inspect previews the target's REMOVAL too (it answers "what breaks if this
    # goes?"), so the target is seeded "removed" for inspect as well as delete —
    # otherwise every dependent would cap to soft and inspect/delete severities
    # would diverge (§13.6 explorer/dialog parity). inspect simply computes no
    # owned cascade closure (``removed`` is just {target}).
    node_state: dict[NodeKey, str] = {target: "removed"}
    for key in removed:
        node_state[key] = "removed"

    # Phase A: resolve every impacted node's state to a FIXED POINT. State only
    # escalates (intact < degraded < broken; removed is terminal from the closure),
    # so repeated passes converge. A pure min_depth-ordered single pass is unsafe:
    # a node can be reached by a SHORT soft path (low depth) and a LONG hard-broken
    # path (high depth), and the breaking dependency would still be "intact" when
    # the node is first visited. The fixed point classifies every node against the
    # final state of all its dependencies regardless of path length.
    changed = True
    passes = 0
    while changed and passes <= len(impacted_keys) + 1:
        changed = False
        passes += 1
        for key in impacted_keys:
            if node_state.get(key) == "removed":
                continue
            edges = reaching_edges.get(key, [])
            eff = _effective_edges(key, edges, node_state)
            sev, effect, _pol, _re = _classify(
                operation, key, graph.node(key), eff or edges, removed,
                has_effective=bool(eff), node_state=node_state,
            )
            new_state = _state_from_severity(operation, key, sev, effect, removed)
            if _STATE_RANK[new_state] > _STATE_RANK.get(node_state.get(key, "intact"), 0):
                node_state[key] = new_state
                changed = True

    # Phase B: build the impact objects from the converged states.
    impacts: list[ImpactedObject] = []
    cascade_count = 0
    hard_count = 0
    soft_count = 0
    unresolved_count = 0
    by_type: dict[str, int] = {}
    max_depth = 0

    for key in sorted(impacted_keys, key=lambda k: (min_depth[k], k.sort_key)):
        node = graph.node(key)
        if node is None:
            continue
        depth = min_depth[key]
        max_depth = max(max_depth, depth)
        edges = reaching_edges.get(key, [])
        effective_edges = _effective_edges(key, edges, node_state)
        severity, effect, delete_policy, reason_edge = _classify(
            operation, key, node, effective_edges or edges, removed,
            has_effective=bool(effective_edges), node_state=node_state,
        )
        if key.object_type == ObjectType.UNRESOLVED_REFERENCE:
            unresolved_count += 1
        by_type[key.object_type.value] = by_type.get(key.object_type.value, 0) + 1
        if severity == "hard_break":
            hard_count += 1
        elif severity == "soft_degrade":
            soft_count += 1
        if effect == "cascade_deleted":
            cascade_count += 1

        paths = _witness_paths(graph, target, key, best_path, max_paths_per_object)
        # reason_edge is the SAME edge that determined severity (from _classify),
        # so the reason label never diverges from the severity/effect shown.
        impacts.append(
            ImpactedObject(
                node=node,
                severity=severity,
                effect=effect,
                delete_policy=delete_policy,
                direct=(depth == 1),
                min_depth=depth,
                reason_key=reason_key(reason_edge.kind) if reason_edge else "impactAnalysis.reason.generic",
                reason_params={"field": reason_edge.source_field} if reason_edge else {},
                paths=paths,
                scc_id=graph.scc_of.get(key) if graph.scc_of.get(key) in graph.cycles else None,
            )
        )

    impacts.sort(key=_impact_sort_key)

    truncated = len(impacts) > max_display_impacts
    display_impacts = tuple(impacts[:max_display_impacts]) if truncated else tuple(impacts)

    summary = ImpactSummary(
        total=len(impacts),
        hard_break=hard_count,
        soft_degrade=soft_count,
        cascade_deleted=cascade_count,
        direct=sum(1 for i in impacts if i.direct),
        max_depth=max_depth,
        by_object_type=by_type,
        truncated=truncated,
        unresolved=unresolved_count,
    )

    cycles = tuple(
        tuple(k.token() for k in members)
        for members in graph.cycles.values()
    )

    return ImpactResult(
        operation=operation,
        target=target_node,
        impacts=display_impacts,
        summary=summary,
        cycles=cycles,
        diagnostics=graph.diagnostics,
        cascade_closure=tuple(sorted(
            (k for k in removed if k != target), key=lambda k: k.sort_key
        )),
    )


def _classify(
    operation: Operation,
    key: NodeKey,
    node,
    edges: list[DependencyEdge],
    removed: frozenset[NodeKey],
    *,
    has_effective: bool = True,
    node_state: Optional[dict[NodeKey, str]] = None,
) -> tuple[Severity, str, str, Optional[DependencyEdge]]:
    """Resulting (severity, effect, delete_policy, winning_edge) for one impacted
    object (spec §7.6).

    ``edges`` are the EFFECTIVE edges (those whose dependency endpoint was removed
    or hard-broken) when ``has_effective`` is True; otherwise they are all reaching
    edges and the object was reached only through non-breaking (degraded/intact)
    dependencies, so it can degrade at most softly. Severity is the MAX over the
    effective edges after per-edge §7.6 relaxation; ties break deterministically so
    effect/policy/reason all come from one stable dominant edge.
    """
    if not edges:
        return "informational", "cleanup", "detach", None

    # A cascade-closure member is reported as cascade_deleted for both delete AND
    # inspect (inspect previews the same removal read-only). ``removed`` is the
    # owned closure for both operations, so this stays consistent with the
    # ``cascade_closure`` field.
    if key in removed:
        return "informational", "cascade_deleted", "cascade", _dominant_edge(edges)

    # Unresolved references fail closed as hard (spec §5.5, §7.6). (A cascaded
    # unresolved node was already caught by the removed-branch above.)
    if key.object_type == ObjectType.UNRESOLVED_REFERENCE:
        return "hard_break", "breaks_reference", "restrict", _dominant_edge(edges)

    # No effective breaking edge: every dependency this object reaches through is
    # intact or merely degraded, so this object cannot hard-break. It degrades at
    # most softly (or is a pure cleanup/cascade dependent of a surviving parent,
    # i.e. informational). Take the strongest reaching edge but cap at soft.
    ns = node_state or {}

    def _dep_state(e: DependencyEdge) -> str:
        return ns.get(e.dependency, "removed" if not node_state else "intact")

    if not has_effective:
        classified = sorted(
            ((_classify_edge(key, e, _dep_state(e)), e) for e in edges),
            key=lambda ce: (_RESULT_SEVERITY_RANK[ce[0][0]], ce[1].kind.value, ce[1].source_field),
        )
        (sev, eff, pol), winning = classified[0]
        if sev == "hard_break":
            # cap: the dependency did not break, so a hard edge only degrades. A
            # cascade_deleted effect is also incoherent here (the parent survived),
            # so relabel it as the generic degrade effect for this edge.
            degrade_eff = eff if eff != "cascade_deleted" else "loses_coverage"
            return "soft_degrade", degrade_eff, pol, winning
        return sev, eff, pol, winning

    # Effective breaking edges: classify each, take the highest severity. Ties
    # break deterministically on (severity, kind, source_field).
    classified = sorted(
        ((_classify_edge(key, e, _dep_state(e)), e) for e in edges),
        key=lambda ce: (
            _RESULT_SEVERITY_RANK[ce[0][0]],
            ce[1].kind.value,
            ce[1].source_field,
        ),
    )
    (sev, eff, pol), winning = classified[0]
    return sev, eff, pol, winning


# Propagation state lattice; higher = more severe. State only escalates.
_STATE_RANK = {"intact": 0, "degraded": 1, "broken": 2, "removed": 3}


def _effective_edges(
    key: NodeKey,
    edges: list[DependencyEdge],
    node_state: dict[NodeKey, str],
) -> list[DependencyEdge]:
    """Edges that CAUSE an impact on ``key``: those whose dependency endpoint was
    removed or hard-broken. §12.6 exception: a hard edge into a SECURITY object is
    effective even when its dependency only degraded — a security policy that loses
    any bound scope must fail closed, never silently weaken."""
    is_security = key.object_type in SECURITY_OBJECT_TYPES
    out: list[DependencyEdge] = []
    for e in edges:
        dep_state = node_state.get(e.dependency, "intact")
        if dep_state in ("removed", "broken"):
            out.append(e)
        elif is_security and e.strength == "hard" and dep_state == "degraded":
            out.append(e)
    return out


def _state_from_severity(
    operation: Operation,
    key: NodeKey,
    severity: Severity,
    effect: str,
    removed: frozenset[NodeKey],
) -> str:
    """Map an object's resulting impact to its propagation state (spec §7.6).

    ``removed`` (deleted with the target) and ``broken`` (a surviving object whose
    required dependency vanished) propagate hard downstream; ``degraded`` and
    ``intact`` do not cause a downstream hard break.
    """
    if key in removed:  # closure member (delete or inspect preview)
        return "removed"
    if effect == "cascade_deleted":
        return "removed"
    if severity == "hard_break":
        return "broken"
    if severity == "soft_degrade":
        return "degraded"
    return "intact"


def _edge_effect(edge: DependencyEdge, dep_state: str) -> str:
    """The effect to REPORT for this edge given its dependency's resulting state.

    A ``cascade_deleted`` effect is only truthful when the edge's dependency was
    actually removed (so the dependent is deleted with it). A cascade edge from a
    broken-but-surviving dependency does NOT delete the dependent — report a
    reference break instead, so ``summary.cascade_deleted`` can never disagree with
    ``cascade_closure`` (§5.2, §7.2.4)."""
    if edge.effect == "cascade_deleted" and dep_state != "removed":
        return "breaks_reference"
    return edge.effect


def _classify_edge(
    key: NodeKey, edge: DependencyEdge, dep_state: str = "removed"
) -> tuple[Severity, str, str]:
    """Severity/effect/policy for ONE reaching edge (spec §7.6). ``dep_state`` is
    the resulting state of the edge's dependency endpoint."""
    effect = _edge_effect(edge, dep_state)
    # Security objects that lose a hard binding are always hard (§7.6, §12.6).
    if key.object_type in SECURITY_OBJECT_TYPES and edge.strength == "hard":
        return "hard_break", "breaks_reference", edge.delete_policy

    # §7.6 aggregate-invalidation relaxation: a GRAIN or MEASURE-coverage
    # dependency lost by an aggregate is soft_degrade when the aggregate is
    # automatically invalidated and queries safely fall back to source. In v1
    # aggregates are transparent accelerators, so safe fallback is the default;
    # the loader marks ``serves_when_stale=true`` in the edge evidence only for an
    # aggregate that would keep serving stale results, which stays hard.
    #
    # Scoped to the grain/measure edge KINDS only — NOT the dependent object type.
    # ``refresh_dependency`` (aggregate -> aggregate) is also invalidate-policy
    # into an aggregate but §5.3 mandates it stay HARD ("refresh order/source
    # materialisation breaks"); keying on object_type would wrongly soften it.
    if edge.kind in (EdgeKind.AGGREGATE_GRAIN, EdgeKind.AGGREGATE_MEASURE):
        if edge.evidence.get("serves_when_stale") == "true":
            return "hard_break", effect, edge.delete_policy
        return "soft_degrade", effect, edge.delete_policy

    if edge.strength == "hard":
        return "hard_break", effect, edge.delete_policy
    return "soft_degrade", effect, edge.delete_policy


_SEVERITY_RANK = {"hard": 2, "soft": 1}


def _dominant_edge(edges: list[DependencyEdge]) -> Optional[DependencyEdge]:
    """Highest-severity reaching edge, deterministic on ties (spec §7.6)."""
    if not edges:
        return None
    return sorted(
        edges,
        key=lambda e: (
            -_SEVERITY_RANK.get(e.strength, 0),
            e.kind.value,
            e.source_field,
        ),
    )[0]


def _witness_paths(
    graph: DependencyGraph,
    target: NodeKey,
    dest: NodeKey,
    best_path: dict[NodeKey, tuple[list[str], list[DependencyEdge]]],
    max_paths: int,
) -> tuple[ImpactPath, ...]:
    """One deterministic shortest witness path (from BFS) plus up to
    ``max_paths - 1`` additional materially-distinct paths (spec §7.5)."""
    primary_nodes, primary_edges = best_path[dest]
    paths = [ImpactPath(nodes=tuple(primary_nodes), edges=tuple(primary_edges))]
    if max_paths <= 1:
        return tuple(paths)
    extra = _alternate_paths(graph, target, dest, max_paths - 1, exclude=tuple(primary_nodes))
    paths.extend(extra)
    return tuple(paths)


def _alternate_paths(
    graph: DependencyGraph,
    target: NodeKey,
    dest: NodeKey,
    limit: int,
    exclude: tuple[str, ...],
) -> list[ImpactPath]:
    """Deterministic additional shortest paths avoiding the primary's interior
    nodes, found by Dijkstra-like search ordered by (depth, node token). Bounded:
    never enumerates all simple paths (spec §7.1)."""
    if limit <= 0:
        return []
    interior = set(exclude[1:-1])  # exclude endpoints
    results: list[ImpactPath] = []
    # priority queue of (depth, tiebreak_seq, path_node_tokens, path_edges).
    # The monotonic ``seq`` guarantees heap comparison never reaches the token
    # list or the (non-orderable) DependencyEdge list.
    start_token = target.token()
    seq = 0
    heap: list[tuple[int, int, list[str], list[DependencyEdge]]] = [(0, seq, [start_token], [])]
    # Seed with the primary signature so an identical path is never re-emitted.
    seen_signatures: set[tuple[str, ...]] = {tuple(exclude)}
    guard = 0
    while heap and len(results) < limit and guard < 10000:
        guard += 1
        depth, _seq, tokens, edges = heapq.heappop(heap)
        last_token = tokens[-1]
        if last_token == dest.token() and len(tokens) > 1:
            sig = tuple(tokens)
            # materially distinct = does not reuse the primary path's interior.
            if sig not in seen_signatures and not (interior & set(tokens[1:-1])):
                seen_signatures.add(sig)
                results.append(ImpactPath(nodes=tuple(tokens), edges=tuple(edges)))
            continue
        # expand
        cur_key = _token_to_key(graph, last_token)
        if cur_key is None:
            continue
        for edge in sorted(
            graph.dependents_of(cur_key),
            key=lambda e: e.dependent.token(),
        ):
            child = edge.dependent.token()
            if child in tokens:  # avoid cycles in a simple path
                continue
            seq += 1
            heapq.heappush(heap, (depth + 1, seq, tokens + [child], edges + [edge]))
    return results


def _token_to_key(graph: DependencyGraph, token: str) -> Optional[NodeKey]:
    for key in graph.nodes:
        if key.token() == token:
            return key
    return None


_RESULT_SEVERITY_RANK = {"hard_break": 0, "soft_degrade": 1, "informational": 2}


def _impact_sort_key(impact: ImpactedObject):
    """Sort by severity, min depth, object type, display name, then ID (§9.3)."""
    return (
        _RESULT_SEVERITY_RANK.get(impact.severity, 3),
        impact.min_depth,
        impact.node.key.object_type.value,
        impact.node.display_name.lower(),
        impact.node.key.object_id,
    )
