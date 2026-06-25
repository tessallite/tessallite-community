"""Tests for the headless REST API (Block K, reworked in B10).

Covers:
- Query endpoint executes through the REAL shared pipeline
  (``routes.execute_with_observation``) with only the executor function
  and persistence/audit sinks mocked (F-027-01 regression: the old suite
  patched the endpoint's own seam and never crossed into routes.py).
- QueryLog written for every headless query; QueryMissLog on source
  routes (F-030-01).
- Persona resolution + allow-list gate on the query path and metadata
  endpoints (F-027-05).
- project_id validation (F-027-09).
- Canonical filter contract incl. aliases and strict 422s (F-027-03 /
  F-027-04 / F-027-10).
- Metadata endpoints, rate limiter, auth.
"""
from __future__ import annotations

import types
import uuid
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from jose import jwt

from shared.config.settings import get_settings

_settings = get_settings()

TEST_TENANT = "test-tenant"
TEST_MODEL_ID = str(uuid.uuid4())
NOW = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _mint(role: str = "member") -> str:
    payload = {
        "sub": "user@example.com",
        "tenant_id": TEST_TENANT,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        "role": role,
    }
    return jwt.encode(
        payload, _settings.JWT_SECRET_KEY, algorithm=_settings.JWT_ALGORITHM
    )


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {_mint()}"}


def _mint_embed(model_ids) -> str:
    """Mint an embed token. ``model_ids`` may be a list (incl. empty) or None.

    An explicit empty list is a deliberately zero-scoped credential (F-027-14):
    it must enumerate NO models. ``None`` omits the claim entirely (unscoped).
    """
    payload = {
        "sub": "embed@example.com",
        "tenant_id": TEST_TENANT,
        "aud": "embed",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        "capabilities": ["query"],
    }
    if model_ids is not None:
        payload["model_ids"] = model_ids
    return jwt.encode(
        payload, _settings.JWT_SECRET_KEY, algorithm=_settings.JWT_ALGORITHM
    )


def _embed_headers(model_ids) -> dict:
    return {"Authorization": f"Bearer {_mint_embed(model_ids)}"}


@pytest.fixture
async def client():
    from src.main import app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as ac:
        yield ac


@pytest.fixture(autouse=True)
def _allow_project_access(monkeypatch):
    monkeypatch.setattr(
        "src.api.headless.load_authorized_model",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "src.api.headless.ensure_project_model_access",
        AsyncMock(return_value=None),
    )


def _make_model():
    return types.SimpleNamespace(
        id=uuid.UUID(TEST_MODEL_ID),
        project_id=uuid.uuid4(),
        slug="test-model",
        display_name="Test Model",
        description="A test model",
        deployed_version_id="v1",
    )


def _make_measure(name: str, default_agg: str = "sum"):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        display_name=name.replace("_", " ").title(),
        description=f"Measure {name}",
        format=None,
        default_agg=default_agg,
        variant_kind=None,
        model_id=uuid.UUID(TEST_MODEL_ID),
        source_column_id=uuid.uuid4(),
        is_additive=True,
    )


def _make_dimension(name: str, is_time_dim: bool = False):
    # NOTE: mirrors the real Dimension ORM — it has NO data_type
    # attribute. The listing endpoint joins ModelColumn for the type
    # (regression guard: a stub carrying data_type masked a live
    # AttributeError 500 on /models/{id}/dimensions).
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        display_name=name.replace("_", " ").title(),
        description=f"Dimension {name}",
        is_time_dim=is_time_dim,
        model_id=uuid.UUID(TEST_MODEL_ID),
        source_column_id=uuid.uuid4(),
    )


def _mock_tenant_db():
    db = AsyncMock()
    return db


def _async_gen(db):
    async def _gen(*args, **kwargs):
        yield db
    return _gen


def _make_bound(model=None):
    bound = MagicMock()
    bound.model = model or _make_model()
    bound.resolved_measures = [_make_measure("revenue")]
    bound.resolved_dimensions = [_make_dimension("region")]
    bound.resolved_filters = []
    bound.logical_query.protocol = "headless"
    bound.logical_query.query_fingerprint = "f" * 64
    bound.logical_query.raw_query = ""
    return bound


def _make_decision(route_type: str = "source"):
    decision = MagicMock()
    decision.route_type = route_type
    decision.rewritten_query = "SELECT 1"
    decision.aggregate_id = None
    decision.pocket_id = None
    decision.reason = "no aggregate matched"
    decision.aggregate_skipped_reasons = None
    return decision


class _Pipeline:
    """Patches for the routes-level shared pipeline: only the executor
    function and persistence/audit sinks are mocked, so headless requests
    flow through the real ``execute_with_observation`` /
    ``record_query_success`` code (F-027-01 / F-030-01 seam)."""

    def __init__(self, rows=None, columns=None):
        self.execute = AsyncMock(
            return_value=(rows or [], 0, columns or [], MagicMock())
        )
        self.log_query = AsyncMock()
        self.log_query_miss = AsyncMock()
        self.log_query_failure = AsyncMock()
        self.audit = AsyncMock()
        self.audit_filters_present = MagicMock()
        self.audit_result_columns = MagicMock()

    def patches(self):
        return [
            patch("src.api.routes.execute_routed_query", self.execute),
            patch("src.api.routes.log_query", self.log_query),
            patch("src.api.routes.log_query_miss", self.log_query_miss),
            patch("src.api.routes.log_query_failure", self.log_query_failure),
            patch("src.api.routes.audit", self.audit),
            patch("src.api.routes.audit_filters_present", self.audit_filters_present),
            patch("src.api.routes.audit_result_columns", self.audit_result_columns),
        ]


def _headless_patches(
    stack: ExitStack,
    *,
    bound,
    decision=None,
    persona=None,
    rows=None,
    columns=None,
    db=None,
) -> _Pipeline:
    pipeline = _Pipeline(rows=rows, columns=columns)
    stack.enter_context(
        patch("src.api.headless.get_tenant_db", _async_gen(db or _mock_tenant_db()))
    )
    stack.enter_context(
        patch(
            "src.api.headless.resolve_execution_persona",
            AsyncMock(return_value=persona),
        )
    )
    stack.enter_context(
        patch("src.api.headless.bind_query_to_model", AsyncMock(return_value=bound))
    )
    stack.enter_context(
        patch(
            "src.api.headless.route_query",
            AsyncMock(return_value=decision or _make_decision()),
        )
    )
    for p in pipeline.patches():
        stack.enter_context(p)
    return pipeline


def _query_body(model, **overrides) -> dict:
    body = {
        "project_id": str(model.project_id),
        "model_id": TEST_MODEL_ID,
        "measures": ["revenue"],
        "dimensions": ["region"],
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Query endpoint tests
# ---------------------------------------------------------------------------

class TestHeadlessQuery:
    @pytest.mark.asyncio
    async def test_query_returns_columns_and_rows(self, client):
        model = _make_model()
        bound = _make_bound(model)
        mock_rows = [{"region": "US", "revenue": 1000}, {"region": "EU", "revenue": 800}]
        mock_columns = ["region", "revenue"]

        with ExitStack() as stack:
            _headless_patches(
                stack, bound=bound, rows=mock_rows, columns=mock_columns,
            )
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(model),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["columns"] == ["region", "revenue"]
        assert len(data["rows"]) == 2
        # F-027-15: page_row_count is the returned-page count; total_rows is a
        # deprecated alias carrying the same value.
        assert data["page_row_count"] == 2
        assert data["total_rows"] == 2
        assert "query_id" in data

    @pytest.mark.asyncio
    async def test_query_id_differs_per_page(self, client):
        """F-027-15: query_id folds in limit/offset so two pages of the same
        semantic query get distinct ids (safe as a result-cache key)."""
        model = _make_model()
        bound = _make_bound(model)

        async def _one(body_overrides):
            with ExitStack() as stack:
                _headless_patches(
                    stack, bound=bound,
                    rows=[{"region": "US", "revenue": 1}], columns=["region", "revenue"],
                )
                resp = await client.post(
                    "/api/v1/headless/query",
                    json=_query_body(model, **body_overrides),
                    headers=_auth_headers(),
                )
            assert resp.status_code == 200
            return resp.json()["query_id"]

        page0 = await _one({"limit": 10, "offset": 0})
        page1 = await _one({"limit": 10, "offset": 10})
        same = await _one({"limit": 10, "offset": 0})
        assert page0 != page1
        assert page0 == same  # deterministic for the same page

    @pytest.mark.asyncio
    async def test_query_writes_query_log(self, client):
        """F-030-01: every headless query must produce a QueryLog row with
        protocol 'headless' and the JWT-derived identity."""
        model = _make_model()
        bound = _make_bound(model)

        with ExitStack() as stack:
            pipeline = _headless_patches(
                stack, bound=bound,
                rows=[{"region": "US", "revenue": 1}], columns=["region", "revenue"],
            )
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(model),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        pipeline.log_query.assert_awaited_once()
        kwargs = pipeline.log_query.await_args.kwargs
        assert kwargs["user_identity"] == "user@example.com"
        assert kwargs["bound_query"].logical_query.protocol == "headless"
        assert kwargs["rows_returned"] == 1
        pipeline.audit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_source_route_writes_miss_log(self, client):
        """F-030-01: source-routed headless queries must feed the
        optimizer's QueryMissLog."""
        model = _make_model()
        bound = _make_bound(model)

        with ExitStack() as stack:
            pipeline = _headless_patches(
                stack, bound=bound, decision=_make_decision(route_type="source"),
            )
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(model),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        pipeline.log_query_miss.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_aggregate_route_writes_no_miss_log(self, client):
        model = _make_model()
        bound = _make_bound(model)
        decision = _make_decision(route_type="aggregate")
        decision.aggregate_id = str(uuid.uuid4())

        with ExitStack() as stack:
            pipeline = _headless_patches(stack, bound=bound, decision=decision)
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(model),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        pipeline.log_query.assert_awaited_once()
        pipeline.log_query_miss.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_filter_audit_guardrail_runs(self, client):
        """F-027-11: the Layer-1 filter-presence audit must run on the
        headless path before execution."""
        model = _make_model()
        bound = _make_bound(model)

        with ExitStack() as stack:
            pipeline = _headless_patches(stack, bound=bound)
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(model),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        pipeline.audit_filters_present.assert_called_once()
        pipeline.audit_result_columns.assert_called_once()

    @pytest.mark.asyncio
    async def test_audit_block_emits_telemetry_and_clear_error(self, client):
        """B10 round-1 finding 4 (+ Bug-1045 fix direction): a security-
        audit block must (a) never execute the query, (b) persist a
        failure QueryLog row (error_type=security_audit), (c) write a
        query.security_audit_block audit event, and (d) return a precise
        structured 403 — not a bare 500."""
        from src.security.query_audit import SecurityAuditError

        model = _make_model()
        bound = _make_bound(model)

        with ExitStack() as stack:
            pipeline = _headless_patches(stack, bound=bound)
            pipeline.audit_filters_present.side_effect = SecurityAuditError(
                "SECURITY AUDIT FAILURE: 1 filter(s) missing from rewritten "
                "SQL: ['business_date_month']."
            )
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(model),
                headers=_auth_headers(),
            )

        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error_type"] == "security_audit_failure"
        assert detail["audit_layer"] == "filter_presence"
        assert "business_date_month" in detail["message"]
        assert "No data was returned" in detail["message"]

        # Fail-closed: the source query never ran.
        pipeline.execute.assert_not_awaited()
        # Telemetry: failure QueryLog row + platform audit event.
        pipeline.log_query_failure.assert_awaited_once()
        assert (
            pipeline.log_query_failure.await_args.kwargs["error_type"]
            == "security_audit"
        )
        block_calls = [
            c for c in pipeline.audit.await_args_list
            if c.kwargs.get("action") == "query.security_audit_block"
        ]
        assert block_calls, "query.security_audit_block audit event not emitted"
        # The audit vocabulary is critical|warn|info — "warning" would be
        # severity-gated out at the default level (caught live in B10 R2).
        assert block_calls[0].kwargs["severity"] == "warn"
        assert block_calls[0].kwargs["detail"]["audit_layer"] == "filter_presence"

    @pytest.mark.asyncio
    async def test_raw_query_carries_canonical_payload(self, client):
        """B10 round-1 finding 3: headless queries must not log an empty
        raw_query — the canonical semantic payload (measures, dimensions,
        filters) goes into the LogicalQuery so QueryLog previews work."""
        import json as _json

        model = _make_model()
        bound = _make_bound(model)

        with ExitStack() as stack:
            _headless_patches(stack, bound=bound)
            mock_bind = AsyncMock(return_value=bound)
            stack.enter_context(
                patch("src.api.headless.bind_query_to_model", mock_bind)
            )
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(
                    model,
                    filters=[{
                        "dimension": "region",
                        "operator": "in",
                        "values": ["US", "DE"],
                    }],
                    limit=50,
                ),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        lq = mock_bind.await_args.args[0]
        assert lq.raw_query, "headless raw_query must not be empty (finding 3)"
        payload = _json.loads(lq.raw_query)
        assert payload["measures"] == ["revenue"]
        assert payload["dimensions"] == ["region"]
        assert payload["filters"] == [
            {"dimension": "region", "operator": "in", "values": ["US", "DE"]}
        ]
        assert payload["limit"] == 50

    @pytest.mark.asyncio
    async def test_query_with_filters(self, client):
        model = _make_model()
        bound = _make_bound(model)

        with ExitStack() as stack:
            _headless_patches(stack, bound=bound)
            mock_bind = AsyncMock(return_value=bound)
            stack.enter_context(
                patch("src.api.headless.bind_query_to_model", mock_bind)
            )
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(
                    model,
                    filters=[
                        {"dimension": "region", "operator": "eq", "value": "US"},
                        {"dimension": "year", "operator": "between", "values": [2020, 2025]},
                    ],
                ),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        logical_query = mock_bind.call_args[0][0]
        assert len(logical_query.filters) == 2
        assert logical_query.filters[0].dimension_name == "region"
        assert logical_query.filters[0].operator == "eq"
        assert logical_query.filters[0].value == "US"
        assert logical_query.filters[1].operator == "between"
        assert logical_query.filters[1].value == (2020, 2025)

    @pytest.mark.asyncio
    async def test_ne_alias_normalized_to_neq(self, client):
        """F-027-04 regression: 'ne' must reach the rewriter as 'neq',
        never as an unknown operator that silently inverts to equality."""
        model = _make_model()
        bound = _make_bound(model)

        with ExitStack() as stack:
            _headless_patches(stack, bound=bound)
            mock_bind = AsyncMock(return_value=bound)
            stack.enter_context(
                patch("src.api.headless.bind_query_to_model", mock_bind)
            )
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(
                    model,
                    filters=[{"dimension": "region", "operator": "ne", "value": "US"}],
                ),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        logical_query = mock_bind.call_args[0][0]
        assert logical_query.filters[0].operator == "neq"

    @pytest.mark.asyncio
    async def test_degenerate_in_filter_422(self, client):
        """F-027-10: empty in/not_in lists are a 422, not a silent 1=0."""
        model = _make_model()
        with patch("src.api.headless.get_tenant_db", _async_gen(_mock_tenant_db())):
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(
                    model,
                    filters=[{"dimension": "region", "operator": "in", "values": []}],
                ),
                headers=_auth_headers(),
            )
        assert resp.status_code == 422
        assert "at least one" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_degenerate_between_filter_422(self, client):
        """F-027-10: between without exactly two values is a 422, not a
        silent BETWEEN NULL AND NULL."""
        model = _make_model()
        with patch("src.api.headless.get_tenant_db", _async_gen(_mock_tenant_db())):
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(
                    model,
                    filters=[{"dimension": "year", "operator": "between", "values": [2020]}],
                ),
                headers=_auth_headers(),
            )
        assert resp.status_code == 422
        assert "exactly two" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_project_mismatch_returns_403(self, client):
        """F-027-09: project_id is validated against the bound model."""
        model = _make_model()
        bound = _make_bound(model)

        with ExitStack() as stack:
            _headless_patches(stack, bound=bound)
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(model, project_id=str(uuid.uuid4())),
                headers=_auth_headers(),
            )

        assert resp.status_code == 403
        assert "project" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_query_nonexistent_measure_returns_422(self, client):
        from src.ir.logical_query import SemanticBindingError

        model = _make_model()
        with (
            patch("src.api.headless.get_tenant_db", _async_gen(_mock_tenant_db())),
            patch(
                "src.api.headless.resolve_execution_persona",
                AsyncMock(return_value=None),
            ),
            patch(
                "src.api.headless.bind_query_to_model",
                AsyncMock(side_effect=SemanticBindingError("Measure 'xyz' not found")),
            ),
        ):
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(model, measures=["xyz"]),
                headers=_auth_headers(),
            )

        assert resp.status_code == 422
        assert "xyz" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_query_requires_at_least_one_measure(self, client):
        model = _make_model()
        with patch("src.api.headless.get_tenant_db", _async_gen(_mock_tenant_db())):
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(model, measures=[]),
                headers=_auth_headers(),
            )

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_query_pagination(self, client):
        model = _make_model()
        bound = _make_bound(model)

        with ExitStack() as stack:
            _headless_patches(stack, bound=bound)
            mock_bind = AsyncMock(return_value=bound)
            stack.enter_context(
                patch("src.api.headless.bind_query_to_model", mock_bind)
            )
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(model, limit=10, offset=20),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        logical_query = mock_bind.call_args[0][0]
        assert logical_query.limit == 10
        assert logical_query.offset == 20

    @pytest.mark.asyncio
    async def test_query_unsupported_filter_operator(self, client):
        model = _make_model()
        with patch("src.api.headless.get_tenant_db", _async_gen(_mock_tenant_db())):
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(
                    model,
                    filters=[{"dimension": "x", "operator": "REGEX", "value": ".*"}],
                ),
                headers=_auth_headers(),
            )

        assert resp.status_code == 422
        assert "REGEX" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_order_by_injection_returns_422(self, client):
        model = _make_model()
        with patch("src.api.headless.get_tenant_db", _async_gen(_mock_tenant_db())):
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(
                    model,
                    order_by=[{"field": "revenue", "direction": "desc; DROP TABLE users--"}],
                ),
                headers=_auth_headers(),
            )
        assert resp.status_code == 422
        assert "direction" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_order_by_union_injection_returns_422(self, client):
        model = _make_model()
        with patch("src.api.headless.get_tenant_db", _async_gen(_mock_tenant_db())):
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(
                    model,
                    order_by=[{"field": "x", "direction": "asc UNION SELECT 1"}],
                ),
                headers=_auth_headers(),
            )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Persona enforcement on the query path (F-027-05)
# ---------------------------------------------------------------------------

class TestHeadlessPersonaEnforcement:
    @pytest.mark.asyncio
    async def test_persona_gate_and_default_filters_applied(self, client):
        model = _make_model()
        bound = _make_bound(model)
        persona = types.SimpleNamespace(
            id=uuid.uuid4(),
            name="Sales",
            included_measure_ids=[],
            included_dimension_ids=[],
            included_hierarchy_ids=[],
            default_filters={},
        )
        mock_gate = AsyncMock()
        mock_merge = MagicMock(return_value=[])

        with ExitStack() as stack:
            _headless_patches(stack, bound=bound, persona=persona)
            stack.enter_context(
                patch("src.api.headless.enforce_persona_gate", mock_gate)
            )
            stack.enter_context(
                patch("src.api.headless.merge_default_filters", mock_merge)
            )
            mock_route = AsyncMock(return_value=_make_decision())
            stack.enter_context(patch("src.api.headless.route_query", mock_route))
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(model, persona_id=str(persona.id)),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        mock_gate.assert_awaited_once()
        mock_merge.assert_called_once()
        # Persona must reach the router so CLS/RLS closures see it.
        assert mock_route.await_args.kwargs.get("persona") is persona

    @pytest.mark.asyncio
    async def test_persona_denial_propagates_403(self, client):
        from fastapi import HTTPException

        model = _make_model()
        bound = _make_bound(model)
        persona = types.SimpleNamespace(id=uuid.uuid4(), name="Sales")

        with ExitStack() as stack:
            _headless_patches(stack, bound=bound, persona=persona)
            stack.enter_context(
                patch(
                    "src.api.headless.enforce_persona_gate",
                    AsyncMock(side_effect=HTTPException(
                        status_code=403,
                        detail={"error_code": "PERSONA_OBJECT_NOT_INCLUDED"},
                    )),
                )
            )
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(model),
                headers=_auth_headers(),
            )

        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_persona_resolution_403_blocks_query(self, client):
        """Multi-assigned user without a pick is rejected by the resolver
        — the headless path must not bypass the audience matrix."""
        from fastapi import HTTPException

        model = _make_model()
        with (
            patch("src.api.headless.get_tenant_db", _async_gen(_mock_tenant_db())),
            patch(
                "src.api.headless.resolve_execution_persona",
                AsyncMock(side_effect=HTTPException(
                    status_code=403,
                    detail="You have multiple personas assigned — please select one",
                )),
            ),
        ):
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(model),
                headers=_auth_headers(),
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_persona_reaches_column_scope_guard(self, client):
        """F-027-05 (CLS): a headless query must run through the SAME
        column-scope guard as /execute — the persona must reach
        ``audit_result_columns`` so restricted columns in the served result
        are caught (headless is not a bypass route for column security)."""
        model = _make_model()
        bound = _make_bound(model)
        persona = types.SimpleNamespace(
            id=uuid.uuid4(), name="Sales",
            included_measure_ids=[], included_dimension_ids=[],
            included_hierarchy_ids=[], default_filters={},
        )
        with ExitStack() as stack:
            pipeline = _headless_patches(stack, bound=bound, persona=persona)
            stack.enter_context(
                patch("src.api.headless.enforce_persona_gate", AsyncMock())
            )
            stack.enter_context(
                patch("src.api.headless.merge_default_filters", MagicMock())
            )
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(model, persona_id=str(persona.id)),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        # The CLS result-column guard ran with the resolved persona — without
        # this, a restricted column could be served from the headless path.
        pipeline.audit_result_columns.assert_called()
        assert pipeline.audit_result_columns.call_args.args[2] is persona


# ---------------------------------------------------------------------------
# Metadata endpoint tests
# ---------------------------------------------------------------------------

def _no_persona():
    return patch(
        "src.api.headless.resolve_execution_persona", AsyncMock(return_value=None)
    )


class TestHeadlessMetadata:
    @pytest.mark.asyncio
    async def test_list_models(self, client):
        models = [_make_model()]
        db = _mock_tenant_db()
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = models
        db.execute = AsyncMock(return_value=result_mock)

        with patch("src.api.headless.get_tenant_db", _async_gen(db)):
            resp = await client.get(
                "/api/v1/headless/models",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["slug"] == "test-model"
        assert data[0]["display_name"] == "Test Model"

    @pytest.mark.asyncio
    async def test_list_models_embed_empty_scope_lists_nothing(self, client):
        """F-027-14: an embed token scoped to ``model_ids: []`` (explicitly
        zero models) must enumerate NO models — fail-closed. The former
        truthiness guard treated the empty list as unrestricted and leaked
        every model's id/slug/description."""
        models = [_make_model()]
        db = _mock_tenant_db()
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = models
        db.execute = AsyncMock(return_value=result_mock)

        with patch("src.api.headless.get_tenant_db", _async_gen(db)):
            resp = await client.get(
                "/api/v1/headless/models",
                headers=_embed_headers([]),
            )

        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_models_embed_in_scope_listed(self, client):
        """An embed token scoped to a model lists exactly that model."""
        model = _make_model()
        db = _mock_tenant_db()
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [model]
        db.execute = AsyncMock(return_value=result_mock)

        with patch("src.api.headless.get_tenant_db", _async_gen(db)):
            resp = await client.get(
                "/api/v1/headless/models",
                headers=_embed_headers([TEST_MODEL_ID]),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == TEST_MODEL_ID

    @pytest.mark.asyncio
    async def test_list_models_embed_unset_scope_unrestricted(self, client):
        """No ``model_ids`` claim at all (None) keeps the unrestricted meaning —
        the embed token lists every model."""
        model = _make_model()
        db = _mock_tenant_db()
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [model]
        db.execute = AsyncMock(return_value=result_mock)

        with patch("src.api.headless.get_tenant_db", _async_gen(db)):
            resp = await client.get(
                "/api/v1/headless/models",
                headers=_embed_headers(None),
            )

        assert resp.status_code == 200
        assert len(resp.json()) == 1

    @pytest.mark.asyncio
    async def test_list_measures(self, client):
        measures = [_make_measure("revenue"), _make_measure("cost")]
        db = _mock_tenant_db()
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = measures
        db.execute = AsyncMock(return_value=result_mock)

        with patch("src.api.headless.get_tenant_db", _async_gen(db)), _no_persona():
            resp = await client.get(
                f"/api/v1/headless/models/{TEST_MODEL_ID}/measures",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        names = {m["name"] for m in data}
        assert names == {"revenue", "cost"}
        assert data[0]["aggregation_type"] == "sum"

    @pytest.mark.asyncio
    async def test_list_measures_persona_scoped(self, client):
        """F-027-05: persona allow-lists must filter the measure listing —
        restricted metadata is not disclosed to persona-locked callers."""
        revenue = _make_measure("revenue")
        cost = _make_measure("cost")
        persona = types.SimpleNamespace(
            id=uuid.uuid4(),
            included_measure_ids=[str(revenue.id)],
            included_dimension_ids=[],
        )
        db = _mock_tenant_db()
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [revenue, cost]
        db.execute = AsyncMock(return_value=result_mock)

        with (
            patch("src.api.headless.get_tenant_db", _async_gen(db)),
            patch(
                "src.api.headless.resolve_execution_persona",
                AsyncMock(return_value=persona),
            ),
        ):
            resp = await client.get(
                f"/api/v1/headless/models/{TEST_MODEL_ID}/measures",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert [m["name"] for m in data] == ["revenue"]

    @pytest.mark.asyncio
    async def test_list_dimensions(self, client):
        dims = [_make_dimension("region"), _make_dimension("order_date", is_time_dim=True)]
        db = _mock_tenant_db()
        result_mock = MagicMock()
        # Endpoint selects (Dimension, ModelColumn.data_type) rows.
        result_mock.all.return_value = [(d, "string") for d in dims]
        db.execute = AsyncMock(return_value=result_mock)

        with patch("src.api.headless.get_tenant_db", _async_gen(db)), _no_persona():
            resp = await client.get(
                f"/api/v1/headless/models/{TEST_MODEL_ID}/dimensions",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        time_dim = next(d for d in data if d["name"] == "order_date")
        assert time_dim["is_time_dim"] is True
        # F-027-13: data_type is populated from the ORM, not always null.
        assert time_dim["data_type"] == "string"

    @pytest.mark.asyncio
    async def test_list_dimensions_persona_scoped(self, client):
        """F-027-05: persona allow-lists must filter the dimension listing."""
        region = _make_dimension("region")
        salary_band = _make_dimension("salary_band")
        persona = types.SimpleNamespace(
            id=uuid.uuid4(),
            included_measure_ids=[],
            included_dimension_ids=[str(region.id)],
        )
        db = _mock_tenant_db()
        result_mock = MagicMock()
        # Endpoint selects (Dimension, ModelColumn.data_type) rows.
        result_mock.all.return_value = [(region, "string"), (salary_band, "string")]
        db.execute = AsyncMock(return_value=result_mock)

        with (
            patch("src.api.headless.get_tenant_db", _async_gen(db)),
            patch(
                "src.api.headless.resolve_execution_persona",
                AsyncMock(return_value=persona),
            ),
        ):
            resp = await client.get(
                f"/api/v1/headless/models/{TEST_MODEL_ID}/dimensions",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert [d["name"] for d in data] == ["region"]

    @pytest.mark.asyncio
    async def test_measures_404_for_missing_model(self, client):
        db = _mock_tenant_db()
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=result_mock)
        db.get = AsyncMock(return_value=None)

        with patch("src.api.headless.get_tenant_db", _async_gen(db)), _no_persona():
            resp = await client.get(
                f"/api/v1/headless/models/{uuid.uuid4()}/measures",
                headers=_auth_headers(),
            )

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Rate limiter test
# ---------------------------------------------------------------------------

class TestHeadlessRateLimit:
    @pytest.mark.asyncio
    async def test_rate_limit_returns_429_with_retry_after(self, client):
        # F-027-07: the limiter now lives in the shared rate_limit module
        # (plugin + headless draw from the same buckets) and emits Retry-After.
        from src.api import rate_limit as rl

        rl._buckets.clear()
        try:
            with patch.object(_settings, "HEADLESS_RATE_LIMIT", 2):
                db = _mock_tenant_db()
                result_mock = MagicMock()
                result_mock.scalars.return_value.all.return_value = []
                db.execute = AsyncMock(return_value=result_mock)

                with patch("src.api.headless.get_tenant_db", _async_gen(db)):
                    await client.get("/api/v1/headless/models", headers=_auth_headers())
                    await client.get("/api/v1/headless/models", headers=_auth_headers())
                    resp = await client.get("/api/v1/headless/models", headers=_auth_headers())

            assert resp.status_code == 429
            assert "retry-after" in resp.headers
            assert int(resp.headers["retry-after"]) >= 1
        finally:
            rl._buckets.clear()

    @pytest.mark.asyncio
    async def test_rate_limit_header_present(self, client):
        from src.api import rate_limit as rl

        rl._buckets.clear()
        try:
            db = _mock_tenant_db()
            result_mock = MagicMock()
            result_mock.scalars.return_value.all.return_value = []
            db.execute = AsyncMock(return_value=result_mock)

            with patch("src.api.headless.get_tenant_db", _async_gen(db)):
                resp = await client.get("/api/v1/headless/models", headers=_auth_headers())

            assert "x-ratelimit-remaining" in resp.headers
        finally:
            rl._buckets.clear()


# ---------------------------------------------------------------------------
# Row security / principal tests
# ---------------------------------------------------------------------------

class TestHeadlessRowSecurity:
    @pytest.mark.asyncio
    async def test_principal_passed_to_route_query(self, client):
        """The headless endpoint must build a Principal from the JWT and pass
        it to route_query so that compile_row_security fires."""
        model = _make_model()
        bound = _make_bound(model)

        captured = {}

        async def _capture_route_query(bound, db, *, principal=None, persona=None):
            captured["principal"] = principal
            return _make_decision()

        with ExitStack() as stack:
            _headless_patches(stack, bound=bound)
            stack.enter_context(
                patch("src.api.headless.route_query", _capture_route_query)
            )
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(model),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        principal = captured.get("principal")
        assert principal is not None, "route_query must receive a Principal"
        assert principal.user_identity == "user@example.com"

    @pytest.mark.asyncio
    async def test_principal_carries_role(self, client):
        """Principal roles must reflect the JWT role claim."""
        model = _make_model()
        bound = _make_bound(model)

        captured = {}

        async def _capture(bound, db, *, principal=None, persona=None):
            captured["principal"] = principal
            return _make_decision()

        admin_token = _mint(role="tenant_admin")
        with ExitStack() as stack:
            _headless_patches(stack, bound=bound)
            stack.enter_context(patch("src.api.headless.route_query", _capture))
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(model),
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert resp.status_code == 200
        principal = captured["principal"]
        assert "tenant_admin" in principal.roles


class TestHeadlessAuth:
    @pytest.mark.asyncio
    async def test_rejects_unauthenticated(self, client):
        resp = await client.get("/api/v1/headless/models")
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_rejects_no_token_on_query(self, client):
        resp = await client.post(
            "/api/v1/headless/query",
            json={
                "project_id": str(uuid.uuid4()),
                "model_id": TEST_MODEL_ID,
                "measures": ["revenue"],
            },
        )
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Project-RBAC DENIAL tests (Bug-5304)
# These override the autouse _allow_project_access fixture to verify that
# load_authorized_model / ensure_project_model_access denial surfaces as
# 403 on the headless measures, dimensions, and models endpoints.
# ---------------------------------------------------------------------------

class TestHeadlessRBACDenial:
    @pytest.mark.asyncio
    async def test_measures_denied_by_load_authorized_model(self, client, monkeypatch):
        """load_authorized_model raises 403 -> GET /models/{id}/measures returns 403."""
        monkeypatch.setattr(
            "src.api.headless.load_authorized_model",
            AsyncMock(side_effect=HTTPException(
                status_code=403,
                detail="Insufficient project role",
            )),
        )
        db = _mock_tenant_db()
        with patch("src.api.headless.get_tenant_db", _async_gen(db)):
            resp = await client.get(
                f"/api/v1/headless/models/{TEST_MODEL_ID}/measures",
                headers=_auth_headers(),
            )
        assert resp.status_code == 403
        assert "project" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_dimensions_denied_by_load_authorized_model(self, client, monkeypatch):
        """load_authorized_model raises 403 -> GET /models/{id}/dimensions returns 403."""
        monkeypatch.setattr(
            "src.api.headless.load_authorized_model",
            AsyncMock(side_effect=HTTPException(
                status_code=403,
                detail="Insufficient project role",
            )),
        )
        db = _mock_tenant_db()
        with patch("src.api.headless.get_tenant_db", _async_gen(db)):
            resp = await client.get(
                f"/api/v1/headless/models/{TEST_MODEL_ID}/dimensions",
                headers=_auth_headers(),
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_models_denied_by_ensure_project_model_access(self, client, monkeypatch):
        """ensure_project_model_access raises 403 for every model ->
        GET /models returns an empty list (not a 403 — list endpoint
        filters rather than rejects)."""
        model = _make_model()
        db = _mock_tenant_db()
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [model]
        db.execute = AsyncMock(return_value=result_mock)

        monkeypatch.setattr(
            "src.api.headless.ensure_project_model_access",
            AsyncMock(side_effect=HTTPException(
                status_code=403, detail="No access",
            )),
        )
        with patch("src.api.headless.get_tenant_db", _async_gen(db)):
            resp = await client.get(
                "/api/v1/headless/models",
                headers=_auth_headers(),
            )
        assert resp.status_code == 200
        assert resp.json() == [], (
            "Models the caller has no project binding for must be filtered out"
        )

    @pytest.mark.asyncio
    async def test_query_denied_by_load_authorized_model(self, client, monkeypatch):
        """load_authorized_model raises 403 -> POST /query returns 403."""
        monkeypatch.setattr(
            "src.api.headless.load_authorized_model",
            AsyncMock(side_effect=HTTPException(
                status_code=403,
                detail="Insufficient project role",
            )),
        )
        model = _make_model()
        db = _mock_tenant_db()
        with patch("src.api.headless.get_tenant_db", _async_gen(db)):
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(model),
                headers=_auth_headers(),
            )
        assert resp.status_code == 403
