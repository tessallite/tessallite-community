"""
Unit tests for model export/import routes.
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from .conftest import TEST_MODEL_ID, TEST_PROJECT_ID, async_gen_from, client, make_mock_db, make_model

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _ScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items


def _measure_payload(measure_id: uuid.UUID, name: str) -> dict:
    return {
        "id": str(measure_id),
        "model_id": str(TEST_MODEL_ID),
        "name": name,
        "display_name": name,
        "source_column_id": None,
        "source_column_name": None,
        "source_table_id": None,
        "user_defined_attribute_id": None,
        "user_defined_attribute_name": None,
        "measure_type": "standard",
        "expression": None,
        "data_type": "numeric",
        "default_agg": "sum",
        "is_additive": True,
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
    }


def _hierarchy_payload(
    *,
    hierarchy_id: uuid.UUID,
    key_attribute_id: uuid.UUID,
    level_attribute_id: uuid.UUID,
) -> dict:
    level_id = uuid.uuid4()
    return {
        "id": str(hierarchy_id),
        "model_id": str(TEST_MODEL_ID),
        "name": "Geo",
        "type": "explicit",
        "description": None,
        "segment_config": None,
        "date_config": None,
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "levels": [
            {
                "id": str(level_id),
                "hierarchy_id": str(hierarchy_id),
                "name": "Region",
                "ordinal": 0,
                "key_attribute_id": str(key_attribute_id),
                "key_attribute_source": "physical_column",
                "description": None,
                "created_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
                "attributes": [
                    {
                        "id": str(uuid.uuid4()),
                        "attribute_id": str(level_attribute_id),
                        "attribute_source": "physical_column",
                        "role": "display",
                    }
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_import_model_hierarchies_resolves_measure_by_exported_name(client):
    model = make_model()
    live_measure_id = uuid.uuid4()
    exported_measure_id = uuid.uuid4()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)
    mock_db.execute = AsyncMock(
        return_value=_ScalarResult([types.SimpleNamespace(id=live_measure_id, name="total_revenue")])
    )

    body = {
        "replace_hierarchies": False,
        "measures": [_measure_payload(exported_measure_id, "total_revenue")],
        "hierarchies": [
            _hierarchy_payload(
                hierarchy_id=uuid.uuid4(),
                key_attribute_id=uuid.uuid4(),
                level_attribute_id=uuid.uuid4(),
            )
        ],
    }

    with (
        patch("src.api.export.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.export._attribute_in_model", new=AsyncMock(return_value=True)),
    ):
        resp = await client.post(f"{PREFIX}/import", json=body)

    assert resp.status_code == 200
    data = resp.json()
    assert data["imported_hierarchies"] == 1
    assert data["imported_levels"] == 1
    assert data["warnings"] == []
    assert mock_db.commit.await_count == 1
    assert mock_db.rollback.await_count == 0


@pytest.mark.asyncio
async def test_import_model_hierarchies_rolls_back_on_invalid_level_attribute(client):
    model = make_model()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)
    mock_db.execute = AsyncMock(return_value=_ScalarResult([]))

    body = {
        "replace_hierarchies": False,
        "measures": [],
        "hierarchies": [
            _hierarchy_payload(
                hierarchy_id=uuid.uuid4(),
                key_attribute_id=uuid.uuid4(),
                level_attribute_id=uuid.uuid4(),
            )
        ],
    }

    with (
        patch("src.api.export.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.export._attribute_in_model", new=AsyncMock(return_value=False)),
    ):
        resp = await client.post(f"{PREFIX}/import", json=body)

    assert resp.status_code == 422
    assert "key attribute that does not exist" in resp.json()["detail"]
    assert mock_db.commit.await_count == 0
    assert mock_db.rollback.await_count == 1
