"""
Route tests for project/model scope validation on hierarchy endpoints.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    client,
    make_mock_db,
    make_model,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_list_hierarchies_rejects_model_outside_project(client):
    wrong_project_id = uuid.uuid4()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=make_model(project_id=wrong_project_id))

    with patch("src.api.hierarchies.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.get(f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/hierarchies")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Model not found"


@pytest.mark.asyncio
async def test_create_hierarchy_rejects_model_outside_project(client):
    wrong_project_id = uuid.uuid4()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=make_model(project_id=wrong_project_id))

    with patch("src.api.hierarchies.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.post(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/hierarchies",
            json={"name": "Geo", "type": "explicit"},
        )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Model not found"


@pytest.mark.asyncio
async def test_get_hierarchy_rejects_model_outside_project(client):
    wrong_project_id = uuid.uuid4()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=make_model(project_id=wrong_project_id))

    with patch("src.api.hierarchies.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/hierarchies/{uuid.uuid4()}"
        )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Model not found"
