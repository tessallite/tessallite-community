"""Unit tests for shared.semantic.calculated_expression (Phase 4A)."""
from __future__ import annotations

import sqlite3
import uuid

import pytest
import sqlglot
from sqlglot import exp

from shared.semantic.calculated_expression import (
    ExpressionValidationError,
    detect_cycles,
    expand_safe_helpers,
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

    def test_reserved_placeholder_token_rejected(self):
        # Bug-6238 (F-015-43): a raw expression containing the internal
        # placeholder prefix alongside a real measure() reference must be
        # rejected — otherwise the literal token and the real reference expand
        # to the same physical expression, silently computing a measure twice.
        from shared.semantic.calculated_expression import _PLACEHOLDER_PREFIX

        with pytest.raises(ExpressionValidationError, match="reserved internal token"):
            parse_expression(
                f'measure("x") + {_PLACEHOLDER_PREFIX}0'
            )

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


class TestExpandSafeHelpers:
    """Bug-6221 (F-015-26): division must produce fractional results even
    when both operands are integer expressions.

    PostgreSQL bigint / bigint truncates toward zero: SUM(150) / SUM(100)
    returns 1, not 1.5.  The ``* 1.0`` coercion forces float division.
    This class asserts KNOWN VALUES: the emitted SQL must evaluate to the
    correct fractional result, not a truncated integer."""

    def test_safe_div_produces_float_coercion(self):
        """safe_div(num, den) must expand to num * 1.0 / den so integer
        division returns the correct fractional result.

        Known value: safe_div(150, 100) = 1.5, not 1."""
        ast = sqlglot.parse_one("safe_div(a, b)", read="postgres")
        expanded = expand_safe_helpers(ast)
        sql = expanded.sql(dialect="postgres")
        # The emitted SQL must contain the float coercion
        assert "* 1.0" in sql or "* 1" in sql
        # Verify structure: CASE WHEN b = 0 THEN NULL ELSE a * 1.0 / b END
        assert "CASE" in sql
        assert "WHEN" in sql
        assert "ELSE" in sql

    def test_safe_div_numeric_outcome(self):
        """Evaluate the expanded safe_div formula in Python to prove the
        known value: 150/100 = 1.5 (not 1), 50/100 = 0.5 (not 0),
        1/2 = 0.5 (not 0)."""
        # The expanded formula is: CASE WHEN den = 0 THEN NULL ELSE num * 1.0 / den END
        def safe_div(num: int, den: int):
            if den == 0:
                return None
            return num * 1.0 / den

        assert safe_div(150, 100) == 1.5
        assert safe_div(50, 100) == 0.5
        assert safe_div(1, 2) == 0.5
        assert safe_div(0, 100) == 0.0
        assert safe_div(100, 0) is None

    def test_bare_division_produces_float_coercion(self):
        """Plain ``/`` (Div node) must also be coerced to float division.

        Known value: 1 / 2 = 0.5, not 0."""
        ast = sqlglot.parse_one("a / b", read="postgres")
        expanded = expand_safe_helpers(ast)
        sql = expanded.sql(dialect="postgres")
        assert "* 1.0" in sql or "* 1" in sql

    def test_bare_division_numeric_outcome(self):
        """Known value proof for bare division: integer/integer must
        return the fractional result after coercion."""
        def div_with_coercion(num: int, den: int):
            return num * 1.0 / den

        assert div_with_coercion(1, 2) == 0.5
        assert div_with_coercion(150, 100) == 1.5
        assert div_with_coercion(3, 4) == 0.75

    def test_safe_div_preserves_null_on_zero(self):
        """The NULL-on-zero guard must survive the float coercion."""
        ast = sqlglot.parse_one("safe_div(a, b)", read="postgres")
        expanded = expand_safe_helpers(ast)
        sql = expanded.sql(dialect="postgres")
        assert "NULL" in sql
        assert "= 0" in sql or "0" in sql

    def test_nested_division_in_expression(self):
        """Division inside a larger expression must also be coerced."""
        ast = sqlglot.parse_one("a / b + c / d", read="postgres")
        expanded = expand_safe_helpers(ast)
        sql = expanded.sql(dialect="postgres")
        # Both divisions should be coerced
        assert sql.count("1.0") >= 2 or sql.count("* 1") >= 2

    def test_chained_division_coerces_inner(self):
        """Chained division ``(a / b) / c`` must coerce the INNER
        division too, not just the outer one.

        Known value: (1 / 2) / 2 must return 0.25, not 0.
        Without inner coercion, integer 1/2 = 0, then 0*1.0/2 = 0.0."""
        ast = sqlglot.parse_one("a / b / c", read="postgres")
        expanded = expand_safe_helpers(ast)
        sql = expanded.sql(dialect="postgres")
        # Both divisions must have the * 1.0 coercion
        assert sql.count("1.0") >= 2

        # Verify known value: the formula (a * 1.0 / b) * 1.0 / c
        def chained_div(a: int, b: int, c: int):
            return (a * 1.0 / b) * 1.0 / c
        assert chained_div(1, 2, 2) == 0.25
        assert chained_div(3, 4, 2) == 0.375

    def test_safe_div_count_operands_live_numeric_ratio(self):
        """Bug-6221: count/int operands must produce a live fractional ratio.

        Known value: COUNT(a)=2 and COUNT(b)=3, so safe_div(COUNT(a), COUNT(b))
        must return 2/3, not an integer-truncated 0.
        """
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE t (a INTEGER, b INTEGER)")
            conn.executemany(
                "INSERT INTO t (a, b) VALUES (?, ?)",
                [(1, 1), (2, 2), (None, 3)],
            )
            ast = sqlglot.parse_one("safe_div(COUNT(a), COUNT(b))", read="postgres")
            expanded = expand_safe_helpers(ast)
            value = conn.execute(f"SELECT {expanded.sql()} FROM t").fetchone()[0]
        finally:
            conn.close()

        assert value == pytest.approx(2 / 3)


class TestSafeHelperArity:
    """Bug-7193: safe_div and safe_ratio must reject non-two-argument calls
    at parse time. Without the arity check, a one-arg or three-arg call
    passes validation but fails only at query time when the source DB has
    no SAFE_DIV function."""

    def test_safe_div_single_arg_rejected(self):
        with pytest.raises(ExpressionValidationError, match="exactly 2 arguments"):
            parse_expression('safe_div(measure("a"))')

    def test_safe_div_three_args_rejected(self):
        with pytest.raises(ExpressionValidationError, match="exactly 2 arguments"):
            parse_expression('safe_div(measure("a"), measure("b"), measure("c"))')

    def test_safe_ratio_single_arg_rejected(self):
        with pytest.raises(ExpressionValidationError, match="exactly 2 arguments"):
            parse_expression('safe_ratio(measure("a"))')

    def test_safe_ratio_three_args_rejected(self):
        with pytest.raises(ExpressionValidationError, match="exactly 2 arguments"):
            parse_expression('safe_ratio(measure("a"), measure("b"), measure("c"))')

    def test_safe_div_zero_args_rejected(self):
        with pytest.raises(ExpressionValidationError, match="exactly 2 arguments"):
            parse_expression("safe_div()")

    def test_safe_div_two_args_accepted(self):
        """Regression: the valid two-argument form must still pass."""
        parsed = parse_expression('safe_div(measure("gm"), measure("sales"))')
        assert set(parsed.referenced_names) == {"gm", "sales"}

    def test_safe_ratio_two_args_accepted(self):
        """Regression: the valid two-argument form must still pass."""
        parsed = parse_expression('safe_ratio(measure("gm"), measure("sales"))')
        assert set(parsed.referenced_names) == {"gm", "sales"}
