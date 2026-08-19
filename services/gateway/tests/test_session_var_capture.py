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


def test_capture_unescapes_doubled_single_quote(server: PGWireServer):
    # Bug-6416: a SQL-doubled quote inside a single-quoted value must collapse
    # to one quote. The old ``strip("'\\"")`` left it as ``it''s``.
    server._capture_session_var("SET app.owner = 'it''s';")
    assert server._session_vars == {"app.owner": "it's"}


def test_capture_keeps_inner_double_quote_when_single_quoted(server: PGWireServer):
    # Bug-6416: only the matching outer quote pair is stripped; a different
    # quote character inside the value is preserved verbatim (the old
    # ``strip("'\\"")`` stripped both kinds from either end).
    server._capture_session_var("SET app.label = 'a\"b';")
    assert server._session_vars == {"app.label": 'a"b'}


def test_capture_unquoted_value_unchanged(server: PGWireServer):
    # An unquoted value must pass through untouched (no accidental stripping).
    server._capture_session_var("SET app.count = 42;")
    assert server._session_vars == {"app.count": "42"}


def test_capture_set_session_qualified(server: PGWireServer):
    # Bug-6058: ``SET SESSION app.x`` is acknowledged with ``SET`` and must be
    # captured identically to a bare ``SET app.x`` — otherwise the filter value
    # is silently lost.
    server._capture_session_var("SET SESSION app.region = 'EMEA';")
    assert server._session_vars == {"app.region": "EMEA"}


def test_capture_set_local_qualified(server: PGWireServer):
    # Bug-6058 established that ``SET LOCAL app.x`` must be captured at all.
    # Bug-6592 moved WHERE it lands: LOCAL is transaction-scoped and must
    # NOT share connection-lifetime ``_session_vars`` — it belongs in the
    # separate ``_session_vars_local`` dict (see TestBug6592LocalScoping
    # below for the full lifecycle).
    server._capture_session_var("SET LOCAL app.segment = 'RETAIL';")
    assert server._session_vars == {}
    assert server._session_vars_local == {"app.segment": "RETAIL"}


def test_capture_set_session_to_syntax(server: PGWireServer):
    # Bug-6058: the qualifier composes with the ``TO`` value syntax too.
    server._capture_session_var("SET SESSION app.region TO 'AMER';")
    assert server._session_vars == {"app.region": "AMER"}


def test_capture_set_session_case_insensitive(server: PGWireServer):
    # Bug-6058: the SESSION/LOCAL keyword match is case-insensitive.
    server._capture_session_var("set session App.Region = 'apac';")
    assert server._session_vars == {"app.region": "apac"}


def test_capture_set_session_non_app_ignored(server: PGWireServer):
    # A SESSION-qualified SET of a non-app variable is still ignored.
    server._capture_session_var("SET SESSION search_path = public;")
    assert server._session_vars == {}


# ---------------------------------------------------------------------------
# Bug-6592: SET LOCAL is transaction-scoped, not connection-scoped.
#
# ``SET LOCAL app.x`` must land in a dict separate from bare ``SET`` /
# ``SET SESSION`` and be cleared when the transaction ends (COMMIT/ROLLBACK)
# or the connection is reset (DISCARD/RESET) — never on BEGIN/SAVEPOINT,
# which do not end a transaction. ``_execute_for_extended`` and
# ``_handle_user_query`` both acknowledge these commands (extended and simple
# query protocol respectively); both must apply the same clearing rule.
# ---------------------------------------------------------------------------

class _FakeWriter:
    """Minimal writer stand-in for the driver-housekeeping command handlers,
    which only need ``.write()`` and an awaitable ``.drain()``."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, data: bytes) -> None:
        self.buf.extend(data)

    async def drain(self) -> None:
        return None


class TestBug6592LocalScoping:
    def test_bare_set_still_lands_in_session_dict(self, server: PGWireServer):
        server._capture_session_var("SET app.region = 'EMEA';")
        assert server._session_vars == {"app.region": "EMEA"}
        assert server._session_vars_local == {}

    def test_set_session_still_lands_in_session_dict(self, server: PGWireServer):
        server._capture_session_var("SET SESSION app.region = 'EMEA';")
        assert server._session_vars == {"app.region": "EMEA"}
        assert server._session_vars_local == {}

    def test_effective_session_vars_none_when_both_empty(self, server: PGWireServer):
        assert server._effective_session_vars() is None

    def test_effective_session_vars_returns_session_only(self, server: PGWireServer):
        server._capture_session_var("SET app.region = 'EMEA';")
        assert server._effective_session_vars() == {"app.region": "EMEA"}

    def test_effective_session_vars_returns_local_only(self, server: PGWireServer):
        server._capture_session_var("SET LOCAL app.region = 'EMEA';")
        assert server._effective_session_vars() == {"app.region": "EMEA"}

    def test_effective_session_vars_merges_both(self, server: PGWireServer):
        server._capture_session_var("SET app.region = 'EMEA';")
        server._capture_session_var("SET LOCAL app.segment = 'RETAIL';")
        assert server._effective_session_vars() == {
            "app.region": "EMEA",
            "app.segment": "RETAIL",
        }

    def test_effective_session_vars_local_wins_on_collision(self, server: PGWireServer):
        # LOCAL is the narrower, currently-in-effect scope for the duration
        # of the transaction; it must override a same-named SESSION value.
        server._capture_session_var("SET app.region = 'EMEA';")
        server._capture_session_var("SET LOCAL app.region = 'APAC';")
        assert server._effective_session_vars() == {"app.region": "APAC"}
        # The underlying SESSION value is untouched — it reappears once the
        # LOCAL override is cleared at transaction end.
        assert server._session_vars == {"app.region": "EMEA"}

    # -- Extended-protocol path (_execute_for_extended) --------------------

    async def test_local_cleared_on_commit_extended_path(self, server: PGWireServer):
        server._capture_session_var("SET LOCAL app.region = 'EMEA';")
        assert server._session_vars_local == {"app.region": "EMEA"}
        await server._execute_for_extended("COMMIT")
        assert server._session_vars_local == {}

    async def test_local_cleared_on_rollback_extended_path(self, server: PGWireServer):
        server._capture_session_var("SET LOCAL app.region = 'EMEA';")
        await server._execute_for_extended("ROLLBACK")
        assert server._session_vars_local == {}

    async def test_local_not_cleared_on_begin_extended_path(self, server: PGWireServer):
        server._capture_session_var("SET LOCAL app.region = 'EMEA';")
        await server._execute_for_extended("BEGIN")
        assert server._session_vars_local == {"app.region": "EMEA"}

    async def test_local_not_cleared_on_savepoint_extended_path(self, server: PGWireServer):
        server._capture_session_var("SET LOCAL app.region = 'EMEA';")
        await server._execute_for_extended("SAVEPOINT sp1")
        assert server._session_vars_local == {"app.region": "EMEA"}

    async def test_session_survives_commit_extended_path(self, server: PGWireServer):
        # Only the LOCAL capture is transaction-scoped; a bare/SESSION SET
        # keeps its connection lifetime across COMMIT.
        server._capture_session_var("SET app.region = 'EMEA';")
        await server._execute_for_extended("COMMIT")
        assert server._session_vars == {"app.region": "EMEA"}

    async def test_discard_clears_both_dicts_extended_path(self, server: PGWireServer):
        server._capture_session_var("SET app.region = 'EMEA';")
        server._capture_session_var("SET LOCAL app.segment = 'RETAIL';")
        await server._execute_for_extended("DISCARD ALL")
        assert server._session_vars == {}
        assert server._session_vars_local == {}

    async def test_reset_all_clears_both_dicts_extended_path(self, server: PGWireServer):
        server._capture_session_var("SET app.region = 'EMEA';")
        server._capture_session_var("SET LOCAL app.segment = 'RETAIL';")
        await server._execute_for_extended("RESET ALL")
        assert server._session_vars == {}
        assert server._session_vars_local == {}

    async def test_reset_specific_clears_both_dicts_extended_path(self, server: PGWireServer):
        server._capture_session_var("SET app.region = 'EMEA';")
        server._capture_session_var("SET LOCAL app.region = 'APAC';")
        server._capture_session_var("SET app.segment = 'RETAIL';")
        await server._execute_for_extended("RESET app.region")
        assert server._session_vars == {"app.segment": "RETAIL"}
        assert server._session_vars_local == {}

    # -- Simple-query path (_handle_user_query) -----------------------------

    async def test_local_cleared_on_commit_simple_path(self, server: PGWireServer):
        server._capture_session_var("SET LOCAL app.region = 'EMEA';")
        await server._handle_user_query("COMMIT", _FakeWriter())
        assert server._session_vars_local == {}

    async def test_local_cleared_on_rollback_simple_path(self, server: PGWireServer):
        server._capture_session_var("SET LOCAL app.region = 'EMEA';")
        await server._handle_user_query("ROLLBACK", _FakeWriter())
        assert server._session_vars_local == {}

    async def test_local_not_cleared_on_begin_simple_path(self, server: PGWireServer):
        server._capture_session_var("SET LOCAL app.region = 'EMEA';")
        await server._handle_user_query("BEGIN", _FakeWriter())
        assert server._session_vars_local == {"app.region": "EMEA"}

    async def test_discard_clears_both_dicts_simple_path(self, server: PGWireServer):
        server._capture_session_var("SET app.region = 'EMEA';")
        server._capture_session_var("SET LOCAL app.segment = 'RETAIL';")
        await server._handle_user_query("DISCARD ALL", _FakeWriter())
        assert server._session_vars == {}
        assert server._session_vars_local == {}

    async def test_reset_all_clears_both_dicts_simple_path(self, server: PGWireServer):
        server._capture_session_var("SET app.region = 'EMEA';")
        server._capture_session_var("SET LOCAL app.segment = 'RETAIL';")
        await server._handle_user_query("RESET ALL", _FakeWriter())
        assert server._session_vars == {}
        assert server._session_vars_local == {}

    # -- $KPIs fail-closed guard sees the LOCAL dict too --------------------

    def test_kpi_security_error_trips_on_local_only(self, server: PGWireServer):
        # Bug-6592 adjacent: a LOCAL-only filter is just as much an active
        # session-variable context as a SESSION one for the $KPIs fail-closed
        # guard (server.py `_kpi_security_error`).
        server._capture_session_var("SET LOCAL app.region = 'EMEA';")
        error = server._kpi_security_error('SELECT * FROM "modelx$KPIs"', persona_id=None)
        assert error is not None
        assert "$KPIs" in error
