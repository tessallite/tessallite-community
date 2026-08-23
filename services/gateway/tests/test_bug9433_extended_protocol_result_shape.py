"""Bug-9433 / Bug-6776 — the described result shape must be the served one.

What was wrong
--------------
``_describe_columns_metadata_only`` answered ``NoData`` for any projection that
was not a bare (or bare-aliased) column. ``NoData`` means "this statement
returns no rows at all", so a client that treats the STATEMENT description as
authoritative — asyncpg, psycopg3, any prepared-statement BI driver — recorded
zero result columns and then failed. An aggregate could not be served over the
extended protocol at all, and neither could ``SELECT *``.

Bug-6776 filed the companion hazard as theoretical ("if derived typing ever
diverges from executed typing, the client decodes rows with a stale
descriptor"). It is not theoretical: ``proto.data_row`` encodes with the
EXECUTED type OID while the client decodes with the DESCRIBED one, so a
divergence hands back corrupted values rather than an error. Both are closed by
deriving the described shape and the served shape from ONE function
(``_projected_result_shape``) and failing closed when they cannot agree.

Why the tests use a real asyncpg client
---------------------------------------
The defect is invisible to a byte-level unit test that only inspects what the
gateway emits: the gateway ALSO sends a correct RowDescription at Execute, so
the bytes look fine. Only a client that fixes its decoders from the statement
description shows the failure. ``asyncpg`` is already a declared gateway
dependency (pyproject.toml), so this needs no new tech — it is the reference
PostgreSQL client, used here as the oracle. Startup/auth is performed by the
harness (real auth needs a live model-service); everything from the first
frontend message onward is the real production ``_query_loop``.

Execution scope: isolated (ephemeral loopback port, no DB, no live services).
Gate tier: T1 (producer/consumer contract — the PG wire protocol itself).
"""
from __future__ import annotations

import asyncio
import decimal
import struct

import asyncpg
import pytest

import src.jdbc.server as jdbc_server
from src.jdbc import protocol as proto
from src.jdbc.server import PGWireServer

_TABLE_COLUMNS = {
    "modely": [
        {"name": "account_type", "data_type": "text"},
        {"name": "amount", "data_type": "numeric"},
        {"name": "txns", "data_type": "integer"},
    ]
}


async def _harness_startup(reader, writer) -> None:
    """Answer SSLRequest + StartupMessage, then hand over to the real loop."""
    while True:
        header = await reader.readexactly(4)
        length = struct.unpack("!I", header)[0]
        body = await reader.readexactly(length - 4)
        if struct.unpack("!I", body[:4])[0] == 80877103:  # SSLRequest
            writer.write(b"N")
            await writer.drain()
            continue
        break
    writer.write(proto.startup_sequence(pid=4242, secret=1))
    await writer.drain()


def _make_handler():
    server = PGWireServer()
    server._model_names = ["modely"]
    server._table_columns = {k: list(v) for k, v in _TABLE_COLUMNS.items()}
    server._table_model_id = {"modely": "m-1"}
    server._table_persona_id = {"modely": None}
    server._table_include_hidden = {"modely": False}
    server._table_query_name = {"modely": "modely"}
    server._model_id = "m-1"
    server._jwt_token = "jwt"
    server._tenant_slug = "acme"

    async def _no_revalidate():
        return None

    server._revalidate_session = _no_revalidate
    return server


class _GatewayFixture:
    """A real gateway query loop on a loopback port, stubbed at the ROUTER."""

    def __init__(self, router_result):
        self.router_result = router_result
        self._server = None
        self.port = 0

    async def __aenter__(self):
        async def _fake_execute_query(**_kwargs):
            return self.router_result

        self._saved = jdbc_server.execute_query
        jdbc_server.execute_query = _fake_execute_query

        async def _handle(reader, writer):
            await _harness_startup(reader, writer)
            try:
                await _make_handler()._query_loop(reader, writer)
            except Exception:  # noqa: BLE001 — client disconnects end the loop
                pass
            finally:
                writer.close()

        self._server = await asyncio.start_server(_handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_exc):
        jdbc_server.execute_query = self._saved
        self._server.close()
        await self._server.wait_closed()

    async def fetch(self, sql):
        conn = await asyncio.wait_for(
            asyncpg.connect(
                host="127.0.0.1", port=self.port, user="u", database="d",
                password="p", ssl=False, statement_cache_size=0,
            ),
            timeout=15,
        )
        try:
            return await asyncio.wait_for(conn.fetch(sql), timeout=15)
        finally:
            await conn.close(timeout=5)


# ---------------------------------------------------------------------------
# Bug-9433 — the shapes a prepared-statement client could not receive at all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unaliased_aggregate_is_served_to_a_prepared_statement_client():
    """``SELECT COUNT(*)`` — the exact query the Community first-query gate had
    to avoid (deploy/community/.env.example) because the gateway described it as
    zero columns.

    Pre-fix failure (verified on the lane's base SHA): asyncpg raises
    ``ProtocolError: the number of columns in the result row (1) is different
    from what was described (0)``.
    """
    async with _GatewayFixture(
        {"columns": ["count"], "rows": [{"count": 100000}]}
    ) as gw:
        rows = await gw.fetch("SELECT COUNT(*) FROM modely")
    assert len(rows) == 1
    assert list(rows[0].keys()) == ["count"], (
        "the aggregate result column was not described"
    )
    assert rows[0]["count"] == "100000"


@pytest.mark.asyncio
async def test_aliased_aggregate_is_served_under_its_alias():
    async with _GatewayFixture(
        {"columns": ["total"], "rows": [{"total": "42.5"}]}
    ) as gw:
        rows = await gw.fetch("SELECT SUM(amount) AS total FROM modely")
    assert [dict(r) for r in rows] == [{"total": "42.5"}]


@pytest.mark.asyncio
async def test_select_star_is_served_to_a_prepared_statement_client():
    """``SELECT *`` is the commonest BI query shape and was equally broken.

    Pre-fix it also described as zero columns — a blast radius wider than the
    aggregate the bug was filed against.
    """
    async with _GatewayFixture(
        {
            "columns": ["account_type", "amount", "txns"],
            "rows": [{"account_type": "retail", "amount": "12.5", "txns": 3}],
        }
    ) as gw:
        rows = await gw.fetch("SELECT * FROM modely")
    assert len(rows) == 1
    assert list(rows[0].keys()) == ["account_type", "amount", "txns"]
    assert rows[0]["account_type"] == "retail"
    assert rows[0]["amount"] == decimal.Decimal("12.5")
    assert rows[0]["txns"] == 3


@pytest.mark.asyncio
async def test_named_projection_of_numeric_and_integer_round_trips_exactly():
    """Bug-6776's corruption path, closed.

    A NUMERIC column advertises OID 1700, so asyncpg binds a BINARY result
    format for it. Pre-fix the gateway silently downgraded NUMERIC to a text
    payload under that binary format code, and asyncpg failed with
    ``insufficient data in buffer``. The values must now arrive EXACTLY.
    """
    async with _GatewayFixture(
        {
            "columns": ["amount", "txns"],
            "rows": [
                {"amount": "12.5", "txns": 3},
                {"amount": "-0.001", "txns": -7},
                {"amount": "100000", "txns": 0},
            ],
        }
    ) as gw:
        rows = await gw.fetch("SELECT amount, txns FROM modely")
    assert [(r["amount"], r["txns"]) for r in rows] == [
        (decimal.Decimal("12.5"), 3),
        (decimal.Decimal("-0.001"), -7),
        (decimal.Decimal("100000"), 0),
    ]


@pytest.mark.asyncio
async def test_wide_numeric_reaches_the_client_exactly_l1_b1():
    """L1-B1 — a NUMERIC wider than 28 significant digits stays exact."""
    wide = [
        "999999999999999999999999999999.99",
        "12345678901234567890123456789.123456789",
        "-99999999999999999999999999999999999999",
        "0.1234567890123456789012345678901",
    ]
    async with _GatewayFixture(
        {
            "columns": ["amount"],
            "rows": [{"amount": value} for value in wide],
        }
    ) as gw:
        rows = await gw.fetch("SELECT amount FROM modely")
    assert [row["amount"] for row in rows] == [decimal.Decimal(value) for value in wide]


# ---------------------------------------------------------------------------
# The derivation itself — one function behind Describe and the served shape
# ---------------------------------------------------------------------------


def test_computed_projections_are_typed_text_like_the_execute_path():
    """Bug-5186 types every computed projection TEXT on the executed path, so
    the DESCRIBED type must be TEXT too — the two sides agree by rule, not by
    coincidence."""
    server = _make_handler()
    shape = server._describe_columns_metadata_only(
        "SELECT SUM(amount) AS total, account_type FROM modely GROUP BY account_type"
    )
    assert shape == [("total", proto.OID_TEXT), ("account_type", proto.OID_TEXT)]


def test_unaliased_expressions_get_postgresql_names():
    server = _make_handler()
    assert server._describe_columns_metadata_only("SELECT COUNT(*) FROM modely") == [
        ("count", proto.OID_TEXT)
    ]
    assert server._describe_columns_metadata_only("SELECT SUM(amount) FROM modely") == [
        ("sum", proto.OID_TEXT)
    ]
    assert server._describe_columns_metadata_only(
        "SELECT amount + 1 FROM modely"
    ) == [("?column?", proto.OID_TEXT)]


def test_shape_is_still_declined_when_it_cannot_be_derived():
    """The conservative path survives: a multi-relation join has no single
    catalogue to type from, so the executed result stays authoritative."""
    server = _make_handler()
    assert server._describe_columns_metadata_only(
        "SELECT a.account_type FROM modely a JOIN other b ON true"
    ) is None
    assert server._describe_columns_metadata_only(
        "SELECT table_name FROM information_schema.tables"
    ) is None


# ---------------------------------------------------------------------------
# Fail-closed reconciliation — Bug-6776
# ---------------------------------------------------------------------------


def test_arity_disagreement_fails_closed_rather_than_serving_rows():
    """If the executed result carries a different number of columns from the
    shape Describe advertised, the gateway must ERROR. Serving the rows anyway
    is the Bug-6776 path: the client decodes them against the descriptor it was
    given and gets garbage."""
    server = _make_handler()
    conformed, err = server._conform_result_shape(
        "SELECT account_type, amount FROM modely",
        [("account_type", proto.OID_TEXT)],  # executed: only ONE column
    )
    assert conformed is None
    assert err is not None
    assert err[1] == "42601"
    assert "2 column(s)" in err[0] and "1" in err[0]


def test_column_order_disagreement_fails_closed():
    """A name mismatch at a position the gateway did NOT derive as computed
    means the executed ORDER differs — relabelling would move values between
    columns, so it refuses instead."""
    server = _make_handler()
    conformed, err = server._conform_result_shape(
        "SELECT account_type, amount FROM modely",
        [("amount", proto.OID_NUMERIC), ("account_type", proto.OID_TEXT)],
    )
    assert conformed is None
    assert err is not None
    assert err[1] == "42601"


def test_conform_is_inert_when_the_shape_cannot_be_derived():
    """A catalogue query types itself; the conform step must not touch it."""
    server = _make_handler()
    executed = [("table_name", proto.OID_TEXT), ("n", proto.OID_INT4)]
    conformed, err = server._conform_result_shape(
        "SELECT table_name, n FROM information_schema.tables", executed,
    )
    assert err is None
    assert conformed == executed


def test_conform_relabels_a_computed_position_without_moving_values():
    """The one position the gateway is allowed to rename is a computed one:
    the connector names it (``count`` on PostgreSQL, ``f0_`` on BigQuery) and
    the gateway commits to one PostgreSQL-shaped answer on both."""
    server = _make_handler()
    conformed, err = server._conform_result_shape(
        "SELECT account_type, COUNT(*) FROM modely GROUP BY account_type",
        [("account_type", proto.OID_TEXT), ("f0_", proto.OID_TEXT)],
    )
    assert err is None
    assert conformed == [("account_type", proto.OID_TEXT), ("count", proto.OID_TEXT)]
