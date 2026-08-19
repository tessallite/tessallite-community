"""Relationship / join-path reachability what-if (spec §7.4).

A relationship does not own every object on its endpoint tables. Removing it is a
hard break for an object ONLY when a required table becomes unreachable and no
alternate valid join path remains. This module computes that differentially so
models with redundant relationship paths do not produce false-positive hard sets.

Pure: it operates on the relationship endpoints and each object's required-table
set (both derived by the loader), never on a live query builder's chosen path.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .snapshot import ModelDependencySnapshot


@dataclass(frozen=True)
class RelationshipImpact:
    """Result of removing/replacing one relationship (spec §7.4)."""

    # object_id -> hard_break because a required table became unreachable
    hard_break_object_ids: tuple[str, ...]
    # object_id -> soft_degrade (path cost / ambiguity / lost acceleration)
    soft_degrade_object_ids: tuple[str, ...]
    # deterministic shortest witness of the lost table path (table id tokens)
    witness_path: tuple[str, ...]


def _table_graph(
    relationships, exclude_ids: frozenset[str]
) -> dict[str, set[str]]:
    """Undirected table adjacency from all joins except ``exclude_ids``."""
    adj: dict[str, set[str]] = {}
    for r in relationships:
        if r.id in exclude_ids:
            continue
        adj.setdefault(r.left_table_id, set()).add(r.right_table_id)
        adj.setdefault(r.right_table_id, set()).add(r.left_table_id)
    return adj


def _reachable(adj: dict[str, set[str]], start: str) -> set[str]:
    seen = {start}
    q: deque[str] = deque([start])
    while q:
        node = q.popleft()
        for nb in sorted(adj.get(node, ())):
            if nb not in seen:
                seen.add(nb)
                q.append(nb)
    return seen


def _shortest_path(adj: dict[str, set[str]], start: str, goal: str) -> tuple[str, ...]:
    """Deterministic shortest table path start->goal (BFS, sorted neighbours)."""
    if start == goal:
        return (start,)
    prev: dict[str, str] = {}
    seen = {start}
    q: deque[str] = deque([start])
    while q:
        node = q.popleft()
        for nb in sorted(adj.get(node, ())):
            if nb not in seen:
                seen.add(nb)
                prev[nb] = node
                if nb == goal:
                    path = [goal]
                    while path[-1] != start:
                        path.append(prev[path[-1]])
                    return tuple(reversed(path))
                q.append(nb)
    return ()


def relationship_removal_impact(
    snapshot: ModelDependencySnapshot,
    relationship_id: str,
    *,
    object_required_tables: dict[str, tuple[str, tuple[str, ...]]],
) -> RelationshipImpact:
    """Differential reachability for removing ``relationship_id`` (spec §7.4).

    ``object_required_tables`` maps object_id -> (anchor_table_id, required_table_ids)
    as derived by the loader using the same concepts as ``model_validator``. An
    object hard-breaks only when a required table is unreachable from its anchor
    after removal AND was reachable before.
    """
    rel = next((r for r in snapshot.relationships if r.id == relationship_id), None)
    if rel is None:
        return RelationshipImpact((), (), ())

    before = _table_graph(snapshot.relationships, frozenset())
    after = _table_graph(snapshot.relationships, frozenset({relationship_id}))

    hard: list[str] = []
    soft: list[str] = []
    witness: tuple[str, ...] = ()

    for object_id, (anchor, required) in sorted(object_required_tables.items()):
        if not anchor:
            continue
        reach_after = _reachable(after, anchor)
        reach_before = _reachable(before, anchor)
        lost_required = [
            t for t in required
            if t not in reach_after and t in reach_before
        ]
        if lost_required:
            hard.append(object_id)
            if not witness:
                # witness = the pre-removal path to the first lost table.
                witness = _shortest_path(before, anchor, lost_required[0])
        else:
            # Path still exists; note a soft degrade only if the shortest path
            # length changed (cost/ambiguity) for a required table (spec §7.4.7).
            changed = any(
                len(_shortest_path(after, anchor, t)) != len(_shortest_path(before, anchor, t))
                for t in required
                if t in reach_after
            )
            if changed:
                soft.append(object_id)

    return RelationshipImpact(
        hard_break_object_ids=tuple(hard),
        soft_degrade_object_ids=tuple(soft),
        witness_path=witness,
    )


def relationship_change_impact(
    baseline_relationships,
    proposed_relationships,
    *,
    object_required_tables: dict[str, tuple[str, tuple[str, ...]]],
) -> RelationshipImpact:
    """Differential reachability for a relationship CHANGE (re-point / rebind), not
    just a removal (spec §7.4).

    A change (endpoint rebind, re-point, direction flip) is expressed as a full
    before/after relationship set rather than a single excluded ID, so it covers
    cases ``relationship_removal_impact`` cannot (an endpoint moved to a different
    table). An object hard-breaks when a required table is reachable from its
    anchor in the BASELINE join graph but not in the PROPOSED one — an all-valid-
    path property, so a redundant alternate join path is NOT a false hard break.
    A required table that stays reachable but at a changed shortest-path length is
    a soft_degrade (§7.4.7). One deterministic shortest witness path is returned.
    """
    before = _table_graph(baseline_relationships, frozenset())
    after = _table_graph(proposed_relationships, frozenset())

    hard: list[str] = []
    soft: list[str] = []
    witness: tuple[str, ...] = ()

    for object_id, (anchor, required) in sorted(object_required_tables.items()):
        if not anchor:
            continue
        reach_before = _reachable(before, anchor)
        reach_after = _reachable(after, anchor)
        lost_required = [
            t for t in required if t in reach_before and t not in reach_after
        ]
        if lost_required:
            hard.append(object_id)
            if not witness:
                witness = _shortest_path(before, anchor, lost_required[0])
        else:
            changed = any(
                len(_shortest_path(after, anchor, t)) != len(_shortest_path(before, anchor, t))
                for t in required
                if t in reach_after
            )
            if changed:
                soft.append(object_id)

    return RelationshipImpact(
        hard_break_object_ids=tuple(hard),
        soft_degrade_object_ids=tuple(soft),
        witness_path=witness,
    )
