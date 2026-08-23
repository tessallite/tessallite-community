"""Bug-6592 F4 — SET LOCAL follows actual transaction / execution boundaries.

``SET LOCAL app.*`` is transaction-scoped: its value must reach the router for
queries inside the transaction and must NOT leak past the transaction boundary,
nor take effect before its own command actually executes.

The round-1 fix recognised transaction commands by ad-hoc exact strings, tracked
no explicit/implicit transaction state, and let the metadata Describe path call
the side-effecting execution helper. That left three holes this suite locks:

- ``COMMIT;`` / ``ROLLBACK;`` (trailing terminator) did not match the exact
  strings, so LOCAL leaked past a committed/rolled-back transaction.
- Without an explicit ``BEGIN``, an extended-protocol ``Sync`` ends the implicit
  transaction, yet LOCAL was retained across it.
- Portal ``Describe`` executed the bound command, so describing a bound
  ``SET LOCAL`` applied it early and describing a bound ``COMMIT`` cleared LOCAL
  before the client's Execute.

These are asserted against the exact ``session_vars`` handed to the router
before and after each boundary.
"""
from __future__ import annotations

import struct

import pytest

from src.jdbc import protocol as proto
from src.jdbc.server import PGWireServer


# ---------------------------------------------------------------------------
# Wire harness
# ---------------------------------------------------------------------------


class _FakeReader:
    def __init__(self, frames):
        self._frames = frames + [("X", b"")]


class _FakeWriter:
    def __init__(self):
        self.payload = b""

    def write(self, payload):
        self.payload += payload

    async def drain(self):
        pass


def _parse_payload(sql: str, stmt: str = "") -> bytes:
    return stmt.encode() + b"\x00" + sql.encode() + b"\x00" + struct.pack("!H", 0)


def _bind_payload(portal: str = "", stmt: str = "") -> bytes:
    body = portal.encode() + b"\x00" + stmt.encode() + b"\x00"
    body += struct.pack("!H", 0)  # 0 format codes
    body += struct.pack("!H", 0)  # 0 params
    body += struct.pack("!H", 0)  # 0 result format codes
    return body


def _execute_payload(portal: str = "", max_rows: int = 0) -> bytes:
    return portal.encode() + b"\x00" + struct.pack("!I", max_rows)


def _describe_portal_payload(name: str = "") -> bytes:
    return b"P" + name.encode() + b"\x00"


def _make_server() -> PGWireServer:
    server = PGWireServer()
    server._catalogue = None
    server._jwt_token = "token"
    server._tenant_slug = "tenant"
    server._client_kind = None
    server._looker_relations = set()
    server._tls_active = True
    server._session_vars = {}
    server._session_vars_local = {}
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


async def _run(server, frames, monkeypatch):
    reader = _FakeReader(frames)
    writer = _FakeWriter()
    idx = [0]

    async def fake_read_message(_r):
        i = idx[0]
        idx[0] += 1
        f = reader._frames[i] if i < len(reader._frames) else ("X", b"")
        return f[0], f[1]

    monkeypatch.setattr(proto, "read_message", fake_read_message)
    await server._query_loop(reader, writer)
    return writer


def _install_router_capture(monkeypatch):
    """Patch execute_query to record the session_vars of each router call."""
    calls: list[dict | None] = []

    async def fake_execute_query(**kwargs):
        sv = kwargs.get("session_vars")
        calls.append(dict(sv) if sv is not None else None)
        return {"columns": ["region"], "rows": [{"region": "X"}]}

    import src.jdbc.server as jdbc_server
    monkeypatch.setattr(jdbc_server, "execute_query", fake_execute_query)
    return calls


def _q_select() -> tuple[str, bytes]:
    return ("Q", b"SELECT region FROM modelx\x00")


# ---------------------------------------------------------------------------
# Trailing-terminator normalisation
# ---------------------------------------------------------------------------


async def test_commit_with_semicolon_clears_local_simple_path():
    server = _make_server()
    server._capture_session_var("SET LOCAL app.region = 'EMEA'")
    assert server._session_vars_local == {"app.region": "EMEA"}
    await server._handle_user_query("COMMIT;", _FakeWriter())
    assert server._session_vars_local == {}


async def test_rollback_with_semicolon_clears_local_extended_path():
    server = _make_server()
    server._capture_session_var("SET LOCAL app.region = 'EMEA'")
    await server._execute_for_extended("ROLLBACK;")
    assert server._session_vars_local == {}


async def test_commit_with_semicolon_clears_local_extended_path():
    server = _make_server()
    server._capture_session_var("SET LOCAL app.region = 'EMEA'")
    await server._execute_for_extended("COMMIT;")
    assert server._session_vars_local == {}


# ---------------------------------------------------------------------------
# Implicit transaction ended by Sync clears LOCAL (no explicit BEGIN)
# ---------------------------------------------------------------------------


async def test_implicit_sync_clears_local_extended(monkeypatch):
    calls = _install_router_capture(monkeypatch)
    server = _make_server()
    frames = [
        ("P", _parse_payload("SET LOCAL app.region = 'EMEA'")),
        ("B", _bind_payload()),
        ("E", _execute_payload()),
        _q_select(),                 # call #0 — still inside implicit txn
        ("S", b""),                  # implicit Sync ends the transaction
        _q_select(),                 # call #1 — LOCAL must be gone
        ("S", b""),
    ]
    await _run(server, frames, monkeypatch)

    assert calls[0] == {"app.region": "EMEA"}   # LOCAL applied before the Sync
    assert not calls[1]                          # None or {} — cleared at Sync


# ---------------------------------------------------------------------------
# Explicit BEGIN keeps LOCAL across Sync; COMMIT ends it
# ---------------------------------------------------------------------------


async def test_explicit_begin_keeps_local_across_sync_then_commit_clears(monkeypatch):
    calls = _install_router_capture(monkeypatch)
    server = _make_server()
    frames = [
        ("P", _parse_payload("BEGIN")),
        ("B", _bind_payload()),
        ("E", _execute_payload()),
        ("P", _parse_payload("SET LOCAL app.region = 'EMEA'")),
        ("B", _bind_payload()),
        ("E", _execute_payload()),
        ("S", b""),                  # Sync INSIDE the explicit txn — LOCAL kept
        _q_select(),                 # call #0 — still {region: EMEA}
        ("P", _parse_payload("COMMIT")),
        ("B", _bind_payload()),
        ("E", _execute_payload()),
        _q_select(),                 # call #1 — LOCAL cleared by COMMIT
        ("S", b""),
    ]
    await _run(server, frames, monkeypatch)

    assert calls[0] == {"app.region": "EMEA"}   # survived the intervening Sync
    assert not calls[1]                          # COMMIT cleared it
    assert server._in_explicit_txn is False


# ---------------------------------------------------------------------------
# Describe is metadata-only — no execution side effect
# ---------------------------------------------------------------------------


async def test_describe_bound_set_local_does_not_capture(monkeypatch):
    """Describing a bound SET LOCAL portal must NOT apply it — the value lands
    only when the client Executes."""
    _install_router_capture(monkeypatch)
    server = _make_server()
    frames = [
        ("P", _parse_payload("SET LOCAL app.region = 'EMEA'")),
        ("B", _bind_payload()),
        ("D", _describe_portal_payload()),   # metadata only — must not capture
    ]
    await _run(server, frames, monkeypatch)
    assert server._session_vars_local == {}


async def test_execute_after_describe_set_local_captures(monkeypatch):
    """Confirm the deferred command still runs at Execute after a metadata-only
    Describe. State is observed BEFORE any Sync — an implicit Sync would then
    legitimately clear the LOCAL value (see the implicit-Sync test)."""
    _install_router_capture(monkeypatch)
    server = _make_server()
    frames = [
        ("P", _parse_payload("SET LOCAL app.region = 'EMEA'")),
        ("B", _bind_payload()),
        ("D", _describe_portal_payload()),
        ("E", _execute_payload()),
        # no Sync here: capture the immediate post-Execute state.
    ]
    await _run(server, frames, monkeypatch)
    assert server._session_vars_local == {"app.region": "EMEA"}


async def test_describe_bound_commit_does_not_clear_local_early(monkeypatch):
    """Describing a bound COMMIT portal must NOT clear LOCAL — only the Execute
    of COMMIT ends the transaction."""
    _install_router_capture(monkeypatch)
    server = _make_server()
    server._session_vars_local = {"app.region": "EMEA"}
    frames = [
        ("P", _parse_payload("COMMIT")),
        ("B", _bind_payload()),
        ("D", _describe_portal_payload()),   # metadata only — must not clear
    ]
    await _run(server, frames, monkeypatch)
    assert server._session_vars_local == {"app.region": "EMEA"}


async def test_execute_after_describe_commit_clears_local(monkeypatch):
    _install_router_capture(monkeypatch)
    server = _make_server()
    server._session_vars_local = {"app.region": "EMEA"}
    frames = [
        ("P", _parse_payload("COMMIT")),
        ("B", _bind_payload()),
        ("D", _describe_portal_payload()),
        ("E", _execute_payload()),
        ("S", b""),
    ]
    await _run(server, frames, monkeypatch)
    assert server._session_vars_local == {}
