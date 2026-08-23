"""Bug-6056 — a statement-level Describe of a ZERO-parameter statement must be
metadata-only, so the user query runs exactly ONCE.

The extended-protocol metadata-prefetch flow is:

    Parse  →  Describe('S')  →  Bind  →  Execute

Before the fix, the Describe branch only treated *parameterised* statements as
metadata-only; a zero-parameter statement fell through and executed the real
query at Describe. Bind then discarded the rows and Execute ran the query a
SECOND time — the user query ran twice (double source round trip, double
telemetry). These tests drive the real ``_query_loop`` with a byte stream and a
counting stub for ``_execute_for_extended`` to assert the run-once invariant
without a live source.
"""
from __future__ import annotations

import asyncio
import struct

from src.jdbc import protocol as proto
from src.jdbc.server import PGWireServer


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------


def _fe(type_char: str, payload: bytes = b"") -> bytes:
    """Frame a frontend message: type byte + int32 length + payload."""
    return type_char.encode() + struct.pack("!I", len(payload) + 4) + payload


def _parse_msg(sql: str, stmt: str = "") -> bytes:
    payload = stmt.encode() + b"\x00" + sql.encode() + b"\x00" + struct.pack("!H", 0)
    return _fe("P", payload)


def _describe_statement_msg() -> bytes:
    return _fe("D", b"S\x00")


def _bind_zero_param_msg() -> bytes:
    # portal "", stmt "", 0 format codes, 0 params, 0 result formats.
    payload = (
        b"\x00" + b"\x00"
        + struct.pack("!H", 0)
        + struct.pack("!H", 0)
        + struct.pack("!H", 0)
    )
    return _fe("B", payload)


def _execute_msg() -> bytes:
    return _fe("E", b"\x00" + struct.pack("!I", 0))


def _sync_msg() -> bytes:
    return _fe("S")


def _terminate_msg() -> bytes:
    return _fe("X")


class _CollectWriter:
    """Minimal asyncio.StreamWriter stand-in that records emitted bytes."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, data: bytes) -> None:
        self.buf.extend(data)

    async def drain(self) -> None:
        return None


def _reader_for(*messages: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for msg in messages:
        reader.feed_data(msg)
    reader.feed_eof()
    return reader


def _install_execution_counter(server: PGWireServer) -> list[int]:
    """Replace ``_execute_for_extended`` with a stub that counts calls and
    returns a fixed two-row result. Returns a single-element list holding the
    call count (mutable so the test can read it after the loop)."""
    count = [0]
    cols = [("id", proto.OID_INT4)]
    rows = [["1"], ["2"]]

    async def _fake_execute(sql: str):
        count[0] += 1
        return cols, list(rows), None

    server._execute_for_extended = _fake_execute  # type: ignore[assignment]
    return count


async def test_zero_param_statement_describe_runs_query_once():
    server = PGWireServer()
    count = _install_execution_counter(server)
    reader = _reader_for(
        _parse_msg("SELECT id FROM orders"),
        _describe_statement_msg(),
        _bind_zero_param_msg(),
        _execute_msg(),
        _sync_msg(),
        _terminate_msg(),
    )
    writer = _CollectWriter()

    await server._query_loop(reader, writer)

    # The core Bug-6056 invariant: the user query executed exactly once across
    # the whole Describe→Bind→Execute sequence (was twice before the fix).
    assert count[0] == 1, f"user query executed {count[0]} times, expected 1"

    # Metadata + rows are still delivered: the RowDescription carries the
    # column name and Execute reports the two-row result.
    assert b"id" in writer.buf, "RowDescription for the result was not emitted"
    assert b"SELECT 2" in writer.buf, "CommandComplete row count missing/incorrect"


async def test_statement_describe_without_bind_does_not_execute():
    # A pure metadata prefetch (Describe with no following Bind/Execute) must
    # never run the query at all.
    server = PGWireServer()
    count = _install_execution_counter(server)
    reader = _reader_for(
        _parse_msg("SELECT id FROM orders"),
        _describe_statement_msg(),
        _sync_msg(),
        _terminate_msg(),
    )
    writer = _CollectWriter()

    await server._query_loop(reader, writer)

    assert count[0] == 0, "statement-level Describe executed the query (Bug-6056)"


async def test_zero_param_simple_execute_runs_once():
    # The plain Parse→Bind→Execute path (no Describe) must also run exactly
    # once — the Bind-then-Execute deferral still resolves to a single run.
    server = PGWireServer()
    count = _install_execution_counter(server)
    reader = _reader_for(
        _parse_msg("SELECT id FROM orders"),
        _bind_zero_param_msg(),
        _execute_msg(),
        _sync_msg(),
        _terminate_msg(),
    )
    writer = _CollectWriter()

    await server._query_loop(reader, writer)

    assert count[0] == 1, f"user query executed {count[0]} times, expected 1"
    assert b"SELECT 2" in writer.buf
