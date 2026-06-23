"""Empty include lists mean "unrestricted" for that object class.

Phase 8.B.1 acceptance bullet — locks the contract that an empty
`included_measure_ids` does NOT block every measure (the "I made a
persona and now nothing is visible" footgun the action plan calls
out). Same semantics for dimensions and hierarchies.
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
    make_model,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/personas"


def _empty_persona() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name="Empty",
        slug="empty",
        description=None,
        included_measure_ids=[],
        included_dimension_ids=[],
        included_hierarchy_ids=[],
        audience_roles=[],
        default_filters={},
        bypass_row_security=False,
        includes_hidden_columns=False,
        created_at=NOW,
        updated_at=NOW,
    )


def _scripted_get(*, model, persona):
    async def _get(cls, key):
        if cls.__name__ == "Model":
            return model
        if cls.__name__ == "Persona":
            return persona
        return None

    return AsyncMock(side_effect=_get)


@pytest.mark.asyncio
async def test_empty_measure_list_resolves_as_unrestricted(client):
    model = make_model()
    p = _empty_persona()
    measure_id = uuid.uuid4()
    db = make_mock_db()
    db.get = _scripted_get(model=model, persona=p)

    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"{PREFIX}/{p.id}/resolution?measure_id={measure_id}"
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["measure_allowed"] is True
    assert "unrestricted" in (body["reason"] or "")


@pytest.mark.asyncio
async def test_create_with_no_includes_persists_empty_lists(client):
    model = make_model()
    db = make_mock_db()
    db.get = AsyncMock(side_effect=lambda cls, key: model if cls.__name__ == "Model" else None)

    captured: dict = {}

    def _add(obj):
        captured["obj"] = obj
        if not getattr(obj, "id", None):
            obj.id = uuid.uuid4()
        obj.created_at = NOW
        obj.updated_at = NOW

    db.add = _add

    async def _refresh(obj):
        return None

    db.refresh = _refresh

    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            PREFIX, json={"name": "All-Open", "slug": "all_open"}
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["included_measure_ids"] == []
    assert body["included_dimension_ids"] == []
    assert body["included_hierarchy_ids"] == []
    assert body["audience_roles"] == []
    assert body["default_filters"] == {}
