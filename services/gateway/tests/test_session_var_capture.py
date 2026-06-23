"""Gateway JDBC session-variable capture (F-029-01 / Bug-1094).

``SET app.<name> = <value>`` is the wire-level mechanism a BI client uses to
set a parameter value. The gateway captures these into ``_session_vars`` and
forwards them to the query-router, which resolves them against the model's
declared parameters. The captured value must be the clean literal — no
statement terminator, no surrounding quotes — or no parameter ever matches.
"""
from __future__ import annotations

import pytest

from src.jdbc.server import PGWireServer


@pytest.fixture
def server() -> PGWireServer:
    return PGWireServer()


def test_capture_basic_quoted_value(server: PGWireServer):
    server._capture_session_var("SET app.region = 'EMEA'")
    assert server._session_vars == {"app.region": "EMEA"}


def test_capture_strips_trailing_semicolon(server: PGWireServer):
    # Bug-1094: psql terminates statements with ``;``; the value group would
    # otherwise capture ``'RETAIL';`` and fold the terminator into the value.
    server._capture_session_var("SET app.segment = 'RETAIL';")
    assert server._session_vars == {"app.segment": "RETAIL"}


def test_capture_no_spaces_with_semicolon(server: PGWireServer):
    server._capture_session_var("SET app.region='APAC';")
    assert server._session_vars == {"app.region": "APAC"}


def test_capture_to_syntax(server: PGWireServer):
    server._capture_session_var("SET app.region TO 'AMER';")
    assert server._session_vars == {"app.region": "AMER"}


def test_capture_double_quoted_value(server: PGWireServer):
    server._capture_session_var('SET app.region = "EMEA";')
    assert server._session_vars == {"app.region": "EMEA"}


def test_capture_unquoted_value(server: PGWireServer):
    server._capture_session_var("SET app.flag = true;")
    assert server._session_vars == {"app.flag": "true"}


def test_capture_lowercases_name_only(server: PGWireServer):
    # Name is normalised; the VALUE is preserved verbatim (case-sensitive).
    server._capture_session_var("SET app.Segment = 'Retail';")
    assert server._session_vars == {"app.segment": "Retail"}


def test_non_app_set_is_ignored(server: PGWireServer):
    server._capture_session_var("SET search_path = public;")
    assert server._session_vars == {}


def test_value_with_embedded_semicolon_keeps_inner(server: PGWireServer):
    # Only the trailing terminator is stripped; an interior payload (which the
    # query-router will bind as a literal) is preserved for round-trip safety.
    server._capture_session_var("SET app.region = 'A;B';")
    assert server._session_vars == {"app.region": "A;B"}
