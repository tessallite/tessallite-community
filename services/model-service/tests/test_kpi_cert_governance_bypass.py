"""Bug-6264 — KPI certification admin-gate must hold on the create and revert
paths, not just PATCH.

Two modeler-reachable write paths could previously mint / restore a privileged
(``shared`` / ``certified``) certification status without admin authority:

  * ``revert_kpi_version`` (modeler role) restored ``certification_status`` from
    the version snapshot — re-certifying a demoted KPI. Root fix: a revert
    restores DEFINITION only (mirrors named-sets F-018-05); the cert status is
    excluded from the restore and a certified/shared KPI whose reverted
    definition differs is demoted to draft.
  * ``create_kpi`` — ``KPICreate`` deliberately omits and forbids
    ``certification_status``, so privileged create intent fails with 422; the
    write path also rejects non-admin privileged statuses if a future schema
    revision ever exposes the field.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.api.kpis import _enforce_kpi_create_certification_guard, _kpi_snapshot_dict
from src.auth.middleware import CurrentUser, get_current_user
from src.main import app

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    TEST_TENANT,
    async_gen_from,
    make_mock_db,
    routed_execute,
)
from .test_kpi_governance import _binding_rbac_db, _kpi, _make_user

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/kpis"


def _snapshot_of(kpi, *, certification_status: str, **overrides) -> dict:
    """Build a version snapshot dict via the SAME serialiser production uses, so
    the revert change-detection comparison is faithful to real snapshots."""
    snap = _kpi_snapshot_dict(kpi)
    snap["certification_status"] = certification_status
    snap.update(overrides)
    return snap


def _version_lookup_db(kpi, snapshot):
    """A mock DB whose KPIVersion select returns a version with *snapshot*."""
    db = make_mock_db()
    db.get = AsyncMock(return_value=kpi)
    db.scalar = AsyncMock(return_value=0)  # max(version_number) for the new row
    db.refresh = AsyncMock()

    version = MagicMock()
    version.snapshot = snapshot
    result = MagicMock()
    result.scalar_one_or_none.return_value = version
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.fixture
def as_modeler():
    user = _make_user(role="modeler")
    app.dependency_overrides[get_current_user] = lambda: user
    yield user
    app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Revert path — the genuine exploit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revert_does_not_recertify_a_demoted_kpi(client, as_modeler):
    """Exploit-closed: a modeler reverts a draft KPI to a snapshot that was
    certified. The certification status must NOT be restored — the KPI stays
    draft. (Before the fix this re-certified without admin.)"""
    kpi = _kpi(certification_status="draft", name="Revenue")
    kpi.expression = 'measure("rev")'
    # Snapshot captured while the KPI was certified, SAME definition as now.
    snap = _snapshot_of(kpi, certification_status="certified")

    db = _version_lookup_db(kpi, snap)
    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.audit", new_callable=AsyncMock) as mock_audit,
    ):
        resp = await client.post(f"{PREFIX}/{kpi.id}/versions/1/revert")

    assert resp.status_code == 200, resp.text
    assert kpi.certification_status == "draft"
    assert resp.json()["certification_status"] == "draft"
    # Governance-relevant action is audited.
    mock_audit.assert_awaited_once()
    assert mock_audit.await_args.kwargs["action"] == "kpi.revert"


@pytest.mark.asyncio
async def test_revert_keeps_certified_when_definition_identical(client, as_modeler):
    """No over-demote: reverting a certified KPI to a snapshot with an identical
    definition keeps it certified (the reverted definition did not change)."""
    kpi = _kpi(certification_status="certified", name="Revenue")
    kpi.expression = 'measure("rev")'
    snap = _snapshot_of(kpi, certification_status="certified")  # identical def

    db = _version_lookup_db(kpi, snap)
    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.audit", new_callable=AsyncMock),
    ):
        resp = await client.post(f"{PREFIX}/{kpi.id}/versions/1/revert")

    assert resp.status_code == 200, resp.text
    assert kpi.certification_status == "certified"


@pytest.mark.asyncio
async def test_revert_demotes_certified_kpi_on_target_value_only_change(client, as_modeler):
    """Bug-6613: the demote must fire even when the reverted definition differs
    ONLY in a serialised field (target_value) — a field-exclusion compare would
    have kept the certified badge on a changed KPI."""
    kpi = _kpi(certification_status="certified", name="Revenue")
    kpi.expression = 'measure("rev")'
    kpi.target_value = 100.0
    snap = _snapshot_of(
        kpi, certification_status="certified", target_value=200.0,
    )

    db = _version_lookup_db(kpi, snap)
    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.audit", new_callable=AsyncMock),
    ):
        resp = await client.post(f"{PREFIX}/{kpi.id}/versions/1/revert")

    assert resp.status_code == 200, resp.text
    assert kpi.target_value == 200.0  # definition restored
    assert kpi.certification_status == "draft"  # demoted despite same expression


@pytest.mark.asyncio
async def test_revert_demotes_certified_kpi_when_definition_differs(client, as_modeler):
    """A certified KPI reverted to a snapshot with a DIFFERENT definition is
    demoted to draft (never elevated), mirroring update_kpi's auto-demote."""
    kpi = _kpi(certification_status="certified", name="Revenue")
    kpi.expression = 'measure("rev_new")'
    # Snapshot has an OLDER, different definition.
    snap = _snapshot_of(
        kpi, certification_status="certified", expression='measure("rev_old")'
    )

    db = _version_lookup_db(kpi, snap)
    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.audit", new_callable=AsyncMock),
    ):
        resp = await client.post(f"{PREFIX}/{kpi.id}/versions/1/revert")

    assert resp.status_code == 200, resp.text
    assert kpi.expression == 'measure("rev_old")'  # definition restored
    assert kpi.certification_status == "draft"  # demoted, not kept certified


# ---------------------------------------------------------------------------
# Create path — Bug-6893: certification_status IS accepted on KPICreate
# (the governance guard _enforce_kpi_create_certification_guard checks
# ROLE, not field presence). Invalid enum values are still rejected.
# ---------------------------------------------------------------------------


def test_kpicreate_schema_accepts_valid_certification_status():
    """Bug-6893: KPICreate now declares certification_status so
    extra='forbid' does not reject it. Valid enum values pass."""
    from shared.schemas.pydantic_models import KPICreate

    kpi = KPICreate(name="X", certification_status="shared")
    assert kpi.certification_status == "shared"


def test_kpicreate_schema_rejects_invalid_certification_status():
    """KPICreate rejects certification_status values outside the Literal enum."""
    from shared.schemas.pydantic_models import KPICreate

    with pytest.raises(ValidationError):
        KPICreate(name="X", certification_status="published")


@pytest.mark.asyncio
async def test_create_route_rejects_modeler_privileged_certification_status(client, as_modeler):
    """A modeler sending certification_status='certified' gets 403 from the
    governance guard (role check), not 422 from schema validation."""
    with patch("src.api.kpis.get_tenant_db", async_gen_from(make_mock_db())):
        resp = await client.post(
            PREFIX,
            json={"name": "X", "certification_status": "certified"},
        )
    assert resp.status_code == 403, resp.text


# The guard is now async and resolves authority through caller_has_role against
# the caller's EFFECTIVE binding (Bug-9443 Option A), so these direct unit tests
# thread a mock db + project/model ids and drive the real caller_has_role.


@pytest.mark.asyncio
async def test_create_guard_rejects_effective_modeler_privileged_status():
    data = {"name": "X", "certification_status": "certified"}
    with pytest.raises(HTTPException) as exc:
        await _enforce_kpi_create_certification_guard(
            data, _make_user(role="modeler"),
            db=_binding_rbac_db("modeler"),
            project_id=TEST_PROJECT_ID, model_id=TEST_MODEL_ID,
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_create_guard_allows_effective_admin_privileged_status():
    # Bug-9443: authority is the EFFECTIVE admin binding, not the token role.
    data = {"name": "X", "certification_status": "certified"}
    await _enforce_kpi_create_certification_guard(
        data, _make_user(role="viewer"),
        db=_binding_rbac_db("admin"),
        project_id=TEST_PROJECT_ID, model_id=TEST_MODEL_ID,
    )
    assert data["certification_status"] == "certified"


@pytest.mark.asyncio
async def test_create_guard_rejects_invalid_status():
    # Out-of-vocabulary status fails closed with 422 before any authority check.
    data = {"name": "X", "certification_status": "published"}
    with pytest.raises(HTTPException) as exc:
        await _enforce_kpi_create_certification_guard(
            data, _make_user(role="admin"),
            db=make_mock_db(),
            project_id=TEST_PROJECT_ID, model_id=TEST_MODEL_ID,
        )
    assert exc.value.status_code == 422


# ---------------------------------------------------------------------------
# Bug-9443 (Option A) — KPI born-certification requires an EFFECTIVE project/
# model admin binding, resolved through caller_has_role — the coarse JWT token
# role is NOT authority. A T3 cross-family challenger showed a token stamped
# role="admin" backing only a modeler binding could born-certify (RBAC-F2).
# ---------------------------------------------------------------------------


def _born_cert_handler_db(*, binding_role: str | None, cert_status: str = "certified"):
    """Handler DB for create_kpi. Answers the caller's ``user_access_bindings``
    lookup (the query ``caller_has_role`` issues) with a binding of
    *binding_role* (None => no binding), every other query empty, and mirrors a
    refreshed KPI back so a successful create completes at 201.
    """
    db = make_mock_db()
    rows = []
    if binding_role is not None:
        rows = [types.SimpleNamespace(
            id=uuid.uuid4(), role=binding_role, model_id=None,
            project_id=None, user_identity="*",
        )]
    db.execute = routed_execute(user_access_bindings=rows)
    refreshed = _kpi(certification_status=cert_status, name="Born Certified")

    async def mock_refresh(obj):
        for attr, val in vars(refreshed).items():
            setattr(obj, attr, val)

    db.refresh = mock_refresh
    return db


@pytest.mark.asyncio
async def test_bug9443_token_admin_with_modeler_binding_cannot_born_certify(client):
    """RBAC-F2 elevation closed: a caller whose JWT says role='admin' but who
    holds only an effective MODELER binding is DENIED born-certification. The
    token role is not authority; the effective binding is. (Pre-fix this
    returned 201 — the coarse token role granted the privilege.)"""
    admin_token = _make_user(role="admin")
    app.dependency_overrides[get_current_user] = lambda: admin_token
    db = _born_cert_handler_db(binding_role="modeler")
    try:
        with patch("src.api.kpis.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                PREFIX, json={"name": "BC", "certification_status": "certified"},
            )
        assert resp.status_code == 403, resp.text
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_bug9443_effective_admin_binding_can_born_certify(client):
    """An ORDINARY token (role='viewer') backed by an effective project ADMIN
    binding may born-certify — authority follows the real binding, not the
    token role."""
    ordinary = _make_user(role="viewer")
    app.dependency_overrides[get_current_user] = lambda: ordinary
    db = _born_cert_handler_db(binding_role="admin")
    try:
        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.audit", new_callable=AsyncMock),
        ):
            resp = await client.post(
                PREFIX, json={"name": "BC", "certification_status": "certified"},
            )
        assert resp.status_code == 201, resp.text
        assert resp.json()["certification_status"] == "certified"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_bug9443_effective_modeler_draft_create_then_certify_allowed(client):
    """An effective modeler may create a DRAFT KPI (no certification_status) and
    then certify it through the separate /certify action (modeler+ authority,
    unchanged by this lane)."""
    modeler = _make_user(role="modeler")
    app.dependency_overrides[get_current_user] = lambda: modeler
    draft = _kpi(certification_status="draft", name="Draft KPI")

    async def refresh_draft(obj):
        for attr, val in vars(draft).items():
            setattr(obj, attr, val)

    try:
        # Draft create — guard returns early (no privileged status requested).
        create_db = make_mock_db()
        create_db.refresh = refresh_draft
        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(create_db)),
            patch("src.api.kpis.audit", new_callable=AsyncMock),
        ):
            create_resp = await client.post(PREFIX, json={"name": "Draft KPI"})
        assert create_resp.status_code == 201, create_resp.text
        assert create_resp.json()["certification_status"] == "draft"

        # Separate /certify action — modeler+ effective binding is sufficient.
        cert_db = make_mock_db()
        cert_db.get = AsyncMock(return_value=draft)
        cert_db.refresh = refresh_draft
        with (
            patch("src.auth.rbac.get_tenant_db", async_gen_from(_binding_rbac_db("modeler"))),
            patch("src.api.kpis.get_tenant_db", async_gen_from(cert_db)),
            patch("src.api.kpis.audit", new_callable=AsyncMock),
        ):
            cert_resp = await client.post(f"{PREFIX}/{draft.id}/certify", json={})
        assert cert_resp.status_code == 200, cert_resp.text
        assert draft.certification_status == "certified"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_bug9443_viewer_cannot_born_certify_or_certify(client):
    """A viewer is denied on both paths: born-certify at create AND the separate
    /certify action are modeler+ gated at the route dependency."""
    viewer = _make_user(role="viewer")
    app.dependency_overrides[get_current_user] = lambda: viewer
    kpi = _kpi(certification_status="draft")
    try:
        with (
            patch("src.auth.rbac.get_tenant_db", async_gen_from(_binding_rbac_db("viewer"))),
            patch("src.api.kpis.get_tenant_db", async_gen_from(make_mock_db())),
        ):
            create_resp = await client.post(
                PREFIX, json={"name": "BC", "certification_status": "certified"},
            )
        assert create_resp.status_code == 403, create_resp.text

        cert_db = make_mock_db()
        cert_db.get = AsyncMock(return_value=kpi)
        with (
            patch("src.auth.rbac.get_tenant_db", async_gen_from(_binding_rbac_db("viewer"))),
            patch("src.api.kpis.get_tenant_db", async_gen_from(cert_db)),
        ):
            cert_resp = await client.post(f"{PREFIX}/{kpi.id}/certify", json={})
        assert cert_resp.status_code == 403, cert_resp.text
        assert kpi.certification_status == "draft"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize(
    "admin_user",
    [
        CurrentUser(
            user_id="ta@test.com", tenant_id=TEST_TENANT,
            email="ta@test.com", role="tenant_admin",
        ),
        CurrentUser(
            user_id="sa@system", tenant_id="__system__",
            email="sa@system", role="system_admin",
        ),
    ],
    ids=["tenant_admin", "system_admin"],
)
@pytest.mark.asyncio
async def test_bug9443_human_admin_can_born_certify(client, admin_user):
    """The human tenant_admin / canonical system_admin bypass still allows
    born-certification (no per-project binding required) — caller_has_role
    returns True for these principals before any binding lookup."""
    app.dependency_overrides[get_current_user] = lambda: admin_user
    db = _born_cert_handler_db(binding_role=None)  # no binding; human bypass is the authority
    try:
        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch("src.api.kpis.audit", new_callable=AsyncMock),
        ):
            resp = await client.post(
                PREFIX, json={"name": "BC", "certification_status": "certified"},
            )
        assert resp.status_code == 201, resp.text
        assert resp.json()["certification_status"] == "certified"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
