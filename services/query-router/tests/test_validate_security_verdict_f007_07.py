"""F-007-07 / F-008-19 / Bug-9088: expect=error cannot PASS on connect failure."""
from __future__ import annotations

from validate_security import QueryResult, SecurityQuery, _validate_query


def _q() -> SecurityQuery:
    return SecurityQuery(
        label="SB01",
        description="persona blocks hidden measure",
        sql="SELECT fee_amount FROM x",
        table_variant="_restricted",
        expect="error",
    )


def test_f007_07_transport_error_is_environment_not_ready_not_pass():
    result = QueryResult(
        label="SB01",
        columns=[],
        rows=[],
        row_count=0,
        error="Connection error: port 5433",
        transport_error=True,
    )
    verdict, reason = _validate_query(_q(), result)
    assert verdict == "FAIL"
    assert "ENVIRONMENT_NOT_READY" in reason


def test_f007_07_typed_persona_denial_is_pass():
    result = QueryResult(
        label="SB01",
        columns=[],
        rows=[],
        row_count=0,
        error="OBJECT_NOT_AVAILABLE: one or more requested objects",
        transport_error=False,
    )
    verdict, reason = _validate_query(_q(), result)
    assert verdict == "PASS"
    assert "Correctly blocked" in reason


def test_f007_07_untyped_500_is_fail_not_pass():
    result = QueryResult(
        label="SB01",
        columns=[],
        rows=[],
        row_count=0,
        error="Internal Server Error",
        transport_error=False,
    )
    verdict, reason = _validate_query(_q(), result)
    assert verdict == "FAIL"
    assert "Expected a product security denial" in reason
