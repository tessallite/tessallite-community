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
    async def test_bug_8159_column_descriptors_returned(self, client):
        """Bug-8159: the response carries typed ``column_descriptors`` — one
        per output column, order-aligned with ``columns`` — instead of only
        bare name strings. A measure column reports ``kind="measure"`` with its
        aggregation; a dimension reports ``kind="dimension"`` with its
        time-axis flag. Fails pre-fix because ``HeadlessColumnDescriptor`` and
        the ``column_descriptors`` field did not exist."""
        model = _make_model()
        bound = _make_bound(model)
        # bound: measure "revenue" (default_agg=sum), dimension "region".
        mock_rows = [{"region": "US", "revenue": 1000}]
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
        # columns stays a bare list[str] for wire back-compat.
        assert data["columns"] == ["region", "revenue"]
        descriptors = data["column_descriptors"]
        # One descriptor per column, in the same order.
        assert [d["name"] for d in descriptors] == ["region", "revenue"]
        by_name = {d["name"]: d for d in descriptors}
        assert by_name["region"]["kind"] == "dimension"
        assert by_name["region"]["is_time_dim"] is False
        assert by_name["region"]["display_name"] == "Region"
        assert by_name["revenue"]["kind"] == "measure"
        assert by_name["revenue"]["aggregation"] == "sum"

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
    async def test_query_tags_client_kind_headless(self, client):
        """Bug-5813: the headless endpoint must pass client_kind='headless'
        through to the observed pipeline so telemetry can distinguish
        headless queries from other sources."""
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
        assert kwargs["client_kind"] == "headless"

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
    async def test_non_uuid_model_id_returns_400(self, client):
        # Bug-6381: a malformed (non-UUID) model_id must return a clean 400 at
        # the boundary, not a 500 from the downstream UUID parse in
        # shared.auth.project_access._as_uuid. The guard fires before any DB
        # work, so no pipeline patches are needed.
        model = _make_model()
        resp = await client.post(
            "/api/v1/headless/query",
            json=_query_body(model, model_id="not-a-uuid"),
            headers=_auth_headers(),
        )
        assert resp.status_code == 400
        assert "model_id" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_non_uuid_project_id_returns_400(self, client):
        # Bug-6381: same guard for a malformed project_id.
        model = _make_model()
        resp = await client.post(
            "/api/v1/headless/query",
            json=_query_body(model, project_id="12345"),
            headers=_auth_headers(),
        )
        assert resp.status_code == 400
        assert "project_id" in resp.json()["detail"]

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
        # Bug-7998 / F-027-02: the endpoint fetches effective_limit + 1 rows
        # internally to detect truncation, so the LogicalQuery LIMIT is 11
        # for a requested limit of 10. The extra probe row is trimmed before
        # the response (see the completeness tests below).
        assert logical_query.limit == 11
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
# F-027-01 / Bug-7997: strict request schema — unknown fields rejected
# ---------------------------------------------------------------------------

class TestHeadlessStrictSchema:
    @pytest.mark.asyncio
    async def test_misspelled_dimensions_field_rejected_422(self, client):
        """Bug-7997: a misspelled optional field (``dimentions``) must be a
        422 naming the field, NOT silently dropped into a grand-total query.
        The live report saw ``dimentions`` return a one-row grand total 200."""
        model = _make_model()
        body = _query_body(model)
        # Misspell the dimensions field; supply a valid dimensions too so the
        # ONLY defect under test is the unknown extra field.
        body["dimentions"] = ["country_code"]
        resp = await client.post(
            "/api/v1/headless/query",
            json=body,
            headers=_auth_headers(),
        )
        assert resp.status_code == 422
        assert "dimentions" in resp.text

    @pytest.mark.asyncio
    async def test_misspelled_persona_field_rejected_422(self, client):
        model = _make_model()
        body = _query_body(model)
        body["persona"] = "some-id"  # correct field is persona_id
        resp = await client.post(
            "/api/v1/headless/query",
            json=body,
            headers=_auth_headers(),
        )
        assert resp.status_code == 422
        assert "persona" in resp.text

    @pytest.mark.asyncio
    async def test_unknown_filter_property_rejected_422(self, client):
        """A misspelled property INSIDE a filter object is also rejected —
        SemanticFilter is strict, so an ``operatr`` typo cannot silently fall
        through to the default 'eq'."""
        model = _make_model()
        resp = await client.post(
            "/api/v1/headless/query",
            json=_query_body(
                model,
                filters=[{"dimension": "region", "operatr": "neq", "value": "US"}],
            ),
            headers=_auth_headers(),
        )
        assert resp.status_code == 422
        assert "operatr" in resp.text

    @pytest.mark.asyncio
    async def test_unknown_order_by_property_rejected_422(self, client):
        model = _make_model()
        resp = await client.post(
            "/api/v1/headless/query",
            json=_query_body(
                model,
                order_by=[{"field": "revenue", "dir": "desc"}],
            ),
            headers=_auth_headers(),
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# F-027-02 / Bug-7998: explicit completeness / truncation contract
# ---------------------------------------------------------------------------

class TestHeadlessCompleteness:
    @pytest.mark.asyncio
    async def test_complete_result_reports_complete_true(self, client):
        """A result at or under the cap reports complete=true, has_more=false,
        and the effective row_limit."""
        model = _make_model()
        bound = _make_bound(model)
        rows = [{"region": "US", "revenue": 1}, {"region": "EU", "revenue": 2}]

        with ExitStack() as stack:
            _headless_patches(
                stack, bound=bound, rows=rows, columns=["region", "revenue"],
            )
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(model, limit=10),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["complete"] is True
        assert data["has_more"] is False
        assert data["row_limit"] == 10
        assert data["page_row_count"] == 2

    @pytest.mark.asyncio
    async def test_capped_result_reports_complete_false_and_trims_probe(self, client):
        """Bug-7998: when the source returns effective_limit + 1 rows (the
        probe row), the response must report complete=false + has_more=true
        and trim the page back to exactly the requested limit — a capped
        extract is never presented as the whole dataset."""
        model = _make_model()
        bound = _make_bound(model)
        # limit=3 -> probe_limit=4 -> source returns 4 rows.
        rows = [{"region": r, "revenue": i} for i, r in enumerate(["US", "EU", "GB", "FR"])]

        with ExitStack() as stack:
            _headless_patches(
                stack, bound=bound, rows=rows, columns=["region", "revenue"],
            )
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(model, limit=3),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["has_more"] is True
        assert data["complete"] is False
        assert data["row_limit"] == 3
        # Probe row trimmed: exactly the requested limit is returned.
        assert data["page_row_count"] == 3
        assert len(data["rows"]) == 3

    @pytest.mark.asyncio
    async def test_probe_limit_is_effective_limit_plus_one(self, client):
        """The LIMIT handed to the engine is effective_limit + 1 so the
        extra row can be detected (the engine is not modified — only the
        LogicalQuery.limit data field the handler already owns)."""
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
                json=_query_body(model, limit=100),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        lq = mock_bind.call_args[0][0]
        assert lq.limit == 101


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
        # Bug-5973: deployed field is present and True for deployed models.
        assert data[0]["deployed"] is True

    @pytest.mark.asyncio
    async def test_list_models_excludes_undeployed(self, client):
        """Bug-5973 / F-027-04: undeployed models must NOT appear in the
        headless /models listing — querying them returns 409, so listing
        them in a discovery endpoint is misleading for headless clients."""
        deployed_model = _make_model()
        undeployed_model = types.SimpleNamespace(
            id=uuid.uuid4(),
            project_id=deployed_model.project_id,
            slug="draft-model",
            display_name="Draft Model",
            description="Not deployed yet",
            deployed_version_id=None,
        )
        db = _mock_tenant_db()
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [
            deployed_model, undeployed_model,
        ]
        db.execute = AsyncMock(return_value=result_mock)

        with patch("src.api.headless.get_tenant_db", _async_gen(db)):
            resp = await client.get(
                "/api/v1/headless/models",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1, (
            "Only deployed models should be returned"
        )
        assert data[0]["slug"] == "test-model"

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
        # Bug-7418 (GAP 2): mock an UNDEPLOYED model so the live-table
        # fallback path fires (deployed models with unresolvable snapshots
        # now fail closed with 409).
        undeployed_model = _make_model()
        undeployed_model.deployed_version_id = None
        db = _mock_tenant_db()
        db.get = AsyncMock(return_value=undeployed_model)
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = measures
        db.execute = AsyncMock(return_value=result_mock)

        with (
            patch("src.api.headless.get_tenant_db", _async_gen(db)),
            _no_persona(),
            patch("src.api.headless.resolve_deployed_shape", AsyncMock(return_value=None)),
            patch("src.api.headless.cls_blocked_measure_and_dimension_ids", AsyncMock(return_value=(set(), set()))),
        ):
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
        undeployed_model = _make_model()
        undeployed_model.deployed_version_id = None
        db = _mock_tenant_db()
        db.get = AsyncMock(return_value=undeployed_model)
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [revenue, cost]
        db.execute = AsyncMock(return_value=result_mock)

        with (
            patch("src.api.headless.get_tenant_db", _async_gen(db)),
            patch("src.api.headless.resolve_execution_persona", AsyncMock(return_value=persona)),
            patch("src.api.headless.resolve_deployed_shape", AsyncMock(return_value=None)),
            patch("src.api.headless.cls_blocked_measure_and_dimension_ids", AsyncMock(return_value=(set(), set()))),
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
        undeployed_model = _make_model()
        undeployed_model.deployed_version_id = None
        db = _mock_tenant_db()
        db.get = AsyncMock(return_value=undeployed_model)
        result_mock = MagicMock()
        # Endpoint selects (Dimension, ModelColumn.data_type) rows.
        result_mock.all.return_value = [(d, "string") for d in dims]
        db.execute = AsyncMock(return_value=result_mock)

        with (
            patch("src.api.headless.get_tenant_db", _async_gen(db)),
            _no_persona(),
            patch("src.api.headless.resolve_deployed_shape", AsyncMock(return_value=None)),
            patch("src.api.headless.cls_blocked_measure_and_dimension_ids", AsyncMock(return_value=(set(), set()))),
        ):
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
        undeployed_model = _make_model()
        undeployed_model.deployed_version_id = None
        db = _mock_tenant_db()
        db.get = AsyncMock(return_value=undeployed_model)
        result_mock = MagicMock()
        # Endpoint selects (Dimension, ModelColumn.data_type) rows.
        result_mock.all.return_value = [(region, "string"), (salary_band, "string")]
        db.execute = AsyncMock(return_value=result_mock)

        with (
            patch("src.api.headless.get_tenant_db", _async_gen(db)),
            patch("src.api.headless.resolve_execution_persona", AsyncMock(return_value=persona)),
            patch("src.api.headless.resolve_deployed_shape", AsyncMock(return_value=None)),
            patch("src.api.headless.cls_blocked_measure_and_dimension_ids", AsyncMock(return_value=(set(), set()))),
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


# ---------------------------------------------------------------------------
# Bug-7409: headless response carries route visibility
# ---------------------------------------------------------------------------

class TestHeadlessRouteVisibility:
    @pytest.mark.asyncio
    async def test_query_response_includes_route_trace(self, client):
        """Bug-7409: the headless query response must include a ``route``
        field with route_type, reason, aggregate_id, and pocket_id — a SAFE
        route summary so headless callers can see how their query was served.

        Bug-8061 / F-027-03: the route trace must NOT carry the rewritten
        physical SQL — ordinary integration credentials must not learn
        internal schema/table names or security predicates."""
        model = _make_model()
        bound = _make_bound(model)
        decision = _make_decision(route_type="source")
        # A physical rewrite that MUST NOT reach the response.
        decision.rewritten_query = 'SELECT * FROM "trgt"."43c58412260b"'

        with ExitStack() as stack:
            _headless_patches(
                stack, bound=bound, decision=decision,
                rows=[{"region": "US", "revenue": 1}],
                columns=["region", "revenue"],
            )
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(model),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "route" in data
        route = data["route"]
        assert route["route_type"] == "source"
        assert route["reason"] == "no aggregate matched"
        assert route["aggregate_id"] is None
        assert route["pocket_id"] is None
        # F-027-03: physical SQL must be redacted from the headless surface.
        assert "rewritten_query" not in route
        assert "trgt" not in resp.text
        assert "43c58412260b" not in resp.text

    @pytest.mark.asyncio
    async def test_aggregate_route_carries_aggregate_id(self, client):
        """Bug-7409: when the route is an aggregate, the route trace
        must carry the aggregate_id."""
        model = _make_model()
        bound = _make_bound(model)
        decision = _make_decision(route_type="aggregate")
        agg_id = str(uuid.uuid4())
        decision.aggregate_id = agg_id

        with ExitStack() as stack:
            _headless_patches(
                stack, bound=bound, decision=decision,
                rows=[], columns=["region", "revenue"],
            )
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(model),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        route = resp.json()["route"]
        assert route["route_type"] == "aggregate"
        assert route["aggregate_id"] == agg_id


# ---------------------------------------------------------------------------
# Bug-7418: discovery reads deployed snapshot (not live draft)
# ---------------------------------------------------------------------------

class TestHeadlessSnapshotDiscovery:
    @pytest.mark.asyncio
    async def test_list_measures_reads_deployed_snapshot(self, client):
        """Bug-7418: when a model is deployed, list_measures must resolve
        from the deployed snapshot, not the live DB tables. This ensures
        discovery agrees with what the execution binder binds."""
        from src.semantic import snapshot_resolver

        model = _make_model()
        snap_measure = _make_measure("snap_revenue")
        deployed_shape = MagicMock()
        deployed_shape.measures = [snap_measure]

        db = _mock_tenant_db()
        db.get = AsyncMock(return_value=model)

        with (
            patch("src.api.headless.get_tenant_db", _async_gen(db)),
            _no_persona(),
            patch(
                "src.api.headless.resolve_deployed_shape",
                AsyncMock(return_value=deployed_shape),
            ),
            patch(
                "src.api.headless.cls_blocked_measure_and_dimension_ids",
                AsyncMock(return_value=(set(), set())),
            ),
        ):
            resp = await client.get(
                f"/api/v1/headless/models/{TEST_MODEL_ID}/measures",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "snap_revenue"

    @pytest.mark.asyncio
    async def test_list_dimensions_reads_deployed_snapshot(self, client):
        """Bug-7418: when a model is deployed, list_dimensions must resolve
        from the deployed snapshot, not the live DB tables."""
        dim = _make_dimension("snap_region")
        deployed_shape = MagicMock()
        deployed_shape.dimensions = [dim]
        deployed_shape.columns_by_id = {
            str(dim.source_column_id): {"data_type": "varchar"},
        }

        model = _make_model()
        db = _mock_tenant_db()
        db.get = AsyncMock(return_value=model)

        with (
            patch("src.api.headless.get_tenant_db", _async_gen(db)),
            _no_persona(),
            patch(
                "src.api.headless.resolve_deployed_shape",
                AsyncMock(return_value=deployed_shape),
            ),
            patch(
                "src.api.headless.cls_blocked_measure_and_dimension_ids",
                AsyncMock(return_value=(set(), set())),
            ),
        ):
            resp = await client.get(
                f"/api/v1/headless/models/{TEST_MODEL_ID}/dimensions",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "snap_region"
        assert data[0]["data_type"] == "varchar"

    @pytest.mark.asyncio
    async def test_list_measures_falls_back_to_live_when_undeployed(self, client):
        """Bug-7418: when a model is NOT deployed, list_measures falls back
        to the live DB tables (resolve_deployed_shape returns None)."""
        live_measure = _make_measure("live_cost")
        model = _make_model()
        model.deployed_version_id = None  # undeployed

        db = _mock_tenant_db()
        db.get = AsyncMock(return_value=model)
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [live_measure]
        db.execute = AsyncMock(return_value=result_mock)

        with (
            patch("src.api.headless.get_tenant_db", _async_gen(db)),
            _no_persona(),
            patch(
                "src.api.headless.resolve_deployed_shape",
                AsyncMock(return_value=None),
            ),
            patch(
                "src.api.headless.cls_blocked_measure_and_dimension_ids",
                AsyncMock(return_value=(set(), set())),
            ),
        ):
            resp = await client.get(
                f"/api/v1/headless/models/{TEST_MODEL_ID}/measures",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "live_cost"

    @pytest.mark.asyncio
    async def test_cls_closure_uses_snapshot_measures(self, client):
        """Bug-7418 (GAP 1): the CLS blocked-ID computation must use the
        snapshot-resolved measures AND dimensions, not live draft tables.
        A post-deploy draft column change must not affect discovery's CLS
        blocked set. Both kwargs must be snapshot-sourced on the deployed
        path (even though list_measures only uses blocked_measures)."""
        snap_measure = _make_measure("snap_revenue")
        snap_dim = _make_dimension("snap_region")
        model = _make_model()
        deployed_shape = MagicMock()
        deployed_shape.measures = [snap_measure]
        deployed_shape.dimensions = [snap_dim]

        db = _mock_tenant_db()
        db.get = AsyncMock(return_value=model)

        persona = types.SimpleNamespace(
            id=uuid.uuid4(),
            included_measure_ids=[str(snap_measure.id)],
            included_dimension_ids=[],
        )

        cls_mock = AsyncMock(return_value=(set(), set()))

        with (
            patch("src.api.headless.get_tenant_db", _async_gen(db)),
            patch(
                "src.api.headless.resolve_execution_persona",
                AsyncMock(return_value=persona),
            ),
            patch(
                "src.api.headless.resolve_deployed_shape",
                AsyncMock(return_value=deployed_shape),
            ),
            patch(
                "src.api.headless.cls_blocked_measure_and_dimension_ids",
                cls_mock,
            ),
        ):
            resp = await client.get(
                f"/api/v1/headless/models/{TEST_MODEL_ID}/measures",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        # The CLS function must have been called with BOTH snapshot kwargs
        # so neither set falls back to live drafts on the deployed path.
        cls_mock.assert_awaited_once()
        call_kwargs = cls_mock.await_args.kwargs
        assert call_kwargs["measures"] is not None
        assert len(call_kwargs["measures"]) == 1
        assert call_kwargs["measures"][0].name == "snap_revenue"
        assert call_kwargs["dimensions"] is not None
        assert len(call_kwargs["dimensions"]) == 1
        assert call_kwargs["dimensions"][0].name == "snap_region"

    @pytest.mark.asyncio
    async def test_cls_closure_uses_snapshot_dimensions(self, client):
        """Bug-7418 (GAP 1): the CLS blocked-ID computation for dimensions
        must use snapshot-resolved dimensions AND measures, not live draft
        tables. Both kwargs must be snapshot-sourced on the deployed path."""
        snap_dim = _make_dimension("snap_region")
        snap_measure = _make_measure("snap_revenue")
        model = _make_model()
        deployed_shape = MagicMock()
        deployed_shape.dimensions = [snap_dim]
        deployed_shape.measures = [snap_measure]
        deployed_shape.columns_by_id = {
            str(snap_dim.source_column_id): {"data_type": "varchar"},
        }

        db = _mock_tenant_db()
        db.get = AsyncMock(return_value=model)

        persona = types.SimpleNamespace(
            id=uuid.uuid4(),
            included_measure_ids=[],
            included_dimension_ids=[str(snap_dim.id)],
        )

        cls_mock = AsyncMock(return_value=(set(), set()))

        with (
            patch("src.api.headless.get_tenant_db", _async_gen(db)),
            patch(
                "src.api.headless.resolve_execution_persona",
                AsyncMock(return_value=persona),
            ),
            patch(
                "src.api.headless.resolve_deployed_shape",
                AsyncMock(return_value=deployed_shape),
            ),
            patch(
                "src.api.headless.cls_blocked_measure_and_dimension_ids",
                cls_mock,
            ),
        ):
            resp = await client.get(
                f"/api/v1/headless/models/{TEST_MODEL_ID}/dimensions",
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        cls_mock.assert_awaited_once()
        call_kwargs = cls_mock.await_args.kwargs
        assert call_kwargs["dimensions"] is not None
        assert len(call_kwargs["dimensions"]) == 1
        assert call_kwargs["dimensions"][0].name == "snap_region"
        # Both kwargs must be snapshot-sourced on the deployed path.
        assert call_kwargs["measures"] is not None
        assert len(call_kwargs["measures"]) == 1
        assert call_kwargs["measures"][0].name == "snap_revenue"

    @pytest.mark.asyncio
    async def test_deployed_model_unresolvable_snapshot_fails_closed_measures(self, client):
        """Bug-7418 (GAP 2): a deployed model with an unresolvable snapshot
        must fail closed (409) on list_measures, not silently serve draft
        metadata that may disagree with what execution binds."""
        model = _make_model()
        # deployed_version_id is set (truthy) but resolve_deployed_shape returns None
        db = _mock_tenant_db()
        db.get = AsyncMock(return_value=model)

        with (
            patch("src.api.headless.get_tenant_db", _async_gen(db)),
            _no_persona(),
            patch(
                "src.api.headless.resolve_deployed_shape",
                AsyncMock(return_value=None),
            ),
        ):
            resp = await client.get(
                f"/api/v1/headless/models/{TEST_MODEL_ID}/measures",
                headers=_auth_headers(),
            )

        assert resp.status_code == 409
        assert "snapshot" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_deployed_model_unresolvable_snapshot_fails_closed_dimensions(self, client):
        """Bug-7418 (GAP 2): a deployed model with an unresolvable snapshot
        must fail closed (409) on list_dimensions, not silently serve draft
        metadata that may disagree with what execution binds."""
        model = _make_model()
        db = _mock_tenant_db()
        db.get = AsyncMock(return_value=model)

        with (
            patch("src.api.headless.get_tenant_db", _async_gen(db)),
            _no_persona(),
            patch(
                "src.api.headless.resolve_deployed_shape",
                AsyncMock(return_value=None),
            ),
        ):
            resp = await client.get(
                f"/api/v1/headless/models/{TEST_MODEL_ID}/dimensions",
                headers=_auth_headers(),
            )

        assert resp.status_code == 409
        assert "snapshot" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Bug-8453 / R6 finding 2 — the headless denial channel must be REVERTIBLE-PROOF
#
# The R5 fix (adding ``security_rules_applied`` to HeadlessQueryResponse and
# populating it) shipped with zero coverage: reverting both lines left 236
# headless tests passing. That is the same "producer added, nothing asserts it"
# gap that let five earlier misses through in this lane. These tests live in
# THIS file deliberately -- it owns the autouse ``_allow_project_access``
# fixture that the route needs.
#
# The surface matters: /api/v1/headless/query is a customer-facing API whose
# Bug-7998 contract states, in the same payload, that the result is complete.
# A denial returning `rows: [], complete: true` with no denial channel tells an
# API consumer authoritatively that the empty set IS the whole dataset.
# ---------------------------------------------------------------------------

_DENY_ALL_RULE = {"rule_id": "__deny_all__", "rule_name": "coverage deny-all"}
_NARROWING_RULE = {"rule_id": "region-rule", "rule_name": "EMEA only"}


def _decision_with_rules(rules):
    decision = _make_decision()
    decision.security_rules_applied = rules
    return decision


async def _run_headless(client, *, rules, rows=None, columns=None):
    model = _make_model()
    bound = _make_bound(model)
    with ExitStack() as stack:
        _headless_patches(
            stack,
            bound=bound,
            decision=_decision_with_rules(rules),
            rows=rows if rows is not None else [],
            columns=columns if columns is not None else [],
        )
        resp = await client.post(
            "/api/v1/headless/query",
            json=_query_body(model),
            headers=_auth_headers(),
        )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_headless_publishes_the_deny_all_sentinel(client):
    """The producer half. Without this field an API consumer cannot tell "your
    row-security policy grants you no rows" from "there is no data" -- and the
    ``complete`` flag in the same payload actively asserts the latter."""
    body = await _run_headless(client, rules=[_DENY_ALL_RULE])
    assert body["security_rules_applied"] == ["__deny_all__"], body
    assert body["rows"] == []


@pytest.mark.asyncio
async def test_headless_publishes_a_narrowing_rule_without_the_sentinel(client):
    """A narrowing rule returns CORRECT, caller-scoped rows. It must be
    reported (so a consumer can disclose the scoping) but must NOT carry the
    deny-all sentinel, or every row-restricted user's valid result would be
    treated as a denial."""
    body = await _run_headless(
        client,
        rules=[_NARROWING_RULE],
        rows=[{"region": "EMEA", "revenue": 10}],
        columns=["region", "revenue"],
    )
    assert body["security_rules_applied"] == ["region-rule"]
    assert "__deny_all__" not in body["security_rules_applied"]
    assert body["rows"] == [{"region": "EMEA", "revenue": 10}]


@pytest.mark.asyncio
async def test_headless_reports_no_rules_when_row_security_is_inactive(client):
    """Regression guard the other way: an ordinary empty result must NOT look
    like a denial, or every genuinely-empty slice would be mis-reported as a
    permissions problem."""
    body = await _run_headless(client, rules=[])
    assert body["security_rules_applied"] == []


@pytest.mark.asyncio
async def test_headless_complete_stays_row_cap_only_under_a_denial(client):
    """R6 finding 4 -- ONE field, ONE meaning, on every surface.

    ``complete`` answers only "did the row cap withhold anything?". R5 briefly
    overloaded it here to also mean "not denied", which made the same field
    mean different things on headless and /plugin/execute and would force a
    consumer to branch per surface for one property. The denial signal is
    ``security_rules_applied``; ``complete`` must agree with ``has_more``.
    """
    body = await _run_headless(client, rules=[_DENY_ALL_RULE])
    assert body["has_more"] is False
    assert body["complete"] is True, (
        "complete must remain the inverse of has_more; the denial is reported "
        "by security_rules_applied, not by overloading the completeness flag"
    )
    assert body["security_rules_applied"] == ["__deny_all__"]


# ---------------------------------------------------------------------------
# Disclosure is decided by ENTITLEMENT, not by authentication method.
#
# Decision 2026-08-11, option C
# (docs/questions/questions_disclosure-by-entitlement-not-auth-method.md): the
# embed physical-detail withhold was REMOVED. It gated on the token TYPE, so
# this route answered the same question differently depending on which door the
# caller used. These tests previously asserted the withhold; they now pin the
# replacement contract, which is the stronger property to guard because a
# silently reintroduced token-type branch is exactly what would break it.
#
# This is an end-to-end wiring guard driving the real ASGI route with real
# tokens and scanning the SERVED BODY, not a unit test of a helper.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_and_tenant_sessions_receive_identical_route_detail(client):
    """An embed principal and a tenant principal get the SAME physical detail.

    Fails against the pre-decision code, where the embed body was stripped of
    the aggregate/pocket ids and had its ``reason`` replaced by a sentinel
    token while the tenant body kept both.
    """
    def _decision():
        decision = _make_decision(route_type="aggregate")
        decision.reason = (
            "served from aggregate agg_sales_v3 in schema acme_aggregates "
            "(grain region_code)"
        )
        decision.aggregate_id = "agg-uuid-1"
        decision.pocket_id = "pkt-uuid-1"
        return decision

    bodies = {}
    for label, headers in (
        ("embed", _embed_headers(None)),
        ("tenant", _auth_headers()),
    ):
        model = _make_model()
        with ExitStack() as stack:
            _headless_patches(
                stack, bound=_make_bound(model), decision=_decision(),
                rows=[{"region": "US", "revenue": 1}],
                columns=["region", "revenue"],
            )
            resp = await client.post(
                "/api/v1/headless/query",
                json=_query_body(model),
                headers=headers,
            )
        assert resp.status_code == 200, f"{label}: {resp.text}"
        bodies[label] = resp.json()

    assert bodies["embed"]["route"] == bodies["tenant"]["route"], (
        "the route trace still differs by authentication method; disclosure "
        "must be decided by entitlement, not by how the caller signed in"
    )
    # And it is the REAL detail both receive, not a jointly-stripped one.
    for label, data in bodies.items():
        assert data["route"]["aggregate_id"] == "agg-uuid-1", label
        assert data["route"]["pocket_id"] == "pkt-uuid-1", label
        assert "agg_sales_v3" in data["route"]["reason"], label
        assert "acme_aggregates" in data["route"]["reason"], label
        assert data["route"]["route_type"] == "aggregate", label
    # Rows and the Bug-8453 denial channel are unaffected either way.
    assert bodies["embed"]["rows"] == [{"region": "US", "revenue": 1}]
    assert "security_rules_applied" in bodies["embed"]


@pytest.mark.asyncio
async def test_headless_route_trace_publishes_no_redaction_flag(client):
    """``reason_redacted`` was removed with the control that was its only
    writer. A permanently-false "was this withheld" flag is false assurance,
    so the field must not come back as a decorative default."""
    model = _make_model()
    with ExitStack() as stack:
        _headless_patches(
            stack, bound=_make_bound(model), decision=_make_decision(),
            rows=[], columns=["region"],
        )
        resp = await client.post(
            "/api/v1/headless/query",
            json=_query_body(model),
            headers=_embed_headers(None),
        )
    assert resp.status_code == 200, resp.text
    assert "reason_redacted" not in resp.json()["route"]
