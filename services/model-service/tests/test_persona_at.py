"""L13-PERSONA-AT: explicit persona parameter targeting and preflight."""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from src.api.personas import (
    _validate_persona_scope,
    persona_parameter_collision_preflight,
)

from .conftest import TEST_MODEL_ID, TEST_PROJECT_ID, async_gen_from

pytestmark = pytest.mark.unit


class _ScalarView:
    def __init__(self, values):
        self.values = list(values)

    def all(self):
        return list(self.values)


class _ScalarResult:
    def __init__(self, values):
        self.values = list(values)

    def scalars(self):
        return _ScalarView(self.values)


def _validation_db(*, dimensions: list[str], parameters: list[str]):
    db = types.SimpleNamespace()
    db.execute = AsyncMock(
        side_effect=[_ScalarResult(dimensions), _ScalarResult(parameters)]
    )
    return db


@pytest.mark.asyncio
async def test_l13_persona_at_allows_dimension_and_explicit_parameter_targets():
    db = _validation_db(dimensions=["Region"], parameters=["@Region"])

    await _validate_persona_scope(
        db,
        TEST_MODEL_ID,
        default_filters={"Region": "EMEA", "@Region": "APAC"},
    )


@pytest.mark.asyncio
async def test_l13_r1_f2_accepts_lossless_typed_parameter_values():
    db = _validation_db(
        dimensions=["Region"],
        parameters=[
            types.SimpleNamespace(name="@code", param_type="string"),
            types.SimpleNamespace(name="@count", param_type="number"),
            types.SimpleNamespace(name="@enabled", param_type="boolean"),
            types.SimpleNamespace(name="@regions", param_type="multi_value"),
            types.SimpleNamespace(name="@period", param_type="date_range"),
        ],
    )

    await _validate_persona_scope(
        db,
        TEST_MODEL_ID,
        default_filters={
            "Region": "EMEA",
            "@code": "001",
            "@count": 10.5,
            "@enabled": True,
            "@regions": ["North, America", "EMEA"],
            "@period": {"from": "2026-01-01", "to": "2026-12-31"},
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("@count", "10"),
        ("@enabled", "true"),
        ("@regions", []),
        ("@regions", [{"label": "EMEA"}]),
        ("@period", {"from": "2026-01-01"}),
        ("@period", {"from": "2026-12-31", "to": "2026-01-01"}),
        ("@period", {"eq": {"from": "2026-01-01", "to": "2026-12-31"}}),
    ],
)
async def test_l13_r1_f2_rejects_lossy_or_wrong_typed_parameter_values(key, value):
    db = _validation_db(
        dimensions=["Region"],
        parameters=[
            types.SimpleNamespace(name="@count", param_type="number"),
            types.SimpleNamespace(name="@enabled", param_type="boolean"),
            types.SimpleNamespace(name="@regions", param_type="multi_value"),
            types.SimpleNamespace(name="@period", param_type="date_range"),
        ],
    )

    with pytest.raises(HTTPException) as exc:
        await _validate_persona_scope(
            db,
            TEST_MODEL_ID,
            default_filters={key: value},
        )

    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "PERSONA_PARAMETER_VALUE_INVALID"


@pytest.mark.asyncio
async def test_l13_persona_at_rejects_unknown_explicit_parameter():
    db = _validation_db(dimensions=["Region"], parameters=["@Country"])

    with pytest.raises(HTTPException) as exc:
        await _validate_persona_scope(
            db,
            TEST_MODEL_ID,
            default_filters={"@Region": "EMEA"},
        )

    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == (
        "PERSONA_DEFAULT_FILTER_UNKNOWN_PARAMETER"
    )


@pytest.mark.asyncio
async def test_l13_persona_at_preflight_uses_deployed_snapshot_and_reports_no_value():
    persona_id = uuid.uuid4()
    version_id = uuid.uuid4()
    model = types.SimpleNamespace(
        id=TEST_MODEL_ID,
        project_id=TEST_PROJECT_ID,
        deployed_version_id=version_id,
    )
    version = types.SimpleNamespace(
        id=version_id,
        model_id=TEST_MODEL_ID,
        snapshot_json={
            "model_parameters": [
                {"name": "@Region", "param_type": "string", "default_value": "EMEA"}
            ]
        },
    )
    persona = types.SimpleNamespace(
        id=persona_id,
        model_id=TEST_MODEL_ID,
        name="Sales",
        slug="sales",
        default_filters={"region": "APAC", "@Region": "LATAM"},
    )
    db = types.SimpleNamespace()

    async def _get(cls, key):
        if cls.__name__ == "Model":
            return model
        if cls.__name__ == "ModelVersion":
            return version
        return None

    db.get = AsyncMock(side_effect=_get)
    db.execute = AsyncMock(return_value=_ScalarResult([persona]))
    user = types.SimpleNamespace(tenant_id="tenant-1")

    with (
        patch("src.api.personas.ensure_model_in_project", new=AsyncMock()),
        patch("src.api.personas.get_tenant_db", async_gen_from(db)),
    ):
        response = await persona_parameter_collision_preflight(
            TEST_PROJECT_ID, TEST_MODEL_ID, user
        )

    assert response.deployed_version_id == version_id
    assert len(response.collisions) == 1
    collision = response.collisions[0]
    assert collision.default_filter_key == "region"
    assert collision.parameter_name == "@Region"
    assert collision.suggested_key == "@Region"
    assert not hasattr(collision, "value")
