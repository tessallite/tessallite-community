"""Unit tests for shared.semantic.kpi_dependency (Phase 1 — KPI v2 DAG)."""
from __future__ import annotations

import uuid

import pytest

from shared.semantic.kpi_dependency import (
    CycleError,
    DependencyGraphResult,
    KPINode,
    analyse_dependencies,
    build_graph,
    detect_cycles,
    get_evaluation_order_for_kpi,
    topological_sort,
)

pytestmark = pytest.mark.unit


def _kpi(name: str, expression: str | None = None) -> dict:
    """Helper to create a KPI dict for graph building."""
    return {"id": uuid.uuid4(), "name": name, "expression": expression}


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

class TestBuildGraph:
    def test_simple_measure_no_deps(self):
        graph = build_graph([_kpi("Revenue", 'measure("Sales")')])
        assert "Revenue" in graph
        assert graph["Revenue"].depends_on == []

    def test_kpi_reference_creates_dep(self):
        graph = build_graph([
            _kpi("A", 'measure("Sales")'),
            _kpi("B", 'kpi("A") * 100'),
        ])
        assert graph["B"].depends_on == ["A"]

    def test_multiple_deps(self):
        graph = build_graph([
            _kpi("A", 'measure("x")'),
            _kpi("B", 'measure("y")'),
            _kpi("C", 'kpi("A") * 0.4 + kpi("B") * 0.6'),
        ])
        assert set(graph["C"].depends_on) == {"A", "B"}

    def test_deduplicate_deps(self):
        graph = build_graph([
            _kpi("A", 'measure("x")'),
            _kpi("B", 'kpi("A") + kpi("A")'),
        ])
        assert graph["B"].depends_on == ["A"]

    def test_no_expression(self):
        graph = build_graph([_kpi("Revenue", None)])
        assert "Revenue" in graph
        assert graph["Revenue"].depends_on == []

    def test_invalid_expression_ignored(self):
        """Unparseable expressions should not crash graph building."""
        graph = build_graph([_kpi("Bad", "this is not valid $$")])
        assert "Bad" in graph
        assert graph["Bad"].depends_on == []

    def test_empty_list(self):
        graph = build_graph([])
        assert graph == {}


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------

class TestDetectCycles:
    def test_no_cycles_in_dag(self):
        graph = build_graph([
            _kpi("A", 'measure("x")'),
            _kpi("B", 'kpi("A")'),
            _kpi("C", 'kpi("B")'),
        ])
        cycles = detect_cycles(graph)
        assert cycles == []

    def test_direct_self_reference(self):
        graph = build_graph([
            _kpi("A", 'kpi("A")'),
        ])
        cycles = detect_cycles(graph)
        assert len(cycles) >= 1
        assert "A" in cycles[0].cycle_path

    def test_two_node_cycle(self):
        graph = build_graph([
            _kpi("A", 'kpi("B")'),
            _kpi("B", 'kpi("A")'),
        ])
        cycles = detect_cycles(graph)
        assert len(cycles) >= 1
        # The cycle should contain both A and B
        all_nodes = set()
        for c in cycles:
            all_nodes.update(c.cycle_path)
        assert {"A", "B"}.issubset(all_nodes)

    def test_three_node_cycle(self):
        graph = build_graph([
            _kpi("A", 'kpi("B")'),
            _kpi("B", 'kpi("C")'),
            _kpi("C", 'kpi("A")'),
        ])
        cycles = detect_cycles(graph)
        assert len(cycles) >= 1

    def test_cycle_message(self):
        graph = build_graph([
            _kpi("A", 'kpi("B")'),
            _kpi("B", 'kpi("A")'),
        ])
        cycles = detect_cycles(graph)
        assert len(cycles) >= 1
        msg = cycles[0].message
        assert " -> " in msg

    def test_mixed_dag_and_cycle(self):
        """Graph with both a DAG portion and a cycle."""
        graph = build_graph([
            _kpi("A", 'measure("x")'),
            _kpi("B", 'kpi("A")'),
            _kpi("C", 'kpi("D")'),
            _kpi("D", 'kpi("C")'),
        ])
        cycles = detect_cycles(graph)
        # Only C-D should cycle
        assert len(cycles) >= 1
        cycle_nodes = set()
        for c in cycles:
            cycle_nodes.update(c.cycle_path)
        assert "C" in cycle_nodes
        assert "D" in cycle_nodes


# ---------------------------------------------------------------------------
# Topological sort
# ---------------------------------------------------------------------------

class TestTopologicalSort:
    def test_linear_chain(self):
        graph = build_graph([
            _kpi("A", 'measure("x")'),
            _kpi("B", 'kpi("A")'),
            _kpi("C", 'kpi("B")'),
        ])
        ordered, is_valid = topological_sort(graph)
        assert is_valid
        assert ordered.index("A") < ordered.index("B")
        assert ordered.index("B") < ordered.index("C")

    def test_diamond_dag(self):
        graph = build_graph([
            _kpi("A", 'measure("x")'),
            _kpi("B", 'kpi("A")'),
            _kpi("C", 'kpi("A")'),
            _kpi("D", 'kpi("B") + kpi("C")'),
        ])
        ordered, is_valid = topological_sort(graph)
        assert is_valid
        assert ordered.index("A") < ordered.index("B")
        assert ordered.index("A") < ordered.index("C")
        assert ordered.index("B") < ordered.index("D")
        assert ordered.index("C") < ordered.index("D")

    def test_independent_nodes(self):
        graph = build_graph([
            _kpi("A", 'measure("x")'),
            _kpi("B", 'measure("y")'),
            _kpi("C", 'measure("z")'),
        ])
        ordered, is_valid = topological_sort(graph)
        assert is_valid
        assert set(ordered) == {"A", "B", "C"}

    def test_deterministic_order(self):
        """Independent nodes at the same level should be sorted alphabetically."""
        graph = build_graph([
            _kpi("C", 'measure("z")'),
            _kpi("A", 'measure("x")'),
            _kpi("B", 'measure("y")'),
        ])
        ordered, _ = topological_sort(graph)
        assert ordered == ["A", "B", "C"]

    def test_cycle_returns_invalid(self):
        graph = build_graph([
            _kpi("A", 'kpi("B")'),
            _kpi("B", 'kpi("A")'),
        ])
        ordered, is_valid = topological_sort(graph)
        assert not is_valid


# ---------------------------------------------------------------------------
# analyse_dependencies (main entry)
# ---------------------------------------------------------------------------

class TestAnalyseDependencies:
    def test_valid_dag(self):
        kpis = [
            _kpi("Revenue", 'measure("Sales")'),
            _kpi("GM", 'measure("Gross Margin")'),
            _kpi("Margin", 'safe_div(kpi("GM"), kpi("Revenue"))'),
        ]
        result = analyse_dependencies(kpis)
        assert result.is_valid
        assert result.cycles == []
        assert result.orphaned_refs == []
        assert result.node_orders["Revenue"] < result.node_orders["Margin"]
        assert result.node_orders["GM"] < result.node_orders["Margin"]

    def test_cycle_detected(self):
        kpis = [
            _kpi("A", 'kpi("B")'),
            _kpi("B", 'kpi("A")'),
        ]
        result = analyse_dependencies(kpis)
        assert not result.is_valid
        assert len(result.cycles) >= 1

    def test_orphaned_references(self):
        kpis = [
            _kpi("A", 'kpi("NonExistent")'),
        ]
        result = analyse_dependencies(kpis)
        assert not result.is_valid
        assert "NonExistent" in result.orphaned_refs

    def test_evaluation_order_assigned(self):
        kpis = [
            _kpi("Base", 'measure("x")'),
            _kpi("Derived", 'kpi("Base") * 2'),
        ]
        result = analyse_dependencies(kpis)
        assert result.is_valid
        assert result.node_orders["Base"] < result.node_orders["Derived"]

    def test_empty_kpis(self):
        result = analyse_dependencies([])
        assert result.is_valid
        assert result.evaluation_order == []
        assert result.cycles == []
        assert result.orphaned_refs == []


# ---------------------------------------------------------------------------
# get_evaluation_order_for_kpi (single-KPI subgraph)
# ---------------------------------------------------------------------------

class TestGetEvaluationOrderForKPI:
    def test_single_kpi_no_deps(self):
        graph = build_graph([_kpi("A", 'measure("x")')])
        order = get_evaluation_order_for_kpi("A", graph)
        assert order == ["A"]

    def test_transitive_deps_included(self):
        graph = build_graph([
            _kpi("A", 'measure("x")'),
            _kpi("B", 'kpi("A")'),
            _kpi("C", 'kpi("B")'),
            _kpi("D", 'measure("y")'),
        ])
        order = get_evaluation_order_for_kpi("C", graph)
        assert "A" in order
        assert "B" in order
        assert "C" in order
        # D is not a dependency of C
        assert "D" not in order
        # Order must be correct
        assert order.index("A") < order.index("B")
        assert order.index("B") < order.index("C")

    def test_diamond_deps(self):
        graph = build_graph([
            _kpi("A", 'measure("x")'),
            _kpi("B", 'kpi("A")'),
            _kpi("C", 'kpi("A")'),
            _kpi("D", 'kpi("B") + kpi("C")'),
        ])
        order = get_evaluation_order_for_kpi("D", graph)
        assert set(order) == {"A", "B", "C", "D"}
        assert order.index("A") < order.index("B")
        assert order.index("A") < order.index("C")

    def test_nonexistent_kpi(self):
        graph = build_graph([_kpi("A", 'measure("x")')])
        order = get_evaluation_order_for_kpi("NonExistent", graph)
        assert order == []
