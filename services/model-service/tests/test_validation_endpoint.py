"""
Tests for POST /api/v1/projects/{project_id}/models/{model_id}/validate.

Coverage:
  - Valid model (no violations) returns {"valid": true, "violations": []}
  - Broken dimension returns a violation entry with object_type="dimension"
  - Broken measure returns a violation entry with object_type="measure"

Run from tessallite/services/model-service/:
    pytest tests/test_validation_endpoint.py
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.db.models import Model

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    TEST_TENANT,
    async_gen_from,
    make_mock_db,
)

_VALID_URL = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/validate"

_DB_PATCH = "src.api.validation.get_tenant_db"
_STRUCTURE_PATCH = "src.api.validation._load_model_structure"
_VALIDATE_DIM_PATCH = "src.api.validation.validate_dimension"
_VALIDATE_MEASURE_PATCH = "src.api.validation.validate_measure"
_VALIDATE_AGG_PATCH = "src.api.validation.validate_aggregate"


def _make_dim(name: str = "country") -> types.SimpleNamespace:
    return types.SimpleNamespace(id=uuid.uuid4(), name=name)


def _make_measure(name: str = "revenue") -> types.SimpleNamespace:
    return types.SimpleNamespace(id=uuid.uuid4(), name=name, source_table_id=None, source_column_name=None)


def _make_agg(table_name: str = "agg_table") -> types.SimpleNamespace:
    return types.SimpleNamespace(id=uuid.uuid4(), physical_table_name=table_name)


def _make_structure():
    return MagicMock()


def _make_db_with_objects(dims=(), measures=(), aggs=()):
    db = make_mock_db()
    # Bug-8862: the route now proves project -> model before validating, so
    # the mock session must resolve a Model owned by the path project. See
    # test_misc_binding_scoping_8862.py for the denial case.
    #
    # Discriminate on the entity rather than returning one object for every
    # db.get: a blanket return would silently satisfy any future second
    # db.get on a different entity and hide a missing guard.
    async def _get(entity, entity_id):
        if entity is Model:
            return types.SimpleNamespace(id=entity_id, project_id=TEST_PROJECT_ID)
        return None

    db.get = AsyncMock(side_effect=_get)

    def _execute_side_effect(stmt):
        text = str(stmt)
        result = MagicMock()
        if "Dimension" in text or "dimensions" in text.lower():
            result.scalars.return_value.all.return_value = list(dims)
        elif "Measure" in text or "measures" in text.lower():
            result.scalars.return_value.all.return_value = list(measures)
        elif "AggregateDefinition" in text or "aggregate_definitions" in text.lower():
            result.scalars.return_value.all.return_value = list(aggs)
        else:
            result.scalars.return_value.all.return_value = []
            result.scalar_one_or_none.return_value = None
        return result

    db.execute = AsyncMock(side_effect=_execute_side_effect)
    return db


# ---------------------------------------------------------------------------
# Valid model — no violations
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_valid_model_returns_no_violations(client):
    db = _make_db_with_objects(
        dims=[_make_dim("country")],
        measures=[_make_measure("revenue")],
    )
    structure = _make_structure()

    with (
        patch(_DB_PATCH, async_gen_from(db)),
        patch(_STRUCTURE_PATCH, AsyncMock(return_value=structure)),
        patch(_VALIDATE_DIM_PATCH, return_value=None),
        patch(_VALIDATE_MEASURE_PATCH, return_value=None),
        patch(_VALIDATE_AGG_PATCH, AsyncMock(return_value=None)),
    ):
        resp = await client.post(_VALID_URL)

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["violations"] == []


# ---------------------------------------------------------------------------
# Broken dimension — returns violation entry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_broken_dimension_returns_violation(client):
    dim = _make_dim("region")
    db = _make_db_with_objects(dims=[dim])
    structure = _make_structure()

    with (
        patch(_DB_PATCH, async_gen_from(db)),
        patch(_STRUCTURE_PATCH, AsyncMock(return_value=structure)),
        patch(_VALIDATE_DIM_PATCH, return_value="Source column no longer exists"),
        patch(_VALIDATE_MEASURE_PATCH, return_value=None),
        patch(_VALIDATE_AGG_PATCH, AsyncMock(return_value=None)),
    ):
        resp = await client.post(_VALID_URL)

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert len(body["violations"]) == 1
    v = body["violations"][0]
    assert v["object_type"] == "dimension"
    assert v["object_name"] == "region"
    assert "Source column" in v["reason"]


# ---------------------------------------------------------------------------
# Broken measure — returns violation entry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_broken_measure_returns_violation(client):
    measure = _make_measure("profit")
    db = _make_db_with_objects(measures=[measure])
    structure = _make_structure()

    with (
        patch(_DB_PATCH, async_gen_from(db)),
        patch(_STRUCTURE_PATCH, AsyncMock(return_value=structure)),
        patch(_VALIDATE_DIM_PATCH, return_value=None),
        patch(_VALIDATE_MEASURE_PATCH, return_value="Model has no tables"),
        patch(_VALIDATE_AGG_PATCH, AsyncMock(return_value=None)),
    ):
        resp = await client.post(_VALID_URL)

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert any(
        v["object_type"] == "measure" and v["object_name"] == "profit"
        for v in body["violations"]
    )
