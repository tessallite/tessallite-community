"""Unit tests for shared.semantic.calculated_expression (Phase 4A)."""
from __future__ import annotations

import uuid

import pytest

from shared.semantic.calculated_expression import (
    ExpressionValidationError,
    detect_cycles,
    parse_expression,
)

pytestmark = pytest.mark.unit


class TestParseExpression:
    def test_single_reference(self):
        parsed = parse_expression('measure("sales")')
        assert parsed.referenced_names == ("sales",)
        assert len(parsed.references) == 1

    def test_arithmetic_between_two_measures(self):
        parsed = parse_expression('measure("gm") / measure("sales")')
        assert set(parsed.referenced_names) == {"gm", "sales"}

    def test_safe_div_allowed(self):
        parsed = parse_expression('safe_div(measure("gm"), measure("sales"))')
        assert set(parsed.referenced_names) == {"gm", "sales"}

    def test_coalesce_allowed(self):
        parsed = parse_expression('coalesce(measure("a"), 0)')
        assert parsed.referenced_names == ("a",)

    def test_case_when_allowed(self):
        parsed = parse_expression(
            'CASE WHEN measure("sales") > 0 THEN measure("gm") / measure("sales") ELSE 0 END'
        )
        assert set(parsed.referenced_names) == {"sales", "gm"}

    def test_empty_expression_rejected(self):
        with pytest.raises(ExpressionValidationError, match="empty"):
            parse_expression("")
        with pytest.raises(ExpressionValidationError, match="empty"):
            parse_expression("   ")

    def test_empty_measure_name_rejected(self):
        with pytest.raises(ExpressionValidationError, match="measure"):
            parse_expression('measure("")')

    def test_bare_column_rejected(self):
        with pytest.raises(ExpressionValidationError, match="bare identifier"):
            parse_expression("sales / 100")

    def test_disallowed_function_rejected(self):
        with pytest.raises(ExpressionValidationError, match="not in the allowed list"):
            parse_expression('random_fn(measure("sales"))')

    def test_subquery_rejected(self):
        with pytest.raises(ExpressionValidationError):
            parse_expression('measure("sales") + (SELECT 1)')

    def test_aggregate_inline_rejected(self):
        with pytest.raises(ExpressionValidationError):
            parse_expression('SUM(measure("sales"))')

    def test_window_function_rejected(self):
        with pytest.raises(ExpressionValidationError):
            parse_expression(
                'measure("sales") / SUM(measure("sales")) OVER ()'
            )


class TestDetectCycles:
    def test_no_cycles(self):
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        deps = {a: [b], b: [c], c: []}
        assert detect_cycles(deps) == []

    def test_self_cycle(self):
        a = uuid.uuid4()
        cycles = detect_cycles({a: [a]})
        assert len(cycles) == 1
        assert cycles[0][0] == a
        assert cycles[0][-1] == a

    def test_two_node_cycle(self):
        a, b = uuid.uuid4(), uuid.uuid4()
        cycles = detect_cycles({a: [b], b: [a]})
        assert len(cycles) >= 1
        assert {a, b}.issubset(set(cycles[0]))

    def test_three_node_cycle(self):
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        cycles = detect_cycles({a: [b], b: [c], c: [a]})
        assert len(cycles) >= 1

    def test_empty_map(self):
        assert detect_cycles({}) == []

    def test_diamond_dag_is_not_a_cycle(self):
        """F-015-10: a diamond A->B, A->C, B->D, C->D is a DAG, not a cycle.
        The old accumulating path_set treated the second arm's arrival at D
        as a back-edge and then crashed on path.index(D)."""
        a, b, c, d = (uuid.uuid4() for _ in range(4))
        deps = {a: [b, c], b: [d], c: [d], d: []}
        assert detect_cycles(deps) == []

    def test_diamond_with_back_edge_is_a_cycle(self):
        """A diamond plus a back-edge D->A is a genuine cycle and must be
        reported (not crash)."""
        a, b, c, d = (uuid.uuid4() for _ in range(4))
        cycles = detect_cycles({a: [b, c], b: [d], c: [d], d: [a]})
        assert len(cycles) >= 1
        assert cycles[0][0] == cycles[0][-1]

    def test_shared_subdag_no_false_cycle(self):
        """A node reached by two disjoint roots (shared sub-DAG) is black on
        the second visit and must not be flagged."""
        a, b, shared = (uuid.uuid4() for _ in range(3))
        assert detect_cycles({a: [shared], b: [shared], shared: []}) == []
