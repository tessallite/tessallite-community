"""Tests for embed token scope enforcement on query-router endpoints."""
from __future__ import annotations

import pytest
from fastapi import HTTPException
import inspect

from shared.auth.middleware import CurrentEmbedUser, CurrentUser

pytestmark = pytest.mark.unit


class TestIntrospectModuleImport:
    """Verify the introspect module can be imported without AttributeError."""

    def test_import_introspect_module(self):
        import src.api.introspect  # noqa: F401

    def test_mutating_node_types_are_valid_classes(self):
        from src.api.introspect import _MUTATING_NODE_TYPES
        for cls in _MUTATING_NODE_TYPES:
            assert hasattr(cls, "__mro__"), f"{cls} is not a valid class"


class TestLockedPersonaResolution:
    """F-008-18: the embed locked-persona contract is enforced by
    ``resolve_effective_persona`` (the dead ``resolve_embed_persona`` helper
    was deleted). These assert the locked-persona branch fails closed: a
    conflicting requested persona is rejected with 403, and a matching/absent
    one resolves to the locked persona."""

    @staticmethod
    def _resolver():
        from shared.security.persona_resolver import resolve_effective_persona
        return resolve_effective_persona

    async def test_rejects_conflicting_persona(self):
        from unittest.mock import AsyncMock
        from uuid import uuid4
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
            persona_id=str(uuid4()),
        )
        db = AsyncMock()
        with pytest.raises(HTTPException) as exc_info:
            await self._resolver()(
                db, current_user=user, model_id=uuid4(),
                requested_persona_id=str(uuid4()),  # different from locked
            )
        assert exc_info.value.status_code == 403

    async def test_uses_locked_persona_when_request_matches(self, monkeypatch):
        from unittest.mock import AsyncMock
        from uuid import uuid4
        import shared.security.persona_resolver as pr
        locked = str(uuid4())
        sentinel = object()
        monkeypatch.setattr(pr, "load_persona_or_fail", AsyncMock(return_value=sentinel))
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v", persona_id=locked,
        )
        db = AsyncMock()
        result = await self._resolver()(
            db, current_user=user, model_id=uuid4(),
            requested_persona_id=locked,
        )
        assert result is sentinel


class TestCapabilityGating:
    """Verify require_capability blocks embed users missing the capability."""

    async def test_chat_only_token_blocked_from_query(self):
        from shared.auth.middleware import require_capability
        dep = require_capability("query")
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
            capabilities=["chat"],
        )
        with pytest.raises(HTTPException) as exc_info:
            await dep(user)
        assert exc_info.value.status_code == 403
        assert "query" in exc_info.value.detail

    async def test_query_token_allowed(self):
        from shared.auth.middleware import require_capability
        dep = require_capability("query")
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
            capabilities=["query"],
        )
        result = await dep(user)
        assert result is user

    async def test_regular_user_always_allowed(self):
        from shared.auth.middleware import require_capability
        dep = require_capability("query")
        user = CurrentUser(
            user_id="u", tenant_id="t", email="u", role="member",
        )
        result = await dep(user)
        assert result is user


class TestDrillMeasureModelScope:
    """Verify _enforce_measure_model_scope resolves model_id and enforces scope."""

    async def test_blocks_out_of_scope_measure(self):
        from unittest.mock import AsyncMock, MagicMock
        from uuid import uuid4
        from src.api.drill_routes import _enforce_measure_model_scope
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
            model_ids=["model-1"],
        )
        measure_id = uuid4()
        db = AsyncMock()
        row = MagicMock()
        row.scalar_one_or_none.return_value = uuid4()
        db.execute.return_value = row
        with pytest.raises(HTTPException) as exc_info:
            await _enforce_measure_model_scope(db, user, measure_id)
        assert exc_info.value.status_code == 403

    async def test_allows_in_scope_measure(self):
        from unittest.mock import AsyncMock, MagicMock
        from uuid import UUID, uuid4
        from src.api.drill_routes import _enforce_measure_model_scope
        model_id = uuid4()
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
            model_ids=[str(model_id)],
        )
        measure_id = uuid4()
        db = AsyncMock()
        row = MagicMock()
        row.scalar_one_or_none.return_value = model_id
        db.execute.return_value = row
        await _enforce_measure_model_scope(db, user, measure_id)

    async def test_skips_for_regular_user(self):
        from unittest.mock import AsyncMock
        from uuid import uuid4
        from src.api.drill_routes import _enforce_measure_model_scope
        user = CurrentUser(
            user_id="u", tenant_id="t", email="u", role="member",
        )
        db = AsyncMock()
        await _enforce_measure_model_scope(db, user, uuid4())
        db.execute.assert_not_called()

    async def test_skips_when_no_model_restriction(self):
        from unittest.mock import AsyncMock
        from uuid import uuid4
        from src.api.drill_routes import _enforce_measure_model_scope
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
        )
        db = AsyncMock()
        await _enforce_measure_model_scope(db, user, uuid4())
        db.execute.assert_not_called()


class TestIntrospectReadOnly:
    """Verify _assert_read_only blocks mutating SQL."""

    def test_select_allowed(self):
        from src.api.introspect import _assert_read_only
        _assert_read_only("SELECT 1")

    def test_with_cte_allowed(self):
        from src.api.introspect import _assert_read_only
        _assert_read_only("WITH cte AS (SELECT 1) SELECT * FROM cte")

    def test_insert_blocked(self):
        from src.api.introspect import _assert_read_only
        with pytest.raises(HTTPException) as exc_info:
            _assert_read_only("INSERT INTO t VALUES (1)")
        assert exc_info.value.status_code == 422

    def test_drop_blocked(self):
        from src.api.introspect import _assert_read_only
        with pytest.raises(HTTPException) as exc_info:
            _assert_read_only("DROP TABLE t")
        assert exc_info.value.status_code == 422

    def test_update_blocked(self):
        from src.api.introspect import _assert_read_only
        with pytest.raises(HTTPException) as exc_info:
            _assert_read_only("UPDATE t SET x = 1")
        assert exc_info.value.status_code == 422

    def test_delete_blocked(self):
        from src.api.introspect import _assert_read_only
        with pytest.raises(HTTPException) as exc_info:
            _assert_read_only("DELETE FROM t")
        assert exc_info.value.status_code == 422

    def test_empty_sql_blocked(self):
        from src.api.introspect import _assert_read_only
        with pytest.raises(HTTPException) as exc_info:
            _assert_read_only("")
        assert exc_info.value.status_code == 422


class TestIntrospectModelScopeDenial:
    """Verify introspect honours enforce_model_scope for embed tokens."""

    def test_out_of_scope_model_blocked(self):
        from shared.auth.middleware import enforce_model_scope
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
            model_ids=["model-1"],
        )
        with pytest.raises(HTTPException) as exc_info:
            enforce_model_scope(user, "model-2")
        assert exc_info.value.status_code == 403

    def test_in_scope_model_allowed(self):
        from shared.auth.middleware import enforce_model_scope
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
            model_ids=["model-1"],
        )
        enforce_model_scope(user, "model-1")


class TestIntrospectCapabilityDenial:
    """Verify introspect uses require_capability('query')."""

    async def test_chat_only_token_blocked(self):
        from shared.auth.middleware import require_capability
        dep = require_capability("query")
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
            capabilities=["chat"],
        )
        with pytest.raises(HTTPException) as exc_info:
            await dep(user)
        assert exc_info.value.status_code == 403

    async def test_query_token_allowed(self):
        from shared.auth.middleware import require_capability
        dep = require_capability("query")
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
            capabilities=["query"],
        )
        result = await dep(user)
        assert result is user


class TestIntrospectReadOnlyDeep:
    """Verify _assert_read_only blocks mutating SQL hidden inside SELECT/WITH."""

    def test_select_into_blocked(self):
        from src.api.introspect import _assert_read_only
        with pytest.raises(HTTPException) as exc_info:
            _assert_read_only("SELECT * INTO new_table FROM users")
        assert exc_info.value.status_code == 422

    def test_writable_cte_delete_blocked(self):
        from src.api.introspect import _assert_read_only
        with pytest.raises(HTTPException) as exc_info:
            _assert_read_only(
                "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x"
            )
        assert exc_info.value.status_code == 422

    def test_writable_cte_update_blocked(self):
        from src.api.introspect import _assert_read_only
        with pytest.raises(HTTPException) as exc_info:
            _assert_read_only(
                "WITH x AS (UPDATE t SET a=1 RETURNING *) SELECT * FROM x"
            )
        assert exc_info.value.status_code == 422

    def test_multiple_statements_blocked(self):
        from src.api.introspect import _assert_read_only
        with pytest.raises(HTTPException) as exc_info:
            _assert_read_only("SELECT 1; DROP TABLE users")
        assert exc_info.value.status_code == 422

    def test_cte_with_insert_blocked(self):
        from src.api.introspect import _assert_read_only
        with pytest.raises(HTTPException) as exc_info:
            _assert_read_only(
                "WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x"
            )
        assert exc_info.value.status_code == 422


class TestIntrospectRouteWiring:
    """Verify /introspect routes are wired with capability/scope guards via router."""

    def _find_route(self, path_suffix: str):
        from src.api.introspect import router
        for route in router.routes:
            if hasattr(route, "path") and route.path == path_suffix:
                return route
        raise AssertionError(f"Route {path_suffix} not found in introspect router")

    def test_introspect_route_uses_require_capability(self):
        # F-021-06: introspect is the schema/data EXPLORE surface, so it gates
        # on the previously-inert "explore" embed capability (which makes that
        # capability actually enforce something). An embed token without
        # "explore" can no longer probe source schema/data.
        route = self._find_route("/introspect")
        deps = route.dependant.dependencies
        cap_funcs = [
            d.call for d in deps
            if d.call is not None and hasattr(d.call, "__wrapped_capability__")
        ]
        assert len(cap_funcs) == 1
        assert cap_funcs[0].__wrapped_capability__ == "explore"

    def test_introspect_batch_route_uses_require_capability(self):
        route = self._find_route("/introspect/batch")
        deps = route.dependant.dependencies
        cap_funcs = [
            d.call for d in deps
            if d.call is not None and hasattr(d.call, "__wrapped_capability__")
        ]
        assert len(cap_funcs) == 1
        assert cap_funcs[0].__wrapped_capability__ == "explore"

    def test_introspect_authorizes_model_before_source_execution(self):
        import src.api.introspect as introspect

        source = inspect.getsource(introspect.introspect_query)
        assert "load_authorized_model" in source
        assert source.index("load_authorized_model") < source.index("_resolve_model_connection")
        assert source.index("load_authorized_model") < source.index("execute_source_sql")

    def test_introspect_batch_authorizes_model_before_source_execution(self):
        import src.api.introspect as introspect

        source = inspect.getsource(introspect.introspect_batch)
        assert "load_authorized_model" in source
        assert source.index("load_authorized_model") < source.index("_resolve_model_connection")
        assert source.index("load_authorized_model") < source.index("execute_source_sql")


class TestTranspilePreviewSql:
    """Verify preview SQL transpilation for all connectors using PG-quoted input."""

    def test_sqlserver_limit_becomes_fetch(self):
        from shared.connector_qualify import transpile_preview_sql
        sql = 'SELECT * FROM "orders" LIMIT 51 OFFSET 0'
        result = transpile_preview_sql("sqlserver", sql)
        assert "LIMIT" not in result.upper()
        assert "FETCH" in result.upper() or "TOP" in result.upper()
        assert "[orders]" in result

    def test_snowflake_limit_preserved(self):
        from shared.connector_qualify import transpile_preview_sql
        sql = 'SELECT * FROM "orders" LIMIT 51 OFFSET 0'
        result = transpile_preview_sql("snowflake", sql)
        assert "LIMIT" in result.upper()

    def test_bigquery_uses_backticks(self):
        from shared.connector_qualify import transpile_preview_sql
        sql = 'SELECT * FROM "my_project"."dataset"."orders" LIMIT 51 OFFSET 0'
        result = transpile_preview_sql("bigquery", sql)
        assert "`" in result
        assert "LIMIT" in result.upper()

    def test_spark_uses_backticks(self):
        from shared.connector_qualify import transpile_preview_sql
        sql = 'SELECT * FROM "my_db"."orders" LIMIT 51 OFFSET 0'
        result = transpile_preview_sql("hadoop_spark", sql)
        assert "`" in result

    def test_postgresql_passthrough(self):
        from shared.connector_qualify import transpile_preview_sql
        sql = 'SELECT * FROM "orders" LIMIT 51 OFFSET 0'
        result = transpile_preview_sql("postgresql", sql)
        assert result == sql

    def test_redshift_passthrough(self):
        from shared.connector_qualify import transpile_preview_sql
        sql = 'SELECT * FROM "orders" LIMIT 51 OFFSET 0'
        result = transpile_preview_sql("redshift", sql)
        assert result == sql

    def test_full_table_preview_flow(self):
        """Simulate the exact table_preview.py call pattern."""
        from shared.connector_qualify import quote_table_ref, transpile_preview_sql
        pg_quoted = quote_table_ref("postgresql", "dbo.orders")
        canonical = f"SELECT * FROM {pg_quoted} LIMIT 51 OFFSET 0"
        result = transpile_preview_sql("sqlserver", canonical)
        assert "LIMIT" not in result.upper()
        assert "[dbo]" in result or "[orders]" in result


class TestHeadlessModelScope:
    def test_enforce_model_scope_blocks_out_of_scope(self):
        from shared.auth.middleware import enforce_model_scope
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
            model_ids=["m1"],
        )
        with pytest.raises(HTTPException) as exc_info:
            enforce_model_scope(user, "m2")
        assert exc_info.value.status_code == 403

    def test_enforce_model_scope_allows_in_scope(self):
        from shared.auth.middleware import enforce_model_scope
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
            model_ids=["m1", "m2"],
        )
        enforce_model_scope(user, "m1")
