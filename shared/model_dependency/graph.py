"""Graph builder, adjacency indexes, and SCC analysis (spec §7.1).

Consumes a ``ModelDependencySnapshot`` and produces an immutable
``DependencyGraph`` with forward (dependency -> dependents) and reverse adjacency,
Tarjan strongly-connected components, and diagnostics. All name/JSON references
are already resolved to IDs by the loader, so this module only materializes the
declared edges; unresolved references arrive as explicit rows and become
``unresolved_reference`` nodes/edges (spec §5.5).

Complexity is O(V + E). No path enumeration happens here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .snapshot import ModelDependencySnapshot
from .types import (
    DependencyEdge,
    DependencyNode,
    NodeKey,
    ObjectType,
)
from .edge_builders import build_all_edges


def _route(snapshot: ModelDependencySnapshot, object_type: ObjectType, object_id: str) -> Optional[str]:
    template = snapshot.route_templates.get(object_type.value)
    if not template:
        return None
    return template.format(
        project_id=snapshot.project_id,
        model_id=snapshot.model_id,
        object_id=object_id,
    )


@dataclass(frozen=True)
class DependencyGraph:
    """Immutable frozen graph (spec §7.1 step 9)."""

    tenant_id: str
    project_id: str
    model_id: str
    dependency_revision: int
    nodes: dict[NodeKey, DependencyNode]
    edges: tuple[DependencyEdge, ...]
    # forward[dependency] -> ordered list of edges out of it
    forward: dict[NodeKey, tuple[DependencyEdge, ...]]
    # reverse[dependent] -> ordered list of edges into it
    reverse: dict[NodeKey, tuple[DependencyEdge, ...]]
    # SCC id per node; nodes in a non-trivial SCC share an id.
    scc_of: dict[NodeKey, int]
    # scc_id -> member node keys (only for non-trivial cycles, |members| > 1)
    cycles: dict[int, tuple[NodeKey, ...]]
    diagnostics: tuple[dict[str, str], ...]

    def node(self, key: NodeKey) -> Optional[DependencyNode]:
        return self.nodes.get(key)

    def dependents_of(self, key: NodeKey) -> tuple[DependencyEdge, ...]:
        return self.forward.get(key, ())

    def counts(self) -> dict[str, int]:
        return {
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "cycles": len(self.cycles),
            "diagnostics": len(self.diagnostics),
        }


class GraphBuilder:
    """Builds a ``DependencyGraph`` from a snapshot (spec §7.1)."""

    def __init__(self, snapshot: ModelDependencySnapshot) -> None:
        self._s = snapshot
        self._nodes: dict[NodeKey, DependencyNode] = {}
        self._edges: list[DependencyEdge] = []
        self._diagnostics: list[dict[str, str]] = []

    # -- node registration --------------------------------------------------

    def _key(self, object_type: ObjectType, object_id: str, model_id: Optional[str] = None) -> NodeKey:
        return NodeKey(
            tenant_id=self._s.tenant_id,
            project_id=self._s.project_id,
            model_id=model_id or self._s.model_id,
            object_type=object_type,
            object_id=object_id,
        )

    def add_node(
        self,
        object_type: ObjectType,
        object_id: str,
        name: str,
        display_name: str,
        *,
        model_id: Optional[str] = None,
        container_ids: Optional[dict[str, str]] = None,
        valid: bool = True,
        unresolved_reason: Optional[str] = None,
    ) -> NodeKey:
        key = self._key(object_type, object_id, model_id)
        if key not in self._nodes:
            self._nodes[key] = DependencyNode(
                key=key,
                name=name,
                display_name=display_name or name,
                container_ids=dict(container_ids or {}),
                valid=valid,
                route=_route(self._s, object_type, object_id),
                unresolved_reason=unresolved_reason,
            )
        return key

    def add_edge(self, edge: DependencyEdge) -> None:
        self._edges.append(edge)

    def add_diagnostic(self, diag: dict[str, str]) -> None:
        self._diagnostics.append(diag)

    # -- build --------------------------------------------------------------

    def build(self) -> DependencyGraph:
        build_all_edges(self)
        return self._freeze()

    def _freeze(self) -> DependencyGraph:
        # Drop edges whose endpoints are missing (defensive: loader guarantees
        # resolution, but a stale ID must not create a dangling adjacency entry).
        valid_edges = [
            e for e in self._edges
            if e.dependency in self._nodes and e.dependent in self._nodes
        ]
        # Deterministic edge order: by (dependency, dependent, kind, field).
        valid_edges.sort(
            key=lambda e: (
                e.dependency.sort_key,
                e.dependent.sort_key,
                e.kind.value,
                e.source_field,
            )
        )
        forward: dict[NodeKey, list[DependencyEdge]] = {}
        reverse: dict[NodeKey, list[DependencyEdge]] = {}
        for e in valid_edges:
            forward.setdefault(e.dependency, []).append(e)
            reverse.setdefault(e.dependent, []).append(e)

        scc_of, cycles = _tarjan_scc(self._nodes.keys(), forward)

        return DependencyGraph(
            tenant_id=self._s.tenant_id,
            project_id=self._s.project_id,
            model_id=self._s.model_id,
            dependency_revision=self._s.dependency_revision,
            nodes=dict(self._nodes),
            edges=tuple(valid_edges),
            forward={k: tuple(v) for k, v in forward.items()},
            reverse={k: tuple(v) for k, v in reverse.items()},
            scc_of=scc_of,
            cycles=cycles,
            diagnostics=tuple(self._diagnostics),
        )


def build_graph(snapshot: ModelDependencySnapshot) -> DependencyGraph:
    """Convenience entry point (spec §7.1)."""
    return GraphBuilder(snapshot).build()


# -- Tarjan strongly-connected components (iterative, O(V+E)) ---------------


def _tarjan_scc(
    node_keys: Iterable[NodeKey],
    forward: dict[NodeKey, list[DependencyEdge]],
) -> tuple[dict[NodeKey, int], dict[int, tuple[NodeKey, ...]]]:
    """Iterative Tarjan (spec §7.1 step 8). Returns scc id per node and the map
    of non-trivial cycle components (more than one member, OR a self-loop)."""

    index_counter = 0
    stack: list[NodeKey] = []
    on_stack: set[NodeKey] = set()
    indices: dict[NodeKey, int] = {}
    lowlink: dict[NodeKey, int] = {}
    scc_of: dict[NodeKey, int] = {}
    cycles: dict[int, tuple[NodeKey, ...]] = {}
    next_scc = 0

    # Deterministic node order so SCC ids are stable across runs.
    ordered_nodes = sorted(node_keys, key=lambda k: k.sort_key)

    def successors(node: NodeKey) -> list[NodeKey]:
        return sorted(
            (e.dependent for e in forward.get(node, [])),
            key=lambda k: k.sort_key,
        )

    for root in ordered_nodes:
        if root in indices:
            continue
        # (node, iterator position) work stack for iterative DFS.
        work: list[tuple[NodeKey, int]] = [(root, 0)]
        while work:
            node, pi = work[-1]
            if pi == 0:
                indices[node] = index_counter
                lowlink[node] = index_counter
                index_counter += 1
                stack.append(node)
                on_stack.add(node)
            succ = successors(node)
            if pi < len(succ):
                work[-1] = (node, pi + 1)
                w = succ[pi]
                if w not in indices:
                    work.append((w, 0))
                elif w in on_stack:
                    lowlink[node] = min(lowlink[node], indices[w])
            else:
                if lowlink[node] == indices[node]:
                    members: list[NodeKey] = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        members.append(w)
                        if w == node:
                            break
                    scc_id = next_scc
                    next_scc += 1
                    for m in members:
                        scc_of[m] = scc_id
                    # Non-trivial cycle: >1 member, or a self-loop.
                    is_self_loop = any(
                        e.dependent == node for e in forward.get(node, [])
                    )
                    if len(members) > 1 or is_self_loop:
                        cycles[scc_id] = tuple(
                            sorted(members, key=lambda k: k.sort_key)
                        )
                work.pop()
                if work:
                    parent = work[-1][0]
                    lowlink[parent] = min(lowlink[parent], lowlink[node])

    return scc_of, cycles
