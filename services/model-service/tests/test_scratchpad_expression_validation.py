"""Bug-7446: scratchpad _validate_expression must enforce a single
*scalar* expression boundary — not just a single-statement boundary.

Covers:
  - Valid scalar expressions pass.
  - UNION, subqueries, multi-statement inputs are rejected.
  - sqlglot parse errors are rejected.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.api.scratchpad_measures import _validate_expression

pytestmark = pytest.mark.unit


class TestValidateExpressionScalarBoundary:
    def test_simple_arithmetic_accepted(self):
        _validate_expression("1 + 2")

    def test_function_call_accepted(self):
        _validate_expression("ROUND(price * 1.1, 2)")

    def test_column_reference_accepted(self):
        _validate_expression("amount * 0.9")

    def test_case_expression_accepted(self):
        _validate_expression("CASE WHEN x > 0 THEN 1 ELSE 0 END")

    def test_aggregate_function_accepted(self):
        _validate_expression("SUM(amount)")

    def test_union_rejected(self):
        """Bug-7446: ``1 UNION SELECT 2`` wraps to one AST but is not scalar."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_expression("1 UNION SELECT 2")
        assert exc_info.value.status_code == 400
        assert "scalar" in exc_info.value.detail.lower() or "set operation" in exc_info.value.detail.lower()

    def test_subquery_rejected(self):
        """Bug-7446: ``(SELECT MAX(secret) FROM other)`` is a subquery."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_expression("(SELECT MAX(secret) FROM other)")
        assert exc_info.value.status_code == 400

    def test_multi_statement_rejected(self):
        with pytest.raises(HTTPException) as exc_info:
            _validate_expression("1; SELECT 2")
        assert exc_info.value.status_code == 400

    def test_malformed_sql_rejected(self):
        with pytest.raises(HTTPException) as exc_info:
            _validate_expression("SELECT FROM WHERE")
        assert exc_info.value.status_code == 400

    def test_intersect_rejected(self):
        with pytest.raises(HTTPException) as exc_info:
            _validate_expression("1 INTERSECT SELECT 2")
        assert exc_info.value.status_code == 400

    def test_correlated_subquery_rejected(self):
        with pytest.raises(HTTPException) as exc_info:
            _validate_expression("(SELECT COUNT(*) FROM t WHERE t.id = 1)")
        assert exc_info.value.status_code == 400
