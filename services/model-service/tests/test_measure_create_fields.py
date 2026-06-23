"""Verify measure create persists all accepted semantic fields.

The create endpoint previously dropped description, display_folder,
semi_additive_behavior, semi_additive_account_column_id, hierarchy_id,
and date_dimension_column_id. This test ensures they are persisted.
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.db.models import HierarchyDefinition, Measure
from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    client,
    make_mock_db,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/measures"


def _patch_scope():
    async def _noop(db, *, project_id, model_id):
        return None
    return patch("src.api.measures.ensure_model_in_project", _noop)


def _col_row(col_id, table_id):
    return types.SimpleNamespace(
        id=col_id,
        column_name="amount",
        model_table_id=table_id,
        data_type="numeric",
    )


def _make_db_for_create(col_id, table_id, *, hierarchy_id=None, date_dim_col_id=None):
    mock_db = make_mock_db()
    col = _col_row(col_id, table_id)

    model_col = types.SimpleNamespace(
        id=col_id,
        column_name="amount",
        model_table_id=table_id,
        is_hidden=False,
    )

    hierarchy_obj = None
    if hierarchy_id is not None:
        hierarchy_obj = types.SimpleNamespace(
            id=hierarchy_id,
            model_id=TEST_MODEL_ID,
            name="Time Hierarchy",
            dimension_kind="time",
        )

    date_dim_col_obj = None
    fact_table_obj = None
    if date_dim_col_id is not None:
        date_dim_col_obj = types.SimpleNamespace(
            id=date_dim_col_id,
            column_name="order_date",
            model_table_id=table_id,
        )
        fact_table_obj = types.SimpleNamespace(
            id=table_id,
            model_id=TEST_MODEL_ID,
            table_type="fact",
        )

    async def _exec(stmt):
        text = str(stmt)
        r = MagicMock()
        if "model_columns" in text.lower():
            r.scalar_one_or_none.return_value = col
        else:
            r.scalar_one_or_none.return_value = None
            r.scalars.return_value.all.return_value = []
            r.first.return_value = None
        return r

    mock_db.execute = AsyncMock(side_effect=_exec)

    async def _get(cls, key):
        if key == col_id:
            return model_col
        if hierarchy_obj and key == hierarchy_id:
            return hierarchy_obj
        if date_dim_col_obj and key == date_dim_col_id:
            return date_dim_col_obj
        if fact_table_obj and key == table_id:
            return fact_table_obj
        return None

    mock_db.get = AsyncMock(side_effect=_get)

    async def _refresh(obj):
        obj.id = obj.id or uuid.uuid4()
        obj.created_at = obj.created_at or NOW
        obj.updated_at = obj.updated_at or NOW
        obj.is_hidden = getattr(obj, "is_hidden", False)
        obj.is_invalid = getattr(obj, "is_invalid", False)
        obj.invalid_reason = getattr(obj, "invalid_reason", None)

    mock_db.refresh = AsyncMock(side_effect=_refresh)
    return mock_db


@pytest.mark.asyncio
async def test_create_persists_description_and_display_folder(client):
    col_id = uuid.uuid4()
    table_id = uuid.uuid4()
    mock_db = _make_db_for_create(col_id, table_id)

    with (
        _patch_scope(),
        patch("src.api.measures.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.measures._glossary_text_for_target", AsyncMock(return_value=None)),
    ):
        resp = await client.post(PREFIX, json={
            "name": "test_revenue",
            "source_table_id": str(table_id),
            "source_column_name": "amount",
            "measure_type": "standard",
            "description": "Total revenue across all channels",
            "display_folder": "Finance/Revenue",
        })

    assert resp.status_code == 201, resp.text
    added_objects = [call[0][0] for call in mock_db.add.call_args_list]
    measure = next(o for o in added_objects if isinstance(o, Measure))
    assert measure.description == "Total revenue across all channels"
    assert measure.display_folder == "Finance/Revenue"


@pytest.mark.asyncio
async def test_create_persists_semi_additive_behavior(client):
    col_id = uuid.uuid4()
    table_id = uuid.uuid4()
    mock_db = _make_db_for_create(col_id, table_id)

    with (
        _patch_scope(),
        patch("src.api.measures.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.measures._glossary_text_for_target", AsyncMock(return_value=None)),
    ):
        resp = await client.post(PREFIX, json={
            "name": "balance",
            "source_table_id": str(table_id),
            "source_column_name": "amount",
            "measure_type": "standard",
            "semi_additive_behavior": "last_non_empty",
        })

    assert resp.status_code == 201, resp.text
    added_objects = [call[0][0] for call in mock_db.add.call_args_list]
    measure = next(o for o in added_objects if isinstance(o, Measure))
    assert measure.semi_additive_behavior == "last_non_empty"


@pytest.mark.asyncio
async def test_create_persists_hierarchy_and_date_dimension(client):
    col_id = uuid.uuid4()
    table_id = uuid.uuid4()
    hierarchy_id = uuid.uuid4()
    date_dim_col_id = uuid.uuid4()
    mock_db = _make_db_for_create(col_id, table_id, hierarchy_id=hierarchy_id, date_dim_col_id=date_dim_col_id)

    with (
        _patch_scope(),
        patch("src.api.measures.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.measures._glossary_text_for_target", AsyncMock(return_value=None)),
    ):
        resp = await client.post(PREFIX, json={
            "name": "revenue",
            "source_table_id": str(table_id),
            "source_column_name": "amount",
            "measure_type": "standard",
            "hierarchy_id": str(hierarchy_id),
            "date_dimension_column_id": str(date_dim_col_id),
        })

    assert resp.status_code == 201, resp.text
    added_objects = [call[0][0] for call in mock_db.add.call_args_list]
    measure = next(o for o in added_objects if isinstance(o, Measure))
    assert str(measure.hierarchy_id) == str(hierarchy_id)
    assert str(measure.date_dimension_column_id) == str(date_dim_col_id)
