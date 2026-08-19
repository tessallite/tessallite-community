"""H14 — JDBC gateway protocol fidelity.

F-001-01: a parameterised extended-protocol query must execute exactly once.
F-001-02: result columns must carry real PostgreSQL type OIDs (not all TEXT).
"""
from __future__ import annotations

import asyncio
import struct

import pytest

from src.jdbc import protocol as proto
from src.jdbc.server import PGWireServer, _map_type_oid


# ---------------------------------------------------------------------------
# Fake stream reader/writer driving the extended-query loop end to end
# ---------------------------------------------------------------------------


class _FakeReader:
    """Feeds a fixed sequence of (type_char, payload) frames into _query_loop."""

    def __init__(self, frames: list[tuple[str, bytes]]) -> None:
        # Trailing Terminate so the loop returns cleanly.
        self._frames = frames + [("X", b"")]
        self._idx = 0

    async def readexactly(self, n: int) -> bytes:  # pragma: no cover - unused
        raise AssertionError("loop should consume via read_message stub")


class _FakeWriter:
    def __init__(self) -> None:
        self.payload = b""

    def write(self, payload: bytes) -> None:
        self.payload += payload

    async def drain(self) -> None:
        pass


def _bind_payload(params: list[str | None]) -> bytes:
    # portal\0 stmt\0 fmt_count(0) param_count params... result_fmt_count(0)
    body = b"\x00\x00"  # empty portal + stmt names
    body += struct.pack("!H", 0)  # 0 format codes -> all text
    body += struct.pack("!H", len(params))
    for p in params:
        if p is None:
            body += struct.pack("!i", -1)
        else:
            enc = p.encode("utf-8")
            body += struct.pack("!I", len(enc)) + enc
    body += struct.pack("!H", 0)  # 0 result format codes
    return body


def _parse_payload(sql: str, stmt: str = "") -> bytes:
    return stmt.encode() + b"\x00" + sql.encode() + b"\x00" + struct.pack("!H", 0)


async def _run_loop(server: PGWireServer, frames: list[tuple[str, bytes]]) -> _FakeWriter:
    writer = _FakeWriter()
    reader = _FakeReader(frames)

    seq = list(reader._frames)
    it = iter(seq)

    async def _read_message(_reader):
        return next(it)

    # Patch protocol.read_message used inside _query_loop.
    import src.jdbc.server as srv

    orig = srv.proto.read_message
    srv.proto.read_message = _read_message  # type: ignore[assignment]
    try:
        await server._query_loop(reader, writer)
    finally:
        srv.proto.read_message = orig  # type: ignore[assignment]
    return writer


def _make_server(monkeypatch, call_counter: list, columns, rows):
    server = PGWireServer()
    server._jwt_token = "token"
    server._tenant_slug = "tenant"
    server._table_model_id = {"modelx": "m1"}
    server._table_include_hidden = {"modelx": False}
    server._table_persona_id = {"modelx": None}
    server._table_query_name = {}
    server._table_columns = {
        "modelx": [
            {"name": "region", "data_type": "text"},
            {"name": "revenue", "data_type": "numeric"},
            {"name": "txn_count", "data_type": "bigint"},
            {"name": "business_date", "data_type": "date"},
        ]
    }

    async def fake_execute(*_args, **kwargs):
        call_counter.append(kwargs.get("sql"))
        return {"columns": columns, "rows": rows}

    monkeypatch.setattr("src.jdbc.server.execute_query", fake_execute)
    return server


# ---------------------------------------------------------------------------
# F-001-01 — single execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bind_before_describe_executes_once(monkeypatch):
    """psycopg2/pgJDBC flow: Parse → Bind → Describe(portal) → Execute.

    The real query must run exactly once (at Describe) and Execute reuses rows.
    """
    calls: list = []
    server = _make_server(
        monkeypatch, calls,
        columns=["region", "revenue"],
        rows=[["EMEA", "800"]],
    )
    frames = [
        ("P", b"\x00SELECT region, revenue FROM modelx WHERE region = $1\x00"),
        ("B", _bind_payload(["EMEA"])),
        ("D", b"P"),  # describe portal
        ("E", b"\x00\x00\x00\x00\x00"),  # execute (portal name + max rows)
        ("S", b""),
    ]
    writer = await _run_loop(server, frames)

    assert len(calls) == 1, f"expected exactly one execution, got {len(calls)}: {calls}"
    # The single executed SQL had the real bound value, not NULL.
    assert "EMEA" in calls[0]
    assert "NULL" not in calls[0]


@pytest.mark.asyncio
async def test_statement_describe_before_bind_does_not_probe(monkeypatch):
    """Parse → Describe(statement) → Bind → Execute.

    The pre-Bind Describe must NOT execute a NULL probe; only Execute runs.
    """
    calls: list = []
    server = _make_server(
        monkeypatch, calls,
        columns=["region", "revenue"],
        rows=[["EMEA", "800"]],
    )
    frames = [
        ("P", b"\x00SELECT region, revenue FROM modelx WHERE region = $1\x00"),
        ("D", b"S"),  # describe statement (before bind)
        ("B", _bind_payload(["EMEA"])),
        ("E", b"\x00\x00\x00\x00\x00"),
        ("S", b""),
    ]
    writer = await _run_loop(server, frames)

    assert len(calls) == 1, f"expected exactly one execution, got {len(calls)}: {calls}"
    assert "EMEA" in calls[0]
    assert "NULL" not in calls[0]
    # A ParameterDescription was sent for the statement describe.
    assert b"t" in writer.payload[:1] or b"t" in writer.payload  # ParameterDescription type 't'


@pytest.mark.asyncio
async def test_describe_statement_emits_typed_metadata_without_execution(monkeypatch):
    """Statement describe of a simple projection types columns from catalogue."""
    calls: list = []
    server = _make_server(
        monkeypatch, calls,
        columns=["region", "revenue"],
        rows=[["EMEA", "800"]],
    )
    derived = server._describe_columns_metadata_only(
        "SELECT region, revenue FROM modelx WHERE region = $1"
    )
    assert derived is not None
    by_name = dict(derived)
    assert by_name["region"] == proto.OID_TEXT
    assert by_name["revenue"] == proto.OID_NUMERIC
    assert calls == []  # no execution


@pytest.mark.asyncio
async def test_describe_metadata_only_returns_none_for_star(monkeypatch):
    calls: list = []
    server = _make_server(monkeypatch, calls, columns=["region"], rows=[])
    assert server._describe_columns_metadata_only("SELECT * FROM modelx WHERE x=$1") is None
    assert server._describe_columns_metadata_only(
        "SELECT a.region FROM modelx a JOIN other b ON true WHERE region=$1"
    ) is None


# ---------------------------------------------------------------------------
# F-001-02 — typed result columns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_simple_query_columns_carry_real_oids(monkeypatch):
    calls: list = []
    server = _make_server(
        monkeypatch, calls,
        columns=["region", "revenue", "txn_count", "business_date"],
        rows=[["EMEA", "800.50", "12", "2024-01-01"]],
    )
    writer = _FakeWriter()
    await server._handle_user_query("SELECT region, revenue, txn_count, business_date FROM modelx", writer)

    # RowDescription frame ('T') must include the numeric/int/date OIDs, not text for all.
    payload = writer.payload
    assert struct.pack("!I", proto.OID_NUMERIC) in payload
    assert struct.pack("!I", proto.OID_INT8) in payload
    assert struct.pack("!I", proto.OID_DATE) in payload


@pytest.mark.asyncio
async def test_simple_query_value_normalisation_uses_catalogue_type_context(monkeypatch):
    """Bug-6054: row values are protected at the JDBC wire boundary.

    The gateway must derive numeric context from `_table_columns`, not from
    value shape. Text codes keep leading zeros, bigint strings keep exact
    precision, and numeric aggregate strings are normalised losslessly.
    """
    calls: list = []
    server = PGWireServer()
    server._jwt_token = "token"
    server._tenant_slug = "tenant"
    server._table_model_id = {"modelx": "m1"}
    server._table_include_hidden = {"modelx": False}
    server._table_persona_id = {"modelx": None}
    server._table_query_name = {}
    server._table_columns = {
        "modelx": [
            {"name": "code", "data_type": "text", "kind": "dimension"},
            {"name": "big_id", "data_type": "bigint", "kind": "dimension"},
            {"name": "count_value", "data_type": "numeric", "kind": "measure"},
        ],
    }

    async def fake_execute(*_args, **kwargs):
        calls.append(kwargs)
        return {
            "columns": ["code", "big_id", "count_value"],
            "rows": [{
                "code": "0042",
                "big_id": "9007199254740993",
                "count_value": "1.0E+5",
            }],
        }

    monkeypatch.setattr("src.jdbc.server.execute_query", fake_execute)

    writer = _FakeWriter()
    await server._handle_user_query(
        "SELECT code, big_id, count_value FROM modelx",
        writer,
    )

    assert len(calls) == 1
    payload = writer.payload
    assert b"0042" in payload
    assert b"9007199254740993" in payload
    assert b"100000" in payload
    assert b"1.0E+5" not in payload
    assert b"9007199254740992" not in payload


def test_type_columns_from_catalogue_promotes_names():
    server = PGWireServer()
    server._table_columns = {
        "modelx": [
            {"name": "revenue", "data_type": "numeric"},
            {"name": "region", "data_type": "text"},
        ]
    }
    typed = server._type_columns_from_catalogue(
        "SELECT revenue, region, computed_alias FROM modelx",
        ["revenue", "region", "computed_alias"],
    )
    assert typed[0] == {"name": "revenue", "type": "numeric"}
    assert typed[1] == {"name": "region", "type": "text"}
    # Unknown computed column falls back to text.
    assert typed[2] == {"name": "computed_alias", "type": "text"}


def test_map_type_oid_covers_date_and_int4():
    assert _map_type_oid("date") == proto.OID_DATE
    assert _map_type_oid("integer") == proto.OID_INT4
    assert _map_type_oid("bigint") == proto.OID_INT8
    assert _map_type_oid("timestamp") == proto.OID_TIMESTAMP
    assert _map_type_oid("unknown_thing") == proto.OID_TEXT


# ---------------------------------------------------------------------------
# Bug-6055 / F-001-18 - extended-protocol $KPIs shaping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extended_protocol_raw_kpis_where_is_shaped(monkeypatch):
    """Parse -> Bind -> Describe -> Execute must apply the $KPIs shaper.

    Standard JDBC clients use the extended protocol. A projected $KPIs query is
    classified as raw, but the router returns all KPI rows and columns by
    contract; the gateway must still apply projection and WHERE before emitting
    DataRows.
    """
    calls: list = []
    server = PGWireServer()
    server._jwt_token = "token"
    server._tenant_slug = "tenant"
    server._table_model_id = {"modelx$KPIs": "m1"}
    server._table_include_hidden = {"modelx$KPIs": False}
    server._table_persona_id = {"modelx$KPIs": None}
    server._table_query_name = {}
    server._table_columns = {
        "modelx$KPIs": [
            {"name": "kpi_name", "data_type": "text", "kind": "dimension"},
            {"name": "value", "data_type": "numeric", "kind": "measure"},
            {"name": "status", "data_type": "integer", "kind": "dimension"},
            {"name": "goal", "data_type": "numeric", "kind": "measure"},
        ],
    }

    async def fake_execute(*_args, **kwargs):
        calls.append(kwargs)
        return {
            "columns": ["kpi_name", "value", "status", "goal"],
            "rows": [
                {"kpi_name": "On Target", "value": "1.0E+5", "status": 1, "goal": 90000},
                {"kpi_name": "Needs Attention", "value": "42.0", "status": 0, "goal": 100},
            ],
        }

    monkeypatch.setattr("src.jdbc.server.execute_query", fake_execute)

    sql = 'SELECT kpi_name, value FROM "modelx$KPIs" WHERE status = 0'
    frames = [
        ("P", _parse_payload(sql)),
        ("B", _bind_payload([])),
        ("D", b"P"),
        ("E", b"\x00\x00\x00\x00\x00"),
        ("S", b""),
    ]
    writer = await _run_loop(server, frames)

    assert len(calls) == 1
    assert calls[0].get("force_route") == "raw"
    payload = writer.payload
    assert b"Needs Attention" in payload
    assert b"On Target" not in payload
    assert b"goal" not in payload
    assert b"status" not in payload
    assert b"SELECT 1" in payload
