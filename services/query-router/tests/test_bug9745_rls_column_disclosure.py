"""Bug-9745 — a source error must not disclose a row-security column name.

Live-reproduced: a rule with ``dimension_path = 'nonexistent_dimension_xyz'``
compiles cleanly — ``_path_to_column`` validates the SHAPE of a path, never its
existence — so the predicate reaches the database and PostgreSQL rejects it. The
gateway returned ``502`` with body ``{"detail": "column
\\"nonexistent_dimension_xyz\\" does not exist"}``: the raw driver text, carrying
the internal name from a SECURITY rule, to any caller including an embed session.

Stopping the disclosure is no longer this guard's job. ``client_safe_error``
returns a fixed message for every error the service did not author, so no column
name reaches a caller by any route — see
``shared/tests/test_bug9745_physical_identifier_redaction.py``.

What this guard still decides is WHICH fault the caller is told about. A broken
row-security rule earns the 422 misconfiguration contract, which tells an
administrator there is a rule to fix; without it the same failure would read as a
generic 502, as though the source were down. So these tests now pin a
CLASSIFICATION, not a redaction.

Deciding it at the message boundary also cannot over-block: it changes what a
failing query REPORTS, never whether a query succeeds. An earlier attempt to
validate column existence at compile time was rejected for exactly that reason —
a metadata lookup returning no row does not reliably mean the column is absent,
so it turned working queries into security blocks (61 suite failures).
"""
from __future__ import annotations

from types import SimpleNamespace

from src.api.routes import (
    _extract_missing_column_name,
    _is_missing_column_error,
    _rls_column_disclosure_guard,
)

MISSING_SECURITY_COL = Exception('column "nonexistent_dimension_xyz" does not exist')
MISSING_MODEL_COL = Exception('column "some_model_column" does not exist')


def _predicate(cols, active=True):
    return SimpleNamespace(
        security_dimension_columns=tuple(cols),
        active_rule_ids=("r1",) if active else (),
        sql_expression="1=1" if active else "",
    )


def test_bug9745_detects_a_missing_column_error():
    assert _is_missing_column_error(MISSING_SECURITY_COL)
    assert not _is_missing_column_error(
        Exception('relation "sales" does not exist')
    )


def test_bug9745_extracts_the_column_name():
    assert _extract_missing_column_name(MISSING_SECURITY_COL) == (
        "nonexistent_dimension_xyz"
    )
    # PostgreSQL may qualify it; the predicate records the bare column.
    assert _extract_missing_column_name(
        Exception('column "t.region_code" does not exist')
    ) == "region_code"


def test_bug9745_security_column_gets_the_misconfiguration_contract():
    """THE contract: the failing column belongs to the security predicate."""
    assert _rls_column_disclosure_guard(
        MISSING_SECURITY_COL, _predicate(["nonexistent_dimension_xyz"])
    )


def test_bug9745_non_security_column_is_reported_as_a_source_fault():
    """A model column error is a source-schema fault, not a rule to fix.

    It still discloses nothing: it takes the fixed OBJECT_MISSING message like
    every other source error. It simply is not routed to the row-security
    misconfiguration contract, which would send an administrator hunting for a
    rule that is not broken.
    """
    assert not _rls_column_disclosure_guard(
        MISSING_MODEL_COL, _predicate(["region_code"])
    )


def test_bug9745_no_predicate_is_not_a_rule_fault():
    assert not _rls_column_disclosure_guard(MISSING_SECURITY_COL, None)


def test_bug9745_inactive_predicate_is_not_a_rule_fault():
    assert not _rls_column_disclosure_guard(
        MISSING_SECURITY_COL,
        _predicate(["nonexistent_dimension_xyz"], active=False),
    )


def test_bug9745_a_non_column_error_is_never_a_rule_fault():
    """Timeouts and connection failures are classified on their own terms."""
    assert not _rls_column_disclosure_guard(
        Exception("connection timed out"), _predicate(["region_code"])
    )


# --- the disclosure boundary at the route level -----------------------------

def test_bug9745_the_missing_table_response_names_no_relation():
    """The 422 a caller receives for a dropped source table names nothing.

    This message used to interpolate the relation — "Source table
    'reporting.fact_sales' is no longer accessible" — publishing a physical
    table name, and for BigQuery a full ``project:dataset.table``, to any caller
    who could provoke it. It is now the shared OBJECT_MISSING constant, which
    keeps the remedy and drops the name. The name stays in the service log and
    in the persisted query-failure record.
    """
    from shared.error_sanitizer import CLIENT_MESSAGES, SourceFault

    detail = CLIENT_MESSAGES[SourceFault.OBJECT_MISSING]
    for name in ("fact_sales", "reporting", "acme-prod", "warehouse"):
        assert name not in detail
    # The remedy survives: a caller still learns what to do about it.
    assert "source" in detail.lower()
    assert "model" in detail.lower()


def test_bug9745_every_execute_error_reply_is_a_fixed_message():
    """No route builds a client reply out of an unauthored exception.

    ``client_safe_error`` is the only path from a foreign exception to a
    response body on the execute surfaces, and it can only return a constant.
    """
    import inspect

    from shared.error_sanitizer import CLIENT_MESSAGES, client_safe_error
    from src.api import routes as routes_mod

    source = inspect.getsource(routes_mod)
    assert "sanitize_error_for_client" not in source, (
        "the pattern-subtraction sanitizer is gone; nothing may reintroduce it"
    )
    fixed = set(CLIENT_MESSAGES.values())
    for raw in (
        'column "sec_col" does not exist',
        'relation "hr.salaries" does not exist',
        "postgresql://u:p@10.0.0.4:5432/prod is unreachable",
        "some future driver message nobody has seen",
    ):
        assert client_safe_error(Exception(raw)) in fixed
