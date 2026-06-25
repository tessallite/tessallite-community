"""Direct tests for POST /api/v1/plugin/execute security enforcement.

Covers Finding 3 from the backend change validation report:
- Audience enforcement via resolve_execution_persona
- Object allow-list enforcement via enforce_persona_gate
- Project/model mismatch rejection
- Filter operator validation
- Embed persona lock enforcement
- Endpoint-level integration tests via httpx/ASGI
"""
from __future__ import annotations

import types
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from jose import jwt

from shared.config.settings import get_settings
from src.api.plugin import (
    PluginExecuteRequest,
    PluginFilter,
    _build_filters,
    _compute_fingerprint,
)
from src.security.persona_gate import (
    enforce_persona,
    resolve_execution_persona,
)

from conftest import make_bound_query, make_dimension, make_measure


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user(
    *,
    email: str = "analyst@acme.test",
    tenant_id: str = "acme",
    role: str | None = None,
):
    from shared.auth.middleware import CurrentUser
    return CurrentUser(
        user_id="u-1",
        tenant_id=tenant_id,
        email=email,
        role=role,
    )


def _embed_user(
    *,
    persona_id: str | None = None,
    model_ids: list[str] | None = None,
):
    from shared.auth.middleware import CurrentEmbedUser
    return CurrentEmbedUser(
        user_id="embed-1",
        tenant_id="acme",
        email="embed@acme.test",
        persona_id=persona_id,
        model_ids=model_ids or [],
    )


_DEFAULT_MODEL_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture(autouse=True)
def _allow_project_access(monkeypatch):
    monkeypatch.setattr(
        "src.api.plugin.load_authorized_model",
        AsyncMock(return_value=None),
    )


def _persona(
    *,
    pid: uuid.UUID | None = None,
    name: str = "Sales",
    model_id: uuid.UUID = _DEFAULT_MODEL_ID,
    measure_ids: list | None = None,
    dimension_ids: list | None = None,
    hierarchy_ids: list | None = None,
    audience_roles: list | None = None,
    default_filters: dict | None = None,
    includes_hidden_columns: bool = False,
):
    return types.SimpleNamespace(
        id=pid or uuid.uuid4(),
        model_id=model_id,
        name=name,
        included_measure_ids=measure_ids or [],
        included_dimension_ids=dimension_ids or [],
        included_hierarchy_ids=hierarchy_ids or [],
        audience_roles=audience_roles or [],
        default_filters=default_filters or {},
        includes_hidden_columns=includes_hidden_columns,
    )


def _fake_db(personas: list | None = None):
    """Minimal async DB mock supporting .get(), .execute(), and async-for."""
    personas = personas or []
    persona_map = {str(p.id): p for p in personas}

    class FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return self

        def all(self):
            return self._rows

        def scalar_one_or_none(self):
            return self._rows[0] if self._rows else None

    db = AsyncMock()
    db.get = AsyncMock(side_effect=lambda cls, key: persona_map.get(str(key)))

    def _execute(stmt):
        from shared.db.models import Persona, Dimension
        target = getattr(stmt, "column_descriptions", None)
        matching = [p for p in personas if True]
        return FakeResult(matching)

    db.execute = AsyncMock(side_effect=_execute)
    return db


# ---------------------------------------------------------------------------
# Filter validation (_build_filters)
# ---------------------------------------------------------------------------


class TestBuildFilters:
    def test_valid_eq_filter(self):
        filters = _build_filters([PluginFilter(dimension="region", operator="eq", value="US")])
        assert len(filters) == 1
        assert filters[0].dimension_name == "region"
        assert filters[0].operator == "eq"
        assert filters[0].value == "US"

    def test_invalid_operator_422(self):
        with pytest.raises(HTTPException) as exc:
            _build_filters([PluginFilter(dimension="x", operator="regex")])
        assert exc.value.status_code == 422

    def test_between_needs_two_values(self):
        with pytest.raises(HTTPException) as exc:
            _build_filters([PluginFilter(dimension="x", operator="between", values=[1])])
        assert exc.value.status_code == 422
        assert "exactly two" in exc.value.detail.lower()

    def test_between_with_two_values(self):
        filters = _build_filters([
            PluginFilter(dimension="x", operator="between", values=[1, 10])
        ])
        assert filters[0].value == (1, 10)

    def test_in_empty_values_422(self):
        with pytest.raises(HTTPException) as exc:
            _build_filters([PluginFilter(dimension="x", operator="in", values=[])])
        assert exc.value.status_code == 422
        assert "at least one" in exc.value.detail.lower()

    def test_not_in_empty_values_422(self):
        with pytest.raises(HTTPException) as exc:
            _build_filters([PluginFilter(dimension="x", operator="not_in", values=[])])
        assert exc.value.status_code == 422

    def test_in_with_values(self):
        filters = _build_filters([
            PluginFilter(dimension="region", operator="in", values=["US", "UK"])
        ])
        assert filters[0].value == ["US", "UK"]

    def test_is_null_operator(self):
        filters = _build_filters([PluginFilter(dimension="x", operator="is_null")])
        assert filters[0].operator == "is_null"
        assert filters[0].value is None

    def test_neq_operator_accepted(self):
        filters = _build_filters([PluginFilter(dimension="x", operator="neq", value="Z")])
        assert filters[0].operator == "neq"

    def test_ne_normalized_to_neq(self):
        filters = _build_filters([PluginFilter(dimension="x", operator="ne", value="Z")])
        assert filters[0].operator == "neq"


class TestOrderByDirectionValidation:
    """F-027-17: order-by direction validation moved to the shared
    ``filter_contract.normalize_order_by`` so headless and plugin enforce
    the SAME outcomes. These assert the post-unification behaviour:
    valid directions normalize (case-insensitive); an invalid direction,
    an injection payload, or a SQL fragment is rejected with a 422 — the
    direction slot can never carry a raw SQL fragment to the rewriter."""

    def test_valid_asc(self):
        from src.api.plugin import PluginOrderBy
        from src.api.filter_contract import normalize_order_by
        ob = PluginOrderBy(field="revenue", direction="asc")
        assert normalize_order_by([ob]) == [("revenue", "asc")]

    def test_valid_desc_uppercase(self):
        from src.api.plugin import PluginOrderBy
        from src.api.filter_contract import normalize_order_by
        ob = PluginOrderBy(field="revenue", direction="DESC")
        assert normalize_order_by([ob]) == [("revenue", "desc")]

    def test_injection_payload_rejected(self):
        from src.api.plugin import PluginOrderBy
        from src.api.filter_contract import normalize_order_by
        ob = PluginOrderBy(field="revenue", direction="desc; DROP TABLE users--")
        with pytest.raises(HTTPException) as exc:
            normalize_order_by([ob])
        assert exc.value.status_code == 422

    def test_sql_fragment_rejected(self):
        from src.api.plugin import PluginOrderBy
        from src.api.filter_contract import normalize_order_by
        ob = PluginOrderBy(field="x", direction="asc UNION SELECT 1")
        with pytest.raises(HTTPException) as exc:
            normalize_order_by([ob])
        assert exc.value.status_code == 422


class TestLimitOffsetValidation:
    def test_negative_limit_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            PluginExecuteRequest(
                project_id="p1", model_id="m1", measures=["rev"], limit=-1,
            )

    def test_zero_limit_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            PluginExecuteRequest(
                project_id="p1", model_id="m1", measures=["rev"], limit=0,
            )

    def test_negative_offset_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            PluginExecuteRequest(
                project_id="p1", model_id="m1", measures=["rev"], offset=-5,
            )

    def test_valid_limit_offset(self):
        req = PluginExecuteRequest(
            project_id="p1", model_id="m1", measures=["rev"], limit=100, offset=0,
        )
        assert req.limit == 100
        assert req.offset == 0


# ---------------------------------------------------------------------------
# Persona enforcement on bound query
# ---------------------------------------------------------------------------


class TestPluginPersonaEnforcement:
    def test_measure_blocked_by_persona_403(self):
        m_allowed = make_measure("revenue")
        m_hidden = make_measure("cost")
        bound = make_bound_query(dimensions=[], measures=[m_allowed, m_hidden])
        p = _persona(measure_ids=[str(m_allowed.id)])

        with pytest.raises(HTTPException) as exc:
            enforce_persona(p, bound)
        assert exc.value.status_code == 403
        assert exc.value.detail["object_name"] == "cost"

    def test_dimension_blocked_by_persona_403(self):
        d_allowed = make_dimension("region")
        d_hidden = make_dimension("secret")
        bound = make_bound_query(dimensions=[d_allowed, d_hidden], measures=[])
        p = _persona(dimension_ids=[str(d_allowed.id)])

        with pytest.raises(HTTPException) as exc:
            enforce_persona(p, bound)
        assert exc.value.status_code == 403
        assert exc.value.detail["object_name"] == "secret"

    def test_allowed_persona_passes(self):
        m = make_measure("revenue")
        d = make_dimension("region")
        bound = make_bound_query(dimensions=[d], measures=[m])
        p = _persona(measure_ids=[str(m.id)], dimension_ids=[str(d.id)])

        enforce_persona(p, bound)
        assert len(bound.resolved_measures) == 1
        assert len(bound.resolved_dimensions) == 1


# ---------------------------------------------------------------------------
# Audience enforcement via resolve_execution_persona
# ---------------------------------------------------------------------------


class TestAudienceEnforcement:
    @pytest.mark.asyncio
    async def test_privileged_user_no_persona_returns_none(self):
        user = _user(role="tenant_admin")
        model_id = uuid.uuid4()
        db = _fake_db()
        result = await resolve_execution_persona(
            db, current_user=user, model_id=str(model_id),
            requested_persona_id=None,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_embed_locked_persona_applied(self):
        pid = uuid.uuid4()
        model_id = _DEFAULT_MODEL_ID
        p = _persona(pid=pid, model_id=model_id)

        load_result = MagicMock()
        load_result.scalar_one_or_none.return_value = p
        db = AsyncMock()
        db.execute = AsyncMock(return_value=load_result)

        user = _embed_user(persona_id=str(pid), model_ids=[str(model_id)])
        result = await resolve_execution_persona(
            db, current_user=user, model_id=str(model_id),
            requested_persona_id=None,
        )
        assert result is not None
        assert str(result.id) == str(pid)

    @pytest.mark.asyncio
    async def test_embed_conflict_403(self):
        pid = uuid.uuid4()
        other = uuid.uuid4()
        model_id = _DEFAULT_MODEL_ID
        user = _embed_user(persona_id=str(pid), model_ids=[str(model_id)])
        db = AsyncMock()
        with pytest.raises(HTTPException) as exc:
            await resolve_execution_persona(
                db, current_user=user, model_id=str(model_id),
                requested_persona_id=str(other),
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_unassigned_user_voluntary_persona(self):
        """User with no persona assignments can use any persona as voluntary
        filter (per the resolution matrix: 0 assignments → unrestricted)."""
        pid = uuid.uuid4()
        model_id = _DEFAULT_MODEL_ID
        p = _persona(pid=pid, model_id=model_id, audience_roles=["sales_viewer"])
        user = _user(role="viewer")

        all_result = MagicMock()
        all_result.scalars.return_value.all.return_value = []
        load_result = MagicMock()
        load_result.scalar_one_or_none.return_value = p

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[all_result, load_result])

        result = await resolve_execution_persona(
            db, current_user=user, model_id=str(model_id),
            requested_persona_id=str(pid),
        )
        assert result is not None
        assert str(result.id) == str(pid)

    @pytest.mark.asyncio
    async def test_single_assigned_user_auto_resolves(self):
        pid = uuid.uuid4()
        model_id = _DEFAULT_MODEL_ID
        p = _persona(pid=pid, model_id=model_id, audience_roles=["analyst"])
        user = _user(role="analyst")

        all_result = MagicMock()
        all_result.scalars.return_value.all.return_value = [p]
        db = AsyncMock()
        db.execute = AsyncMock(return_value=all_result)

        result = await resolve_execution_persona(
            db, current_user=user, model_id=str(model_id),
            requested_persona_id=None,
        )
        assert result is not None
        assert str(result.id) == str(pid)

    @pytest.mark.asyncio
    async def test_single_assigned_user_rejects_other_persona(self):
        pid = uuid.uuid4()
        other = uuid.uuid4()
        model_id = _DEFAULT_MODEL_ID
        p = _persona(pid=pid, model_id=model_id, audience_roles=["analyst"])
        user = _user(role="analyst")

        all_result = MagicMock()
        all_result.scalars.return_value.all.return_value = [p]
        db = AsyncMock()
        db.execute = AsyncMock(return_value=all_result)

        with pytest.raises(HTTPException) as exc:
            await resolve_execution_persona(
                db, current_user=user, model_id=str(model_id),
                requested_persona_id=str(other),
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_multi_assigned_user_must_pick(self):
        model_id = _DEFAULT_MODEL_ID
        p1 = _persona(pid=uuid.uuid4(), model_id=model_id, audience_roles=["analyst"])
        p2 = _persona(pid=uuid.uuid4(), model_id=model_id, audience_roles=["analyst"])
        user = _user(role="analyst")

        all_result = MagicMock()
        all_result.scalars.return_value.all.return_value = [p1, p2]
        db = AsyncMock()
        db.execute = AsyncMock(return_value=all_result)

        with pytest.raises(HTTPException) as exc:
            await resolve_execution_persona(
                db, current_user=user, model_id=str(model_id),
                requested_persona_id=None,
            )
        assert exc.value.status_code == 403
        assert "multiple" in exc.value.detail.lower()


# ---------------------------------------------------------------------------
# Project/model mismatch
# ---------------------------------------------------------------------------


class TestProjectModelMismatch:
    def test_project_mismatch_raises_403(self):
        body = PluginExecuteRequest(
            project_id="project-A",
            model_id="model-1",
            measures=["revenue"],
        )
        model = types.SimpleNamespace(
            id="model-1",
            project_id="project-B",
        )
        assert str(model.project_id) != body.project_id


# ---------------------------------------------------------------------------
# Fingerprint computation
# ---------------------------------------------------------------------------


class TestFingerprint:
    def test_fingerprint_deterministic(self):
        body = PluginExecuteRequest(
            project_id="p1", model_id="m1",
            measures=["revenue", "cost"],
            dimensions=["region"],
        )
        fp1 = _compute_fingerprint(body)
        fp2 = _compute_fingerprint(body)
        assert fp1 == fp2
        assert len(fp1) == 64

    def test_fingerprint_changes_with_measures(self):
        b1 = PluginExecuteRequest(
            project_id="p1", model_id="m1", measures=["revenue"],
        )
        b2 = PluginExecuteRequest(
            project_id="p1", model_id="m1", measures=["cost"],
        )
        assert _compute_fingerprint(b1) != _compute_fingerprint(b2)


# ---------------------------------------------------------------------------
# Endpoint-level tests via ASGI transport
# ---------------------------------------------------------------------------

_settings = get_settings()
_EP_PROJECT_ID = str(uuid.uuid4())
_EP_MODEL_ID = str(uuid.uuid4())


def _mint_jwt(role: str = "member") -> str:
    payload = {
        "sub": "analyst@acme.test",
        "tenant_id": "acme",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        "role": role,
    }
    return jwt.encode(payload, _settings.JWT_SECRET_KEY, algorithm=_settings.JWT_ALGORITHM)


def _ep_auth() -> dict:
    return {"Authorization": f"Bearer {_mint_jwt()}"}


def _async_gen(db):
    async def _gen(*args, **kwargs):
        yield db
    return _gen


def _ep_model(project_id: str = _EP_PROJECT_ID):
    return types.SimpleNamespace(
        id=uuid.UUID(_EP_MODEL_ID),
        project_id=project_id,
        slug="test-model",
        display_name="Test Model",
        deployed_version_id="v1",
    )


def _ep_bound(model=None, dim_specs=None):
    """Build a bound query whose dimension stubs MIRROR the real ``Dimension``
    ORM: no ``data_type`` attribute (Bug-1057 regression guard — a stub that
    carries ``data_type`` masks the always-default annotation bug). The real
    physical type lives on the dimension's source ``ModelColumn``, joined in
    ``plugin._resolve_dimension_data_types``.

    ``dim_specs`` is a list of ``(name, display_name, source_column_id_or_None)``;
    defaults to a single ``region`` dimension with a source column.
    """
    m = model or _ep_model()
    mid = uuid.uuid4()
    bound = MagicMock()
    bound.model = m
    bound.resolved_measures = [types.SimpleNamespace(
        id=mid, name="revenue", display_name="Revenue",
        default_agg="sum", format=None,
    )]
    if dim_specs is None:
        dim_specs = [("region", "Region", uuid.uuid4())]
    bound.resolved_dimensions = [
        types.SimpleNamespace(
            id=uuid.uuid4(), name=name, display_name=display,
            source_column_id=src_col_id,
        )
        for (name, display, src_col_id) in dim_specs
    ]
    bound.resolved_filters = []
    return bound


def _stub_dim_types(db, bound, type_by_name):
    """Configure ``db.execute`` so the annotation data_type join
    (``_resolve_dimension_data_types``) returns the requested physical types.

    Maps each dimension's ``source_column_id`` to ``type_by_name[name]`` and
    makes the mocked ``db.execute`` result's ``.all()`` yield
    ``(column_id, data_type)`` rows — exactly the shape the real
    ``select(ModelColumn.id, ModelColumn.data_type)`` query returns.
    """
    rows = [
        (d.source_column_id, type_by_name.get(d.name))
        for d in bound.resolved_dimensions
        if d.source_column_id is not None and d.name in type_by_name
    ]
    result = MagicMock()
    result.all.return_value = rows
    db.execute = AsyncMock(return_value=result)
    return db


class _PipelineMocks:
    """Routes-level patches for the shared observed pipeline (B10).

    The plugin endpoint executes through ``routes.execute_with_observation``;
    only the executor function and the persistence/audit sinks are mocked,
    so the real logging machinery (F-030-01) is exercised by these tests.
    """

    def __init__(self, rows=None, columns=None):
        self.execute = AsyncMock(
            return_value=(rows or [], 0, columns or [], MagicMock())
        )
        self.log_query = AsyncMock()
        self.log_query_miss = AsyncMock()
        self.audit = AsyncMock()

    def patches(self):
        return (
            patch("src.api.routes.execute_routed_query", self.execute),
            patch("src.api.routes.log_query", self.log_query),
            patch("src.api.routes.log_query_miss", self.log_query_miss),
            patch("src.api.routes.audit", self.audit),
            patch("src.api.routes.audit_filters_present", MagicMock()),
            patch("src.api.routes.audit_result_columns", MagicMock()),
        )

    @contextmanager
    def applied(self):
        with ExitStack() as stack:
            for p in self.patches():
                stack.enter_context(p)
            yield self


@pytest.fixture
async def plugin_client():
    from src.main import app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as ac:
        yield ac


class TestPluginEndpoint:
    @pytest.mark.asyncio
    async def test_successful_execution(self, plugin_client):
        model = _ep_model()
        bound = _ep_bound(model)
        mock_decision = MagicMock()
        mock_rows = [{"region": "US", "revenue": 1000}]
        mock_columns = ["region", "revenue"]
        db = AsyncMock()
        _stub_dim_types(db, bound, {"region": "string"})
        pipeline = _PipelineMocks(rows=mock_rows, columns=mock_columns)

        with (
            patch("src.api.plugin.get_tenant_db", _async_gen(db)),
            patch("src.api.plugin.resolve_execution_persona", AsyncMock(return_value=None)),
            patch("src.api.plugin.bind_query_to_model", AsyncMock(return_value=bound)),
            patch("src.api.plugin.route_query", AsyncMock(return_value=mock_decision)),
            pipeline.applied(),
        ):
            resp = await plugin_client.post(
                "/api/v1/plugin/execute",
                json={
                    "project_id": _EP_PROJECT_ID,
                    "model_id": _EP_MODEL_ID,
                    "measures": ["revenue"],
                    "dimensions": ["region"],
                },
                headers=_ep_auth(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "query" in data
        assert "data" in data
        assert data["data"] == [{"region": "US", "revenue": 1000}]
        assert data["query"]["measures"] == ["revenue"]
        assert "annotation" in data
        # F-030-01: the plugin path must write a QueryLog row with the
        # JWT-derived identity, and emit the platform audit record.
        pipeline.log_query.assert_awaited_once()
        kwargs = pipeline.log_query.await_args.kwargs
        assert kwargs["user_identity"] == "analyst@acme.test"
        assert kwargs["rows_returned"] == 1
        # F-025-12: the QueryLog row is tagged client_kind="plugin" so Excel
        # workload is attributable in telemetry / audit / ROI scoring.
        assert kwargs["client_kind"] == "plugin"
        pipeline.audit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_source_route_writes_miss_log(self, plugin_client):
        """F-030-01: source-routed plugin queries must feed the optimizer's
        QueryMissLog so hot Excel workloads can earn aggregates."""
        model = _ep_model()
        bound = _ep_bound(model)
        mock_decision = MagicMock()
        mock_decision.route_type = "source"
        mock_decision.reason = "no aggregate matched"
        mock_decision.aggregate_skipped_reasons = None
        db = AsyncMock()
        _stub_dim_types(db, bound, {"region": "string"})
        pipeline = _PipelineMocks()

        with (
            patch("src.api.plugin.get_tenant_db", _async_gen(db)),
            patch("src.api.plugin.resolve_execution_persona", AsyncMock(return_value=None)),
            patch("src.api.plugin.bind_query_to_model", AsyncMock(return_value=bound)),
            patch("src.api.plugin.route_query", AsyncMock(return_value=mock_decision)),
            pipeline.applied(),
        ):
            resp = await plugin_client.post(
                "/api/v1/plugin/execute",
                json={
                    "project_id": _EP_PROJECT_ID,
                    "model_id": _EP_MODEL_ID,
                    "measures": ["revenue"],
                },
                headers=_ep_auth(),
            )

        assert resp.status_code == 200
        pipeline.log_query_miss.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_raw_query_carries_canonical_payload(self, plugin_client):
        """B10 round-1 finding 3: plugin queries must not log an empty
        raw_query — the canonical semantic payload goes into the
        LogicalQuery so QueryLog previews work."""
        import json as _json

        model = _ep_model()
        bound = _ep_bound(model)
        db = AsyncMock()
        _stub_dim_types(db, bound, {"region": "string"})
        pipeline = _PipelineMocks(
            rows=[{"region": "US", "revenue": 1}], columns=["region", "revenue"],
        )
        mock_bind = AsyncMock(return_value=bound)

        with (
            patch("src.api.plugin.get_tenant_db", _async_gen(db)),
            patch("src.api.plugin.resolve_execution_persona", AsyncMock(return_value=None)),
            patch("src.api.plugin.bind_query_to_model", mock_bind),
            patch("src.api.plugin.route_query", AsyncMock(return_value=MagicMock())),
            pipeline.applied(),
        ):
            resp = await plugin_client.post(
                "/api/v1/plugin/execute",
                json={
                    "project_id": _EP_PROJECT_ID,
                    "model_id": _EP_MODEL_ID,
                    "measures": ["revenue"],
                    "dimensions": ["region"],
                    "filters": [{
                        "dimension": "region",
                        "operator": "in",
                        "values": ["US", "DE"],
                    }],
                },
                headers=_ep_auth(),
            )

        assert resp.status_code == 200
        lq = mock_bind.await_args.args[0]
        assert lq.raw_query, "plugin raw_query must not be empty (finding 3)"
        payload = _json.loads(lq.raw_query)
        assert payload["measures"] == ["revenue"]
        assert payload["dimensions"] == ["region"]
        assert payload["filters"] == [
            {"dimension": "region", "operator": "in", "values": ["US", "DE"]}
        ]

    @pytest.mark.asyncio
    async def test_project_mismatch_returns_403(self, plugin_client):
        model = _ep_model(project_id="other-project")
        bound = _ep_bound(model)
        db = AsyncMock()

        with (
            patch("src.api.plugin.get_tenant_db", _async_gen(db)),
            patch("src.api.plugin.resolve_execution_persona", AsyncMock(return_value=None)),
            patch("src.api.plugin.bind_query_to_model", AsyncMock(return_value=bound)),
        ):
            resp = await plugin_client.post(
                "/api/v1/plugin/execute",
                json={
                    "project_id": _EP_PROJECT_ID,
                    "model_id": _EP_MODEL_ID,
                    "measures": ["revenue"],
                },
                headers=_ep_auth(),
            )

        assert resp.status_code == 403
        assert "does not belong" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_invalid_filter_operator_returns_422(self, plugin_client):
        db = AsyncMock()

        with (
            patch("src.api.plugin.get_tenant_db", _async_gen(db)),
        ):
            resp = await plugin_client.post(
                "/api/v1/plugin/execute",
                json={
                    "project_id": _EP_PROJECT_ID,
                    "model_id": _EP_MODEL_ID,
                    "measures": ["revenue"],
                    "filters": [{"dimension": "x", "operator": "regex"}],
                },
                headers=_ep_auth(),
            )

        assert resp.status_code == 422
        assert "regex" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_persona_gate_called_when_persona_present(self, plugin_client):
        model = _ep_model()
        bound = _ep_bound(model)
        mock_persona = _persona(model_id=uuid.UUID(_EP_MODEL_ID))
        mock_decision = MagicMock()
        db = AsyncMock()
        _stub_dim_types(db, bound, {"region": "string"})
        pipeline = _PipelineMocks()

        mock_gate = AsyncMock()
        mock_merge = MagicMock()

        with (
            patch("src.api.plugin.get_tenant_db", _async_gen(db)),
            patch("src.api.plugin.resolve_execution_persona", AsyncMock(return_value=mock_persona)),
            patch("src.api.plugin.bind_query_to_model", AsyncMock(return_value=bound)),
            patch("src.api.plugin.enforce_persona_gate", mock_gate),
            patch("src.api.plugin.merge_default_filters", mock_merge),
            patch("src.api.plugin.route_query", AsyncMock(return_value=mock_decision)),
            pipeline.applied(),
        ):
            resp = await plugin_client.post(
                "/api/v1/plugin/execute",
                json={
                    "project_id": _EP_PROJECT_ID,
                    "model_id": _EP_MODEL_ID,
                    "measures": ["revenue"],
                },
                headers=_ep_auth(),
            )

        assert resp.status_code == 200
        mock_gate.assert_awaited_once()
        mock_merge.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_measures_returns_422(self, plugin_client):
        resp = await plugin_client.post(
            "/api/v1/plugin/execute",
            json={
                "project_id": _EP_PROJECT_ID,
                "model_id": _EP_MODEL_ID,
                "measures": [],
            },
            headers=_ep_auth(),
        )

        assert resp.status_code == 422
        assert "measure" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_response_contract(self, plugin_client):
        model = _ep_model()
        bound = _ep_bound(model)
        mock_decision = MagicMock()
        mock_rows = [{"region": "US", "revenue": 500}]
        mock_columns = ["region", "revenue"]
        db = AsyncMock()
        _stub_dim_types(db, bound, {"region": "string"})
        pipeline = _PipelineMocks(rows=mock_rows, columns=mock_columns)

        with (
            patch("src.api.plugin.get_tenant_db", _async_gen(db)),
            patch("src.api.plugin.resolve_execution_persona", AsyncMock(return_value=None)),
            patch("src.api.plugin.bind_query_to_model", AsyncMock(return_value=bound)),
            patch("src.api.plugin.route_query", AsyncMock(return_value=mock_decision)),
            pipeline.applied(),
        ):
            resp = await plugin_client.post(
                "/api/v1/plugin/execute",
                json={
                    "project_id": _EP_PROJECT_ID,
                    "model_id": _EP_MODEL_ID,
                    "measures": ["revenue"],
                    "dimensions": ["region"],
                    "limit": 100,
                },
                headers=_ep_auth(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["query"], dict)
        assert isinstance(data["data"], list)
        assert isinstance(data["annotation"], dict)
        assert "measures" in data["annotation"]
        assert "dimensions" in data["annotation"]
        assert "timeDimensions" in data["annotation"]
        assert data["query"]["limit"] == 100

    @pytest.mark.asyncio
    async def test_response_carries_route_trace(self, plugin_client):
        """F-025-20: the response must include the route decision (route_type,
        reason, rewritten SQL) so the Excel Query Trace modal can show whether
        the report hit an aggregate/pocket/source."""
        model = _ep_model()
        bound = _ep_bound(model)
        mock_decision = MagicMock()
        mock_decision.route_type = "aggregate"
        mock_decision.reason = "matched daily revenue aggregate"
        mock_decision.aggregate_id = "agg-123"
        mock_decision.pocket_id = None
        mock_decision.rewritten_query = "SELECT region, SUM(revenue) FROM agg_daily GROUP BY region"
        db = AsyncMock()
        _stub_dim_types(db, bound, {"region": "string"})
        pipeline = _PipelineMocks(rows=[{"region": "US", "revenue": 9}], columns=["region", "revenue"])

        with (
            patch("src.api.plugin.get_tenant_db", _async_gen(db)),
            patch("src.api.plugin.resolve_execution_persona", AsyncMock(return_value=None)),
            patch("src.api.plugin.bind_query_to_model", AsyncMock(return_value=bound)),
            patch("src.api.plugin.route_query", AsyncMock(return_value=mock_decision)),
            pipeline.applied(),
        ):
            resp = await plugin_client.post(
                "/api/v1/plugin/execute",
                json={
                    "project_id": _EP_PROJECT_ID,
                    "model_id": _EP_MODEL_ID,
                    "measures": ["revenue"],
                    "dimensions": ["region"],
                },
                headers=_ep_auth(),
            )

        assert resp.status_code == 200
        route = resp.json()["route"]
        assert route is not None
        assert route["route_type"] == "aggregate"
        assert route["reason"] == "matched daily revenue aggregate"
        assert route["aggregate_id"] == "agg-123"
        assert route["pocket_id"] is None
        assert "SELECT region" in route["rewritten_query"]

    @pytest.mark.asyncio
    async def test_annotation_reports_real_dimension_types(self, plugin_client):
        """Bug-1057: dimension annotation ``type`` must carry the source
        column's physical data_type — joined from ModelColumn — not the
        always-default 'string'. A boolean dimension reports 'boolean',
        a date dimension reports 'date'; a calculated dimension with no
        source column reports 'string'. The Report Builder uses these types
        for cell formatting and operator choice.
        """
        model = _ep_model()
        bound = _ep_bound(
            model,
            dim_specs=[
                ("active_flag", "Active Flag", uuid.uuid4()),
                ("business_date", "Business Date", uuid.uuid4()),
                ("calc_dim", "Calculated Dim", None),  # no source column
            ],
        )
        db = AsyncMock()
        _stub_dim_types(
            db, bound,
            {"active_flag": "boolean", "business_date": "date"},
        )
        pipeline = _PipelineMocks(
            rows=[{"active_flag": True, "business_date": "2026-01-01",
                   "calc_dim": "x", "revenue": 1}],
            columns=["active_flag", "business_date", "calc_dim", "revenue"],
        )

        with (
            patch("src.api.plugin.get_tenant_db", _async_gen(db)),
            patch("src.api.plugin.resolve_execution_persona", AsyncMock(return_value=None)),
            patch("src.api.plugin.bind_query_to_model", AsyncMock(return_value=bound)),
            patch("src.api.plugin.route_query", AsyncMock(return_value=MagicMock())),
            pipeline.applied(),
        ):
            resp = await plugin_client.post(
                "/api/v1/plugin/execute",
                json={
                    "project_id": _EP_PROJECT_ID,
                    "model_id": _EP_MODEL_ID,
                    "measures": ["revenue"],
                    "dimensions": ["active_flag", "business_date", "calc_dim"],
                },
                headers=_ep_auth(),
            )

        assert resp.status_code == 200
        dims = resp.json()["annotation"]["dimensions"]
        # The Bug-1057 defect annotated ALL of these as 'string'.
        assert dims["active_flag"]["type"] == "boolean"
        assert dims["business_date"]["type"] == "date"
        # Calculated dimension (no source column) keeps the safe default.
        assert dims["calc_dim"]["type"] == "string"

    @pytest.mark.asyncio
    async def test_order_by_injection_returns_422(self, plugin_client):
        db = AsyncMock()
        with patch("src.api.plugin.get_tenant_db", _async_gen(db)):
            resp = await plugin_client.post(
                "/api/v1/plugin/execute",
                json={
                    "project_id": _EP_PROJECT_ID,
                    "model_id": _EP_MODEL_ID,
                    "measures": ["revenue"],
                    "order_by": [{"field": "revenue", "direction": "desc; DROP TABLE users--"}],
                },
                headers=_ep_auth(),
            )
        assert resp.status_code == 422
        assert "direction" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_order_by_union_injection_returns_422(self, plugin_client):
        db = AsyncMock()
        with patch("src.api.plugin.get_tenant_db", _async_gen(db)):
            resp = await plugin_client.post(
                "/api/v1/plugin/execute",
                json={
                    "project_id": _EP_PROJECT_ID,
                    "model_id": _EP_MODEL_ID,
                    "measures": ["revenue"],
                    "order_by": [{"field": "x", "direction": "asc UNION SELECT 1"}],
                },
                headers=_ep_auth(),
            )
        assert resp.status_code == 422


class TestPluginRateLimit:
    """F-027-07: the plugin endpoint shares the per-tenant limiter and emits
    a Retry-After header on 429 (previously it had no limiter at all)."""

    @pytest.mark.asyncio
    async def test_plugin_emits_ratelimit_header(self, plugin_client):
        from src.api import rate_limit as rl

        model = _ep_model()
        bound = _ep_bound(model)
        db = AsyncMock()
        _stub_dim_types(db, bound, {"region": "string"})
        pipeline = _PipelineMocks(rows=[{"region": "US", "revenue": 1}], columns=["region", "revenue"])

        rl._buckets.clear()
        with (
            patch("src.api.plugin.get_tenant_db", _async_gen(db)),
            patch("src.api.plugin.resolve_execution_persona", AsyncMock(return_value=None)),
            patch("src.api.plugin.bind_query_to_model", AsyncMock(return_value=bound)),
            patch("src.api.plugin.route_query", AsyncMock(return_value=MagicMock())),
            pipeline.applied(),
        ):
            resp = await plugin_client.post(
                "/api/v1/plugin/execute",
                json={"project_id": _EP_PROJECT_ID, "model_id": _EP_MODEL_ID, "measures": ["revenue"], "dimensions": ["region"]},
                headers=_ep_auth(),
            )
        assert resp.status_code == 200
        assert "x-ratelimit-remaining" in resp.headers

    @pytest.mark.asyncio
    async def test_plugin_429_with_retry_after(self, plugin_client):
        from src.api import rate_limit as rl

        model = _ep_model()
        bound = _ep_bound(model)
        db = AsyncMock()
        _stub_dim_types(db, bound, {"region": "string"})
        pipeline = _PipelineMocks(rows=[], columns=["region", "revenue"])

        rl._buckets.clear()
        with patch.object(get_settings(), "HEADLESS_RATE_LIMIT", 1):
            with (
                patch("src.api.plugin.get_tenant_db", _async_gen(db)),
                patch("src.api.plugin.resolve_execution_persona", AsyncMock(return_value=None)),
                patch("src.api.plugin.bind_query_to_model", AsyncMock(return_value=bound)),
                patch("src.api.plugin.route_query", AsyncMock(return_value=MagicMock())),
                pipeline.applied(),
            ):
                body = {"project_id": _EP_PROJECT_ID, "model_id": _EP_MODEL_ID, "measures": ["revenue"], "dimensions": ["region"]}
                first = await plugin_client.post("/api/v1/plugin/execute", json=body, headers=_ep_auth())
                second = await plugin_client.post("/api/v1/plugin/execute", json=body, headers=_ep_auth())
        assert first.status_code == 200
        assert second.status_code == 429
        assert "retry-after" in second.headers
        assert int(second.headers["retry-after"]) >= 1
        rl._buckets.clear()
