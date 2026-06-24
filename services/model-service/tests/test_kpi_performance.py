"""Performance benchmarks for KPI subsystem (Phase 14).

Lightweight unit-level timing tests that validate single-request latency
expectations on local-only components (no network, no database).  These are
guard-rails, not load tests — they catch accidental O(n^2) regressions or
costly hot-path changes.

All assertions use p95 latency so that a single cold outlier does not cause
flaky failures.
"""
from __future__ import annotations

import random
import time
import uuid

import pytest

from shared.semantic.kpi_expression import validate_expression
from shared.semantic.kpi_dependency import (
    analyse_dependencies,
    get_evaluation_order_for_kpi,
)
from src.kpi_compiler import CompilerContext, compile_expression
from src.kpi_threshold import evaluate_threshold
from src.kpi_formatter import format_value

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MEASURES = [f"measure_{i}" for i in range(20)]
_KPI_NAMES = [f"kpi_{i}" for i in range(20)]


def _random_expression(complexity: int = 1) -> str:
    """Generate a random valid KPI expression of varying complexity."""
    m = random.choice(_MEASURES)
    base = f'measure("{m}")'

    if complexity <= 1:
        return base

    if complexity == 2:
        m2 = random.choice(_MEASURES)
        op = random.choice(["+", "-", "*"])
        return f'measure("{m}") {op} measure("{m2}")'

    if complexity == 3:
        m2 = random.choice(_MEASURES)
        return f'safe_div(measure("{m}"), measure("{m2}"))'

    if complexity == 4:
        m2 = random.choice(_MEASURES)
        return (
            f'safe_div(measure("{m}") + measure("{m2}"), '
            f'measure("{m}") - literal(1))'
        )

    # complexity >= 5: nested expression
    m2 = random.choice(_MEASURES)
    m3 = random.choice(_MEASURES)
    return (
        f'safe_div(measure("{m}") + measure("{m2}"), '
        f'measure("{m3}")) * literal(100)'
    )


def _percentile(timings: list[float], pct: int) -> float:
    """Compute the pct-th percentile of a list of timings."""
    s = sorted(timings)
    idx = int(len(s) * pct / 100)
    idx = min(idx, len(s) - 1)
    return s[idx]


# ---------------------------------------------------------------------------
# 4.1  Expression validation performance
# ---------------------------------------------------------------------------

class TestExpressionValidationPerformance:
    """p95 of validate_expression should be < 10ms for 100 random expressions."""

    def test_validation_p95_under_10ms(self):
        measure_set = set(_MEASURES)
        kpi_set = set(_KPI_NAMES)

        expressions = [_random_expression(random.randint(1, 5)) for _ in range(100)]
        timings: list[float] = []

        for expr in expressions:
            start = time.perf_counter()
            validate_expression(
                expr,
                model_measures=measure_set,
                model_kpis=kpi_set,
            )
            elapsed = time.perf_counter() - start
            timings.append(elapsed)

        p95 = _percentile(timings, 95)
        assert p95 < 0.010, (
            f"Expression validation p95 = {p95 * 1000:.2f}ms, expected < 10ms"
        )

    def test_validation_invalid_expressions_fast(self):
        """Invalid expressions should fail fast, not slower than valid ones."""
        invalids = [
            "",
            "measure(",
            'measure("x") +',
            'unknown_func("x")',
            '((measure("x")',
            'measure("x") measure("y")',
        ]
        timings: list[float] = []

        for expr in invalids:
            start = time.perf_counter()
            validate_expression(expr)
            elapsed = time.perf_counter() - start
            timings.append(elapsed)

        p95 = _percentile(timings, 95)
        assert p95 < 0.010, (
            f"Invalid expression validation p95 = {p95 * 1000:.2f}ms, expected < 10ms"
        )


# ---------------------------------------------------------------------------
# 4.2  Expression compilation performance
# ---------------------------------------------------------------------------

class TestExpressionCompilationPerformance:
    """p95 of compile_expression should be < 10ms for 100 random expressions."""

    def test_compilation_p95_under_10ms(self):
        ctx = CompilerContext(
            model_slug="perf_model",
            measure_aggs={m: "sum" for m in _MEASURES},
        )
        expressions = [_random_expression(random.randint(1, 5)) for _ in range(100)]
        timings: list[float] = []

        for expr in expressions:
            start = time.perf_counter()
            compile_expression(expr, ctx)
            elapsed = time.perf_counter() - start
            timings.append(elapsed)

        p95 = _percentile(timings, 95)
        assert p95 < 0.010, (
            f"Expression compilation p95 = {p95 * 1000:.2f}ms, expected < 10ms"
        )


# ---------------------------------------------------------------------------
# 4.3  Dependency graph performance
# ---------------------------------------------------------------------------

class TestDependencyGraphPerformance:
    """Building and querying a 500-node dependency graph should be < 50ms."""

    @staticmethod
    def _build_kpi_list(n: int = 500, max_depth: int = 4) -> list[dict]:
        """Generate n KPIs with realistic dependency chains."""
        kpis: list[dict] = []

        # Create leaf KPIs (no dependencies) — about 60%
        n_leaves = int(n * 0.6)
        for i in range(n_leaves):
            kpis.append({
                "id": str(uuid.uuid4()),
                "name": f"leaf_{i}",
                "expression": f'measure("m_{i % 20}")',
            })

        # Create dependent KPIs that reference earlier KPIs
        for i in range(n_leaves, n):
            depth = random.randint(1, min(max_depth, i))
            # Pick random earlier KPIs to depend on (1-3 deps)
            num_deps = random.randint(1, 3)
            deps = random.sample(kpis[:i], min(num_deps, len(kpis[:i])))
            parts = [f'kpi("{d["name"]}")' for d in deps]
            expr = " + ".join(parts) if len(parts) > 1 else parts[0]
            kpis.append({
                "id": str(uuid.uuid4()),
                "name": f"composite_{i}",
                "expression": expr,
            })

        return kpis

    def test_full_graph_analysis_under_50ms(self):
        kpis = self._build_kpi_list(500)

        start = time.perf_counter()
        result = analyse_dependencies(kpis)
        elapsed = time.perf_counter() - start

        assert result.is_valid, f"Graph has issues: cycles={result.cycles}"
        assert elapsed < 0.050, (
            f"Full graph analysis = {elapsed * 1000:.2f}ms, expected < 50ms"
        )

    def test_single_kpi_order_under_5ms(self):
        kpis = self._build_kpi_list(500)
        result = analyse_dependencies(kpis)
        assert result.is_valid

        # Build the graph dict needed by get_evaluation_order_for_kpi
        from shared.semantic.kpi_dependency import KPINode, _collect_kpi_refs
        from shared.semantic.kpi_expression import parse_kpi_expression

        graph: dict[str, KPINode] = {}
        for k in kpis:
            node = KPINode(
                kpi_id=uuid.UUID(k["id"]),
                name=k["name"],
                expression=k["expression"],
            )
            try:
                ast = parse_kpi_expression(k["expression"])
                node.depends_on = _collect_kpi_refs(ast)
            except Exception:
                node.depends_on = []
            graph[k["name"]] = node

        # Pick a composite KPI near the end
        composite_names = [k["name"] for k in kpis if k["name"].startswith("composite_")]
        if not composite_names:
            pytest.skip("No composite KPIs generated")

        target = composite_names[-1]
        timings: list[float] = []

        for _ in range(20):
            start = time.perf_counter()
            get_evaluation_order_for_kpi(target, graph)
            elapsed = time.perf_counter() - start
            timings.append(elapsed)

        p95 = _percentile(timings, 95)
        assert p95 < 0.005, (
            f"Single KPI evaluation order p95 = {p95 * 1000:.2f}ms, expected < 5ms"
        )


# ---------------------------------------------------------------------------
# 4.4  Threshold evaluation performance
# ---------------------------------------------------------------------------

class TestThresholdEvaluationPerformance:
    """p95 of evaluate_threshold should be < 1ms for 1000 evaluations."""

    def test_threshold_p95_under_1ms(self):
        bands = [
            {"label": "Off Target", "color": "#D32F2F", "min": None, "max": 0.80},
            {"label": "Near Target", "color": "#F57C00", "min": 0.80, "max": 1.00},
            {"label": "On Track", "color": "#388E3C", "min": 1.00, "max": None},
        ]

        timings: list[float] = []
        for _ in range(1000):
            value = random.uniform(0, 200)
            target = random.uniform(50, 150)

            start = time.perf_counter()
            evaluate_threshold(
                value=value,
                target=target,
                direction="higher_is_better",
                evaluation_type="percentage_of_target",
                bands=bands,
            )
            elapsed = time.perf_counter() - start
            timings.append(elapsed)

        p95 = _percentile(timings, 95)
        assert p95 < 0.001, (
            f"Threshold evaluation p95 = {p95 * 1000:.3f}ms, expected < 1ms"
        )

    def test_threshold_default_bands_p95_under_1ms(self):
        """Using default bands (no explicit band list) should be equally fast."""
        timings: list[float] = []
        for _ in range(1000):
            value = random.uniform(0, 200)
            target = random.uniform(50, 150)

            start = time.perf_counter()
            evaluate_threshold(value=value, target=target)
            elapsed = time.perf_counter() - start
            timings.append(elapsed)

        p95 = _percentile(timings, 95)
        assert p95 < 0.001, (
            f"Threshold (default bands) p95 = {p95 * 1000:.3f}ms, expected < 1ms"
        )


# ---------------------------------------------------------------------------
# 4.5  Formatter performance
# ---------------------------------------------------------------------------

class TestFormatterPerformance:
    """p95 of format_value should be < 1ms for 1000 format operations."""

    _TOKENS = [
        "currency",
        "currency_k",
        "percent",
        "percent_decimal",
        "decimal_0dp",
        "decimal_1dp",
        "decimal_2dp",
        "integer",
    ]

    def test_formatter_p95_under_1ms(self):
        timings: list[float] = []
        for _ in range(1000):
            value = random.uniform(-1e9, 1e9)
            token = random.choice(self._TOKENS)

            start = time.perf_counter()
            format_value(value=value, format_token=token)
            elapsed = time.perf_counter() - start
            timings.append(elapsed)

        p95 = _percentile(timings, 95)
        assert p95 < 0.001, (
            f"Formatter p95 = {p95 * 1000:.3f}ms, expected < 1ms"
        )

    def test_formatter_null_handling_fast(self):
        """Null values should be formatted instantly."""
        timings: list[float] = []
        for _ in range(100):
            start = time.perf_counter()
            format_value(value=None, null_display_value="--")
            elapsed = time.perf_counter() - start
            timings.append(elapsed)

        p95 = _percentile(timings, 95)
        assert p95 < 0.001, (
            f"Null formatter p95 = {p95 * 1000:.3f}ms, expected < 1ms"
        )

    def test_formatter_custom_format(self):
        """Custom format strings should not be significantly slower."""
        timings: list[float] = []
        for _ in range(100):
            value = random.uniform(0, 1e6)
            start = time.perf_counter()
            format_value(
                value=value,
                format_token="custom",
                format_custom="{:,.4f}",
            )
            elapsed = time.perf_counter() - start
            timings.append(elapsed)

        p95 = _percentile(timings, 95)
        assert p95 < 0.001, (
            f"Custom formatter p95 = {p95 * 1000:.3f}ms, expected < 1ms"
        )
