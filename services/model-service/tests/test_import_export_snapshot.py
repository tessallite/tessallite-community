"""Contract tests for the model snapshot export consumed by LookML export."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from .conftest import TEST_MODEL_ID, TEST_PROJECT_ID, async_gen_from, make_mock_db, make_model

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_snapshot_export_includes_deployed_version_for_looker_manifest(client) -> None:
    deployed_version_id = uuid.uuid4()
    model = make_model()
    model.deployed_version_id = deployed_version_id
    db = make_mock_db()
    snapshot = {"data_sources": [], "data_targets": []}

    with (
        patch("src.api.import_export.get_tenant_db", async_gen_from(db)),
        patch("src.api.import_export._ensure_model_access", new=AsyncMock(return_value=model)),
        patch("src.api.import_export.snapshot_model", new=AsyncMock(return_value=snapshot)),
    ):
        response = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/snapshot-export"
        )

    assert response.status_code == 200
    assert (
        response.json()["bundle"]["snapshot"]["exported_deployed_version_id"]
        == str(deployed_version_id)
    )
