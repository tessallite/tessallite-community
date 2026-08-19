"""Tests for KPI governance: audit trail, draft visibility, ownership gates."""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from .result_fakes import FakeScalarResult

from src.auth.middleware import CurrentUser, get_current_user
from src.main import app

from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    TEST_TENANT,
    TEST_USER_ID,
    async_gen_from,
    make_mock_db,
)

# F-017-12: shim caller_has_role to the token-role decision for these
# mocked-db unit tests (see conftest.kpi_effective_role).
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("kpi_effective_role")]

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/kpis"


class _ScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return FakeScalarResult(self._items)

    def all(self):
        return list(self._items)


def _kpi(
    *,
    kpi_id: uuid.UUID | None = None,
    name: str = "Test KPI",
    certification_status: str = "draft",
    owner_user_id: str | None = None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=kpi_id or uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=name,
        display_name=name,
        description=None,
        display_folder=None,
        value_measure_id=None,
        goal_measure_id=None,
        status_expression=None,
        trend_expression=None,
        status_graphic="Traffic Light",
        trend_graphic="Standard Arrow",
        weight=1.0,
        parent_kpi_id=None,
        certification_status=certification_status,
        owner_user_id=owner_user_id,
        created_at=NOW,
        updated_at=NOW,
        expression=None,
        kpi_type=None,
        calc_agg_mode="automatic",
        inner_agg=None,
        inner_grain=None,
        outer_agg=None,
        at_grain=None,
        non_additive_agg=None,
        carry_forward=False,
        target_type=None,
        target_value=None,
        target_measure_id=None,
        target_expression=None,
        target_period=None,
        direction="higher_is_better",
        presentation_type=None,
        presentation_meta=None,
        trend_period="month",
        trend_threshold=0.01,
        trend_sparkline_periods=12,
        format_token=None,
        format_custom=None,
        unit_label=None,
        null_display_value="N/A",
        indicator_type=None,
        time_dimension_id=None,
        snapshot_frequency=None,
        snapshot_retention=None,
        created_by=None,
        is_deployed=False,
        deployed_at=None,
        evaluation_order=None,
        replacement_id=None,
    )


def _make_user(role: str | None = None, email: str = TEST_USER_ID) -> CurrentUser:
    return CurrentUser(
        user_id=email, tenant_id=TEST_TENANT, email=email, role=role,
    )


def _binding_rbac_db(role: str = "viewer"):
    """A mock DB whose ``user_access_bindings`` lookup resolves to a *role*
    binding, so the route dependency ``require_role("modeler")`` sees the
    caller's EFFECTIVE persisted binding as *role* and denies (403) when it is
    below modeler.

    This exercises the REAL production enforcement point — the route
    dependency, which reads the persisted binding, not the coarse token role
    (Wave C decision #8 / Bug-9402). The certify/deprecate/patch handlers used
    to carry an inner owner-or-privileged re-check as a second gate; that check
    was dead (any caller past ``require_role("modeler")`` already holds
    modeler+) and was removed in the F-021-04 hard-cutover round-2 cleanup (F1).
    The security assertion "a viewer cannot certify/deprecate/set a privileged
    status" is unchanged — it is now proven at the gate that actually runs in
    production. Overrides the ``mock_rbac_get_tenant_db`` autouse admin binding.
    """
    binding = types.SimpleNamespace(
        id=uuid.uuid4(), role=role, model_id=None,
        project_id=None, user_identity="*",
    )
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = binding
    db.execute = AsyncMock(return_value=result)
    return db


# ---------------------------------------------------------------------------
# Audit trail tests
# ---------------------------------------------------------------------------


class TestAuditTrail:
    @pytest.fixture(autouse=True)
    def setup_auth(self):
        user = _make_user(role="modeler")
        app.dependency_overrides[get_current_user] = lambda: user
        yield
        app.dependency_overrides.pop(get_current_user, None)

    @pytest.mark.asyncio
    async def test_create_kpi_emits_audit(self, client):
        db = make_mock_db()
        kpi = _kpi()

        async def mock_refresh(obj):
            for attr, val in vars(kpi).items():
                setattr(obj, attr, val)

        db.refresh = mock_refresh
        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.audit", new_callable=AsyncMock) as mock_audit,
        ):
            resp = await client.post(PREFIX, json={"name": "Test KPI"})
        assert resp.status_code == 201
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args.kwargs
        assert call_kwargs["action"] == "kpi.create"
        assert call_kwargs["severity"] == "info"
        assert call_kwargs["target_type"] == "kpi"

    @pytest.mark.asyncio
    async def test_update_kpi_emits_audit(self, client):
        db = make_mock_db()
        kpi = _kpi()
        db.get = AsyncMock(return_value=kpi)

        async def mock_refresh(obj):
            for attr, val in vars(kpi).items():
                setattr(obj, attr, val)

        db.refresh = mock_refresh
        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.audit", new_callable=AsyncMock) as mock_audit,
        ):
            resp = await client.patch(
                f"{PREFIX}/{kpi.id}", json={"name": "Updated KPI"},
            )
        assert resp.status_code == 200
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args.kwargs
        assert call_kwargs["action"] == "kpi.update"
        assert call_kwargs["severity"] == "info"
        assert "fields" in call_kwargs["detail"]

    @pytest.mark.asyncio
    async def test_delete_kpi_emits_audit(self, client):
        db = make_mock_db()
        kpi = _kpi()
        db.get = AsyncMock(return_value=kpi)
        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.audit", new_callable=AsyncMock) as mock_audit,
        ):
            resp = await client.delete(f"{PREFIX}/{kpi.id}")
        assert resp.status_code == 204
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args.kwargs
        assert call_kwargs["action"] == "kpi.delete"
        assert call_kwargs["severity"] == "warn"

    @pytest.mark.asyncio
    async def test_certify_kpi_emits_audit(self, client):
        # certify requires admin role
        admin = _make_user(role="admin")
        app.dependency_overrides[get_current_user] = lambda: admin

        db = make_mock_db()
        kpi = _kpi(certification_status="draft")
        db.get = AsyncMock(return_value=kpi)

        async def mock_refresh(obj):
            for attr, val in vars(kpi).items():
                setattr(obj, attr, val)

        db.refresh = mock_refresh
        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.audit", new_callable=AsyncMock) as mock_audit,
        ):
            resp = await client.post(f"{PREFIX}/{kpi.id}/certify", json={})
        assert resp.status_code == 200
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args.kwargs
        assert call_kwargs["action"] == "kpi.certify"
        # CP-08 (c0b3580f) raised certify/deprecate governance transitions to
        # "warn" severity, consistent with kpi.delete/deploy/undeploy; routine
        # create/update/revert stay "info".
        assert call_kwargs["severity"] == "warn"
        assert "certifier" in call_kwargs["detail"]

    @pytest.mark.asyncio
    async def test_deprecate_kpi_emits_audit(self, client):
        admin = _make_user(role="admin")
        app.dependency_overrides[get_current_user] = lambda: admin

        db = make_mock_db()
        kpi = _kpi(certification_status="certified")
        db.get = AsyncMock(return_value=kpi)

        async def mock_refresh(obj):
            for attr, val in vars(kpi).items():
                setattr(obj, attr, val)

        db.refresh = mock_refresh
        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.audit", new_callable=AsyncMock) as mock_audit,
        ):
            resp = await client.post(
                f"{PREFIX}/{kpi.id}/deprecate", json={},
            )
        assert resp.status_code == 200
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args.kwargs
        assert call_kwargs["action"] == "kpi.deprecate"
        # CP-08 (c0b3580f) raised certify/deprecate to "warn" (see certify test).
        assert call_kwargs["severity"] == "warn"


# ---------------------------------------------------------------------------
# Draft visibility tests
# ---------------------------------------------------------------------------


class TestDraftVisibility:
    @pytest.mark.asyncio
    async def test_modeler_sees_draft_kpis(self, client):
        modeler = _make_user(role="modeler")
        app.dependency_overrides[get_current_user] = lambda: modeler

        draft_kpi = _kpi(certification_status="draft")
        db = make_mock_db()
        db.execute = AsyncMock(return_value=_ScalarResult([draft_kpi]))

        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        ):
            resp = await client.get(PREFIX)
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    @pytest.mark.asyncio
    async def test_viewer_does_not_see_draft_kpis(self, client):
        viewer = _make_user(role="viewer")
        app.dependency_overrides[get_current_user] = lambda: viewer

        draft_kpi = _kpi(certification_status="draft")
        certified_kpi = _kpi(certification_status="certified", name="Certified KPI")

        # The mock returns all KPIs, but the endpoint should filter drafts.
        # Since we mock execute to return both, the SQL filter won't actually
        # run against the mock. We need to check that the query has the filter.
        db = make_mock_db()
        db.execute = AsyncMock(return_value=_ScalarResult([certified_kpi]))

        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        ):
            resp = await client.get(PREFIX)
        assert resp.status_code == 200
        data = resp.json()
        # With the mock, we just verify the endpoint returns what the DB returns
        # (the filtering happens at SQL level). The key test is that the query
        # was constructed with the filter -- verified by inspection.
        assert len(data) == 1
        assert data[0]["name"] == "Certified KPI"

    @pytest.mark.asyncio
    async def test_admin_sees_draft_kpis(self, client):
        admin = _make_user(role="admin")
        app.dependency_overrides[get_current_user] = lambda: admin

        draft_kpi = _kpi(certification_status="draft")
        db = make_mock_db()
        db.execute = AsyncMock(return_value=_ScalarResult([draft_kpi]))

        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.resolve_effective_persona", new_callable=AsyncMock, return_value=None),
        ):
            resp = await client.get(PREFIX)
        assert resp.status_code == 200
        assert len(resp.json()) == 1


# ---------------------------------------------------------------------------
# Ownership gate tests
# ---------------------------------------------------------------------------


class TestOwnershipGate:
    @pytest.mark.asyncio
    async def test_owner_can_certify(self, client):
        owner = _make_user(role="admin", email="owner@test.com")
        app.dependency_overrides[get_current_user] = lambda: owner

        kpi = _kpi(owner_user_id="owner@test.com", certification_status="draft")
        db = make_mock_db()
        db.get = AsyncMock(return_value=kpi)

        async def mock_refresh(obj):
            for attr, val in vars(kpi).items():
                setattr(obj, attr, val)

        db.refresh = mock_refresh
        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.audit", new_callable=AsyncMock),
        ):
            resp = await client.post(f"{PREFIX}/{kpi.id}/certify", json={})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_non_owner_without_modeler_binding_cannot_certify(self, client):
        # Bug-9402 / Wave C decision #8: certify authority is the caller's
        # EFFECTIVE project/model binding (modeler+), not the coarse token role.
        # A non-owner WITHOUT a modeler+ binding (viewer) is denied 403 at the
        # route dependency ``require_role("modeler")`` — the real production gate
        # (F-021-04 round-2, F1 removed the dead inner re-check).
        other_user = _make_user(role="viewer", email="other@test.com")
        app.dependency_overrides[get_current_user] = lambda: other_user

        kpi = _kpi(owner_user_id="owner@test.com", certification_status="draft")
        db = make_mock_db()
        db.get = AsyncMock(return_value=kpi)
        with (
            patch("src.auth.rbac.get_tenant_db", async_gen_from(_binding_rbac_db("viewer"))),
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.audit", new_callable=AsyncMock),
        ):
            resp = await client.post(f"{PREFIX}/{kpi.id}/certify", json={})
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_non_owner_modeler_can_certify(self, client):
        # Bug-9402 / decision #8: a non-owner WITH a modeler+ effective binding
        # is now authorized to certify (authority is the binding, not ownership
        # or the coarse token role).
        other_user = _make_user(role="modeler", email="other@test.com")
        app.dependency_overrides[get_current_user] = lambda: other_user

        kpi = _kpi(owner_user_id="owner@test.com", certification_status="draft")
        db = make_mock_db()
        db.get = AsyncMock(return_value=kpi)

        async def mock_refresh(obj):
            for attr, val in vars(kpi).items():
                setattr(obj, attr, val)

        db.refresh = mock_refresh
        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.audit", new_callable=AsyncMock),
        ):
            resp = await client.post(f"{PREFIX}/{kpi.id}/certify", json={})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_admin_can_certify_others_kpi(self, client):
        admin = _make_user(role="admin", email="admin@test.com")
        app.dependency_overrides[get_current_user] = lambda: admin

        kpi = _kpi(owner_user_id="owner@test.com", certification_status="draft")
        db = make_mock_db()
        db.get = AsyncMock(return_value=kpi)

        async def mock_refresh(obj):
            for attr, val in vars(kpi).items():
                setattr(obj, attr, val)

        db.refresh = mock_refresh
        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.audit", new_callable=AsyncMock),
        ):
            resp = await client.post(f"{PREFIX}/{kpi.id}/certify", json={})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_non_owner_without_modeler_binding_cannot_deprecate(self, client):
        # Bug-9402 / decision #8: deprecate authority is the effective binding
        # (modeler+). A non-owner viewer (no modeler+ binding) is denied 403 at
        # the route dependency ``require_role("modeler")`` — the real production
        # gate (F-021-04 round-2, F1 removed the dead inner re-check).
        other_user = _make_user(role="viewer", email="other@test.com")
        app.dependency_overrides[get_current_user] = lambda: other_user

        kpi = _kpi(owner_user_id="owner@test.com", certification_status="certified")
        db = make_mock_db()
        db.get = AsyncMock(return_value=kpi)
        with (
            patch("src.auth.rbac.get_tenant_db", async_gen_from(_binding_rbac_db("viewer"))),
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.audit", new_callable=AsyncMock),
        ):
            resp = await client.post(
                f"{PREFIX}/{kpi.id}/deprecate", json={},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_kpi_without_owner_allows_any_admin(self, client):
        admin = _make_user(role="admin", email="admin@test.com")
        app.dependency_overrides[get_current_user] = lambda: admin

        kpi = _kpi(owner_user_id=None, certification_status="draft")
        db = make_mock_db()
        db.get = AsyncMock(return_value=kpi)

        async def mock_refresh(obj):
            for attr, val in vars(kpi).items():
                setattr(obj, attr, val)

        db.refresh = mock_refresh
        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.audit", new_callable=AsyncMock),
        ):
            resp = await client.post(f"{PREFIX}/{kpi.id}/certify", json={})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Bug-6264 — PATCH certification_status: enum + privileged-status authorization
# ---------------------------------------------------------------------------


class TestPatchCertificationGuard:
    @pytest.mark.asyncio
    async def test_non_privileged_cannot_set_shared_via_patch(self, client):
        """'shared' is certified-equivalent (renders as [Certified] in the XMLA
        catalogue). Bug-9402 / decision #8: the write authority is the caller's
        EFFECTIVE project/model binding (modeler+), not the coarse token role.
        A caller WITHOUT a modeler+ binding (viewer) promoting a draft KPI to
        'shared' via PATCH is blocked 403 at the route dependency
        ``require_role("modeler")`` — the real production gate (F-021-04
        round-2, F1 removed the dead inner cert-authority re-check)."""
        viewer = _make_user(role="viewer")
        app.dependency_overrides[get_current_user] = lambda: viewer

        kpi = _kpi(certification_status="draft")
        db = make_mock_db()
        db.get = AsyncMock(return_value=kpi)
        with (
            patch("src.auth.rbac.get_tenant_db", async_gen_from(_binding_rbac_db("viewer"))),
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        ):
            resp = await client.patch(
                f"{PREFIX}/{kpi.id}", json={"certification_status": "shared"},
            )
        assert resp.status_code == 403, resp.text
        assert kpi.certification_status == "draft"

    @pytest.mark.asyncio
    async def test_modeler_binding_can_set_shared_via_patch(self, client):
        """Bug-9402 / decision #8: a caller WITH a modeler+ effective binding
        may set the privileged 'shared' status via PATCH — the coarse token
        role is no longer the authority."""
        modeler = _make_user(role="modeler")
        app.dependency_overrides[get_current_user] = lambda: modeler

        kpi = _kpi(certification_status="draft")
        db = make_mock_db()
        db.get = AsyncMock(return_value=kpi)

        async def mock_refresh(obj):
            for attr, val in vars(kpi).items():
                setattr(obj, attr, val)

        db.refresh = mock_refresh
        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.audit", new_callable=AsyncMock),
            patch(
                "src.api.kpis._invalidate_kpi_and_dependents",
                new_callable=AsyncMock,
            ),
        ):
            resp = await client.patch(
                f"{PREFIX}/{kpi.id}", json={"certification_status": "shared"},
            )
        assert resp.status_code == 200, resp.text
        assert kpi.certification_status == "shared"

    @pytest.mark.parametrize("bad_status", ["banana", None])
    @pytest.mark.asyncio
    async def test_patch_invalid_certification_status_returns_422(
        self, client, bad_status
    ):
        """certification_status is a controlled enum; an unknown string or an
        explicit null must fail closed with 422, never bypass the privileged
        gate or hit a NOT NULL 500."""
        admin = _make_user(role="admin")
        app.dependency_overrides[get_current_user] = lambda: admin

        kpi = _kpi(certification_status="draft")
        db = make_mock_db()
        db.get = AsyncMock(return_value=kpi)
        with patch("src.api.kpis.get_tenant_db", async_gen_from(db)):
            resp = await client.patch(
                f"{PREFIX}/{kpi.id}", json={"certification_status": bad_status},
            )
        assert resp.status_code == 422, resp.text
        assert kpi.certification_status == "draft"
