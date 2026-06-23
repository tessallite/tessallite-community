"""
Route tests for project/model scope validation on non-hierarchy endpoints.
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
async def test_list_dimensions_rejects_model_outside_project(client):
    wrong_project_id = uuid.uuid4()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=make_model(project_id=wrong_project_id))

    with patch("src.api.dimensions.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.get(f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/dimensions")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Model not found"


@pytest.mark.asyncio
async def test_list_measures_rejects_model_outside_project(client):
    wrong_project_id = uuid.uuid4()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=make_model(project_id=wrong_project_id))

    with patch("src.api.measures.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.get(f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/measures")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Model not found"


@pytest.mark.asyncio
async def test_list_user_defined_attributes_rejects_model_outside_project(client):
    wrong_project_id = uuid.uuid4()
    table_id = uuid.uuid4()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=make_model(project_id=wrong_project_id))

    with patch("src.api.user_defined_attributes.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/tables/{table_id}/user-defined-attributes"
        )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Model not found"


@pytest.mark.asyncio
async def test_list_table_attributes_rejects_model_outside_project(client):
    wrong_project_id = uuid.uuid4()
    table_id = uuid.uuid4()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=make_model(project_id=wrong_project_id))

    with patch("src.api.table_attributes.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/tables/{table_id}/attributes"
        )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Model not found"
