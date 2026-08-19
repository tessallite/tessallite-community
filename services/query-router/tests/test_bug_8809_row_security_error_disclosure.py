"""Bug-8809 [SECURITY] — the row-security ERROR path must disclose nothing.

Every disclosure control this service owns (``api/_sql_disclosure.py``) acts on
a SUCCESS response, i.e. on an object that passes through a ``response_model``.
An ``HTTPException`` passes through none of them, so the 4xx bodies were a
second, uncovered door out of the same room:

* ``router._inject_security_where`` folded the ``RowSecurityDialectError``
  message into a 403 detail, and ``shared/security/predicate_compiler.py``
  builds that message out of the model's SECURITY-DIMENSION COLUMN NAMES;
* two of its other reject branches interpolated the physical TABLE name;
* the ``RowSecurityCompileError`` -> 422 arm on all four row-returning routes
  folded in the compiler's message, which names dimension paths, rule ids and
  mapping-table ids;
* ``drill_routes`` returned ``str(exc)`` from the execute pipeline verbatim.

All four are reachable by an EMBED session — an anonymous, admin-issued,
link-shareable credential that is specifically designed to carry an RLS subject
(``_sql_disclosure`` module docstring, and ``Principal.from_current_user``),
which is what makes it reach the row-security branches on every query.

These tests assert the ABSENCE of a category, on the payload the caller
actually receives (FastAPI's own exception serialiser), not on one field.
"""
from __future__ import annotations

import ast
import inspect
import json
import logging
from pathlib import Path

import pytest
from fastapi import HTTPException

from src.routing.router import (
    ROW_SECURITY_REJECT_REASON_CODES,
    _inject_security_where,
    _reject_row_security_shape,
)
from src.security import CompiledPredicate

pytestmark = pytest.mark.integration

# Distinctive names that only ever appear inside the internal diagnostic, so a
# scan for them cannot false-positive on ordinary English in the message.
_SECRET_COL = "secret_col"
_SECRET_TABLE = "confidential_fact_ledger"


def _served_body(exc: HTTPException) -> str:
    """The JSON the CLIENT receives, produced by FastAPI's own handler.

    Asserting on ``exc.detail`` alone would only prove the dict is clean; this
    proves the serialised response body is, which is the property that matters
    and the step that made every response-model sanitiser irrelevant here.
    """
    import asyncio

    from fastapi.exception_handlers import http_exception_handler

    class _Req:
        scope = {"type": "http"}

    response = asyncio.run(http_exception_handler(_Req(), exc))
    return response.body.decode()


# ---------------------------------------------------------------------------
# The headline leak (plan "Test to land for finding 2", promoted verbatim)
# ---------------------------------------------------------------------------


def test_rls_reject_message_carries_no_security_column_names():
    """The 403 from a failed dialect move must not name the security columns.

    predicate_compiler.py:179-185 and 193-196 interpolate identifiers into
    RowSecurityDialectError, and router.py folded that text into a
    caller-visible detail -- reaching an embed session, which the response-shape
    embed withhold cannot see because an HTTPException passes through none of
    the sanitizers.
    """
    bad = CompiledPredicate(
        sql_expression="(NOT \"region_code\" = 'EMEA')",
        active_rule_ids=("r1",),
        security_dimension_columns=("customer_region_code", _SECRET_COL),
        compile_connector="bigquery",   # mislabelled on purpose
    )
    with pytest.raises(HTTPException) as exc:
        _inject_security_where(
            "SELECT region_code FROM agg_sales", bad, dialect="bigquery",
        )
    msg = str(exc.value.detail)
    assert "customer_region_code" not in msg, msg
    assert _SECRET_COL not in msg, msg


def test_rls_reject_served_body_carries_no_security_column_names():
    """Same property, asserted on the bytes the client actually receives."""
    bad = CompiledPredicate(
        sql_expression="(NOT \"region_code\" = 'EMEA')",
        active_rule_ids=("r1",),
        security_dimension_columns=("customer_region_code", _SECRET_COL),
        compile_connector="bigquery",
    )
    with pytest.raises(HTTPException) as exc:
        _inject_security_where(
            "SELECT region_code FROM agg_sales", bad, dialect="bigquery",
        )
    body = _served_body(exc.value)
    assert "customer_region_code" not in body, body
    assert _SECRET_COL not in body, body
    assert json.loads(body)["detail"]["error_code"] == "row_security_unsupported_shape"


def test_rls_reject_carries_no_physical_table_name():
    """The two scan-resolution branches named the physical table.

    Same class as the dialect leak and reachable by the same principal: the
    physical table name is exactly what the semantic layer exists to keep behind
    logical names.

    NOTE this guard got MORE load-bearing on 2026-08-11, not less. The
    response-level embed withhold that used to scrub physical identifiers out of
    success payloads was removed by user decision (option C — see
    ``_sql_disclosure``'s module docstring), so constructing these ERROR bodies
    safe for the lowest-trust caller is now the only thing standing between a
    malformed row-security rule and a caller-visible physical table name.
    """
    pred = CompiledPredicate(
        sql_expression="(\"region_code\" = 'NORTH')",
        active_rule_ids=("r1",),
        security_dimension_columns=("region_code",),
        compile_connector="postgresql",
    )
    with pytest.raises(HTTPException) as exc:
        _inject_security_where(
            f"INSERT INTO {_SECRET_TABLE} SELECT region_code FROM sales", pred,
        )
    body = _served_body(exc.value)
    assert _SECRET_TABLE not in body, body
    assert json.loads(body)["detail"]["reason_code"] == "table_outside_select_scope"


# ---------------------------------------------------------------------------
# The channel itself, not just the two leaks that were found in it
# ---------------------------------------------------------------------------


_REJECT_TRIGGERS = {
    # reason_code -> (sql, predicate kwargs override, dialect)
    "sql_unparseable": ("SELECT FROM WHERE LIMIT GROUP !!", {}, "postgres"),
    "no_table_scan": ("SELECT 1", {}, "postgres"),
    "table_outside_select_scope": (
        f"INSERT INTO {_SECRET_TABLE} SELECT region_code FROM sales", {}, "postgres",
    ),
    "predicate_dialect_unsupported": (
        "SELECT region_code FROM agg_sales",
        {"compile_connector": "bigquery"},
        "bigquery",
    ),
}


@pytest.mark.parametrize("expected_code", sorted(_REJECT_TRIGGERS))
def test_every_reachable_reject_publishes_a_registered_code_and_no_prose(
    expected_code,
):
    """One shape per reachable branch: the body is exactly three known keys, the
    reason is a registered token, and the sentence is the fixed generic one."""
    sql, overrides, dialect = _REJECT_TRIGGERS[expected_code]
    pred = CompiledPredicate(**{
        "sql_expression": "(NOT \"region_code\" = 'EMEA')",
        "active_rule_ids": ("r1",),
        "security_dimension_columns": ("region_code", _SECRET_COL),
        "compile_connector": "postgresql",
        **overrides,
    })
    with pytest.raises(HTTPException) as exc:
        _inject_security_where(sql, pred, dialect=dialect)

    detail = exc.value.detail
    assert set(detail) == {"error_code", "reason_code", "message"}, detail
    assert detail["error_code"] == "row_security_unsupported_shape"
    assert detail["reason_code"] == expected_code
    assert detail["reason_code"] in ROW_SECURITY_REJECT_REASON_CODES
    assert detail["message"] == (
        "This query cannot be safely constrained by the active "
        "row-level security rules. Rewrite it as a plain SELECT "
        "(or a UNION of plain SELECTs) over the model."
    )


def test_the_diagnostic_reaches_the_service_log_and_only_the_service_log(caplog):
    """The operator still needs the cause. It must travel by log, not by body."""
    pred = CompiledPredicate(
        sql_expression="(\"region_code\" = 'NORTH')",
        active_rule_ids=("r1",),
        security_dimension_columns=("region_code",),
        compile_connector="postgresql",
    )
    with caplog.at_level(logging.WARNING, logger="src.routing.router"):
        with pytest.raises(HTTPException) as exc:
            _inject_security_where(
                f"INSERT INTO {_SECRET_TABLE} SELECT region_code FROM sales", pred,
            )
    assert _SECRET_TABLE in caplog.text, "the operator lost the diagnostic entirely"
    assert _SECRET_TABLE not in _served_body(exc.value)


def test_force_route_hint_is_the_callers_own_value_and_is_still_returned():
    """Bug-7029's actionable hint must survive the Bug-8809 lockdown: it echoes
    a value the caller submitted, so it discloses nothing."""
    pred = CompiledPredicate(
        sql_expression="(\"region_code\" = 'NORTH')",
        active_rule_ids=("r1",),
        security_dimension_columns=("region_code",),
        compile_connector="postgresql",
    )
    with pytest.raises(HTTPException) as exc:
        _inject_security_where("SELECT 1", pred, force_route="aggregate")
    assert "force_route='aggregate'" in exc.value.detail["message"]


def test_an_unregistered_reason_code_fails_closed(caplog):
    """The guard guards itself: a call site that invents a token must not be
    able to smuggle it (or anything shaped like it) into the body."""
    with caplog.at_level(logging.ERROR, logger="src.routing.router"):
        with pytest.raises(HTTPException) as exc:
            _reject_row_security_shape(
                f"leaked column {_SECRET_COL}", diagnostic="x",
            )
    assert exc.value.detail["reason_code"] == "unsupported_shape"
    assert _SECRET_COL not in _served_body(exc.value)


def test_no_reject_call_site_can_pass_a_computed_reason_code():
    """Coverage-tool guard (CLAUDE.md): prove the CHANNEL is closed, not just
    that today's two leaks are plugged.

    Every call to ``_reject_row_security_shape`` must pass a literal string
    constant drawn from the registered vocabulary as its first argument. An
    f-string, a name or a call there is how the original defect was written, so
    it is rejected structurally rather than re-discovered per branch.

    Stated limits: this reads ``router.py`` only. That is sound because the
    function is module-private -- ``test_reject_helper_has_no_callers_outside_
    the_router`` pins that -- and it fails closed on any argument shape it does
    not recognise rather than skipping it.
    """
    import src.routing.router as router_mod

    tree = ast.parse(Path(inspect.getsourcefile(router_mod)).read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_reject_row_security_shape"
    ]
    assert len(calls) >= 8, (
        f"the scanner found only {len(calls)} reject call sites; its verdict is "
        "no longer trustworthy"
    )
    offenders = []
    for call in calls:
        if not call.args:
            offenders.append(f"line {call.lineno}: no positional reason_code")
            continue
        first = call.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            offenders.append(f"line {call.lineno}: reason_code is {type(first).__name__}")
        elif first.value not in ROW_SECURITY_REJECT_REASON_CODES:
            offenders.append(f"line {call.lineno}: unregistered {first.value!r}")
        if len(call.args) > 1:
            offenders.append(f"line {call.lineno}: extra positional args")
    assert not offenders, (
        "a row-security rejection may only publish a registered literal token; "
        "put anything derived from the query, the model or an exception into "
        "the keyword-only `diagnostic` (log-only) instead: " + "; ".join(offenders)
    )


def test_reject_helper_has_no_callers_outside_the_router():
    """Pins the assumption the AST guard above depends on."""
    import src.routing.router as router_mod

    src_root = Path(inspect.getsourcefile(router_mod)).parents[1]
    referencing = sorted(
        path.relative_to(src_root).as_posix()
        for path in src_root.rglob("*.py")
        if "_reject_row_security_shape" in path.read_text(encoding="utf-8")
    )
    assert referencing == ["routing/router.py"], (
        "the reject helper gained a reference outside router.py; the AST guard "
        f"above no longer covers every call site: {referencing}"
    )


# ---------------------------------------------------------------------------
# The 422 arm — the compiler's own message, on all four row-returning routes
# ---------------------------------------------------------------------------


def test_row_security_misconfigured_detail_publishes_no_compiler_text(caplog):
    from src.api._sql_disclosure import row_security_misconfigured_detail
    from shared.security import RowSecurityCompileError

    exc = RowSecurityCompileError(
        f"invalid dimension path in row-security rule: 'customer.{_SECRET_COL}'"
    )
    with caplog.at_level(logging.WARNING, logger="src.api._sql_disclosure"):
        detail = row_security_misconfigured_detail(exc, surface="/execute")

    assert _SECRET_COL not in json.dumps(detail), detail
    assert detail["error_type"] == "row_security_misconfigured"
    assert _SECRET_COL in caplog.text, "the operator lost the compiler diagnostic"


def test_no_row_returning_route_interpolates_the_compile_error():
    """All four surfaces caught ``RowSecurityCompileError`` and each wrote its
    own ``f"...: {e}."`` body. One shared builder now owns that payload; this
    fails if a route re-grows its own."""
    import src.api.headless as headless_mod
    import src.api.plugin as plugin_mod
    import src.api.routes as routes_mod

    for mod in (routes_mod, headless_mod, plugin_mod):
        text = inspect.getsource(mod)
        assert "could not be compiled: {e}" not in text, (
            f"{mod.__name__} re-grew an interpolated row-security compile error"
        )
        assert "row_security_misconfigured_detail(" in text, (
            f"{mod.__name__} no longer routes its 422 through the shared builder"
        )
