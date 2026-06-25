"""API tests for generated LookML project downloads."""
from __future__ import annotations

import io
import types
import uuid
import zipfile
from unittest.mock import AsyncMock, patch

import pytest

from .conftest import TEST_MODEL_ID, TEST_PROJECT_ID, async_gen_from, make_mock_db, make_model

pytestmark = pytest.mark.unit


def _snapshot() -> dict:
    return {
        "model": {"id": str(TEST_MODEL_ID), "slug": "test-model"},
        "tables": [
            {
                "id": "fact-orders",
                "alias": "Orders",
                "display_name": "Orders",
                "table_type": "fact",
            }
        ],
        "columns": [
            {
                "id": "order-id",
                "model_table_id": "fact-orders",
                "column_name": "order_id",
                "data_type": "integer",
                "is_primary_key": True,
            }
        ],
        "dimensions": [
            {
                "id": "dimension-order-id",
                "name": "Order ID",
                "display_name": "Order ID",
                "source_column_id": "order-id",
            }
        ],
        "measures": [],
        "joins": [],
    }


@pytest.mark.asyncio
async def test_lookml_export_downloads_generated_archive_from_deployed_snapshot(client) -> None:
    model = make_model()
    model.deployed_version_id = uuid.uuid4()
    db = make_mock_db()
    db.get = AsyncMock(
        return_value=types.SimpleNamespace(
            model_id=TEST_MODEL_ID,
            snapshot_json=_snapshot(),
        )
    )

    with (
        patch("src.api.lookml_export.get_tenant_db", async_gen_from(db)),
        patch("src.api.lookml_export._ensure_model_access", new=AsyncMock(return_value=model)),
    ):
        response = await client.post(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/export/lookml",
            json={"connection": "tessallite_gateway"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["x-tessallite-lookml-warnings"] == "0"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist() == [
            "manifest.lkml",
            "models/test_model.model.lkml",
            "views/orders.view.lkml",
        ]
        model_lkml = archive.read("models/test_model.model.lkml").decode()
    assert 'connection: "tessallite_gateway"' in model_lkml
    db.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_lookml_export_requires_deployed_model(client) -> None:
    model = make_model()
    model.deployed_version_id = None
    db = make_mock_db()

    with (
        patch("src.api.lookml_export.get_tenant_db", async_gen_from(db)),
        patch("src.api.lookml_export._ensure_model_access", new=AsyncMock(return_value=model)),
    ):
        response = await client.post(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/export/lookml",
            json={"connection": "tessallite_gateway"},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "Deploy the model before exporting LookML."


@pytest.mark.asyncio
async def test_lookml_export_rejects_missing_deployed_version_snapshot(client) -> None:
    model = make_model()
    model.deployed_version_id = uuid.uuid4()
    db = make_mock_db()
    db.get = AsyncMock(return_value=None)

    with (
        patch("src.api.lookml_export.get_tenant_db", async_gen_from(db)),
        patch("src.api.lookml_export._ensure_model_access", new=AsyncMock(return_value=model)),
    ):
        response = await client.post(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/export/lookml",
            json={"connection": "tessallite_gateway"},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "The deployed model version could not be found."
