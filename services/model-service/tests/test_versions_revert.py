"""
Unit tests for the ``revert`` endpoint's deploy-pointer retargeting.

B4 bug: before the fix, revert only retargeted ``deployed_version_id`` when
the previously-deployed version had been deleted by the revert. If the model
was deployed to an older version that *survived* the revert, the deploy
pointer stayed on that older version while the live state was rewritten to
the reverted version — a divergence that violates F-8.

After the fix, revert retargets the deploy pointer to the reverted version
whenever the model was deployed, regardless of whether the previously-
deployed version still exists. If the model was undeployed, the pointer
stays None.
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    client,
    make_mock_db,
    make_model,
)

pytestmark = pytest.mark.unit


PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"


def _version(version_id: uuid.UUID, number: int) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=version_id,
        model_id=TEST_MODEL_ID,
        version_number=number,
        summary=None,
        snapshot_json={
            "schema_version": 1,
            "model": {"id": str(TEST_MODEL_ID)},
            "tables": [],
            "columns": [],
            "physical_attributes": [],
            "user_defined_attributes": [],
            "targets": [],
            "measures": [],
            "joins": [],
            "hierarchies": [],
            "aggregates": [],
        },
        created_at=NOW,
        created_by="user@example.com",
    )


async def _rehydrate_noop(*_args, **_kwargs):
    return None


@pytest.mark.asyncio
async def test_revert_retargets_deploy_pointer_when_prior_deploy_survives(client):
    """Model deployed to v2, revert to v3 → deploy pointer moves to v3."""
    v2_id = uuid.uuid4()
    v3_id = uuid.uuid4()
    model = make_model()
    model.deployed_version_id = v2_id  # deployed to an older version

    before_ts = datetime.now(timezone.utc)

    mock_db = make_mock_db()
    # _ensure_model_access → Model lookup; revert → ModelVersion lookup
    mock_db.get = AsyncMock(side_effect=[model, _version(v3_id, 3)])

    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.rehydrate_into_live", new=_rehydrate_noop),
    ):
        resp = await client.post(
            f"{PREFIX}/versions/{v3_id}/revert",
            json={"confirm": "revert to v3"},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    # The deploy pointer must follow the revert even though v2 still exists.
    assert model.deployed_version_id == v3_id
    assert model.last_deployed_at is not None
    assert model.last_deployed_at >= before_ts


@pytest.mark.asyncio
async def test_revert_retargets_when_deployed_version_is_deleted(client):
    """Model deployed to v5 (which gets deleted by the revert), revert to v3
    → deploy pointer moves to v3. Pre-existing behaviour; covered so the
    regression is obvious if someone reintroduces the stale guard."""
    v3_id = uuid.uuid4()
    v5_id = uuid.uuid4()
    model = make_model()
    model.deployed_version_id = v5_id

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=[model, _version(v3_id, 3)])

    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.rehydrate_into_live", new=_rehydrate_noop),
    ):
        resp = await client.post(
            f"{PREFIX}/versions/{v3_id}/revert",
            json={"confirm": "revert to v3"},
        )

    assert resp.status_code == 200
    assert model.deployed_version_id == v3_id
    assert model.last_deployed_at is not None


@pytest.mark.asyncio
async def test_revert_leaves_pointer_none_when_undeployed(client):
    """An undeployed model stays undeployed after a revert."""
    v3_id = uuid.uuid4()
    model = make_model()
    assert model.deployed_version_id is None  # baseline

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=[model, _version(v3_id, 3)])

    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.rehydrate_into_live", new=_rehydrate_noop),
    ):
        resp = await client.post(
            f"{PREFIX}/versions/{v3_id}/revert",
            json={"confirm": "revert to v3"},
        )

    assert resp.status_code == 200
    assert model.deployed_version_id is None
    assert model.last_deployed_at is None


@pytest.mark.asyncio
async def test_revert_rejects_wrong_confirm_phrase(client):
    """Confirmation string must exactly match ``revert to v{N}``."""
    v3_id = uuid.uuid4()
    model = make_model()

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=[model, _version(v3_id, 3)])

    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.rehydrate_into_live", new=_rehydrate_noop),
    ):
        resp = await client.post(
            f"{PREFIX}/versions/{v3_id}/revert",
            json={"confirm": "revert"},
        )

    assert resp.status_code == 400
    assert "confirm must equal" in resp.json()["detail"]
