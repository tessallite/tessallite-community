"""Bug-8107 / Bug-8108 — prepared-statement and portal lifecycle.

Bug-8107 (GPT-2026-07-19:F-001-05): the extended query protocol acknowledged
invalid client state instead of rejecting it. ``Bind`` to an unknown/
un-Parse'd statement name silently fell back to the UNNAMED portal's SQL
(``self._statements.get(_stmt_peek, _get_portal("")["sql"])``) — a stale or
buggy client sequence could therefore BIND AND LATER EXECUTE THE WRONG
STATEMENT instead of getting a protocol error. Statement-level ``Describe``
had the identical fallback. Portal-level ``Describe``/``Execute`` against a
named portal that was never created by ``Bind`` auto-vivified an empty
portal and returned a silent NoData / "SELECT 0" success instead of an
error. This is the wrong-numbers half of Bug-8107: the fix rejects each case
with a proper PG ErrorResponse (SQLSTATE 26000 invalid_sql_statement_name for
the statement-name cases, 34000 invalid_cursor_name for the portal cases) and
NEVER falls back to the unnamed portal's SQL, without crashing the connection
loop.

Bug-8108: ``_execute_for_extended`` (the function that produces a portal's row
buffer) materialises the FULL result before Describe/Execute can page it out.
Unbounded, this is an unbounded-memory defect. The fix caps the number of
rows a single portal will buffer (configurable, not hard-coded — see
``GATEWAY_JDBC_PORTAL_ROW_BUFFER_CAP`` / ``gateway.jdbc_portal_row_buffer_cap``
in shared/config/settings.py + registry.py) and fails closed with SQLSTATE
54000 (program_limit_exceeded) when the cap is exceeded.
"""
from __future__ import annotations

import struct

import pytest

from src.jdbc import protocol as proto
from src.jdbc.server import PGWireServer


# ---------------------------------------------------------------------------
# Wire helpers (mirrors test_bug_6919_6934_jdbc_ingest.py's TestBug6934NamedPortals)
# ---------------------------------------------------------------------------


class _FakeReader:
    """Feeds a fixed sequence of (type_char, payload) frames."""

    def __init__(self, frames: list[tuple[str, bytes]]) -> None:
        self._frames = frames + [("X", b"")]


class _FakeWriter:
    def __init__(self) -> None:
        self.payload = b""

    def write(self, payload: bytes) -> None:
        self.payload += payload

    async def drain(self) -> None:
        pass


def _parse_payload(sql: str, stmt: str = "") -> bytes:
    """Parse message payload: stmt_name\\0 sql\\0 param_count(0)."""
    return stmt.encode("utf-8") + b"\x00" + sql.encode("utf-8") + b"\x00" + struct.pack("!H", 0)


def _bind_payload(portal: str = "", stmt: str = "", params: list[str | None] | None = None) -> bytes:
    """Bind payload: portal\\0 stmt\\0 fmt_count(0) params result_fmt(0)."""
    body = portal.encode("utf-8") + b"\x00"
    body += stmt.encode("utf-8") + b"\x00"
    body += struct.pack("!H", 0)  # 0 format codes -> all text
    p = params or []
    body += struct.pack("!H", len(p))
    for v in p:
        if v is None:
            body += struct.pack("!i", -1)
        else:
            enc = v.encode("utf-8")
            body += struct.pack("!I", len(enc)) + enc
    body += struct.pack("!H", 0)  # 0 result format codes
    return body


def _execute_payload(portal: str = "", max_rows: int = 0) -> bytes:
    return portal.encode("utf-8") + b"\x00" + struct.pack("!I", max_rows)


def _describe_statement_payload(name: str = "") -> bytes:
    return b"S" + name.encode("utf-8") + b"\x00"


def _describe_portal_payload(name: str = "") -> bytes:
    return b"P" + name.encode("utf-8") + b"\x00"


def _make_server() -> PGWireServer:
    server = PGWireServer()
    server._catalogue = None
    server._jwt_token = "token"
    server._tenant_slug = "tenant"
    server._client_kind = None
    server._looker_relations = set()
    server._tls_active = True
    server._session_vars = {}
    server._table_model_id = {"modelx": "model-1"}
    server._table_include_hidden = {"modelx": False}
    server._table_persona_id = {"modelx": None}
    server._table_query_name = {}
    server._table_columns = {
        "modelx": [
            {"name": "region", "kind": "dimension", "data_type": "text"},
            {"name": "revenue", "kind": "measure", "data_type": "numeric"},
        ]
    }
    return server


async def _run(server: PGWireServer, frames: list[tuple[str, bytes]], monkeypatch) -> _FakeWriter:
    reader = _FakeReader(frames)
    writer = _FakeWriter()
    frame_idx = [0]

    async def fake_read_message(_r):
        idx = frame_idx[0]
        frame_idx[0] += 1
        f = reader._frames[idx] if idx < len(reader._frames) else ("X", b"")
        return f[0], f[1]

    monkeypatch.setattr(proto, "read_message", fake_read_message)
    await server._query_loop(reader, writer)
    return writer


# ---------------------------------------------------------------------------
# Bug-8107a — Bind to an unknown/un-Parse'd statement -> 26000
# ---------------------------------------------------------------------------


async def test_bind_unknown_statement_rejected_26000(monkeypatch):
    calls: list[str] = []

    async def fake_execute_query(**kwargs):
        calls.append(kwargs["sql"])
        return {"columns": ["region"], "rows": [{"region": "EMEA"}]}

    import src.jdbc.server as jdbc_server
    monkeypatch.setattr(jdbc_server, "execute_query", fake_execute_query)

    server = _make_server()
    frames = [
        # Parse ONLY the unnamed statement — S9 is never Parsed. Before the
        # fix, Bind to S9 would silently fall back to this (wrong) SQL.
        ("P", _parse_payload("SELECT region FROM modelx", "")),
        ("B", _bind_payload(portal="P1", stmt="S9")),
        ("S", b""),
    ]
    writer = await _run(server, frames, monkeypatch)

    assert b"26000" in writer.payload
    # The wrong-numbers defect: the unnamed statement's SQL must NEVER run.
    assert calls == []


async def test_bind_unknown_statement_never_binds_portal(monkeypatch):
    """A rejected Bind must not create the named portal — a later
    Describe/Execute against it must also fail (invalid cursor), never
    silently succeed against leftover/default state."""
    import src.jdbc.server as jdbc_server

    async def fake_execute_query(**kwargs):
        raise AssertionError("must never execute after a rejected Bind")

    monkeypatch.setattr(jdbc_server, "execute_query", fake_execute_query)

    server = _make_server()
    frames = [
        ("P", _parse_payload("SELECT region FROM modelx", "")),
        ("B", _bind_payload(portal="P1", stmt="S9")),  # rejected: S9 unknown
        # Per pgwire spec, everything until Sync is skipped once an error is
        # emitted in the extended protocol — a real client always Syncs
        # after seeing an ErrorResponse before issuing more commands.
        ("S", b""),
        ("E", _execute_payload(portal="P1")),  # P1 was never actually bound
        ("S", b""),
    ]
    writer = await _run(server, frames, monkeypatch)

    assert writer.payload.count(b"26000") == 1
    assert b"34000" in writer.payload  # the Execute against P1 also fails


# ---------------------------------------------------------------------------
# Bug-8107b — statement-level Describe of an unknown statement -> 26000
# ---------------------------------------------------------------------------


async def test_describe_statement_unknown_rejected_26000(monkeypatch):
    import src.jdbc.server as jdbc_server

    async def fake_execute_query(**kwargs):
        raise AssertionError("statement Describe must never execute a query")

    monkeypatch.setattr(jdbc_server, "execute_query", fake_execute_query)

    server = _make_server()
    frames = [
        ("P", _parse_payload("SELECT region FROM modelx", "S1")),
        ("D", _describe_statement_payload("S2")),  # S2 was never Parsed
        ("S", b""),
    ]
    writer = await _run(server, frames, monkeypatch)

    assert b"26000" in writer.payload


# ---------------------------------------------------------------------------
# Bug-8107c — Describe/Execute on a never-Bind'd portal -> 34000
# ---------------------------------------------------------------------------


async def test_describe_portal_never_bound_rejected_34000(monkeypatch):
    import src.jdbc.server as jdbc_server

    async def fake_execute_query(**kwargs):
        raise AssertionError("portal Describe must never execute against a phantom portal")

    monkeypatch.setattr(jdbc_server, "execute_query", fake_execute_query)

    server = _make_server()
    frames = [
        ("P", _parse_payload("SELECT region FROM modelx", "S1")),
        ("B", _bind_payload(portal="P1", stmt="S1")),
        # P9 was never bound — must not auto-vivify into an empty NoData.
        ("D", _describe_portal_payload("P9")),
        ("S", b""),
    ]
    writer = await _run(server, frames, monkeypatch)

    assert b"34000" in writer.payload


async def test_execute_portal_never_bound_rejected_34000(monkeypatch):
    calls: list[str] = []

    async def fake_execute_query(**kwargs):
        calls.append(kwargs["sql"])
        return {"columns": ["region"], "rows": [{"region": "EMEA"}]}

    import src.jdbc.server as jdbc_server
    monkeypatch.setattr(jdbc_server, "execute_query", fake_execute_query)

    server = _make_server()
    frames = [
        ("P", _parse_payload("SELECT region FROM modelx", "S1")),
        ("B", _bind_payload(portal="P1", stmt="S1")),
        # P9 was never bound.
        ("E", _execute_payload(portal="P9")),
        ("S", b""),
    ]
    writer = await _run(server, frames, monkeypatch)

    assert b"34000" in writer.payload
    # The never-bound portal must never silently report "SELECT 0" success.
    assert b"SELECT 0" not in writer.payload
    assert calls == []


async def test_unnamed_portal_describe_before_bind_still_works(monkeypatch):
    """Regression guard: the Bug-6056/F-001-01 describe-before-bind flow for
    the UNNAMED portal (Parse -> Describe(portal "") -> Bind -> Execute) must
    keep working — the Bug-8107 fix only rejects portals that were NEVER
    created by Parse or Bind, not the intentionally-supported pre-bind
    describe of the unnamed portal."""
    calls: list[str] = []

    async def fake_execute_query(**kwargs):
        calls.append(kwargs["sql"])
        return {"columns": ["region"], "rows": [{"region": "EMEA"}]}

    import src.jdbc.server as jdbc_server
    monkeypatch.setattr(jdbc_server, "execute_query", fake_execute_query)

    server = _make_server()
    frames = [
        ("P", _parse_payload("SELECT region FROM modelx", "")),
        ("D", _describe_portal_payload("")),
        ("B", _bind_payload(portal="", stmt="")),
        ("E", _execute_payload(portal="")),
        ("S", b""),
    ]
    writer = await _run(server, frames, monkeypatch)

    assert b"34000" not in writer.payload
    assert b"26000" not in writer.payload
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Bug-8107d — the connection loop must not crash; it keeps serving requests
# after rejecting invalid extended-protocol state.
# ---------------------------------------------------------------------------


async def test_loop_survives_rejected_bind_and_serves_next_query(monkeypatch):
    calls: list[str] = []

    async def fake_execute_query(**kwargs):
        calls.append(kwargs["sql"])
        return {"columns": ["region"], "rows": [{"region": "EMEA"}]}

    import src.jdbc.server as jdbc_server
    monkeypatch.setattr(jdbc_server, "execute_query", fake_execute_query)

    server = _make_server()
    frames = [
        ("P", _parse_payload("SELECT region FROM modelx", "")),
        ("B", _bind_payload(portal="P1", stmt="UNKNOWN")),  # rejected -> 26000
        ("S", b""),  # Sync clears the error-skip state
        # A fresh, well-formed cycle must succeed normally afterward.
        ("P", _parse_payload("SELECT revenue FROM modelx", "")),
        ("B", _bind_payload(portal="", stmt="")),
        ("E", _execute_payload(portal="")),
        ("S", b""),
    ]
    writer = await _run(server, frames, monkeypatch)

    assert b"26000" in writer.payload
    assert len(calls) == 1
    assert "revenue" in calls[0].lower()
    # ReadyForQuery ('Z') was sent for both Syncs — the loop kept going.
    assert writer.payload.count(b"Z") >= 2


# ---------------------------------------------------------------------------
# Bug-8108 — portal row-buffer cap
# ---------------------------------------------------------------------------


async def test_execute_for_extended_rejects_over_cap_54000(monkeypatch):
    import src.jdbc.server as jdbc_server

    async def fake_execute_query(**kwargs):
        return {
            "columns": ["region"],
            "rows": [{"region": f"R{i}"} for i in range(10)],
        }

    monkeypatch.setattr(jdbc_server, "execute_query", fake_execute_query)
    monkeypatch.setattr(jdbc_server, "_portal_row_buffer_cap", lambda: 3)

    server = _make_server()
    cols, rows, err = await server._execute_for_extended("SELECT region FROM modelx")

    assert cols is None
    assert rows is None
    assert err is not None
    assert err[1] == "54000"
    assert "10" in err[0]  # actual row count surfaced for diagnosis
    assert "3" in err[0]  # configured cap surfaced for diagnosis


async def test_execute_for_extended_under_cap_passes(monkeypatch):
    import src.jdbc.server as jdbc_server

    async def fake_execute_query(**kwargs):
        return {
            "columns": ["region"],
            "rows": [{"region": f"R{i}"} for i in range(3)],
        }

    monkeypatch.setattr(jdbc_server, "execute_query", fake_execute_query)
    monkeypatch.setattr(jdbc_server, "_portal_row_buffer_cap", lambda: 3)

    server = _make_server()
    cols, rows, err = await server._execute_for_extended("SELECT region FROM modelx")

    assert err is None
    assert cols is not None
    assert len(rows) == 3


async def test_execute_for_extended_cap_disabled_when_zero(monkeypatch):
    import src.jdbc.server as jdbc_server

    async def fake_execute_query(**kwargs):
        return {
            "columns": ["region"],
            "rows": [{"region": f"R{i}"} for i in range(100)],
        }

    monkeypatch.setattr(jdbc_server, "execute_query", fake_execute_query)
    monkeypatch.setattr(jdbc_server, "_portal_row_buffer_cap", lambda: 0)

    server = _make_server()
    cols, rows, err = await server._execute_for_extended("SELECT region FROM modelx")

    assert err is None
    assert len(rows) == 100


async def test_wire_level_execute_rejects_over_cap_54000(monkeypatch):
    """End-to-end: a client that Binds and Executes a query whose result
    exceeds the configured portal row-buffer cap gets SQLSTATE 54000 over
    the wire — never a truncated/partial DataRow stream and never a silent
    success."""
    import src.jdbc.server as jdbc_server

    async def fake_execute_query(**kwargs):
        return {
            "columns": ["region"],
            "rows": [{"region": f"R{i}"} for i in range(10)],
        }

    monkeypatch.setattr(jdbc_server, "execute_query", fake_execute_query)
    monkeypatch.setattr(jdbc_server, "_portal_row_buffer_cap", lambda: 3)

    server = _make_server()
    frames = [
        ("P", _parse_payload("SELECT region FROM modelx", "")),
        ("B", _bind_payload(portal="", stmt="")),
        ("E", _execute_payload(portal="")),
        ("S", b""),
    ]
    writer = await _run(server, frames, monkeypatch)

    assert b"54000" in writer.payload
    assert b"SELECT 10" not in writer.payload
    assert b"R0" not in writer.payload  # no partial row data leaked to the client


# ---------------------------------------------------------------------------
# Bug-8107 F1 — portal existence is NOT a proxy for a successful Bind.
# The unnamed portal must only ever run SQL that was actually Bound into it.
# ---------------------------------------------------------------------------


async def test_named_parse_does_not_populate_unnamed_portal_execute_rejected(monkeypatch):
    """A NAMED Parse must NOT populate the unnamed portal. Execute("") on a
    fresh connection (no unnamed Parse, no Bind) must fail 34000 and NEVER
    run the named statement's SQL."""
    calls: list[str] = []

    async def fake_execute_query(**kwargs):
        calls.append(kwargs["sql"])
        return {"columns": ["region"], "rows": [{"region": "EMEA"}]}

    import src.jdbc.server as jdbc_server
    monkeypatch.setattr(jdbc_server, "execute_query", fake_execute_query)

    server = _make_server()
    frames = [
        ("P", _parse_payload("SELECT region FROM modelx", "S1")),  # NAMED parse
        ("E", _execute_payload(portal="")),  # unnamed portal was never created
        ("S", b""),
    ]
    writer = await _run(server, frames, monkeypatch)

    assert b"34000" in writer.payload
    assert b"SELECT 0" not in writer.payload
    # The wrong-numbers defect: the named statement's SQL must NEVER run
    # against the unnamed portal without a Bind.
    assert calls == []


async def test_unnamed_parse_execute_before_bind_rejected_34000(monkeypatch):
    """An unnamed Parse creates the describe-before-bind placeholder, but it is
    NOT bound. Execute("") before any Bind must fail 34000 and never run the
    parameterless/un-bound SQL."""
    calls: list[str] = []

    async def fake_execute_query(**kwargs):
        calls.append(kwargs["sql"])
        return {"columns": ["region"], "rows": [{"region": "EMEA"}]}

    import src.jdbc.server as jdbc_server
    monkeypatch.setattr(jdbc_server, "execute_query", fake_execute_query)

    server = _make_server()
    frames = [
        ("P", _parse_payload("SELECT region FROM modelx", "")),  # unnamed parse
        ("E", _execute_payload(portal="")),  # execute BEFORE any Bind
        ("S", b""),
    ]
    writer = await _run(server, frames, monkeypatch)

    assert b"34000" in writer.payload
    assert b"SELECT 0" not in writer.payload
    assert calls == []


async def test_simple_query_invalidates_unnamed_statement_stale_bind_rejected(monkeypatch):
    """A Simple Query destroys the unnamed prepared statement + portal, so a
    later unnamed Bind cannot resurrect the stale statement's SQL. The stale
    statement must NEVER execute; the Bind fails loud (26000)."""
    calls: list[str] = []

    async def fake_execute_query(**kwargs):
        calls.append(kwargs["sql"])
        return {"columns": ["region"], "rows": [{"region": "EMEA"}]}

    import src.jdbc.server as jdbc_server
    monkeypatch.setattr(jdbc_server, "execute_query", fake_execute_query)

    server = _make_server()
    frames = [
        ("P", _parse_payload("SELECT region FROM modelx", "")),  # unnamed A
        ("Q", b"SELECT 1\x00"),  # Simple Query B — destroys the unnamed stmt/portal
        ("B", _bind_payload(portal="", stmt="")),  # stale unnamed Bind -> 26000
        ("E", _execute_payload(portal="")),  # skipped (error_skip)
        ("S", b""),
    ]
    writer = await _run(server, frames, monkeypatch)

    # The stale unnamed statement ("... region FROM modelx") must never run.
    assert calls == []
    # Bind to the now-destroyed unnamed statement fails loud.
    assert b"26000" in writer.payload


# ---------------------------------------------------------------------------
# Bug-8108 F2 — the cap also covers catalogue-backed portals (the bypass).
# ---------------------------------------------------------------------------


class _FakeCatalogue:
    """Minimal catalogue stand-in: every query is served from a fixed rowset."""

    def __init__(self, columns, rows):
        self._columns = columns
        self._rows = rows

    def references_catalogue(self, sql):  # noqa: ARG002 — fixed rowset
        return False

    def execute(self, sql):  # noqa: ARG002 — fixed rowset
        return (self._columns, self._rows)


async def test_catalogue_portal_over_cap_rejected_54000(monkeypatch):
    """A catalogue-backed portal returning more than the cap must fail 54000 —
    the cap was previously only enforced on the two router branches, so a
    catalogue result slipped through unbounded."""
    import src.jdbc.server as jdbc_server

    async def fake_execute_query(**kwargs):
        raise AssertionError("catalogue portal must not reach the router")

    monkeypatch.setattr(jdbc_server, "execute_query", fake_execute_query)
    monkeypatch.setattr(jdbc_server, "_portal_row_buffer_cap", lambda: 3)

    server = _make_server()
    # cap + 1 catalogue rows.
    server._catalogue = _FakeCatalogue(
        [("region", proto.OID_TEXT)],
        [[f"CATROW{i}"] for i in range(4)],
    )
    frames = [
        ("P", _parse_payload("SELECT region FROM some_catalogue_table", "")),
        ("B", _bind_payload(portal="", stmt="")),
        ("E", _execute_payload(portal="")),
        ("S", b""),
    ]
    writer = await _run(server, frames, monkeypatch)

    assert b"54000" in writer.payload
    assert b"CATROW0" not in writer.payload  # no partial catalogue data leaked
    assert b"SELECT 4" not in writer.payload


async def test_catalogue_portal_under_cap_passes(monkeypatch):
    """The catalogue cap must not reject a result at or below the cap."""
    import src.jdbc.server as jdbc_server
    monkeypatch.setattr(jdbc_server, "_portal_row_buffer_cap", lambda: 3)

    server = _make_server()
    server._catalogue = _FakeCatalogue(
        [("region", proto.OID_TEXT)],
        [[f"CATROW{i}"] for i in range(3)],
    )
    cols, rows, err = await server._execute_for_extended(
        "SELECT region FROM some_catalogue_table"
    )
    assert err is None
    assert len(rows) == 3


# ---------------------------------------------------------------------------
# Bug-8108 F3 — the advertised env var actually configures the gateway cap.
# ---------------------------------------------------------------------------


def test_portal_cap_env_reachable_when_nothing_stored(monkeypatch):
    """Real resolution (no monkeypatch of ``_portal_row_buffer_cap``): with no
    stored system value, the ``GATEWAY_JDBC_PORTAL_ROW_BUFFER_CAP`` env value
    must be honored. On the pre-fix code ``system_snapshot_get`` returned the
    registry default 50_000 and this asserted-3 value was unreachable."""
    import src.jdbc.server as jdbc_server
    from shared.config import bootstrap

    bootstrap.clear_snapshot()
    monkeypatch.setattr(jdbc_server.settings, "GATEWAY_JDBC_PORTAL_ROW_BUFFER_CAP", 3)
    try:
        assert jdbc_server._portal_row_buffer_cap() == 3
    finally:
        bootstrap.clear_snapshot()


def test_portal_cap_stored_value_wins(monkeypatch):
    """A hot-reloadable stored system value overrides the env var."""
    import src.jdbc.server as jdbc_server
    from shared.config import bootstrap

    bootstrap.clear_snapshot()
    monkeypatch.setattr(jdbc_server.settings, "GATEWAY_JDBC_PORTAL_ROW_BUFFER_CAP", 3)
    bootstrap.update_snapshot("gateway.jdbc_portal_row_buffer_cap", 9)
    try:
        assert jdbc_server._portal_row_buffer_cap() == 9
    finally:
        bootstrap.clear_snapshot()


def test_portal_cap_negative_env_falls_to_default_not_disabled(monkeypatch):
    """A negative env value must not disable the cap; it falls to the default."""
    import src.jdbc.server as jdbc_server
    from shared.config import bootstrap

    bootstrap.clear_snapshot()
    monkeypatch.setattr(jdbc_server.settings, "GATEWAY_JDBC_PORTAL_ROW_BUFFER_CAP", -5)
    try:
        assert jdbc_server._portal_row_buffer_cap() == 50_000
    finally:
        bootstrap.clear_snapshot()
