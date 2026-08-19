"""Power BI / BI-tool SQL normalization tests.

Validates that _normalize_bi_sql() and _inject_group_by() correctly
transform BI-tool-generated SQL (table aliases, column aliases,
table-qualified columns, missing GROUP BY) into the form the
query-router expects.

These tests act as a regression net: if Npgsql or Power BI changes
its query patterns, the test suite catches the gap immediately.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_GATEWAY_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _GATEWAY_SRC not in sys.path:
    sys.path.insert(0, _GATEWAY_SRC)

from jdbc.server import (
    PGWireServer,
    _apply_projection_aliases,
    _normalize_bi_sql,
)


class TestNormalizeBiSql:
    """_normalize_bi_sql strips table aliases, column aliases, and qualifiers."""

    def test_power_bi_dollar_table(self):
        sql = (
            'SELECT "$Table"."account_type" AS "account_type", '
            '"$Table"."amount" AS "amount" '
            'FROM "project1"."modelx" AS "$Table"'
        )
        result = _normalize_bi_sql(sql)
        assert '"$Table"' not in result
        assert 'AS "account_type"' not in result
        assert '"account_type"' in result
        assert '"project1"."modelx"' in result

    def test_standard_alias(self):
        sql = 'SELECT t."col1" AS "col1" FROM "schema"."table" AS t'
        result = _normalize_bi_sql(sql)
        assert " AS t" not in result
        assert 't."col1"' not in result

    def test_identity_alias_stripped(self):
        sql = 'SELECT "col1" AS "col1", "col2" AS "col2" FROM "modelx"'
        result = _normalize_bi_sql(sql)
        assert 'AS "col1"' not in result
        assert 'AS "col2"' not in result

    def test_non_identity_alias_preserved_in_gateway_output(self):
        sql = 'SELECT "col1" AS "Customer Name", "col2" FROM "modelx"'
        columns, rows = _apply_projection_aliases(
            sql,
            [{"name": "col1", "type": "text"}, {"name": "col2", "type": "int"}],
            [{"col1": "Acme", "col2": 7}],
        )
        assert columns == [
            {"name": "Customer Name", "type": "text"},
            {"name": "col2", "type": "int"},
        ]
        assert rows == [{"Customer Name": "Acme", "col2": 7}]

    def test_no_alias_passthrough(self):
        sql = 'SELECT col1, col2 FROM modelx'
        result = _normalize_bi_sql(sql)
        assert "col1" in result
        assert "col2" in result

    def test_table_qualifier_stripped(self):
        sql = 'SELECT modelx.col1 FROM project1.modelx'
        result = _normalize_bi_sql(sql)
        assert "modelx.col1" not in result
        assert "col1" in result

    def test_where_clause_preserved(self):
        sql = (
            'SELECT "$Table"."col1" AS "col1" '
            'FROM "project1"."modelx" AS "$Table" '
            'WHERE "$Table"."col1" = \'value\''
        )
        result = _normalize_bi_sql(sql)
        assert '"$Table"' not in result
        assert "'value'" in result

    def test_limit_preserved(self):
        sql = (
            'SELECT "$Table"."col1" AS "col1" '
            'FROM "project1"."modelx" AS "$Table" LIMIT 100'
        )
        result = _normalize_bi_sql(sql)
        assert "LIMIT" in result.upper() or "limit" in result.lower()

    def test_non_select_passthrough(self):
        sql = "SET search_path TO public"
        assert _normalize_bi_sql(sql) == sql

    def test_invalid_sql_passthrough(self):
        sql = "NOT VALID SQL @@!!"
        assert _normalize_bi_sql(sql) == sql

    def test_existing_group_by_not_duplicated(self):
        sql = 'SELECT col1, col2 FROM modelx GROUP BY col1'
        result = _normalize_bi_sql(sql)
        assert result.upper().count("GROUP BY") == 1


class TestInjectGroupBy:
    """_inject_group_by adds GROUP BY for dimensions when measures present."""

    def _make_server_stub(self, table_columns):
        """Create a minimal object with _table_columns for testing."""
        class Stub:
            pass
        from jdbc.server import PGWireServer
        stub = Stub()
        stub._table_columns = table_columns
        stub._inject_group_by = PGWireServer._inject_group_by.__get__(stub)
        return stub

    def test_adds_group_by_for_dims(self):
        cols = [
            {"name": "region", "kind": "dimension"},
            {"name": "city", "kind": "dimension"},
            {"name": "revenue", "kind": "measure"},
        ]
        stub = self._make_server_stub({"modelx": cols})
        sql = 'SELECT region, city, revenue FROM modelx'
        result = stub._inject_group_by(sql)
        assert "GROUP BY" in result.upper()
        assert '"region"' in result
        assert '"city"' in result
        assert '"revenue"' not in result.split("GROUP BY")[1]

    def test_no_group_by_without_measures(self):
        cols = [
            {"name": "region", "kind": "dimension"},
            {"name": "city", "kind": "dimension"},
        ]
        stub = self._make_server_stub({"modelx": cols})
        sql = 'SELECT region, city FROM modelx'
        result = stub._inject_group_by(sql)
        assert "GROUP BY" not in result.upper()

    def test_existing_group_by_untouched(self):
        cols = [
            {"name": "region", "kind": "dimension"},
            {"name": "revenue", "kind": "measure"},
        ]
        stub = self._make_server_stub({"modelx": cols})
        sql = 'SELECT region, revenue FROM modelx GROUP BY region'
        result = stub._inject_group_by(sql)
        assert result.upper().count("GROUP BY") == 1

    def test_unknown_table_passthrough(self):
        stub = self._make_server_stub({})
        sql = 'SELECT col1 FROM unknown_table'
        result = stub._inject_group_by(sql)
        assert "GROUP BY" not in result.upper()

    def test_measures_only_no_group_by(self):
        cols = [
            {"name": "revenue", "kind": "measure"},
            {"name": "count", "kind": "measure"},
        ]
        stub = self._make_server_stub({"modelx": cols})
        sql = 'SELECT revenue, count FROM modelx'
        result = stub._inject_group_by(sql)
        assert "GROUP BY" not in result.upper()

    def test_case_insensitive_column_match(self):
        cols = [
            {"name": "Region", "kind": "dimension"},
            {"name": "Revenue", "kind": "measure"},
        ]
        stub = self._make_server_stub({"modelx": cols})
        sql = 'SELECT region, revenue FROM modelx'
        result = stub._inject_group_by(sql)
        assert "GROUP BY" in result.upper()


class TestUnreachableMeasureRegex:
    """_UNREACHABLE_MEASURE_RE matches multiple query-router error patterns."""

    def test_depends_on_fields(self):
        from jdbc.server import _UNREACHABLE_MEASURE_RE
        m = _UNREACHABLE_MEASURE_RE.search(
            "Base Amount YTD depends on fields that are not reachable in this model."
        )
        assert m is not None
        assert (m.group(1) or m.group(2)).strip() == "Base Amount YTD"

    def test_has_no_compatible_dimensions(self):
        from jdbc.server import _UNREACHABLE_MEASURE_RE
        m = _UNREACHABLE_MEASURE_RE.search(
            "Bsse amount plusCommession has no compatible dimensions in this model."
        )
        assert m is not None
        assert (m.group(1) or m.group(2)).strip() == "Bsse amount plusCommession"

    def test_aggregation_path(self):
        from jdbc.server import _UNREACHABLE_MEASURE_RE
        m = _UNREACHABLE_MEASURE_RE.search(
            "There is no aggregation path between NPS SCORE for_KPI (Trailing N) "
            "and branch code at the level of detail available for this measure."
        )
        assert m is not None
        name = (m.group(1) or m.group(2) or "").strip()
        assert name == "NPS SCORE for_KPI (Trailing N)"

    def test_no_match_on_unrelated_error(self):
        from jdbc.server import _UNREACHABLE_MEASURE_RE
        m = _UNREACHABLE_MEASURE_RE.search("relation 'demo_data.calendar' does not exist")
        assert m is None


class TestBug5580RetryIntent:
    def _make_server(self):
        server = PGWireServer()
        server._catalogue = None
        server._jwt_token = "token"
        server._tenant_slug = "tenant"
        server._client_kind = None
        server._looker_relations = {}
        server._tls_active = True
        server._session_vars = {}
        server._table_model_id = {"modelx": "model-1"}
        server._table_include_hidden = {"modelx": False}
        server._table_persona_id = {"modelx": None}
        server._table_columns = {
            "modelx": [
                {"name": "region", "kind": "dimension", "data_type": "text"},
                {"name": "revenue", "kind": "measure", "data_type": "numeric"},
                {"name": "cost", "kind": "measure", "data_type": "numeric"},
            ]
        }
        return server

    @pytest.mark.asyncio
    async def test_unreachable_measure_error_is_not_retried_with_measure_removed(self, monkeypatch):
        from jdbc import server as jdbc_server
        from src.router_client import QueryRouterError

        calls = []

        async def fake_execute_query(**kwargs):
            calls.append(kwargs["sql"])
            raise QueryRouterError(
                "Cost has no compatible dimensions in this model.",
                400,
                sqlstate="42601",
            )

        monkeypatch.setattr(jdbc_server, "execute_query", fake_execute_query)
        server = self._make_server()

        cols, rows, err = await server._execute_for_extended(
            "SELECT region, revenue, cost FROM modelx GROUP BY region"
        )

        assert cols is None
        assert rows is None
        assert err == ("Cost has no compatible dimensions in this model.", "42601")
        assert len(calls) == 1
        assert "cost" in calls[0].lower()


class TestBug5581ProjectionAliases:
    def _make_server(self):
        server = PGWireServer()
        server._catalogue = None
        server._jwt_token = "token"
        server._tenant_slug = "tenant"
        server._client_kind = None
        server._looker_relations = {}
        server._tls_active = True
        server._session_vars = {}
        server._table_model_id = {"modelx": "model-1"}
        server._table_include_hidden = {"modelx": False}
        server._table_persona_id = {"modelx": None}
        server._table_columns = {
            "modelx": [
                {"name": "region", "kind": "dimension", "data_type": "text"},
                {"name": "revenue", "kind": "measure", "data_type": "numeric"},
            ]
        }
        return server

    @pytest.mark.asyncio
    async def test_extended_query_preserves_direct_projection_aliases(self, monkeypatch):
        from jdbc import server as jdbc_server

        async def fake_execute_query(**kwargs):
            assert 'AS "Sales Region"' not in kwargs["sql"]
            return {
                "columns": ["region", "revenue"],
                "rows": [{"region": "EMEA", "revenue": 12.5}],
            }

        monkeypatch.setattr(jdbc_server, "execute_query", fake_execute_query)
        server = self._make_server()

        cols, rows, err = await server._execute_for_extended(
            'SELECT "region" AS "Sales Region", revenue FROM modelx GROUP BY region'
        )

        assert err is None
        assert [name for name, _oid in cols] == ["Sales Region", "revenue"]
        assert rows == [["EMEA", "12.5"]]


class TestBug5582KpiSecurityBoundary:
    def _make_server(self, persona_id=None, session_vars=None):
        server = PGWireServer()
        server._table_model_id = {"modelx$KPIs": "model-1"}
        server._table_include_hidden = {"modelx$KPIs": False}
        server._table_persona_id = {"modelx$KPIs": persona_id}
        server._session_vars = session_vars or {}
        return server

    def test_kpi_read_rejected_for_persona_context(self):
        # Defence-in-depth: if a non-null relation persona is ever supplied for a
        # $KPIs read, the guard still fails closed. NOTE: in production the $KPIs
        # relation always registers persona_id=None (see
        # test_kpi_read_relation_persona_is_none_in_production), so this branch is
        # not the real persona-enforcement path — persona/CLS gating happens in the
        # query-router. This only proves the guard does not open a hole if a
        # persona id leaks through.
        server = self._make_server(persona_id="persona-1")
        err = server._kpi_security_error('SELECT * FROM "modelx$KPIs"', "persona-1")
        assert err is not None
        assert "row-level security" in err

    def test_kpi_read_rejected_for_session_variable_context(self):
        # Real, gateway-detectable fail-closed case: a JDBC SET app.* filter cannot
        # be honoured against pre-aggregated kpi_latest rows, so the read is
        # rejected. This is the branch the guard actually protects at the gateway.
        server = self._make_server(session_vars={"app.region": "EMEA"})
        err = server._kpi_security_error('SELECT * FROM "modelx$KPIs"', None)
        assert err is not None
        assert "session-variable" in err

    def test_kpi_read_allowed_without_security_context(self):
        server = self._make_server()
        assert server._kpi_security_error('SELECT * FROM "modelx$KPIs"', None) is None

    def test_kpi_read_base_user_delegates_rls_to_router(self):
        """Bug-6930: a base user (persona_id=None, no session vars) is PERMITTED by
        the gateway guard — principal-based row-level security is invisible here and
        is enforced by the query-router's $KPIs handler, which withholds all KPI
        rows for a row-restricted principal (see
        services/query-router/tests/test_kpi_table_query.py::TestKpiTableQueryRowSecurity).

        This test documents the deliberate split: the gateway guard defends the
        session-variable case it CAN detect and delegates principal RLS to the
        router. It is not tautological — it asserts the gateway does NOT reject a
        legitimate unrestricted read (so the common BI-scorecard path keeps working)
        while the router closes the leak for restricted principals.
        """
        server = self._make_server(persona_id=None)
        assert (
            server._kpi_security_error('SELECT * FROM "modelx$KPIs"', None) is None
        )
        # But a session variable (which the router withhold does NOT cover) is
        # still rejected at the gateway.
        server._session_vars = {"app.region": "EMEA"}
        assert (
            server._kpi_security_error('SELECT * FROM "modelx$KPIs"', None) is not None
        )
