"""Security guards for the model-service correctness/RBAC wave.

Covers two access-control invariants:

Bug-7776 (AUTH-RR-05) — scoped service tokens must not enumerate human-facing
read-only metadata. A leaked internal service token (e.g. the KPI-evaluate or
pocket-refresh principal) previously reached ``list_projects``, ``tenants/me``,
``/edition`` and ``/limits`` because those routes only required
``get_current_user``/``forbid_embed_user``, both of which admit
``CurrentServiceUser``. The routes now fail closed:
  - ``GET /projects``           -> ``forbid_service_user`` (embed still allowed)
  - ``GET /tenants/me``         -> ``require_human_user``  (service + embed blocked)
  - ``GET /edition`` / ``/limits`` -> ``require_human_user``

Bug-7300 (F-020-03) — the model snapshot-export bundle discloses the full
governance/security configuration (row-security predicates, CLS data-tags,
persona default_filters, measure formulas). It was gated at ``viewer``; it is
now ``modeler`` to match the sibling single-model export surface (LookML).
"""
from __future__ import annotations

import types
import uuid
from contextlib import asynccontextmanager, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from .result_fakes import FakeScalarResult

from shared.auth.service_principal import SCOPE_KPI_EVALUATE
from src.auth.middleware import CurrentEmbedUser, CurrentServiceUser, get_current_user
from src.main import app

from .conftest import (
    TEST_AGG_ID,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    TEST_TENANT,
    TEST_USER_ID,
    async_gen_from,
    client,
    make_mock_db,
    make_model,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Auth-override helpers
# ---------------------------------------------------------------------------

def _service_user() -> CurrentServiceUser:
    """A validly-scoped internal service principal (KPI evaluator)."""
    return CurrentServiceUser(
        principal="kpi-snapshot-sweep",
        tenant_id=TEST_TENANT,
        role="kpi_evaluator",
        scopes=[SCOPE_KPI_EVALUATE],
    )


def _embed_user() -> CurrentEmbedUser:
    return CurrentEmbedUser(
        user_id="embed@x",
        tenant_id=TEST_TENANT,
        email="embed@x",
        project_ids=None,
        model_ids=None,
    )


@contextmanager
def _as(user):
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Bug-7776 — service tokens blocked from human read-only surfaces
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_projects_rejects_service_token(client):
    """A scoped service token cannot enumerate the tenant's projects."""
    with _as(_service_user()):
        resp = await client.get("/api/v1/projects")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_projects_still_allows_embed_token(client):
    """Embed dashboards legitimately list their scoped projects — the service
    guard must NOT regress embed access."""
    mock_db = make_mock_db()

    class _Res:
        def scalars(self):
            return FakeScalarResult([])

        def all(self):
            return []

    mock_db.execute = AsyncMock(return_value=_Res())

    with _as(_embed_user()):
        with patch("src.api.projects.get_tenant_db", async_gen_from(mock_db)):
            resp = await client.get("/api/v1/projects")
    # Embed passes the guard (200 with its scoped/empty list), never 403.
    assert resp.status_code != 403


@pytest.mark.asyncio
async def test_tenant_me_rejects_service_token(client):
    with _as(_service_user()):
        resp = await client.get("/api/v1/tenants/me")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_tenant_me_rejects_embed_token(client):
    """/tenants/me is a human management surface — embed tokens are rejected
    (require_human_user), matching the prior forbid_embed_user behaviour."""
    with _as(_embed_user()):
        resp = await client.get("/api/v1/tenants/me")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_edition_rejects_service_token(client):
    with _as(_service_user()):
        resp = await client.get("/api/v1/edition")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_limits_rejects_service_token(client):
    with _as(_service_user()):
        resp = await client.get("/api/v1/limits")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_edition_still_allows_human_user(client):
    """A regular human session (the default override) still reads the badge."""
    resp = await client.get("/api/v1/edition")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Bug-7300 — snapshot-export requires modeler, not viewer (model-DEFINITION
# export stays modeler+; only the credential-bearing PROJECT export is admin).
# ---------------------------------------------------------------------------

def _binding(role: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        user_identity=TEST_USER_ID,
        project_id=TEST_PROJECT_ID,
        model_id=None,
        role=role,
    )


@contextmanager
def _rbac_role(role: str):
    """Resolve require_role to a specific binding role, overriding the autouse
    bootstrap-admin mock (mirrors test_versions_rbac._rbac_role)."""
    mock_db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = _binding(role)
    result.first.return_value = (uuid.uuid4(),)
    mock_db.execute = AsyncMock(return_value=result)
    with patch("src.auth.rbac.get_tenant_db", async_gen_from(mock_db)):
        yield


SNAPSHOT_EXPORT = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/snapshot-export"
)


@pytest.mark.asyncio
async def test_snapshot_export_denies_viewer(client):
    """A viewer must NOT be able to download the governance/security bundle."""
    with _rbac_role("viewer"):
        resp = await client.get(SNAPSHOT_EXPORT)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_snapshot_export_allows_modeler(client):
    """A modeler passes the gate (proves the route lets modeler+ through, not
    only admin) — reaching the handler, which we stub. Model-DEFINITION export
    stays modeler+ (user decision 2026-08-19)."""
    model = make_model()
    db = make_mock_db()
    snapshot = {"data_sources": [], "data_targets": []}

    @asynccontextmanager
    async def consistent_session(_tenant_id):
        yield db

    with _rbac_role("modeler"):
        with (
            patch(
                "src.api.import_export.consistent_read_session",
                new=consistent_session,
            ),
            patch(
                "src.api.import_export._ensure_model_access",
                new=AsyncMock(return_value=model),
            ),
            patch(
                "src.api.import_export.snapshot_model",
                new=AsyncMock(return_value=snapshot),
            ),
        ):
            resp = await client.get(SNAPSHOT_EXPORT)
    assert resp.status_code != 403


# ---------------------------------------------------------------------------
# Bug-7891 / Bug-7776 residual — service tokens blocked from refresh and
# branding routes that previously only used forbid_embed_user
# ---------------------------------------------------------------------------

REFRESH_POLICY_URL = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"
    f"/aggregates/{TEST_AGG_ID}/refresh/policy"
)
REFRESH_RUNS_URL = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"
    f"/aggregates/{TEST_AGG_ID}/refresh/runs"
)
MODEL_REFRESH_RUNS_URL = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/refresh/runs"
)
BRANDING_URL = f"/api/v1/tenants/{TEST_TENANT}/branding"


@pytest.mark.asyncio
async def test_refresh_policy_post_rejects_service_token(client):
    """Bug-7891: the refresh policy POST (upsert) is a mutation that must not be
    reachable by a scoped service token. require_role('modeler') now gates it."""
    with _as(_service_user()):
        resp = await client.post(
            REFRESH_POLICY_URL,
            json={"refresh_mode": "full", "cron_expression": "0 0 * * *"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_refresh_policy_get_rejects_service_token(client):
    """Refresh policy GET is model-scoped metadata — service tokens rejected."""
    with _as(_service_user()):
        resp = await client.get(REFRESH_POLICY_URL)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_refresh_runs_rejects_service_token(client):
    """Refresh run history GET — service tokens rejected."""
    with _as(_service_user()):
        resp = await client.get(REFRESH_RUNS_URL)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_model_refresh_runs_rejects_service_token(client):
    """Model-level refresh run history GET — service tokens rejected."""
    with _as(_service_user()):
        resp = await client.get(MODEL_REFRESH_RUNS_URL)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_branding_get_rejects_service_token(client):
    """Branding GET is a tenant management surface — service tokens rejected
    (require_human_user)."""
    with _as(_service_user()):
        resp = await client.get(BRANDING_URL)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_branding_get_rejects_embed_token(client):
    """Branding GET — embed tokens also rejected (require_human_user)."""
    with _as(_embed_user()):
        resp = await client.get(BRANDING_URL)
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Bug-7776 class audit — service tokens blocked from all human-facing metadata
# GETs that previously only used forbid_embed_user
# ---------------------------------------------------------------------------

# Project/model-scoped metadata routes (require_role rejects service tokens)
PROJECT_SETTINGS_URL = f"/api/v1/projects/{TEST_PROJECT_ID}/settings"
MODEL_SETTINGS_URL = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/settings"
)
JOINS_URL = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/joins"
)
# auth.py user-facing routes (require_human_user rejects service+embed)
GET_ME_URL = "/api/v1/auth/users/me"
PAT_TOKENS_URL = "/api/v1/auth/tokens"


@pytest.mark.asyncio
async def test_project_settings_get_rejects_service_token(client):
    """Bug-7776 class audit: project settings GET now has require_role."""
    with _as(_service_user()):
        resp = await client.get(PROJECT_SETTINGS_URL)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_model_settings_get_rejects_service_token(client):
    """Bug-7776 class audit: model settings GET now has require_role."""
    with _as(_service_user()):
        resp = await client.get(MODEL_SETTINGS_URL)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_joins_list_rejects_service_token(client):
    """Bug-7776 class audit: joins list GET now has require_role."""
    with _as(_service_user()):
        resp = await client.get(JOINS_URL)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_me_rejects_service_token(client):
    """Bug-7776 class audit: /users/me now uses require_human_user."""
    with _as(_service_user()):
        resp = await client.get(GET_ME_URL)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_pat_create_rejects_service_token(client):
    """Bug-7776 class audit: PAT creation now uses require_human_user.
    A service token must never mint authentication tokens."""
    with _as(_service_user()):
        resp = await client.post(PAT_TOKENS_URL, json={"label": "test"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_pat_list_rejects_service_token(client):
    """Bug-7776 class audit: PAT list now uses require_human_user."""
    with _as(_service_user()):
        resp = await client.get(PAT_TOKENS_URL)
    assert resp.status_code == 403
