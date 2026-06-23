"""PATCH /measures/{id} guards for variant rows.

Covers:
  * Fields inherited from the base measure (source, agg, data_type, is_additive,
    measure_type, expression, user_defined_attribute_id) are rejected with
    HTTP 400 on a variant row.
  * variant_n is rejected on non-parametric variant rows and on plain
    (non-variant) rows.
  * Allowed fields (display_name, format, variant_n on trailing_n) succeed.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    NOW,
    async_gen_from,
    client,
    make_mock_db,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/measures"


def _variant_row(kind: str = "ytd", variant_n: int | None = None) -> types.SimpleNamespace:
    base_id = uuid.uuid4()
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=f"revenue_{kind}",
        display_name=f"Revenue ({kind})",
        description=None,
        display_folder=None,
        is_hidden=False,
        source_column_id=uuid.uuid4(),
        source_table_id=None,
        user_defined_attribute_id=None,
        measure_type="standard",
        expression=None,
        data_type="numeric",
        default_agg="sum",
        format=None,
        variant_kind=kind,
        variant_of_measure_id=base_id,
        variant_n=variant_n,
        is_additive=True,
        is_invalid=False,
        invalid_reason=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _plain_row() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name="revenue",
        display_name="Revenue",
        description=None,
        display_folder=None,
        is_hidden=False,
        source_column_id=uuid.uuid4(),
        source_table_id=None,
        user_defined_attribute_id=None,
        measure_type="standard",
        expression=None,
        data_type="numeric",
        default_agg="sum",
        format=None,
        variant_kind=None,
        variant_of_measure_id=None,
        variant_n=None,
        is_additive=True,
        is_invalid=False,
        invalid_reason=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _patch_scope():
    """Stub out ensure_model_in_project so it doesn't hit the DB."""
    async def _noop(db, *, project_id, model_id):
        return None
    return patch("src.api.measures.ensure_model_in_project", _noop)


def _get_for(measure):
    """Return a side_effect that yields the measure for its own id and None
    for every other lookup (e.g. ModelColumn during _build_response)."""
    async def _get(entity, entity_id):
        if entity_id == measure.id:
            return measure
        return None
    return _get


@pytest.mark.asyncio
async def test_patch_variant_row_rejects_inherited_fields(client):
    mock_db = make_mock_db()
    variant = _variant_row("ytd")
    mock_db.get = AsyncMock(return_value=variant)

    body = {"default_agg": "avg", "data_type": "integer"}

    with _patch_scope(), patch(
        "src.api.measures.get_tenant_db", async_gen_from(mock_db)
    ):
        resp = await client.patch(f"{PREFIX}/{variant.id}", json=body)

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "default_agg" in detail
    assert "data_type" in detail
    assert "inherit" in detail.lower()


@pytest.mark.asyncio
async def test_patch_variant_row_rejects_source_rebind(client):
    mock_db = make_mock_db()
    variant = _variant_row("trailing_n", variant_n=12)
    mock_db.get = AsyncMock(return_value=variant)

    body = {
        "source_table_id": str(uuid.uuid4()),
        "source_column_name": "other_col",
    }

    with _patch_scope(), patch(
        "src.api.measures.get_tenant_db", async_gen_from(mock_db)
    ):
        resp = await client.patch(f"{PREFIX}/{variant.id}", json=body)

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "source_table_id" in detail
    assert "source_column_name" in detail


@pytest.mark.asyncio
async def test_patch_variant_n_on_non_parametric_variant_rejected(client):
    mock_db = make_mock_db()
    variant = _variant_row("ytd")  # period-to-date, not parametric
    mock_db.get = AsyncMock(return_value=variant)

    body = {"variant_n": 6}

    with _patch_scope(), patch(
        "src.api.measures.get_tenant_db", async_gen_from(mock_db)
    ):
        resp = await client.patch(f"{PREFIX}/{variant.id}", json=body)

    assert resp.status_code == 400
    assert "variant_n is only valid" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_patch_variant_n_on_plain_measure_rejected(client):
    mock_db = make_mock_db()
    plain = _plain_row()
    mock_db.get = AsyncMock(return_value=plain)

    body = {"variant_n": 6}

    with _patch_scope(), patch(
        "src.api.measures.get_tenant_db", async_gen_from(mock_db)
    ):
        resp = await client.patch(f"{PREFIX}/{plain.id}", json=body)

    assert resp.status_code == 400
    assert "variant_n is only valid" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_patch_variant_n_on_trailing_n_succeeds(client):
    mock_db = make_mock_db()
    variant = _variant_row("trailing_n", variant_n=12)
    mock_db.get = AsyncMock(side_effect=_get_for(variant))

    body = {"variant_n": 6}

    with _patch_scope(), patch(
        "src.api.measures.get_tenant_db", async_gen_from(mock_db)
    ):
        resp = await client.patch(f"{PREFIX}/{variant.id}", json=body)

    assert resp.status_code == 200
    assert variant.variant_n == 6


@pytest.mark.asyncio
async def test_patch_variant_display_name_and_format_succeed(client):
    mock_db = make_mock_db()
    variant = _variant_row("ytd")
    mock_db.get = AsyncMock(side_effect=_get_for(variant))

    body = {"display_name": "Revenue YTD", "format": "currency"}

    with _patch_scope(), patch(
        "src.api.measures.get_tenant_db", async_gen_from(mock_db)
    ):
        resp = await client.patch(f"{PREFIX}/{variant.id}", json=body)

    assert resp.status_code == 200
    assert variant.display_name == "Revenue YTD"
    assert variant.format == "currency"
