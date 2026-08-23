"""Bug-374: dimension rename must cascade grain but NOT rewrite
grain_physical_cols, and must mark affected aggregates as pending.

The materialized table still uses the old column name until rebuilt.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from .result_fakes import FakeScalarResult

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    NOW,
    client,
    make_mock_db,
    async_gen_from,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/dimensions"


class _ScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return FakeScalarResult(self._items)

    def all(self):
        return self._items

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None


def _make_dimension(name="country", dim_id=None):
    return types.SimpleNamespace(
        id=dim_id or uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=name,
        display_name=name.title(),
        description=None,
        display_folder=None,
        source_column_id=uuid.uuid4(),
        source_column_name="country",
        user_defined_attribute_id=None,
        data_type="varchar",
        is_hidden=False,
        is_time_dim=False,
        time_grain=None,
        is_invalid=False,
        invalid_reason=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _make_aggregate(grain, status="active", physical_cols=None):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        grain=list(grain),
        grain_physical_cols=physical_cols or list(grain),
        status=status,
        target_id=uuid.uuid4(),
        physical_table_name="agg_test",
        created_at=NOW,
        updated_at=NOW,
    )


def _stub_response(**kwargs):
    from src.api.dimensions import DimensionResponse
    return DimensionResponse(
        id=kwargs.get("id", uuid.uuid4()),
        model_id=TEST_MODEL_ID,
        name=kwargs.get("name", "nation"),
        display_name="Nation",
        source_column_id=None,
        source_column_name="country",
        data_type="varchar",
        is_hidden=False,
        is_time_dim=False,
        time_grain=None,
        is_invalid=False,
        warnings=[],
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.asyncio
async def test_dimension_rename_marks_aggregate_pending(client):
    """Renaming a dimension used in an active aggregate's grain must:
    1. Update agg.grain (logical name cascade)
    2. NOT update agg.grain_physical_cols
    3. Mark agg status = 'pending'
    """
    dim = _make_dimension("country")
    agg = _make_aggregate(["country", "region"], physical_cols=["country", "region"])
    assert agg.status == "active"

    mock_db = make_mock_db()

    async def _get(cls, key):
        if key == dim.id:
            return dim
        return None

    mock_db.get = AsyncMock(side_effect=_get)

    exec_calls = []

    async def _exec(stmt):
        exec_calls.append(stmt)
        text = str(stmt)
        if "aggregate" in text.lower():
            return _ScalarResult([agg])
        if "persona" in text.lower():
            return _ScalarResult([])
        if "hierarchy_level" in text.lower():
            return _ScalarResult([])
        return _ScalarResult([])

    mock_db.execute = AsyncMock(side_effect=_exec)

    async def _refresh(obj):
        obj.updated_at = NOW

    mock_db.refresh = AsyncMock(side_effect=_refresh)

    async def _noop(db, *, project_id, model_id):
        return None

    async def _fake_build_response(db, dim_obj, *, warnings=None, redundant_partners=None):
        return _stub_response(id=dim_obj.id, name=dim_obj.name)

    with (
        patch("src.api.dimensions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.dimensions.ensure_model_in_project", _noop),
        patch("src.api.dimensions._build_response", _fake_build_response),
    ):
        resp = await client.patch(
            f"{PREFIX}/{dim.id}",
            json={"name": "nation"},
        )

    assert resp.status_code == 200, resp.text
    assert agg.grain == ["nation", "region"]
    assert agg.grain_physical_cols == ["country", "region"]
    assert agg.status == "pending"
