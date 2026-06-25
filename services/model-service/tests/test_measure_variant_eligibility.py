"""Bug-377: create_measure must enforce variant eligibility rules.

Previously _check_variant_eligibility was advisory-only (called by
/available-variants but not by the create endpoint). Now the create
endpoint calls it and returns 422 when the variant is ineligible.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.db.models import Measure
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


def _make_db_for_variant(col_id, table_id, *, base_measure=None):
    mock_db = make_mock_db()
    col = _col_row(col_id, table_id)

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
            return types.SimpleNamespace(
                id=col_id,
                column_name="amount",
                model_table_id=table_id,
                is_hidden=False,
            )
        if base_measure and key == base_measure.id:
            return base_measure
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
async def test_create_ytd_variant_without_hierarchy_rejected(client):
    """ytd variant requires a time hierarchy linked to the base measure.
    Without one, the endpoint must return 422."""
    col_id = uuid.uuid4()
    table_id = uuid.uuid4()
    base_id = uuid.uuid4()
    base = types.SimpleNamespace(
        id=base_id,
        model_id=TEST_MODEL_ID,
        name="revenue",
        measure_type="standard",
        variant_kind=None,
        calendar_model_table_id=None,
    )
    mock_db = _make_db_for_variant(col_id, table_id, base_measure=base)

    with (
        _patch_scope(),
        patch("src.api.measures.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.measures._glossary_text_for_target", AsyncMock(return_value=None)),
    ):
        resp = await client.post(PREFIX, json={
            "name": "revenue_ytd",
            "source_table_id": str(table_id),
            "source_column_name": "amount",
            "measure_type": "standard",
            "variant_kind": "ytd",
            "variant_of_measure_id": str(base_id),
        })

    assert resp.status_code == 422, resp.text
    assert "hierarchy" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_create_variant_of_calculated_measure_rejected(client):
    """Time variants of calculated measures are not supported — 422."""
    col_id = uuid.uuid4()
    table_id = uuid.uuid4()
    base_id = uuid.uuid4()
    base = types.SimpleNamespace(
        id=base_id,
        model_id=TEST_MODEL_ID,
        name="margin",
        measure_type="calculated",
        variant_kind=None,
        calendar_model_table_id=None,
    )
    mock_db = _make_db_for_variant(col_id, table_id, base_measure=base)

    with (
        _patch_scope(),
        patch("src.api.measures.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.measures._glossary_text_for_target", AsyncMock(return_value=None)),
    ):
        resp = await client.post(PREFIX, json={
            "name": "margin_ytd",
            "source_table_id": str(table_id),
            "source_column_name": "amount",
            "measure_type": "standard",
            "variant_kind": "ytd",
            "variant_of_measure_id": str(base_id),
        })

    assert resp.status_code == 422, resp.text
    assert "calculated" in resp.json()["detail"].lower()
