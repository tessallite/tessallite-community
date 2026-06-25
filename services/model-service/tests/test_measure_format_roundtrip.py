"""Phase 1 — measure.format PATCH round-trip.

The save-time validator in ``MeasureUpdate`` is already exercised by
``test_phase1_field_validators``; this test covers the surface API to
make sure a legitimate format token flows through PATCH unchanged, and
an unknown token is rejected at the request boundary (HTTP 422) rather
than silently persisted.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest

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


def _plain_row(format_: str | None = None) -> types.SimpleNamespace:
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
        calc_agg_mode=None,
        data_type="numeric",
        default_agg="sum",
        format=format_,
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
    async def _noop(db, *, project_id, model_id):
        return None
    return patch("src.api.measures.ensure_model_in_project", _noop)


def _get_for(measure):
    async def _get(entity, entity_id):
        if entity_id == measure.id:
            return measure
        return None
    return _get


@pytest.mark.asyncio
async def test_patch_format_known_token_persists(client):
    mock_db = make_mock_db()
    m = _plain_row(format_="currency")
    mock_db.get = AsyncMock(side_effect=_get_for(m))

    with _patch_scope(), patch(
        "src.api.measures.get_tenant_db", async_gen_from(mock_db)
    ):
        resp = await client.patch(f"{PREFIX}/{m.id}", json={"format": "percent_2dp"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["format"] == "percent_2dp"
    assert m.format == "percent_2dp"


@pytest.mark.asyncio
async def test_patch_format_unknown_token_rejected_at_boundary(client):
    mock_db = make_mock_db()
    m = _plain_row(format_="currency")
    mock_db.get = AsyncMock(side_effect=_get_for(m))

    with _patch_scope(), patch(
        "src.api.measures.get_tenant_db", async_gen_from(mock_db)
    ):
        resp = await client.patch(f"{PREFIX}/{m.id}", json={"format": "made_up"})

    # Pydantic validation error → 422, not 400; the token never reaches the handler.
    assert resp.status_code == 422, resp.text
    # The stored format must not have changed.
    assert m.format == "currency"


@pytest.mark.asyncio
async def test_patch_format_null_clears(client):
    mock_db = make_mock_db()
    m = _plain_row(format_="percent")
    mock_db.get = AsyncMock(side_effect=_get_for(m))

    with _patch_scope(), patch(
        "src.api.measures.get_tenant_db", async_gen_from(mock_db)
    ):
        resp = await client.patch(f"{PREFIX}/{m.id}", json={"format": None})

    assert resp.status_code == 200, resp.text
    assert resp.json()["format"] is None
    assert m.format is None
