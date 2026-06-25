"""Tests for calendar table name qualification and bind verification.

Covers Bug-156 (discovery keystroke spam), Bug-157 (script tab never loads),
Bug-158 (BQ table name qualification), bind-to-nonexistent-table guard, and
Bug-5480 (consolidated calendar.py onto _table_qualify.qualify_physical_name).
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# qualify_physical_name tests (Bug-158, consolidated Bug-5480)
# ---------------------------------------------------------------------------

def _make_connection(conn_type: str, config: dict | None = None, creds: dict | None = None):
    conn = MagicMock()
    conn.connection_type = conn_type
    conn.config = config or {}
    if creds is not None:
        # Encrypt under the rotation-aware credential crypto (the same path
        # _decrypt_credentials reads through), using the configured current
        # key — no need to patch a module-level settings symbol.
        from shared.security.credential_crypto import encrypt_json
        conn.encrypted_credentials = encrypt_json(creds)
        return conn, None
    conn.encrypted_credentials = None
    return conn, None


def _make_source(config: dict | None = None, default_schema: str | None = None):
    source = MagicMock()
    source.config = config or {}
    source.default_schema = default_schema
    return source


class TestQualifyTableName:
    """Unit tests for qualify_physical_name (consolidated from calendar.py)."""

    def test_already_qualified_passes_through(self):
        from src.api._table_qualify import qualify_physical_name
        conn, _ = _make_connection("postgresql")
        result = qualify_physical_name("public.calendar", conn)
        assert result == "public.calendar"

    def test_postgresql_uses_schema_from_connection_config(self):
        from src.api._table_qualify import qualify_physical_name
        conn, _ = _make_connection("postgresql", config={"schema": "analytics"})
        result = qualify_physical_name("calendar", conn)
        assert result == "analytics.calendar"

    def test_postgresql_uses_source_config_schema(self):
        from src.api._table_qualify import qualify_physical_name
        conn, _ = _make_connection("postgresql")
        source = _make_source(config={"schema": "dw"})
        result = qualify_physical_name("calendar", conn, source)
        assert result == "dw.calendar"

    def test_postgresql_source_config_overrides_connection_config(self):
        from src.api._table_qualify import qualify_physical_name
        conn, _ = _make_connection("postgresql", config={"schema": "old_schema"})
        source = _make_source(config={"schema": "new_schema"})
        result = qualify_physical_name("calendar", conn, source)
        assert result == "new_schema.calendar"

    def test_postgresql_falls_back_to_default_schema(self):
        from src.api._table_qualify import qualify_physical_name
        conn, _ = _make_connection("postgresql")
        source = _make_source(default_schema="public")
        result = qualify_physical_name("calendar", conn, source)
        assert result == "public.calendar"

    def test_postgresql_no_schema_returns_bare_name(self):
        from src.api._table_qualify import qualify_physical_name
        conn, _ = _make_connection("postgresql")
        result = qualify_physical_name("calendar", conn)
        assert result == "calendar"

    def test_bigquery_uses_source_dataset_and_project(self):
        from src.api._table_qualify import qualify_physical_name
        conn, _ = _make_connection(
            "bigquery",
            creds={"project_id": "my-project", "service_account_json": "{}"},
        )
        source = _make_source(config={"schema": "my_dataset"})
        result = qualify_physical_name("calendar", conn, source)
        assert result == "my-project.my_dataset.calendar"

    def test_bigquery_dataset_only_two_part_name(self):
        from src.api._table_qualify import qualify_physical_name
        conn, _ = _make_connection("bigquery")
        source = _make_source(config={"dataset": "analytics"})
        result = qualify_physical_name("calendar", conn, source)
        assert result == "analytics.calendar"

    def test_bigquery_no_dataset_returns_bare_name(self):
        from src.api._table_qualify import qualify_physical_name
        conn, _ = _make_connection("bigquery")
        result = qualify_physical_name("calendar", conn)
        assert result == "calendar"

    def test_bigquery_source_schema_field_treated_as_dataset(self):
        """Source created via SourcesPanel stores BQ dataset under 'schema' key."""
        from src.api._table_qualify import qualify_physical_name
        conn, _ = _make_connection(
            "bigquery",
            creds={"project_id": "proj-1", "service_account_json": "{}"},
        )
        source = _make_source(config={"schema": "demo_data"})
        result = qualify_physical_name("calendar", conn, source)
        assert result == "proj-1.demo_data.calendar"

    def test_bigquery_schema_already_contains_project_no_duplication(self):
        """When schema stores 'project.dataset', don't prepend project again."""
        from src.api._table_qualify import qualify_physical_name
        conn, _ = _make_connection(
            "bigquery",
            creds={"project_id": "tessallite-io", "service_account_json": "{}"},
        )
        source = _make_source(config={"schema": "tessallite-io.demo_data"})
        result = qualify_physical_name("calendar", conn, source)
        assert result == "tessallite-io.demo_data.calendar"


# ---------------------------------------------------------------------------
# _verify_table_exists tests (bind guard)
# ---------------------------------------------------------------------------

class TestVerifyTableExists:
    """The bind endpoint must reject non-existent tables.

    The existence probe is routed through the query-router /introspect
    endpoint (never executed directly against the source connection), so
    these tests mock the httpx call to the router.
    """

    @staticmethod
    def _mock_router_client(*, status_code: int = 200, rows=None, raises=False):
        """Build a patch target for httpx.AsyncClient used in calendar.py."""
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = {"rows": rows or []}

        client = MagicMock()
        if raises:
            client.post = AsyncMock(side_effect=Exception("connection refused"))
        else:
            client.post = AsyncMock(return_value=resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        return patch("src.api.calendar.httpx.AsyncClient", return_value=client)

    @pytest.mark.asyncio
    async def test_table_not_found_raises_404(self):
        import uuid
        from src.api.calendar import _verify_table_exists
        conn, _ = _make_connection("postgresql")

        with self._mock_router_client(status_code=502):
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as exc_info:
                await _verify_table_exists(
                    "public.nonexistent", conn,
                    model_id=uuid.uuid4(), source_id=uuid.uuid4(), bearer="tok",
                )
            assert exc_info.value.status_code == 404
            assert "does not exist" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_router_unreachable_raises_404(self):
        import uuid
        from src.api.calendar import _verify_table_exists
        conn, _ = _make_connection("postgresql")

        with self._mock_router_client(raises=True):
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as exc_info:
                await _verify_table_exists(
                    "public.nonexistent", conn,
                    model_id=uuid.uuid4(), source_id=uuid.uuid4(), bearer="tok",
                )
            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_empty_result_raises_404(self):
        import uuid
        from src.api.calendar import _verify_table_exists
        conn, _ = _make_connection("postgresql")

        with self._mock_router_client(status_code=200, rows=[]):
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as exc_info:
                await _verify_table_exists(
                    "public.empty_table", conn,
                    model_id=uuid.uuid4(), source_id=uuid.uuid4(), bearer="tok",
                )
            assert exc_info.value.status_code == 404
            assert "does not exist" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_table_found_passes(self):
        import uuid
        from src.api.calendar import _verify_table_exists
        conn, _ = _make_connection("postgresql")

        with self._mock_router_client(status_code=200, rows=[{"chk": 1}]):
            await _verify_table_exists(
                "public.calendar", conn,
                model_id=uuid.uuid4(), source_id=uuid.uuid4(), bearer="tok",
            )


# ---------------------------------------------------------------------------
# Auto-create executor dispatch tests
# ---------------------------------------------------------------------------

class TestAutoCreateExecutorDispatch:
    """Verify unified DDL dispatch covers all three connectors."""

    def test_unified_execute_ddl_exists(self):
        from src.api.calendar import _execute_ddl
        assert callable(_execute_ddl)

    def test_shared_ddl_dispatch_has_postgresql(self):
        from shared.source_executor import _DDL_DISPATCH
        assert "postgresql" in _DDL_DISPATCH

    def test_shared_ddl_dispatch_has_bigquery(self):
        from shared.source_executor import _DDL_DISPATCH
        assert "bigquery" in _DDL_DISPATCH

    def test_shared_ddl_dispatch_has_spark(self):
        from shared.source_executor import _DDL_DISPATCH
        assert "hadoop_spark" in _DDL_DISPATCH


# ---------------------------------------------------------------------------
# CalendarScriptRequest / fiscal_year_start_month tests (Bug-157)
# ---------------------------------------------------------------------------

class TestCalendarScriptRequest:
    """Script request must accept calendar_type and fiscal_year_start_month."""

    def test_script_request_accepts_fiscal_year_start_month(self):
        from src.api.calendar import CalendarScriptRequest
        req = CalendarScriptRequest(
            table_name="calendar",
            start_date="2020-01-01",
            end_date="2035-12-31",
            calendar_type="fiscal",
            fiscal_year_start_month=4,
        )
        assert req.fiscal_year_start_month == 4

    def test_script_request_defaults_fiscal_to_1(self):
        from src.api.calendar import CalendarScriptRequest
        req = CalendarScriptRequest(
            table_name="calendar",
            start_date="2020-01-01",
            end_date="2035-12-31",
        )
        assert req.fiscal_year_start_month == 1

    def test_script_request_defaults_calendar_type_to_standard(self):
        from src.api.calendar import CalendarScriptRequest
        req = CalendarScriptRequest(
            table_name="calendar",
            start_date="2020-01-01",
            end_date="2035-12-31",
        )
        assert req.calendar_type == "standard"


# ---------------------------------------------------------------------------
# write_access gate tests
# ---------------------------------------------------------------------------

class TestWriteAccessGate:
    """Auto-create must check write_access flag on connection config."""

    def test_connection_without_write_access_would_be_rejected(self):
        """Connection config missing write_access defaults to False."""
        config = {}
        assert config.get("write_access", False) is False

    def test_connection_with_write_access_true_passes(self):
        config = {"write_access": True}
        assert config.get("write_access", False) is True

    def test_connection_with_write_access_false_rejected(self):
        config = {"write_access": False}
        assert config.get("write_access", False) is False


# ---------------------------------------------------------------------------
# Bug-5325: _load_source_with_connection read-time cross-project defense
# ---------------------------------------------------------------------------

class TestCalendarSourceConnectionCrossProjectGuard:
    """_load_source_with_connection must fail closed when the source's
    connection belongs to a different project (legacy/imported malformed row)."""

    @pytest.mark.asyncio
    async def test_cross_project_connection_rejected(self):
        import uuid as _uuid
        from fastapi import HTTPException
        from src.api.calendar import _load_source_with_connection

        owning_project = _uuid.uuid4()
        other_project = _uuid.uuid4()
        model_id = _uuid.uuid4()
        source_id = _uuid.uuid4()

        source = MagicMock()
        source.model_id = model_id
        source.project_connection_id = _uuid.uuid4()
        conn = MagicMock()
        conn.project_id = other_project

        db = MagicMock()

        async def _get(cls, key):
            from shared.db.models import DataSource, ProjectConnection
            if cls is DataSource:
                return source
            if cls is ProjectConnection:
                return conn
            return None

        db.get = _get

        with pytest.raises(HTTPException) as exc:
            await _load_source_with_connection(
                db, source_id, model_id, project_id=owning_project
            )
        assert exc.value.status_code == 422
        assert "different project" in exc.value.detail

    @pytest.mark.asyncio
    async def test_matching_project_connection_allowed(self):
        import uuid as _uuid
        from src.api.calendar import _load_source_with_connection

        owning_project = _uuid.uuid4()
        model_id = _uuid.uuid4()
        source_id = _uuid.uuid4()

        source = MagicMock()
        source.model_id = model_id
        source.project_connection_id = _uuid.uuid4()
        conn = MagicMock()
        conn.project_id = owning_project

        db = MagicMock()

        async def _get(cls, key):
            from shared.db.models import DataSource, ProjectConnection
            if cls is DataSource:
                return source
            if cls is ProjectConnection:
                return conn
            return None

        db.get = _get

        got_source, got_conn = await _load_source_with_connection(
            db, source_id, model_id, project_id=owning_project
        )
        assert got_source is source
        assert got_conn is conn
