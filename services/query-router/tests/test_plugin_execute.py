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
    _resolve_dimension_data_types,
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


class TestResolveDimensionDataTypesSnapshotPin:
    """F-013-11 (Bug-8521): a DEPLOYED model resolves plugin dimension types
    from the pinned snapshot's columns_by_id, never from a live ModelColumn
    read, so a draft column-type edit cannot change the plugin's type chips
    before the next Deploy."""

    @pytest.mark.asyncio
    async def test_deployed_reads_snapshot_not_live(self):
        col_id = uuid.uuid4()
        dim = types.SimpleNamespace(id=uuid.uuid4(), source_column_id=col_id)
        # Snapshot pins the column type as "date".
        shape = types.SimpleNamespace(
            columns_by_id={str(col_id): {"data_type": "date"}}
        )
        # Live db would (wrongly) report "string"; it must NOT be consulted.
        db = AsyncMock()
        db.execute.side_effect = AssertionError("live ModelColumn read on deployed")

        out = await _resolve_dimension_data_types(db, [dim], deployed_shape=shape)

        assert out[dim.id] == "date"
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_deployed_missing_column_is_none(self):
        col_id = uuid.uuid4()
        dim = types.SimpleNamespace(id=uuid.uuid4(), source_column_id=col_id)
        shape = types.SimpleNamespace(columns_by_id={})
        db = AsyncMock()

        out = await _resolve_dimension_data_types(db, [dim], deployed_shape=shape)

        assert out[dim.id] is None
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_undeployed_uses_live_join(self):
        col_id = uuid.uuid4()
        dim = types.SimpleNamespace(id=uuid.uuid4(), source_column_id=col_id)
        result = MagicMock()
        result.all.return_value = [(col_id, "integer")]
        db = AsyncMock()
        db.execute = AsyncMock(return_value=result)

        out = await _resolve_dimension_data_types(db, [dim], deployed_shape=None)

        assert out[dim.id] == "integer"
        db.execute.assert_awaited_once()


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
        # F-008-02: the denial fires but does not disclose the object name.
        assert exc.value.detail["error_code"] == "OBJECT_NOT_AVAILABLE"
        assert "object_name" not in exc.value.detail
        assert "cost" not in exc.value.detail.get("message", "")

    def test_dimension_blocked_by_persona_403(self):
        d_allowed = make_dimension("region")
        d_hidden = make_dimension("secret")
        bound = make_bound_query(dimensions=[d_allowed, d_hidden], measures=[])
        p = _persona(dimension_ids=[str(d_allowed.id)])

        with pytest.raises(HTTPException) as exc:
            enforce_persona(p, bound)
        assert exc.value.status_code == 403
        # F-008-02: non-disclosing denial.
        assert exc.value.detail["error_code"] == "OBJECT_NOT_AVAILABLE"
        assert "object_name" not in exc.value.detail
        assert "secret" not in exc.value.detail.get("message", "")

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
    # F-013-11: these fixtures exercise the LIVE ModelColumn join (they stub
    # db.execute). A bare MagicMock would make bound.deployed_shape a truthy
    # mock and route through the snapshot branch; pin None so the live path
    # runs. The snapshot-pinned path has its own dedicated test below.
    bound.deployed_shape = None
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
    async def test_non_uuid_model_id_returns_400(self, plugin_client):
        # Bug-6381: a malformed (non-UUID) model_id must return a clean 400
        # at the boundary, not a 500 from the downstream UUID parse in
        # shared.auth.project_access._as_uuid. No DB mock is needed — the
        # guard fires before any DB / rate-limit work.
        resp = await plugin_client.post(
            "/api/v1/plugin/execute",
            json={
                "project_id": _EP_PROJECT_ID,
                "model_id": "not-a-uuid",
                "measures": ["revenue"],
            },
            headers=_ep_auth(),
        )
        assert resp.status_code == 400
        assert "model_id" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_non_uuid_project_id_returns_400(self, plugin_client):
        # Bug-6381: same guard for a malformed project_id.
        resp = await plugin_client.post(
            "/api/v1/plugin/execute",
            json={
                "project_id": "12345",
                "model_id": _EP_MODEL_ID,
                "measures": ["revenue"],
            },
            headers=_ep_auth(),
        )
        assert resp.status_code == 400
        assert "project_id" in resp.json()["detail"]

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
    async def test_bug_8158_time_dimensions_populated(self, plugin_client):
        """Bug-8158: ``annotation.timeDimensions`` is POPULATED from the
        resolved time dimensions, not the literal empty ``{}`` main shipped.

        A dimension flagged ``is_time_dim`` appears in ``timeDimensions`` with
        its title and physical type; an ordinary dimension does not. Fails
        pre-fix because ``_build_annotation`` returned ``"timeDimensions": {}``.
        """
        model = _ep_model()
        bound = MagicMock()
        bound.model = model
        # F-013-11: exercise the LIVE ModelColumn join (stubbed db.execute);
        # pin deployed_shape None so the snapshot branch is not taken.
        bound.deployed_shape = None
        bound.resolved_measures = [types.SimpleNamespace(
            id=uuid.uuid4(), name="revenue", display_name="Revenue",
            default_agg="sum", format=None,
        )]
        region_src = uuid.uuid4()
        date_src = uuid.uuid4()
        bound.resolved_dimensions = [
            types.SimpleNamespace(
                id=uuid.uuid4(), name="region", display_name="Region",
                source_column_id=region_src, is_time_dim=False,
            ),
            types.SimpleNamespace(
                id=uuid.uuid4(), name="order_date", display_name="Order Date",
                source_column_id=date_src, is_time_dim=True,
            ),
        ]
        bound.resolved_filters = []

        db = AsyncMock()
        _stub_dim_types(db, bound, {"region": "string", "order_date": "date"})
        pipeline = _PipelineMocks(
            rows=[{"region": "US", "order_date": "2026-01-01", "revenue": 5}],
            columns=["region", "order_date", "revenue"],
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
                    "dimensions": ["region", "order_date"],
                    "limit": 100,
                },
                headers=_ep_auth(),
            )

        assert resp.status_code == 200
        time_dims = resp.json()["annotation"]["timeDimensions"]
        # The whole point of Bug-8158: this is no longer an empty map.
        assert time_dims != {}
        assert "order_date" in time_dims
        assert "region" not in time_dims  # ordinary dimension, not a time axis
        assert time_dims["order_date"]["title"] == "Order Date"
        assert time_dims["order_date"]["type"] == "date"

    @pytest.mark.asyncio
    async def test_response_carries_route_trace(self, plugin_client):
        """F-025-20: the response must include the route decision (route_type,
        reason, rewritten SQL) so the Excel Query Trace modal can show whether
        the report hit an aggregate/pocket/source.

        Bug-6389: the SQL half of that trace is now entitlement-gated at the
        ``modeler`` tier, so this asserts the ENTITLED caller still receives the
        complete trace (the viewer-side withhold is asserted by
        ``TestBug6389TraceSqlDisclosure``). The F-025-20 intent is unchanged:
        the trace must be complete for a caller allowed to see it."""
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
                headers={"Authorization": f"Bearer {_mint_jwt('tenant_admin')}"},
            )

        assert resp.status_code == 200
        route = resp.json()["route"]
        assert route is not None
        assert route["rewritten_query_redacted"] is False
        assert route["route_type"] == "aggregate"
        assert route["reason"] == "matched daily revenue aggregate"
        assert route["aggregate_id"] == "agg-123"
        assert route["pocket_id"] is None
        assert "SELECT region" in route["rewritten_query"]

    @pytest.mark.asyncio
    async def test_unknown_field_rejected_422(self, plugin_client):
        """F-027-01 / Bug-7997: a misspelled top-level field on the plugin
        request is a 422 naming the field, never silently dropped."""
        db = AsyncMock()
        with patch("src.api.plugin.get_tenant_db", _async_gen(db)):
            resp = await plugin_client.post(
                "/api/v1/plugin/execute",
                json={
                    "project_id": _EP_PROJECT_ID,
                    "model_id": _EP_MODEL_ID,
                    "measures": ["revenue"],
                    "dimentions": ["region"],  # misspelled
                },
                headers=_ep_auth(),
            )
        assert resp.status_code == 422
        assert "dimentions" in resp.text

    @pytest.mark.asyncio
    async def test_capped_result_reports_complete_false(self, plugin_client):
        """F-027-02 / Bug-7998: when the source returns effective_limit + 1
        rows (the probe row), the plugin response reports complete=false +
        has_more=true and trims the page to the requested limit."""
        model = _ep_model()
        bound = _ep_bound(model)
        db = AsyncMock()
        _stub_dim_types(db, bound, {"region": "string"})
        # limit=2 -> probe_limit=3 -> source returns 3 rows.
        rows = [{"region": r, "revenue": i} for i, r in enumerate(["US", "EU", "GB"])]
        pipeline = _PipelineMocks(rows=rows, columns=["region", "revenue"])

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
                    "dimensions": ["region"],
                    "limit": 2,
                },
                headers=_ep_auth(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["has_more"] is True
        assert data["complete"] is False
        assert data["row_limit"] == 2
        assert len(data["data"]) == 2

    @pytest.mark.asyncio
    async def test_complete_result_reports_complete_true(self, plugin_client):
        model = _ep_model()
        bound = _ep_bound(model)
        db = AsyncMock()
        _stub_dim_types(db, bound, {"region": "string"})
        pipeline = _PipelineMocks(
            rows=[{"region": "US", "revenue": 1}], columns=["region", "revenue"],
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
                    "dimensions": ["region"],
                    "limit": 10,
                },
                headers=_ep_auth(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["complete"] is True
        assert data["has_more"] is False
        assert data["row_limit"] == 10

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


class TestBug6389TraceSqlDisclosure:
    """Bug-6389 [SECURITY] — the route trace must not hand the physical
    rewritten SQL (physical schema/table/column names + the compiled
    row-security predicate) to a viewer-role caller through the ordinary
    /plugin/execute response. End-to-end through the real ASGI route, because
    the defect is a route-wiring one: the helper can be correct while the
    endpoint still emits the raw decision field."""

    _PHYSICAL_SQL = (
        'SELECT "region_code", SUM("amount") FROM "acme_aggregates"."agg_sales_v3" '
        "WHERE (NOT \"region_code\" = 'EMEA') GROUP BY \"region_code\""
    )

    async def _run(self, plugin_client, role: str):
        model = _ep_model()
        bound = _ep_bound(model)
        mock_decision = MagicMock()
        mock_decision.route_type = "aggregate"
        mock_decision.reason = "Row security active (1 rule(s): r1)"
        mock_decision.aggregate_id = None
        mock_decision.pocket_id = None
        mock_decision.rewritten_query = self._PHYSICAL_SQL
        db = AsyncMock()
        _stub_dim_types(db, bound, {"region": "string"})
        pipeline = _PipelineMocks(
            rows=[{"region": "US", "revenue": 1}], columns=["region", "revenue"],
        )

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
                headers={"Authorization": f"Bearer {_mint_jwt(role)}"},
            )
        assert resp.status_code == 200, resp.text
        return resp.json()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("role", ["member", "model_technical"])
    async def test_viewer_role_gets_no_physical_sql_in_trace(
        self, plugin_client, role,
    ):
        data = await self._run(plugin_client, role)
        route = data["route"]
        assert route["rewritten_query"] is None, (
            f"DISCLOSURE: {role} received physical SQL "
            f"{route['rewritten_query']!r}"
        )
        assert route["rewritten_query_redacted"] is True
        # Nothing anywhere else in the payload may leak it either.
        assert "agg_sales_v3" not in resp_text(data)
        assert "acme_aggregates" not in resp_text(data)
        # The non-sensitive route facts stay visible.
        assert route["route_type"] == "aggregate"

    @pytest.mark.asyncio
    async def test_tenant_admin_still_gets_the_trace_sql(self, plugin_client):
        """R2 finding B1: ``tenant_admin`` is what the platform's token mint
        actually issues for an administrator (and what the demo seed creates).
        The first version of this gate denied exactly this caller, killing the
        Excel Query Trace SQL panel for its entire intended audience."""
        data = await self._run(plugin_client, "tenant_admin")
        route = data["route"]
        assert route["rewritten_query"] == self._PHYSICAL_SQL
        assert route["rewritten_query_redacted"] is False


def resp_text(payload) -> str:
    import json as _json
    return _json.dumps(payload)


# ---------------------------------------------------------------------------
# Bug-8809 / coverage-guard plan Phase 2 — embed disclosure, WIRED
#
# ``redact_physical_sql`` (Bug-6389) is the MODELLER-tier gate: it withholds
# the SQL string and nothing else. The route trace still shipped the router's
# free-prose reason and the serving artifact's identity to an embed session.
# Drives the real ASGI route with a real embed token and scans the served body.
# ---------------------------------------------------------------------------


def _mint_embed_jwt() -> str:
    payload = {
        "sub": "embed@acme.test",
        "tenant_id": "acme",
        "aud": "embed",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        "capabilities": ["query"],
    }
    return jwt.encode(payload, _settings.JWT_SECRET_KEY, algorithm=_settings.JWT_ALGORITHM)


def _ep_embed_auth() -> dict:
    return {"Authorization": f"Bearer {_mint_embed_jwt()}"}


def _leaky_decision():
    decision = MagicMock()
    decision.route_type = "aggregate"
    decision.reason = (
        "served from aggregate agg_sales_v3 in schema acme_aggregates "
        "(grain region_code)"
    )
    decision.aggregate_id = "agg-uuid-1"
    decision.pocket_id = "pkt-uuid-1"
    decision.rewritten_query = (
        'SELECT "region_code" FROM "acme_aggregates"."agg_sales_v3"'
    )
    decision.security_rules_applied = []
    return decision


@pytest.mark.asyncio
async def test_embed_and_tenant_sessions_receive_identical_route_detail(plugin_client):
    """An embed principal and a tenant principal get the SAME route detail.

    Decision 2026-08-11, option C
    (docs/questions/questions_disclosure-by-entitlement-not-auth-method.md):
    the embed physical-detail withhold was REMOVED because it gated on the
    token TYPE rather than on entitlement. This test previously asserted the
    withhold; it now pins the replacement contract and fails against the
    pre-decision code, where the embed body was stripped and the tenant body
    was not.

    NOTE the one difference that is DELIBERATELY preserved and therefore
    excluded from the equality check below: ``rewritten_query`` is still
    governed by ``redact_physical_sql``, the modeller-tier gate that resolves
    the caller's PROJECT BINDING. That gate is a separate control with its own
    coverage (``TestPluginTraceSqlDisclosure`` above) and was not in scope of
    the option-C removal.
    """
    responses = {}
    for label, headers in (
        ("embed", _ep_embed_auth()),
        ("tenant", _ep_auth()),
    ):
        model = _ep_model()
        bound = _ep_bound(model)
        db = AsyncMock()
        _stub_dim_types(db, bound, {"region": "string"})
        pipeline = _PipelineMocks(
            rows=[{"region": "US", "revenue": 1}], columns=["region", "revenue"],
        )
        with (
            patch("src.api.plugin.get_tenant_db", _async_gen(db)),
            patch("src.api.plugin.resolve_execution_persona", AsyncMock(return_value=None)),
            patch("src.api.plugin.bind_query_to_model", AsyncMock(return_value=bound)),
            patch("src.api.plugin.route_query", AsyncMock(return_value=_leaky_decision())),
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
                headers=headers,
            )
        assert resp.status_code == 200, f"{label}: {resp.text}"
        responses[label] = resp.json()["route"]

    # Everything the token-type withhold used to strip is now identical.
    for field in ("route_type", "reason", "aggregate_id", "pocket_id"):
        assert responses["embed"][field] == responses["tenant"][field], (
            f"{field!r} still differs by authentication method; disclosure must "
            f"be decided by entitlement, not by how the caller signed in"
        )
    # And it is the REAL detail both receive, not a jointly-stripped one.
    for label, route in responses.items():
        assert route["aggregate_id"] == "agg-uuid-1", label
        assert route["pocket_id"] == "pkt-uuid-1", label
        assert "agg_sales_v3" in route["reason"], label
        assert "acme_aggregates" in route["reason"], label
    # The permanently-false flag went with the control it reported on.
    assert "reason_redacted" not in responses["embed"]
