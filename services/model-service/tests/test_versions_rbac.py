"""F-013-04: versioning / deploy / revert / settings / import endpoints must
enforce the binding ROLE, not merely binding existence.

Before the fix, ``_ensure_model_access`` checked only that *some*
UserAccessBinding row existed for the project, never its ``role`` — so a
``viewer`` could Save, Deploy, Undeploy, Revert, import models, and write
settings. The routes now carry ``require_role`` (viewer for reads, modeler for
Save/deploy/undeploy/settings/import, admin for revert), which reads the
effective role and denies under-privileged callers with 403.

The tests patch ``src.auth.rbac.get_tenant_db`` so ``require_role`` resolves a
specific binding role, overriding the autouse bootstrap-admin mock.
"""
from __future__ import annotations

import types
import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    TEST_USER_ID,
    async_gen_from,
    client,
    make_mock_db,
    make_model,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"


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
    """Patch require_role's DB so the caller resolves to ``role``.

    The first model-scoped lookup returns the binding; the bootstrap
    existence probe never fires because a role is already resolved.
    """
    mock_db = AsyncMock()
    result = MagicMock()
    # require_role first runs the model-scoped binding query when model_id is
    # in the path; return the binding there.
    result.scalar_one_or_none.return_value = _binding(role)
    result.first.return_value = (uuid.uuid4(),)
    mock_db.execute = AsyncMock(return_value=result)
    with patch("src.auth.rbac.get_tenant_db", async_gen_from(mock_db)):
        yield


# ---------------------------------------------------------------------------
# Deny: viewer cannot mutate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_viewer_cannot_deploy(client):
    with _rbac_role("viewer"):
        resp = await client.post(f"{PREFIX}/deploy", json={})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_viewer_cannot_save_version(client):
    with _rbac_role("viewer"):
        resp = await client.post(f"{PREFIX}/versions", json={})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_viewer_cannot_undeploy(client):
    with _rbac_role("viewer"):
        resp = await client.post(f"{PREFIX}/undeploy")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_modeler_cannot_revert_requires_admin(client):
    """Revert is the most destructive op → admin only."""
    v_id = uuid.uuid4()
    with _rbac_role("modeler"):
        resp = await client.post(
            f"{PREFIX}/versions/{v_id}/revert",
            json={"confirm": "revert to v1"},
        )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Allow: sufficient role passes the gate (then hits the handler)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_modeler_can_deploy(client):
    """Modeler passes the role gate; deploy fails only because the mock model
    has no saved version (400), proving the gate let it through (not 403)."""
    model = make_model()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)
    with _rbac_role("modeler"):
        with patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)):
            resp = await client.post(f"{PREFIX}/deploy", json={})
    assert resp.status_code != 403
    assert resp.status_code == 400  # "no saved versions; click Save first"


@pytest.mark.asyncio
async def test_viewer_can_list_versions(client):
    """Read routes allow viewers."""
    model = make_model()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)
    with _rbac_role("viewer"):
        with patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)):
            resp = await client.get(f"{PREFIX}/versions")
    assert resp.status_code == 200
