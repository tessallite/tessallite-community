"""Primary-key declaration contract for model table attributes."""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .conftest import TEST_MODEL_ID, TEST_PROJECT_ID, async_gen_from, make_mock_db

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_physical_attribute_list_exposes_declared_primary_key(client) -> None:
    table_id = uuid.uuid4()
    column_id = uuid.uuid4()
    db = make_mock_db()
    db.get = AsyncMock(return_value=types.SimpleNamespace(model_id=TEST_MODEL_ID))
    physical_result = MagicMock()
    physical_result.scalars.return_value.all.return_value = [
        types.SimpleNamespace(
            id=column_id,
            column_name="payment_id",
            display_name="Payment ID",
            description=None,
            is_hidden=True,
            hidden_reason=None,
            is_primary_key=True,
            data_type="bigint",
        )
    ]
    uda_result = MagicMock()
    uda_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(side_effect=[physical_result, uda_result])

    with (
        patch("src.api.table_attributes.get_tenant_db", async_gen_from(db)),
        patch("src.api.table_attributes.ensure_model_in_project", new=AsyncMock()),
    ):
        response = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/tables/{table_id}/attributes"
        )

    assert response.status_code == 200
    assert response.json()[0]["is_primary_key"] is True


@pytest.mark.asyncio
async def test_physical_attribute_update_persists_declared_primary_key(client) -> None:
    table_id = uuid.uuid4()
    column_id = uuid.uuid4()
    table = types.SimpleNamespace(model_id=TEST_MODEL_ID)
    column = types.SimpleNamespace(
        id=column_id,
        model_table_id=table_id,
        column_name="payment_id",
        display_name=None,
        description=None,
        is_hidden=False,
        hidden_reason=None,
        is_primary_key=False,
        data_type="bigint",
    )
    db = make_mock_db()
    db.get = AsyncMock(side_effect=[table, column])

    with (
        patch("src.api.table_attributes.get_tenant_db", async_gen_from(db)),
        patch("src.api.table_attributes.ensure_model_in_project", new=AsyncMock()),
    ):
        response = await client.patch(
            (
                f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"
                f"/tables/{table_id}/columns/{column_id}"
            ),
            json={"is_primary_key": True},
        )

    assert response.status_code == 200
    assert column.is_primary_key is True
    assert response.json()["is_primary_key"] is True
    db.commit.assert_awaited_once()
