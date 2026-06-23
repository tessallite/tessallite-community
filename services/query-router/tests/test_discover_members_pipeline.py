"""Tests for the discoverMembers pipeline rewrite.

Validates that POST /discover/members routes through the central
LogicalQuery -> bind -> persona gate -> route -> execute -> log pipeline,
ensuring persona default filters, row-security, dialect translation,
and post-execution logging all work correctly.
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from jose import jwt

from shared.config.settings import get_settings
from src.ir.logical_query import BoundQuery, LogicalQuery, RouteDecision, SelectExpression


_settings = get_settings()
_MODEL_ID = str(uuid.uuid4())


def _mint_jwt(role: str = "member") -> str:
    payload = {
        "sub": "analyst@acme.test",
        "tenant_id": "acme",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        "role": role,
    }
    return jwt.encode(payload, _settings.JWT_SECRET_KEY, algorithm=_settings.JWT_ALGORITHM)


def _auth() -> dict:
    return {"Authorization": f"Bearer {_mint_jwt()}"}


def _async_gen(db):
    async def _gen(*args, **kwargs):
        yield db
    return _gen


def _make_model(model_id: str = _MODEL_ID):
    return types.SimpleNamespace(
        id=uuid.UUID(model_id),
        slug="test-model",
        display_name="Test Model",
        deployed_version_id="v1",
        status="active",
        aggregations_enabled=True,
    )


def _make_dimension(name: str, *, dim_id: str | None = None):
    return types.SimpleNamespace(
        id=dim_id or f"d-{name}",
        name=name,
        source_column_id=f"col-{name}",
        user_defined_attribute_id=None,
        is_invalid=False,
    )


def _make_persona(
    *,
    pid: uuid.UUID | None = None,
    default_filters: dict | None = None,
    bypass_row_security: bool = False,
    included_dimension_ids: list | None = None,
):
    return types.SimpleNamespace(
        id=pid or uuid.uuid4(),
        model_id=uuid.UUID(_MODEL_ID),
        name="Sales",
        included_measure_ids=[],
        included_dimension_ids=included_dimension_ids or [],
        included_hierarchy_ids=[],
        audience_roles=[],
        default_filters=default_filters or {},
        includes_hidden_columns=False,
        bypass_row_security=bypass_row_security,
    )


def _make_bound(dim_name: str = "Region", model=None):
    m = model or _make_model()
    dim = _make_dimension(dim_name)
    lq = LogicalQuery(
        model_id=_MODEL_ID,
        protocol="discover_members",
        raw_query=f"DISCOVER_MEMBERS({dim_name})",
        requested_measures=[],
        requested_dimensions=[dim_name],
        filters=[],
        grain=[],
        order_by=[(dim_name, "asc")],
        limit=1000,
        offset=None,
        query_fingerprint="test_fp",
        has_distinct=True,
        select_expressions=[
            SelectExpression(
                raw_text=dim_name,
                alias=None,
                classification="passthrough",
                agg_function=None,
                inner_column=dim_name,
                inner_literal=None,
            ),
        ],
    )
    return BoundQuery(
        logical_query=lq,
        model=m,
        resolved_measures=[],
        resolved_dimensions=[dim],
        resolved_filters=[],
        resolved_dimensions_by_name={dim_name: dim},
    )


def _make_decision(sql: str = 'SELECT DISTINCT "Region" FROM "fact" ORDER BY "Region" LIMIT 1000'):
    return RouteDecision(
        route_type="source",
        rewritten_query=sql,
        reason="force_route=source",
        aggregate_id=None,
        pocket_id=None,
    )


@pytest.fixture
async def client():
    from src.main import app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as ac:
        yield ac


class TestDiscoverMembersPipeline:
    @pytest.mark.asyncio
    async def test_routes_through_central_pipeline(self, client):
        """bind, route, execute, log are all called in sequence."""
        bound = _make_bound()
        decision = _make_decision()
        rows = [{"Region": "US"}, {"Region": "UK"}]
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(
            all=MagicMock(return_value=[]),
            scalar_one_or_none=MagicMock(return_value=None),
        ))
        db.flush = AsyncMock()

        mock_bind = AsyncMock(return_value=bound)
        mock_route = AsyncMock(return_value=decision)
        mock_execute = AsyncMock(return_value=(rows, 0, ["Region"], MagicMock()))
        mock_log = AsyncMock()
        mock_persona_resolve = AsyncMock(return_value=None)

        with (
            patch("src.api.routes.get_tenant_db", _async_gen(db)),
            patch("src.api.routes.load_authorized_model", AsyncMock(return_value=None)),
            patch("src.api.routes.resolve_execution_persona", mock_persona_resolve),
            patch("src.api.routes.bind_query_to_model", mock_bind),
            patch("src.api.routes.apply_persona_gate", AsyncMock(return_value=None)),
            patch("src.api.routes.route_query", mock_route),
            patch("src.api.routes.execute_routed_query", mock_execute),
            patch("src.api.routes.log_query", mock_log),
        ):
            resp = await client.post(
                "/api/v1/discover/members",
                json={"model_id": _MODEL_ID, "dimension_name": "Region"},
                headers=_auth(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["members"]) == 2
        assert data["members"][0]["name"] == "US"
        assert data["members"][1]["name"] == "UK"

        mock_bind.assert_awaited_once()
        mock_route.assert_awaited_once()
        route_kwargs = mock_route.call_args
        assert route_kwargs.kwargs.get("force_route") == "source"
        assert route_kwargs.kwargs.get("principal") is not None

        mock_execute.assert_awaited_once()
        mock_log.assert_awaited_once()
        log_kwargs = mock_log.call_args.kwargs
        assert log_kwargs["rows_returned"] == 2
        assert log_kwargs["user_identity"] == "analyst@acme.test"

    @pytest.mark.asyncio
    async def test_persona_default_filters_merged(self, client):
        """merge_default_filters is called when persona has defaults."""
        bound = _make_bound()
        decision = _make_decision()
        persona = _make_persona(default_filters={"Country": "US"})
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(
            all=MagicMock(return_value=[]),
            scalar_one_or_none=MagicMock(return_value=None),
        ))
        db.flush = AsyncMock()

        mock_merge = MagicMock(return_value=["Country"])

        with (
            patch("src.api.routes.get_tenant_db", _async_gen(db)),
            patch("src.api.routes.load_authorized_model", AsyncMock(return_value=None)),
            patch("src.api.routes.resolve_execution_persona", AsyncMock(return_value=persona)),
            patch("src.api.routes.bind_query_to_model", AsyncMock(return_value=bound)),
            patch("src.api.routes.apply_persona_gate", AsyncMock(return_value=persona)),
            patch("src.api.routes.merge_default_filters", mock_merge),
            patch("src.api.routes.route_query", AsyncMock(return_value=decision)),
            patch("src.api.routes.execute_routed_query", AsyncMock(return_value=([], 0, ["Region"], MagicMock()))),
            patch("src.api.routes.log_query", AsyncMock()),
        ):
            resp = await client.post(
                "/api/v1/discover/members",
                json={"model_id": _MODEL_ID, "dimension_name": "Region"},
                headers=_auth(),
            )

        assert resp.status_code == 200
        mock_merge.assert_called_once_with(persona, bound)

    @pytest.mark.asyncio
    async def test_row_security_applied_via_route(self, client):
        """Principal is passed to route_query for row-security compilation."""
        bound = _make_bound()
        decision = _make_decision()
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(
            all=MagicMock(return_value=[]),
            scalar_one_or_none=MagicMock(return_value=None),
        ))
        db.flush = AsyncMock()

        mock_route = AsyncMock(return_value=decision)

        with (
            patch("src.api.routes.get_tenant_db", _async_gen(db)),
            patch("src.api.routes.load_authorized_model", AsyncMock(return_value=None)),
            patch("src.api.routes.resolve_execution_persona", AsyncMock(return_value=None)),
            patch("src.api.routes.bind_query_to_model", AsyncMock(return_value=bound)),
            patch("src.api.routes.apply_persona_gate", AsyncMock(return_value=None)),
            patch("src.api.routes.route_query", mock_route),
            patch("src.api.routes.execute_routed_query", AsyncMock(return_value=([], 0, [], MagicMock()))),
            patch("src.api.routes.log_query", AsyncMock()),
        ):
            resp = await client.post(
                "/api/v1/discover/members",
                json={"model_id": _MODEL_ID, "dimension_name": "Region"},
                headers=_auth(),
            )

        assert resp.status_code == 200
        principal = mock_route.call_args.kwargs["principal"]
        assert principal is not None
        assert principal.user_identity == "analyst@acme.test"

    @pytest.mark.asyncio
    async def test_log_query_called_after_execution(self, client):
        """QueryLog is written post-execution with real elapsed/row values."""
        bound = _make_bound()
        decision = _make_decision()
        rows = [{"Region": "US"}, {"Region": "UK"}, {"Region": "DE"}]
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(
            all=MagicMock(return_value=[]),
            scalar_one_or_none=MagicMock(return_value=None),
        ))
        db.flush = AsyncMock()

        mock_log = AsyncMock()

        with (
            patch("src.api.routes.get_tenant_db", _async_gen(db)),
            patch("src.api.routes.load_authorized_model", AsyncMock(return_value=None)),
            patch("src.api.routes.resolve_execution_persona", AsyncMock(return_value=None)),
            patch("src.api.routes.bind_query_to_model", AsyncMock(return_value=bound)),
            patch("src.api.routes.apply_persona_gate", AsyncMock(return_value=None)),
            patch("src.api.routes.route_query", AsyncMock(return_value=decision)),
            patch("src.api.routes.execute_routed_query", AsyncMock(return_value=(rows, 1024, ["Region"], MagicMock()))),
            patch("src.api.routes.log_query", mock_log),
        ):
            resp = await client.post(
                "/api/v1/discover/members",
                json={"model_id": _MODEL_ID, "dimension_name": "Region"},
                headers=_auth(),
            )

        assert resp.status_code == 200
        mock_log.assert_awaited_once()
        log_kw = mock_log.call_args.kwargs
        assert log_kw["rows_returned"] == 3
        assert log_kw["bytes_processed"] == 1024
        assert log_kw["execution_ms"] >= 0

    @pytest.mark.asyncio
    async def test_unknown_dimension_returns_empty(self, client):
        """SemanticBindingError from binder returns empty response, not 500."""
        from src.ir.logical_query import SemanticBindingError
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(
            all=MagicMock(return_value=[]),
            scalar_one_or_none=MagicMock(return_value=None),
        ))

        with (
            patch("src.api.routes.get_tenant_db", _async_gen(db)),
            patch("src.api.routes.load_authorized_model", AsyncMock(return_value=None)),
            patch("src.api.routes.resolve_execution_persona", AsyncMock(return_value=None)),
            patch("src.api.routes.bind_query_to_model", AsyncMock(
                side_effect=SemanticBindingError("Unknown column: 'FakeDim'")
            )),
        ):
            resp = await client.post(
                "/api/v1/discover/members",
                json={"model_id": _MODEL_ID, "dimension_name": "FakeDim"},
                headers=_auth(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["members"] == []
        assert data["levels"] == []

    @pytest.mark.asyncio
    async def test_undeployed_model_returns_409(self, client):
        """ModelNotDeployedError surfaces as HTTP 409."""
        from src.ir.logical_query import ModelNotDeployedError
        db = AsyncMock()

        with (
            patch("src.api.routes.get_tenant_db", _async_gen(db)),
            patch("src.api.routes.load_authorized_model", AsyncMock(return_value=None)),
            patch("src.api.routes.resolve_execution_persona", AsyncMock(return_value=None)),
            patch("src.api.routes.bind_query_to_model", AsyncMock(
                side_effect=ModelNotDeployedError("Model not deployed")
            )),
        ):
            resp = await client.post(
                "/api/v1/discover/members",
                json={"model_id": _MODEL_ID, "dimension_name": "Region"},
                headers=_auth(),
            )

        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_logical_query_has_distinct_and_limit(self, client):
        """Synthetic LogicalQuery uses has_distinct=True and the configured
        member-discovery limit (Bug-5436a: was a hard-coded 1000)."""
        bound = _make_bound()
        decision = _make_decision()
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(
            all=MagicMock(return_value=[]),
            scalar_one_or_none=MagicMock(return_value=None),
        ))
        db.flush = AsyncMock()

        mock_bind = AsyncMock(return_value=bound)

        with (
            patch("src.api.routes.get_tenant_db", _async_gen(db)),
            patch("src.api.routes.load_authorized_model", AsyncMock(return_value=None)),
            patch("src.api.routes.resolve_execution_persona", AsyncMock(return_value=None)),
            patch("src.api.routes.bind_query_to_model", mock_bind),
            patch("src.api.routes.apply_persona_gate", AsyncMock(return_value=None)),
            patch("src.api.routes.route_query", AsyncMock(return_value=decision)),
            patch("src.api.routes.execute_routed_query", AsyncMock(return_value=([], 0, [], MagicMock()))),
            patch("src.api.routes.log_query", AsyncMock()),
        ):
            resp = await client.post(
                "/api/v1/discover/members",
                json={"model_id": _MODEL_ID, "dimension_name": "Region"},
                headers=_auth(),
            )

        assert resp.status_code == 200
        lq = mock_bind.call_args.args[0]
        assert isinstance(lq, LogicalQuery)
        assert lq.has_distinct is True
        from shared.config.settings import get_settings
        assert lq.limit == get_settings().MEMBER_DISCOVERY_LIMIT
        assert lq.requested_measures == []
        assert lq.requested_dimensions == ["Region"]
        assert lq.protocol == "discover_members"

    @pytest.mark.asyncio
    async def test_persona_id_passed_to_log(self, client):
        """When persona is active, persona_id is included in log_query."""
        bound = _make_bound()
        decision = _make_decision()
        pid = uuid.uuid4()
        persona = _make_persona(pid=pid, default_filters={})
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(
            all=MagicMock(return_value=[]),
            scalar_one_or_none=MagicMock(return_value=None),
        ))
        db.flush = AsyncMock()

        mock_log = AsyncMock()

        with (
            patch("src.api.routes.get_tenant_db", _async_gen(db)),
            patch("src.api.routes.load_authorized_model", AsyncMock(return_value=None)),
            patch("src.api.routes.resolve_execution_persona", AsyncMock(return_value=persona)),
            patch("src.api.routes.bind_query_to_model", AsyncMock(return_value=bound)),
            patch("src.api.routes.apply_persona_gate", AsyncMock(return_value=persona)),
            patch("src.api.routes.merge_default_filters", MagicMock(return_value=[])),
            patch("src.api.routes.route_query", AsyncMock(return_value=decision)),
            patch("src.api.routes.execute_routed_query", AsyncMock(return_value=([], 0, [], MagicMock()))),
            patch("src.api.routes.log_query", mock_log),
        ):
            resp = await client.post(
                "/api/v1/discover/members",
                json={"model_id": _MODEL_ID, "dimension_name": "Region"},
                headers=_auth(),
            )

        assert resp.status_code == 200
        mock_log.assert_awaited_once()
        assert mock_log.call_args.kwargs["persona_id"] == pid

    @pytest.mark.asyncio
    async def test_execution_timeout_returns_408(self, client):
        """QueryTimeoutError yields HTTP 408 and logs failure."""
        from shared.source_executor import QueryTimeoutError
        bound = _make_bound()
        decision = _make_decision()
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(
            all=MagicMock(return_value=[]),
            scalar_one_or_none=MagicMock(return_value=None),
        ))
        db.flush = AsyncMock()

        mock_log_failure = AsyncMock()

        with (
            patch("src.api.routes.get_tenant_db", _async_gen(db)),
            patch("src.api.routes.load_authorized_model", AsyncMock(return_value=None)),
            patch("src.api.routes.resolve_execution_persona", AsyncMock(return_value=None)),
            patch("src.api.routes.bind_query_to_model", AsyncMock(return_value=bound)),
            patch("src.api.routes.apply_persona_gate", AsyncMock(return_value=None)),
            patch("src.api.routes.route_query", AsyncMock(return_value=decision)),
            patch("src.api.routes.execute_routed_query", AsyncMock(
                side_effect=QueryTimeoutError("Timed out")
            )),
            patch("src.api.routes._log_query_failure", mock_log_failure),
        ):
            resp = await client.post(
                "/api/v1/discover/members",
                json={"model_id": _MODEL_ID, "dimension_name": "Region"},
                headers=_auth(),
            )

        assert resp.status_code == 408
        mock_log_failure.assert_awaited_once()
        assert mock_log_failure.call_args.args[6] == "timeout"

    @pytest.mark.asyncio
    async def test_member_values_from_semantic_column_name(self, client):
        """Result rows keyed by semantic dim name, with fallback to first value."""
        bound = _make_bound("Product Category")
        decision = _make_decision()
        rows = [
            {"Product Category": "Electronics"},
            {"Product Category": "Clothing"},
        ]
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(
            all=MagicMock(return_value=[]),
            scalar_one_or_none=MagicMock(return_value=None),
        ))
        db.flush = AsyncMock()

        with (
            patch("src.api.routes.get_tenant_db", _async_gen(db)),
            patch("src.api.routes.load_authorized_model", AsyncMock(return_value=None)),
            patch("src.api.routes.resolve_execution_persona", AsyncMock(return_value=None)),
            patch("src.api.routes.bind_query_to_model", AsyncMock(return_value=bound)),
            patch("src.api.routes.apply_persona_gate", AsyncMock(return_value=None)),
            patch("src.api.routes.route_query", AsyncMock(return_value=decision)),
            patch("src.api.routes.execute_routed_query", AsyncMock(return_value=(rows, 0, ["Product Category"], MagicMock()))),
            patch("src.api.routes.log_query", AsyncMock()),
        ):
            resp = await client.post(
                "/api/v1/discover/members",
                json={"model_id": _MODEL_ID, "dimension_name": "Product Category"},
                headers=_auth(),
            )

        assert resp.status_code == 200
        members = resp.json()["members"]
        assert members[0]["name"] == "Electronics"
        assert members[1]["name"] == "Clothing"

    @pytest.mark.asyncio
    async def test_fallback_to_first_row_value(self, client):
        """When semantic name not in row keys, falls back to first value."""
        bound = _make_bound("Region")
        decision = _make_decision()
        rows = [{"region_col": "US"}]
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(
            all=MagicMock(return_value=[]),
            scalar_one_or_none=MagicMock(return_value=None),
        ))
        db.flush = AsyncMock()

        with (
            patch("src.api.routes.get_tenant_db", _async_gen(db)),
            patch("src.api.routes.load_authorized_model", AsyncMock(return_value=None)),
            patch("src.api.routes.resolve_execution_persona", AsyncMock(return_value=None)),
            patch("src.api.routes.bind_query_to_model", AsyncMock(return_value=bound)),
            patch("src.api.routes.apply_persona_gate", AsyncMock(return_value=None)),
            patch("src.api.routes.route_query", AsyncMock(return_value=decision)),
            patch("src.api.routes.execute_routed_query", AsyncMock(return_value=(rows, 0, ["region_col"], MagicMock()))),
            patch("src.api.routes.audit_result_columns", MagicMock()),
            patch("src.api.routes.log_query", AsyncMock()),
        ):
            resp = await client.post(
                "/api/v1/discover/members",
                json={"model_id": _MODEL_ID, "dimension_name": "Region"},
                headers=_auth(),
            )

        assert resp.status_code == 200
        members = resp.json()["members"]
        assert members[0]["name"] == "US"

    @pytest.mark.asyncio
    async def test_execution_error_logs_failure(self, client):
        """Generic execution errors are persisted via _log_query_failure."""
        bound = _make_bound()
        decision = _make_decision()
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(
            all=MagicMock(return_value=[]),
            scalar_one_or_none=MagicMock(return_value=None),
        ))
        db.flush = AsyncMock()

        mock_log_failure = AsyncMock()

        with (
            patch("src.api.routes.get_tenant_db", _async_gen(db)),
            patch("src.api.routes.load_authorized_model", AsyncMock(return_value=None)),
            patch("src.api.routes.resolve_execution_persona", AsyncMock(return_value=None)),
            patch("src.api.routes.bind_query_to_model", AsyncMock(return_value=bound)),
            patch("src.api.routes.apply_persona_gate", AsyncMock(return_value=None)),
            patch("src.api.routes.route_query", AsyncMock(return_value=decision)),
            patch("src.api.routes.execute_routed_query", AsyncMock(
                side_effect=RuntimeError("Connection refused")
            )),
            patch("src.api.routes._log_query_failure", mock_log_failure),
        ):
            resp = await client.post(
                "/api/v1/discover/members",
                json={"model_id": _MODEL_ID, "dimension_name": "Region"},
                headers=_auth(),
            )

        assert resp.status_code == 502
        mock_log_failure.assert_awaited_once()
        assert mock_log_failure.call_args.args[6] == "execution_error"

    @pytest.mark.asyncio
    async def test_audit_filters_present_called(self, client):
        """audit_filters_present runs between route and execute."""
        bound = _make_bound()
        decision = _make_decision()
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(
            all=MagicMock(return_value=[]),
            scalar_one_or_none=MagicMock(return_value=None),
        ))
        db.flush = AsyncMock()

        mock_audit_filters = MagicMock()

        with (
            patch("src.api.routes.get_tenant_db", _async_gen(db)),
            patch("src.api.routes.load_authorized_model", AsyncMock(return_value=None)),
            patch("src.api.routes.resolve_execution_persona", AsyncMock(return_value=None)),
            patch("src.api.routes.bind_query_to_model", AsyncMock(return_value=bound)),
            patch("src.api.routes.apply_persona_gate", AsyncMock(return_value=None)),
            patch("src.api.routes.route_query", AsyncMock(return_value=decision)),
            patch("src.api.routes.audit_filters_present", mock_audit_filters),
            patch("src.api.routes.execute_routed_query", AsyncMock(return_value=([], 0, [], MagicMock()))),
            patch("src.api.routes.log_query", AsyncMock()),
        ):
            resp = await client.post(
                "/api/v1/discover/members",
                json={"model_id": _MODEL_ID, "dimension_name": "Region"},
                headers=_auth(),
            )

        assert resp.status_code == 200
        # Bug-1045 round-3: the audit is invoked with the dimension-anchor
        # map resolved from model metadata (filter_anchors kwarg).
        mock_audit_filters.assert_called_once()
        call = mock_audit_filters.call_args
        assert call.args[:3] == (bound, decision.rewritten_query, "source")
        assert isinstance(call.kwargs.get("filter_anchors"), dict)

    @pytest.mark.asyncio
    async def test_audit_filters_failure_returns_structured_403(self, client):
        """B10 round-2: SecurityAuditError from audit_filters_present
        yields a precise structured 403 (security_audit_failure), not a
        bare 500 — and the query stays blocked fail-closed."""
        from src.security.query_audit import SecurityAuditError
        bound = _make_bound()
        decision = _make_decision()
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(
            all=MagicMock(return_value=[]),
            scalar_one_or_none=MagicMock(return_value=None),
        ))

        with (
            patch("src.api.routes.get_tenant_db", _async_gen(db)),
            patch("src.api.routes.load_authorized_model", AsyncMock(return_value=None)),
            patch("src.api.routes.resolve_execution_persona", AsyncMock(return_value=None)),
            patch("src.api.routes.bind_query_to_model", AsyncMock(return_value=bound)),
            patch("src.api.routes.apply_persona_gate", AsyncMock(return_value=None)),
            patch("src.api.routes.route_query", AsyncMock(return_value=decision)),
            patch("src.api.routes.audit_filters_present", MagicMock(
                side_effect=SecurityAuditError("Filter dropped")
            )),
        ):
            resp = await client.post(
                "/api/v1/discover/members",
                json={"model_id": _MODEL_ID, "dimension_name": "Region"},
                headers=_auth(),
            )

        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error_type"] == "security_audit_failure"
        assert detail["audit_layer"] == "filter_presence"
        assert "Filter dropped" in detail["message"]

    @pytest.mark.asyncio
    async def test_audit_result_columns_called(self, client):
        """audit_result_columns runs after execution."""
        bound = _make_bound()
        decision = _make_decision()
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(
            all=MagicMock(return_value=[]),
            scalar_one_or_none=MagicMock(return_value=None),
        ))
        db.flush = AsyncMock()

        mock_audit_cols = MagicMock()

        with (
            patch("src.api.routes.get_tenant_db", _async_gen(db)),
            patch("src.api.routes.load_authorized_model", AsyncMock(return_value=None)),
            patch("src.api.routes.resolve_execution_persona", AsyncMock(return_value=None)),
            patch("src.api.routes.bind_query_to_model", AsyncMock(return_value=bound)),
            patch("src.api.routes.apply_persona_gate", AsyncMock(return_value=None)),
            patch("src.api.routes.route_query", AsyncMock(return_value=decision)),
            patch("src.api.routes.execute_routed_query", AsyncMock(return_value=([], 0, ["Region"], MagicMock()))),
            patch("src.api.routes.audit_result_columns", mock_audit_cols),
            patch("src.api.routes.log_query", AsyncMock()),
        ):
            resp = await client.post(
                "/api/v1/discover/members",
                json={"model_id": _MODEL_ID, "dimension_name": "Region"},
                headers=_auth(),
            )

        assert resp.status_code == 200
        mock_audit_cols.assert_called_once_with(bound, ["Region"], None)


class TestNotInPersonaDefaultFilter:
    def test_not_in_operator_accepted(self):
        """not_in is in _SUPPORTED_OPERATORS and coerces correctly."""
        from src.security.persona_gate import _SUPPORTED_OPERATORS, _coerce_filter
        assert "not_in" in _SUPPORTED_OPERATORS
        op, val = _coerce_filter({"not_in": ["Blocked", "Banned"]})
        assert op == "not_in"
        assert val == ["Blocked", "Banned"]

    def test_not_in_merged_into_bound_query(self):
        """merge_default_filters adds not_in as a LogicalFilter."""
        from src.security.persona_gate import merge_default_filters
        from src.ir.logical_query import LogicalFilter

        persona = _make_persona(default_filters={"Country": {"not_in": ["X", "Y"]}})
        bound = _make_bound("Region")

        merged = merge_default_filters(persona, bound)
        assert "Country" in merged
        added = [f for f in bound.resolved_filters if f.dimension_name == "Country"]
        assert len(added) == 1
        assert added[0].operator == "not_in"
        assert added[0].value == ["X", "Y"]
