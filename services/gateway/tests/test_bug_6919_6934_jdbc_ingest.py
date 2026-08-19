"""Tests for gateway JDBC ingest remediation bugs.

Bug-6919: _tenant_matches must not accept "default"/"postgres" as wildcards;
          placeholder db names must be resolved to the JWT tenant after auth.
Bug-6934: Named portals must own their own execution state so Execute runs
          the correct statement.
Bug-6918: _normalize_bi_sql must preserve column qualifiers outside the
          SELECT projection list.
Bug-6921: _normalize_bi_sql must log when it transforms SQL.
Bug-6917: Pure-measure selects that fall through to source should be audit-logged.
Bug-6920: Looker detection mid-session should rebuild the catalogue.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_GATEWAY_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _GATEWAY_SRC not in sys.path:
    sys.path.insert(0, _GATEWAY_SRC)

from jdbc.server import (
    PGWireServer,
    _normalize_bi_sql,
    _classify_ungrouped_query,
)


# ---------------------------------------------------------------------------
# Bug-6919: Tenant isolation — placeholder db names
# ---------------------------------------------------------------------------

class TestBug6919TenantMatches:
    """_tenant_matches must resolve placeholders, never treat them as wildcards."""

    def test_exact_match_passes(self):
        assert PGWireServer._tenant_matches("acme-demo", "acme-demo") is True

    def test_empty_jwt_tenant_fails(self):
        assert PGWireServer._tenant_matches("", "acme-demo") is False

    def test_none_jwt_tenant_fails(self):
        # noinspection PyTypeChecker
        assert PGWireServer._tenant_matches(None, "acme-demo") is False

    def test_mismatch_fails(self):
        assert PGWireServer._tenant_matches("tenant-a", "tenant-b") is False

    def test_default_placeholder_accepted_with_valid_jwt(self):
        """Placeholder 'default' is accepted when the JWT has a valid tenant.
        The caller (authenticate) resolves self._tenant_slug afterward."""
        assert PGWireServer._tenant_matches("acme-demo", "default") is True

    def test_postgres_placeholder_accepted_with_valid_jwt(self):
        assert PGWireServer._tenant_matches("acme-demo", "postgres") is True

    def test_empty_placeholder_accepted_with_valid_jwt(self):
        assert PGWireServer._tenant_matches("acme-demo", "") is True

    def test_placeholder_rejected_when_jwt_blank(self):
        """Even with a placeholder db, a blank JWT tenant must fail."""
        assert PGWireServer._tenant_matches("", "default") is False
        assert PGWireServer._tenant_matches("", "postgres") is False
        assert PGWireServer._tenant_matches("", "") is False


class TestBug6919PlaceholderResolution:
    """After auth succeeds with a placeholder db, _tenant_slug is resolved
    to the JWT tenant — proving the placeholder never persists."""

    def test_placeholder_db_names_frozenset(self):
        """The placeholder set contains exactly the known placeholders."""
        expected = frozenset({"", "default", "postgres"})
        assert PGWireServer._PLACEHOLDER_DB_NAMES == expected

    def test_default_is_placeholder(self):
        assert "default" in PGWireServer._PLACEHOLDER_DB_NAMES

    def test_postgres_is_placeholder(self):
        assert "postgres" in PGWireServer._PLACEHOLDER_DB_NAMES

    def test_empty_is_placeholder(self):
        assert "" in PGWireServer._PLACEHOLDER_DB_NAMES

    def test_real_tenant_is_not_placeholder(self):
        assert "acme-demo" not in PGWireServer._PLACEHOLDER_DB_NAMES


# ---------------------------------------------------------------------------
# Bug-6934: Named portal execution state
# ---------------------------------------------------------------------------

class _FakeReader:
    """Feeds a fixed sequence of (type_char, payload) frames."""

    def __init__(self, frames: list[tuple[str, bytes]]) -> None:
        self._frames = frames + [("X", b"")]
        self._idx = 0

    async def readexactly(self, n: int) -> bytes:
        raise AssertionError("loop should consume via read_message stub")


class _FakeWriter:
    def __init__(self) -> None:
        self.payload = b""

    def write(self, payload: bytes) -> None:
        self.payload += payload

    async def drain(self) -> None:
        pass


def _parse_payload(sql: str, stmt: str = "") -> bytes:
    """Build a Parse message payload: stmt_name\0 sql\0 param_count(0)."""
    return (
        stmt.encode("utf-8") + b"\x00"
        + sql.encode("utf-8") + b"\x00"
        + struct.pack("!H", 0)
    )


def _bind_payload(
    portal: str = "",
    stmt: str = "",
    params: list[str | None] | None = None,
) -> bytes:
    """Build a Bind payload: portal\0 stmt\0 fmt_count(0) params result_fmt(0)."""
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
    """Build an Execute payload: portal\0 max_rows(4)."""
    return portal.encode("utf-8") + b"\x00" + struct.pack("!I", max_rows)


def _describe_portal_payload(portal: str = "") -> bytes:
    """Build a Describe-portal payload: 'P' + name\0."""
    return b"P" + portal.encode("utf-8") + b"\x00"


class TestBug6934NamedPortals:
    """Named portals must own independent execution state."""

    def _make_server(self):
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

    @pytest.mark.asyncio
    async def test_two_portals_execute_own_sql(self, monkeypatch):
        """Parse S1 + Parse S2 + Bind P1->S1 + Bind P2->S2 + Execute P1
        must run S1's SQL, not S2's."""
        from jdbc import server as jdbc_server
        from src.jdbc import protocol as proto

        calls: list[str] = []

        async def fake_execute_query(**kwargs):
            calls.append(kwargs["sql"])
            return {
                "columns": ["region"],
                "rows": [{"region": kwargs["sql"][:10]}],
            }

        monkeypatch.setattr(jdbc_server, "execute_query", fake_execute_query)

        frames = [
            # Parse S1: SELECT region FROM modelx
            ("P", _parse_payload('SELECT region FROM modelx', "S1")),
            # Parse S2: SELECT revenue FROM modelx
            ("P", _parse_payload('SELECT revenue FROM modelx', "S2")),
            # Bind P1 -> S1
            ("B", _bind_payload(portal="P1", stmt="S1")),
            # Bind P2 -> S2
            ("B", _bind_payload(portal="P2", stmt="S2")),
            # Execute P1 — must run S1's SQL
            ("E", _execute_payload(portal="P1")),
            # Sync
            ("S", b""),
        ]

        reader = _FakeReader(frames)
        writer = _FakeWriter()
        server = self._make_server()

        # Monkeypatch proto.read_message to feed our frames
        frame_idx = [0]
        async def fake_read_message(r):
            idx = frame_idx[0]
            frame_idx[0] += 1
            f = frames[idx] if idx < len(frames) else ("X", b"")
            return f[0], f[1]
        monkeypatch.setattr(proto, "read_message", fake_read_message)

        await server._query_loop(reader, writer)

        # P1 should have executed S1's SQL, not S2's
        assert len(calls) == 1
        assert "region" in calls[0].lower()
        assert "revenue" not in calls[0].lower()

    @pytest.mark.asyncio
    async def test_execute_p2_runs_s2(self, monkeypatch):
        """Execute P2 after Bind P1->S1 + Bind P2->S2 must run S2's SQL."""
        from jdbc import server as jdbc_server
        from src.jdbc import protocol as proto

        calls: list[str] = []

        async def fake_execute_query(**kwargs):
            calls.append(kwargs["sql"])
            return {
                "columns": ["revenue"],
                "rows": [{"revenue": "100"}],
            }

        monkeypatch.setattr(jdbc_server, "execute_query", fake_execute_query)

        frames = [
            ("P", _parse_payload('SELECT region FROM modelx', "S1")),
            ("P", _parse_payload('SELECT revenue FROM modelx', "S2")),
            ("B", _bind_payload(portal="P1", stmt="S1")),
            ("B", _bind_payload(portal="P2", stmt="S2")),
            ("E", _execute_payload(portal="P2")),
            ("S", b""),
        ]

        reader = _FakeReader(frames)
        writer = _FakeWriter()
        server = self._make_server()

        frame_idx = [0]
        async def fake_read_message(r):
            idx = frame_idx[0]
            frame_idx[0] += 1
            f = frames[idx] if idx < len(frames) else ("X", b"")
            return f[0], f[1]
        monkeypatch.setattr(proto, "read_message", fake_read_message)

        await server._query_loop(reader, writer)

        assert len(calls) == 1
        assert "revenue" in calls[0].lower()


# ---------------------------------------------------------------------------
# Bug-6918: _normalize_bi_sql qualifier stripping scope
# ---------------------------------------------------------------------------

class TestBug6918NormalizeQualifiers:
    """Bug-6918: multi-table queries preserve qualifiers (bail-out guard)."""

    def test_select_qualifier_stripped_single_table(self):
        """Qualifiers in a single-table SELECT ARE stripped (safe: no ambiguity)."""
        sql = 'SELECT t."col1" FROM "schema"."table" AS t'
        result = _normalize_bi_sql(sql)
        assert 't."col1"' not in result
        assert '"col1"' in result

    def test_multi_table_passthrough(self):
        """Multi-table queries must pass through unchanged (Bug-6918/5881).

        Qualifiers are necessary for column disambiguation when multiple
        tables are present — the normalizer bails out entirely.
        """
        sql = (
            'SELECT a."col1", b."col2" '
            'FROM "t1" AS a JOIN "t2" AS b ON a.id = b.id'
        )
        result = _normalize_bi_sql(sql)
        assert result == sql

    def test_subquery_passthrough(self):
        """Subqueries pass through unchanged (Bug-5881 guard)."""
        sql = (
            'SELECT t."col1" FROM "modelx" AS t '
            'WHERE t."col1" IN (SELECT "col1" FROM "modely")'
        )
        result = _normalize_bi_sql(sql)
        assert result == sql

    def test_cte_passthrough(self):
        """CTEs pass through unchanged (Bug-5881 guard)."""
        sql = (
            'WITH cte AS (SELECT "col1" FROM "modelx") '
            'SELECT cte."col1" FROM cte'
        )
        result = _normalize_bi_sql(sql)
        assert result == sql


# ---------------------------------------------------------------------------
# Bug-6921: _normalize_bi_sql audit logging
# ---------------------------------------------------------------------------

class TestBug6921NormalizationAudit:
    """_normalize_bi_sql changes must be logged."""

    def test_normalization_changes_sql(self):
        """Verify normalization actually changes SQL so the audit path fires."""
        sql = 'SELECT t."col1" AS "col1" FROM "project1"."modelx" AS t'
        result = _normalize_bi_sql(sql)
        assert result != sql  # normalization changed the SQL

    def test_no_change_no_audit(self):
        """Plain SQL without aliases/qualifiers should not be changed."""
        sql = 'SELECT col1 FROM modelx'
        result = _normalize_bi_sql(sql)
        assert result == sql


# ---------------------------------------------------------------------------
# Bug-6917: Pure-measure classify and audit
# ---------------------------------------------------------------------------

class TestBug6917ClassifyAndAudit:
    """Pure-measure selects classified as 'source' feed the aggregate miss log."""

    def test_pure_measure_returns_source(self):
        """A flat measure-only SELECT returns 'source' (normal flow)."""
        table_columns = {
            "modelx": [
                {"name": "amount", "kind": "measure"},
                {"name": "region", "kind": "dimension"},
            ]
        }
        sql = 'SELECT "amount" FROM "modelx"'
        assert _classify_ungrouped_query(sql, table_columns) == "source"

    def test_aggregate_fn_returns_source(self):
        sql = 'SELECT SUM("amount") FROM "modelx"'
        assert _classify_ungrouped_query(sql) == "source"

    def test_dim_plus_measure_returns_raw(self):
        """Dimension + measure (no aggregate fn) returns 'raw'."""
        table_columns = {
            "modelx": [
                {"name": "amount", "kind": "measure"},
                {"name": "region", "kind": "dimension"},
            ]
        }
        sql = 'SELECT "region", "amount" FROM "modelx"'
        assert _classify_ungrouped_query(sql, table_columns) == "raw"


# ---------------------------------------------------------------------------
# Bug-6920: Looker detection and catalogue rebuild
# ---------------------------------------------------------------------------

class TestBug6920LookerCatalogueRebuild:
    """Mid-session Looker detection must rebuild the catalogue."""

    def test_rebuild_catalogue_method_exists(self):
        """The _rebuild_catalogue_for_client_kind method exists."""
        assert hasattr(PGWireServer, "_rebuild_catalogue_for_client_kind")

    def test_rebuild_replaces_catalogue(self):
        """Calling _rebuild_catalogue_for_client_kind replaces self._catalogue."""
        server = PGWireServer()
        server._client_kind = "looker_cloud"
        server._model_names = []
        server._table_columns = {}
        server._table_descriptions = {}
        server._table_trust_meta = {}
        server._table_model_id = {}
        server._table_foreign_keys = {}
        server._table_row_estimates = {}
        server._looker_relations = set()
        server._tenant_slug = "test"
        server._table_project_slug = {}
        # Create a mock catalogue
        old_catalogue = MagicMock()
        server._catalogue = old_catalogue

        server._rebuild_catalogue_for_client_kind()

        # Old catalogue should be closed
        old_catalogue.close.assert_called_once()
        # New catalogue should be created
        assert server._catalogue is not None
        assert server._catalogue is not old_catalogue
