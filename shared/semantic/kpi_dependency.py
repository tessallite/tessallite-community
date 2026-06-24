"""KPI dependency graph — DAG construction, topological sort, cycle detection.

Builds a directed acyclic graph (DAG) from ``kpi()`` references extracted by
the expression parser. Computes evaluation order via Kahn's algorithm so that
composite KPIs are evaluated after their dependencies.

Cycle detection returns the full cycle path for error reporting.

See ``docs/architecture/architecture_kpi-requirements-specification.md``
Section 5.6.2 for semantic validation rules.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional
from uuid import UUID

from shared.semantic.kpi_expression import (
    ASTNode,
    BinaryOp,
    FunctionCall,
    StringLiteral,
    UnaryMinus,
    parse_kpi_expression,
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class KPINode:
    """A node in the KPI dependency graph."""
    kpi_id: UUID
    name: str
    expression: Optional[str] = None
    depends_on: list[str] = field(default_factory=list)  # KPI names
    evaluation_order: Optional[int] = None


@dataclass
class CycleError:
    """Describes a dependency cycle found in the graph."""
    cycle_path: list[str]  # KPI names forming the cycle

    @property
    def message(self) -> str:
        return " -> ".join(self.cycle_path)


@dataclass
class DependencyGraphResult:
    """Result of building and analysing the KPI dependency graph."""
    evaluation_order: list[str]  # KPI names in evaluation order
    node_orders: dict[str, int]  # KPI name -> evaluation_order value
    cycles: list[CycleError]
    is_valid: bool
    orphaned_refs: list[str]  # kpi() refs that don't match any node


# ---------------------------------------------------------------------------
# AST reference extraction
# ---------------------------------------------------------------------------

def _collect_kpi_refs(node: ASTNode) -> list[str]:
    """Walk an AST and collect all ``kpi("Name")`` references."""
    refs: list[str] = []

    def _walk(n: ASTNode) -> None:
        if isinstance(n, FunctionCall):
            if n.name == "kpi" and n.args and isinstance(n.args[0], StringLiteral):
                refs.append(n.args[0].value)
            for arg in n.args:
                _walk(arg)
        elif isinstance(n, BinaryOp):
            _walk(n.left)
            _walk(n.right)
        elif isinstance(n, UnaryMinus):
            _walk(n.operand)

    _walk(node)
    return refs


def extract_kpi_references(expression: str) -> set[str]:
    """Parse an expression string and return the set of KPI names it references.

    This is a convenience wrapper for cache invalidation and dependency
    checks that need to know which KPIs an expression depends on without
    building a full dependency graph.

    Parameters
    ----------
    expression : str
        A KPI expression string (e.g. ``kpi("Revenue Growth") + literal(1)``).

    Returns
    -------
    set[str]
        KPI names referenced via ``kpi("...")`` calls in the expression.

    Raises
    ------
    Exception
        If the expression cannot be parsed.
    """
    ast = parse_kpi_expression(expression)
    return set(_collect_kpi_refs(ast))


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def build_graph(
    kpis: list[dict],
) -> dict[str, KPINode]:
    """Build a dependency graph from a list of KPI dicts.

    Each dict must have at minimum ``id`` (UUID), ``name`` (str), and
    ``expression`` (str | None). The ``expression`` is parsed to extract
    ``kpi()`` references.

    Parameters
    ----------
    kpis : list[dict]
        List of KPI dicts with ``id``, ``name``, and ``expression`` keys.

    Returns
    -------
    dict[str, KPINode]
        Graph nodes keyed by KPI name.
    """
    nodes: dict[str, KPINode] = {}

    for kpi in kpis:
        name = kpi["name"]
        expr = kpi.get("expression")
        deps: list[str] = []

        if expr:
            try:
                ast = parse_kpi_expression(expr)
                deps = _collect_kpi_refs(ast)
            except Exception:
                # Expression fails to parse — skip dependency extraction.
                # The expression validator will report the error separately.
                pass

        nodes[name] = KPINode(
            kpi_id=kpi["id"],
            name=name,
            expression=expr,
            depends_on=list(dict.fromkeys(deps)),  # dedupe, preserve order
        )

    return nodes


# ---------------------------------------------------------------------------
# Cycle detection (DFS)
# ---------------------------------------------------------------------------

def detect_cycles(graph: dict[str, KPINode]) -> list[CycleError]:
    """Detect all cycles in the dependency graph using iterative DFS.

    Returns a list of ``CycleError`` instances. An empty list means the
    graph is a DAG.
    """
    cycles: list[CycleError] = []
    visited: set[str] = set()
    in_stack: set[str] = set()

    for start_name in graph:
        if start_name in visited:
            continue

        # Iterative DFS with explicit stack.
        # Stack entries: (node_name, path_from_root, child_iterator_index)
        stack: list[tuple[str, list[str], int]] = [(start_name, [start_name], 0)]
        in_stack.add(start_name)

        while stack:
            current, path, child_idx = stack[-1]
            node = graph.get(current)
            children = node.depends_on if node else []

            if child_idx < len(children):
                # Advance to next child on next visit to this frame
                stack[-1] = (current, path, child_idx + 1)
                child = children[child_idx]

                if child in in_stack:
                    # Back-edge: found cycle
                    cycle_start = path.index(child)
                    cycle_path = path[cycle_start:] + [child]
                    cycles.append(CycleError(cycle_path=cycle_path))
                elif child not in visited and child in graph:
                    stack.append((child, path + [child], 0))
                    in_stack.add(child)
            else:
                # All children explored
                stack.pop()
                in_stack.discard(current)
                visited.add(current)

    return cycles


# ---------------------------------------------------------------------------
# Topological sort (Kahn's algorithm)
# ---------------------------------------------------------------------------

def topological_sort(graph: dict[str, KPINode]) -> tuple[list[str], bool]:
    """Compute evaluation order using Kahn's algorithm.

    Returns a tuple of ``(ordered_names, is_valid)``. If the graph has
    cycles, ``is_valid`` is False and ``ordered_names`` contains only the
    nodes that could be ordered (i.e. the acyclic portion).
    """
    # Build adjacency list and in-degree map
    in_degree: dict[str, int] = {name: 0 for name in graph}
    dependents: dict[str, list[str]] = defaultdict(list)

    for name, node in graph.items():
        for dep in node.depends_on:
            if dep in graph:
                in_degree[name] += 1
                dependents[dep].append(name)

    # Seed queue with nodes that have no dependencies
    queue: deque[str] = deque()
    for name, deg in in_degree.items():
        if deg == 0:
            queue.append(name)

    # Stable sort: process queue in alphabetical order within each level
    # to produce deterministic output.
    ordered: list[str] = []
    while queue:
        # Sort the current frontier for determinism
        batch = sorted(queue)
        queue.clear()
        for name in batch:
            ordered.append(name)
            for dependent in dependents[name]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

    is_valid = len(ordered) == len(graph)
    return ordered, is_valid


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def analyse_dependencies(
    kpis: list[dict],
) -> DependencyGraphResult:
    """Build graph, detect cycles, compute evaluation order.

    This is the main entry point for KPI dependency analysis.

    Parameters
    ----------
    kpis : list[dict]
        List of KPI dicts with ``id``, ``name``, and ``expression`` keys.

    Returns
    -------
    DependencyGraphResult
        Full dependency analysis result.
    """
    graph = build_graph(kpis)

    # Find orphaned references (kpi() refs pointing to non-existent KPIs)
    all_names = set(graph.keys())
    orphaned: list[str] = []
    for node in graph.values():
        for dep in node.depends_on:
            if dep not in all_names and dep not in orphaned:
                orphaned.append(dep)

    # Detect cycles
    cycles = detect_cycles(graph)

    # Compute evaluation order (Kahn's algorithm)
    ordered, order_valid = topological_sort(graph)

    # Assign evaluation_order to nodes
    node_orders: dict[str, int] = {}
    for idx, name in enumerate(ordered):
        node_orders[name] = idx
        if name in graph:
            graph[name].evaluation_order = idx

    return DependencyGraphResult(
        evaluation_order=ordered,
        node_orders=node_orders,
        cycles=cycles,
        is_valid=len(cycles) == 0 and order_valid and not orphaned,
        orphaned_refs=orphaned,
    )


def get_evaluation_order_for_kpi(
    kpi_name: str,
    graph: dict[str, KPINode],
) -> list[str]:
    """Return the evaluation order for a single KPI and its transitive deps.

    Useful for ad-hoc evaluation of a single KPI: returns the minimal set
    of KPIs that need to be evaluated first, in correct order.
    """
    # BFS to collect transitive dependencies
    needed: set[str] = set()
    queue: deque[str] = deque([kpi_name])

    while queue:
        current = queue.popleft()
        if current in needed:
            continue
        needed.add(current)
        node = graph.get(current)
        if node:
            for dep in node.depends_on:
                if dep not in needed and dep in graph:
                    queue.append(dep)

    # Topologically sort just the needed subset
    sub_graph = {name: graph[name] for name in needed if name in graph}
    ordered, _ = topological_sort(sub_graph)
    return ordered
