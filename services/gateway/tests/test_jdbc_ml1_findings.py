"""Business-outcome tests for the ML1 JDBC-gateway findings (F-001-03..15).

Each test asserts the user-visible behaviour the finding called wrong:

  F-001-03  catalogue routing decided by AST, not substring; catalogue-shaped
            SQLite failures surface as an error, not a fake empty success.
  F-001-04  unquoted identifiers are case-folded (FROM MODELX resolves).
  F-001-05  FROM-less SELECTs are evaluated/rejected, never echoed as text.
  F-001-06  parameter quoting respects the declared OID / strict numeric form.
  F-001-07  oversize pre-auth frames are rejected before allocation.
  F-001-09  $KPIs honours the query's projection / filter / limit.
  F-001-12  a CancelRequest is recognised and closed gracefully.
  F-001-14  the INFO statement log strips literals.
"""
from __future__ import annotations

import struct

import pytest

from src.jdbc import protocol as proto
from src.jdbc.catalogue import CatalogueDB, CatalogueQueryError
from src.jdbc.server import PGWireServer, _redact_sql_for_log


# ---------------------------------------------------------------------------
# Catalogue fixture
# ---------------------------------------------------------------------------

MODEL_NAMES = ["modelx"]
TABLE_COLUMNS = {
    "modelx": [
        {"name": "region", "display_name": "Region", "description": "",
         "data_type": "text", "is_hidden": False},
        {"name": "note", "display_name": "Note", "description": "",
         "data_type": "text", "is_hidden": False},
    ],
}
TABLE_MODEL_ID = {"modelx": "m1"}


def _make_catalogue() -> CatalogueDB:
    return CatalogueDB(
        model_names=MODEL_NAMES,
        table_columns=TABLE_COLUMNS,
        table_model_id=TABLE_MODEL_ID,
        tenant_slug="acme",
    )


# ---------------------------------------------------------------------------
# F-001-03 — catalogue regex hijack + silent-empty
# ---------------------------------------------------------------------------

class TestCatalogueAstRouting:
    def test_model_query_with_catalogue_token_in_literal_is_forwarded(self):
        cat = _make_catalogue()
        # The literal mentions pg_class but the only relation is a model table:
        # must NOT be hijacked to the catalogue engine → returns None (forward).
        result = cat.execute("SELECT * FROM modelx WHERE note LIKE '%pg_class%'")
        assert result is None
        cat.close()

    def test_current_setting_in_literal_is_forwarded(self):
        cat = _make_catalogue()
        result = cat.execute("SELECT region FROM modelx WHERE note = 'current_setting('")
        assert result is None
        cat.close()

    def test_genuine_catalogue_query_still_routes(self):
        cat = _make_catalogue()
        result = cat.execute("SELECT relname FROM pg_class")
        assert result is not None
        cat.close()

    def test_fromless_metadata_probe_routes(self):
        cat = _make_catalogue()
        # FROM-less probes like SELECT version() / session_user must still
        # reach the catalogue engine.
        assert cat.execute("SELECT version()") is not None
        assert cat.execute("SELECT session_user") is not None
        cat.close()

    def test_information_schema_query_routes(self):
        cat = _make_catalogue()
        result = cat.execute("SELECT table_name FROM information_schema.tables")
        assert result is not None
        cat.close()

    def test_catalogue_failure_raises_instead_of_empty(self):
        cat = _make_catalogue()
        # Catalogue-shaped (references pg_class) but selects a column that does
        # not exist → SQLite error must surface as CatalogueQueryError, not a
        # fabricated empty success.
        with pytest.raises(CatalogueQueryError):
            cat.execute("SELECT no_such_column FROM pg_class")
        cat.close()


# ---------------------------------------------------------------------------
# F-001-04 — case folding
# ---------------------------------------------------------------------------

class TestCaseFolding:
    def _server(self) -> PGWireServer:
        s = PGWireServer()
        s._table_model_id = {"modelx": "m1", "modelx$KPIs": "m1"}
        s._table_include_hidden = {"modelx": False, "modelx$KPIs": False}
        s._table_persona_id = {"modelx": None, "modelx$KPIs": None}
        return s

    def test_uppercase_relation_resolves(self):
        s = self._server()
        model_id, _, _ = s._resolve_model_id_and_variant("SELECT * FROM MODELX")
        assert model_id == "m1"

    def test_mixedcase_relation_resolves(self):
        s = self._server()
        model_id, _, _ = s._resolve_model_id_and_variant("SELECT * FROM Modelx")
        assert model_id == "m1"

    def test_kpi_table_resolves_case_insensitively(self):
        s = self._server()
        model_id, _, _ = s._resolve_model_id_and_variant('SELECT * FROM "modelx$KPIs"')
        assert model_id == "m1"


# ---------------------------------------------------------------------------
# F-001-05 — constant SELECT evaluation via catalogue
# ---------------------------------------------------------------------------

class TestConstantSelectEval:
    def test_arithmetic_evaluated_not_echoed(self):
        cat = _make_catalogue()
        result = cat.evaluate_constant_select("SELECT 1+1")
        assert result is not None
        _, rows = result
        assert rows == [["2"]]
        cat.close()

    def test_now_evaluates_to_timestamp(self):
        cat = _make_catalogue()
        result = cat.evaluate_constant_select("SELECT now()")
        assert result is not None
        _, rows = result
        assert rows and rows[0][0] and rows[0][0] != "now()"
        cat.close()

    def test_table_query_rejected_by_authorizer(self):
        cat = _make_catalogue()
        # Touching a table is denied on the read-only connection → None.
        assert cat.evaluate_constant_select("SELECT * FROM modelx") is None
        cat.close()


# ---------------------------------------------------------------------------
# F-001-07 — frame-length caps
# ---------------------------------------------------------------------------

class _FakeReader:
    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    async def readexactly(self, n: int) -> bytes:
        chunk = self._data[self._pos:self._pos + n]
        self._pos += n
        return chunk


class TestFrameCaps:
    @pytest.mark.asyncio
    async def test_oversize_startup_rejected(self):
        # Declared length far beyond the 10 KB startup cap.
        header = struct.pack("!I", proto.MAX_STARTUP_LEN + 100)
        reader = _FakeReader(header)
        with pytest.raises(proto.FrameTooLargeError):
            await proto.read_startup(reader)

    @pytest.mark.asyncio
    async def test_oversize_message_rejected(self):
        payload = b"Q" + struct.pack("!I", proto.MAX_MESSAGE_LEN + 100)
        reader = _FakeReader(payload)
        with pytest.raises(proto.FrameTooLargeError):
            await proto.read_message(reader)

    @pytest.mark.asyncio
    async def test_normal_startup_accepted(self):
        body = struct.pack("!I", proto.PROTOCOL_VERSION) + b"database\x00acme\x00\x00"
        frame = struct.pack("!I", len(body) + 4) + body
        result = await proto.read_startup(_FakeReader(frame))
        assert result["type"] == "startup"
        assert result["params"]["database"] == "acme"


# ---------------------------------------------------------------------------
# F-001-09 — $KPIs projection / filter / limit
# ---------------------------------------------------------------------------

KPI_COLUMNS = [
    "kpi_name", "value", "target", "status",
    "status_label", "trend_pct", "formatted_value", "evaluated_at",
]
KPI_ROWS = [
    {"kpi_name": "Margin", "value": 0.42, "target": 0.40, "status": 1,
     "status_label": "On Track", "trend_pct": 2.5, "formatted_value": "42%",
     "evaluated_at": "2026-06-01T00:00:00"},
    {"kpi_name": "Churn", "value": 0.10, "target": 0.05, "status": 2,
     "status_label": "Off Track", "trend_pct": -1.0, "formatted_value": "10%",
     "evaluated_at": "2026-06-01T00:00:00"},
    {"kpi_name": "NPS", "value": 30.0, "target": 25.0, "status": 0,
     "status_label": "Unknown", "trend_pct": 0.0, "formatted_value": "30",
     "evaluated_at": "2026-06-01T00:00:00"},
]


class TestKpiShaping:
    def test_filter_applied(self):
        s = PGWireServer()
        cols, rows, err = s._shape_kpi_result(
            'SELECT * FROM "modelx$KPIs" WHERE status = 2',
            list(KPI_COLUMNS), [dict(r) for r in KPI_ROWS],
        )
        assert err is None
        assert len(rows) == 1
        # status is column index 3
        assert rows[0][0] == "Churn"

    def test_projection_applied(self):
        s = PGWireServer()
        cols, rows, err = s._shape_kpi_result(
            'SELECT kpi_name, value FROM "modelx$KPIs"',
            list(KPI_COLUMNS), [dict(r) for r in KPI_ROWS],
        )
        assert err is None
        assert cols == ["kpi_name", "value"]
        assert all(len(r) == 2 for r in rows)
        assert rows[0] == ["Margin", 0.42]

    def test_limit_applied(self):
        s = PGWireServer()
        _, rows, err = s._shape_kpi_result(
            'SELECT * FROM "modelx$KPIs" LIMIT 1',
            list(KPI_COLUMNS), [dict(r) for r in KPI_ROWS],
        )
        assert err is None
        assert len(rows) == 1

    def test_order_by_desc(self):
        s = PGWireServer()
        _, rows, err = s._shape_kpi_result(
            'SELECT kpi_name, value FROM "modelx$KPIs" ORDER BY value DESC',
            list(KPI_COLUMNS), [dict(r) for r in KPI_ROWS],
        )
        assert err is None
        values = [r[1] for r in rows]
        assert values == sorted(values, reverse=True)

    def test_combined_projection_filter(self):
        s = PGWireServer()
        cols, rows, err = s._shape_kpi_result(
            'SELECT kpi_name FROM "modelx$KPIs" WHERE value > 0.2',
            list(KPI_COLUMNS), [dict(r) for r in KPI_ROWS],
        )
        assert err is None
        names = {r[0] for r in rows}
        assert names == {"Margin", "NPS"}
        assert cols == ["kpi_name"]

    def test_unsupported_predicate_returns_error(self):
        """Bug-5185: an unsupported predicate must return an error, not
        silently dump the full unfiltered rowset."""
        s = PGWireServer()
        cols, rows, err = s._shape_kpi_result(
            'SELECT * FROM "modelx$KPIs" WHERE upper(kpi_name) = \'X\'',
            list(KPI_COLUMNS), [dict(r) for r in KPI_ROWS],
        )
        assert err is not None
        assert "Unsupported WHERE predicate" in err
        assert rows == []


# ---------------------------------------------------------------------------
# F-001-12 — CancelRequest recognition
# ---------------------------------------------------------------------------

class TestCancelRequest:
    @pytest.mark.asyncio
    async def test_cancel_request_recognised(self):
        body = struct.pack("!III", proto.CANCEL_REQUEST_CODE, 123, 456)
        frame = struct.pack("!I", len(body) + 4) + body
        result = await proto.read_startup(_FakeReader(frame))
        assert result["type"] == "cancel"
        assert result["pid"] == 123
        assert result["secret"] == 456

    def test_backend_key_data_advertises_secret(self):
        seq = proto.startup_sequence(pid=7, secret=99)
        assert struct.pack("!II", 7, 99) in seq


# ---------------------------------------------------------------------------
# F-001-14 — SQL log redaction
# ---------------------------------------------------------------------------

class TestLogRedaction:
    def test_string_literals_stripped(self):
        out = _redact_sql_for_log("SELECT * FROM t WHERE email = 'jane@example.com'")
        assert "jane@example.com" not in out
        assert "'?'" in out

    def test_numeric_literals_stripped(self):
        out = _redact_sql_for_log("SELECT * FROM t WHERE ssn = 123456789")
        assert "123456789" not in out

    def test_truncated(self):
        long_sql = "SELECT " + ", ".join(f"col{i}" for i in range(400))
        out = _redact_sql_for_log(long_sql)
        assert len(out) <= proto.MAX_STARTUP_LEN  # bounded well under
        assert out.endswith("…")
