"""
API validation tests for hierarchy generation and preview endpoints.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    client,
    make_mock_db,
    make_model,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"


def _resolved_sql_attr(data_type: str = "date") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        resolved=types.SimpleNamespace(
            ref=types.SimpleNamespace(name="src_attr", data_type=data_type),
            table=types.SimpleNamespace(id=uuid.uuid4()),
        ),
        expression="src_attr",
        referenced_column_ids=[],
    )


@pytest.mark.asyncio
async def test_update_hierarchy_applies_name_type_kind_description(client):
    """PUT /hierarchies/{id} applies name / type / dimension_kind / description.

    Phase 8.0 parity fix — the frontend Edit Hierarchy flow depends on
    this endpoint accepting all four fields and returning the updated
    detail payload. Locks the contract.
    """
    mock_db = make_mock_db()
    hierarchy_id = uuid.uuid4()

    # Mutable stand-in for the HierarchyDefinition row — SimpleNamespace
    # attributes are mutated in place by update_hierarchy().
    hierarchy = types.SimpleNamespace(
        id=hierarchy_id,
        model_id=TEST_MODEL_ID,
        name="Old Name",
        type="explicit",
        dimension_kind=None,
        description=None,
        segment_config=None,
        date_config=None,
    )

    detail_payload = {
        "id": str(hierarchy_id),
        "model_id": str(TEST_MODEL_ID),
        "name": "New Name",
        "type": "explicit",
        "dimension_kind": "geo",
        "description": "Updated desc",
        "levels": [],
        "segment_config": None,
        "date_config": None,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }

    body = {
        "name": "New Name",
        "type": "explicit",
        "dimension_kind": "geo",
        "description": "Updated desc",
    }

    with (
        patch("src.api.hierarchies.get_tenant_db", async_gen_from(mock_db)),
        patch(
            "src.api.hierarchies._ensure_model_in_project",
            new=AsyncMock(return_value=make_model()),
        ),
        patch(
            "src.api.hierarchies._load_hierarchy_or_404",
            new=AsyncMock(return_value=hierarchy),
        ),
        patch(
            "src.api.hierarchies._hierarchy_detail",
            new=AsyncMock(return_value=detail_payload),
        ),
    ):
        resp = await client.put(
            f"{PREFIX}/hierarchies/{hierarchy_id}", json=body
        )

    assert resp.status_code == 200, resp.text
    # The handler mutated the row before commit.
    assert hierarchy.name == "New Name"
    assert hierarchy.type == "explicit"
    assert hierarchy.dimension_kind == "geo"
    assert hierarchy.description == "Updated desc"
    # Response bubbles through _hierarchy_detail().
    assert resp.json()["name"] == "New Name"
    assert resp.json()["dimension_kind"] == "geo"


@pytest.mark.asyncio
async def test_update_hierarchy_rejects_unsupported_dimension_kind(client):
    """PUT /hierarchies/{id} rejects an unsupported dimension_kind with D2."""
    mock_db = make_mock_db()
    hierarchy_id = uuid.uuid4()
    hierarchy = types.SimpleNamespace(
        id=hierarchy_id,
        model_id=TEST_MODEL_ID,
        name="Old Name",
        type="explicit",
        dimension_kind=None,
        description=None,
        segment_config=None,
        date_config=None,
    )

    with (
        patch("src.api.hierarchies.get_tenant_db", async_gen_from(mock_db)),
        patch(
            "src.api.hierarchies._ensure_model_in_project",
            new=AsyncMock(return_value=make_model()),
        ),
        patch(
            "src.api.hierarchies._load_hierarchy_or_404",
            new=AsyncMock(return_value=hierarchy),
        ),
    ):
        resp = await client.put(
            f"{PREFIX}/hierarchies/{hierarchy_id}",
            json={"dimension_kind": "not_a_real_kind"},
        )

    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "D2"
    # Row must not have been mutated.
    assert hierarchy.dimension_kind is None


@pytest.mark.asyncio
async def test_generate_date_rejects_unsupported_template(client):
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=make_model())

    body = {
        "name": "Date H",
        "source_attribute_id": str(uuid.uuid4()),
        "source_attribute_source": "physical_column",
        "template": "bad_template",
    }

    with (
        patch("src.api.hierarchies.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.hierarchies._resolve_sql_attribute", new=AsyncMock(return_value=_resolved_sql_attr("date"))),
    ):
        resp = await client.post(f"{PREFIX}/hierarchies/generate-date", json=body)

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "D2"


@pytest.mark.asyncio
async def test_generate_segment_delimiter_requires_two_levels(client):
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=make_model())

    body = {
        "name": "Seg H",
        "source_attribute_id": str(uuid.uuid4()),
        "source_attribute_source": "physical_column",
        "mode": "delimiter",
        "delimiter": "-",
        "levels": [{"name": "OnlyOne"}],
    }

    with (
        patch("src.api.hierarchies.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.hierarchies._resolve_sql_attribute", new=AsyncMock(return_value=_resolved_sql_attr("varchar"))),
    ):
        resp = await client.post(f"{PREFIX}/hierarchies/generate-segment", json=body)

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "H2"


@pytest.mark.asyncio
async def test_preview_expand_level_without_parent_key_allowed(client):
    """expand_level > 0 without parent_key should not be rejected — it previews all members at that level."""
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=make_model())
    hierarchy_id = uuid.uuid4()

    levels = [
        types.SimpleNamespace(ordinal=0, name="Region", key_attribute_id=uuid.uuid4(), key_attribute_source="physical_column"),
        types.SimpleNamespace(ordinal=1, name="Country", key_attribute_id=uuid.uuid4(), key_attribute_source="physical_column"),
    ]

    async def fake_resolve_sql(*args, **kwargs):
        raise HTTPException(status_code=404, detail="not found")

    with (
        patch("src.api.hierarchies.get_tenant_db", async_gen_from(mock_db)),
        patch(
            "src.api.hierarchies._load_hierarchy_or_404",
            new=AsyncMock(return_value=types.SimpleNamespace(id=hierarchy_id, name="Geo")),
        ),
        patch("src.api.hierarchies._levels_for_hierarchy", new=AsyncMock(return_value=levels)),
        patch("src.api.hierarchies._resolve_preview_connection", new=AsyncMock(return_value=(None, "postgresql", "No connection available"))),
        patch("src.api.hierarchies._resolve_sql_attribute", new=fake_resolve_sql),
    ):
        resp = await client.get(
            f"{PREFIX}/hierarchies/{hierarchy_id}/preview?sample_size=10&expand_level=1",
            headers={"Authorization": "Bearer test-token"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["warnings"]) > 0


@pytest.mark.asyncio
async def test_preview_returns_warning_when_hierarchy_references_missing_attribute(client):
    mock_db = make_mock_db()
    hierarchy_id = uuid.uuid4()

    levels = [
        types.SimpleNamespace(
            ordinal=0,
            name="Region",
            key_attribute_id=uuid.uuid4(),
            key_attribute_source="physical_column",
        ),
        types.SimpleNamespace(
            ordinal=1,
            name="Country",
            key_attribute_id=uuid.uuid4(),
            key_attribute_source="physical_column",
        ),
    ]

    with (
        patch("src.api.hierarchies.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.hierarchies._ensure_model_in_project", new=AsyncMock(return_value=None)),
        patch(
            "src.api.hierarchies._load_hierarchy_or_404",
            new=AsyncMock(return_value=types.SimpleNamespace(id=hierarchy_id, name="Geo")),
        ),
        patch("src.api.hierarchies._levels_for_hierarchy", new=AsyncMock(return_value=levels)),
        patch(
            "src.api.hierarchies._resolve_sql_attribute",
            new=AsyncMock(side_effect=HTTPException(status_code=404, detail="Attribute not found")),
        ),
    ):
        resp = await client.get(
            f"{PREFIX}/hierarchies/{hierarchy_id}/preview?sample_size=10",
            headers={"Authorization": "Bearer test-token"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["members"] == []
    assert any(w["type"] == "invalid_level_attribute" for w in body["warnings"])


@pytest.mark.asyncio
async def test_preview_warns_for_cross_table_expansion(client):
    mock_db = make_mock_db()
    hierarchy_id = uuid.uuid4()

    levels = [
        types.SimpleNamespace(
            ordinal=0,
            name="Region",
            key_attribute_id=uuid.uuid4(),
            key_attribute_source="physical_column",
        ),
        types.SimpleNamespace(
            ordinal=1,
            name="Country",
            key_attribute_id=uuid.uuid4(),
            key_attribute_source="physical_column",
        ),
    ]

    resolved_parent = types.SimpleNamespace(
        resolved=types.SimpleNamespace(
            table=types.SimpleNamespace(id=uuid.uuid4(), physical_name="dim_region", row_count_estimate=10),
            ref=types.SimpleNamespace(name="region_code", data_type="varchar"),
        ),
        expression="region_code",
        referenced_column_ids=[],
    )
    resolved_child = types.SimpleNamespace(
        resolved=types.SimpleNamespace(
            table=types.SimpleNamespace(id=uuid.uuid4(), physical_name="dim_country", row_count_estimate=10),
            ref=types.SimpleNamespace(name="country_code", data_type="varchar"),
        ),
        expression="country_code",
        referenced_column_ids=[],
    )

    # _introspect_batch_via_router returns results for estimate queries but no
    # sample — because cross-table expansion sets can_sample=False before the
    # sample query is added to batch_queries.
    introspect_mock = AsyncMock(return_value={
        "est_0": ([{"c": 5}], ["c"], None),
        "est_1": ([{"c": 10}], ["c"], None),
    })

    with (
        patch("src.api.hierarchies.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.hierarchies._ensure_model_in_project", new=AsyncMock(return_value=None)),
        patch(
            "src.api.hierarchies._load_hierarchy_or_404",
            new=AsyncMock(return_value=types.SimpleNamespace(id=hierarchy_id, name="Geo")),
        ),
        patch("src.api.hierarchies._levels_for_hierarchy", new=AsyncMock(return_value=levels)),
        patch(
            "src.api.hierarchies._resolve_sql_attribute",
            new=AsyncMock(side_effect=[resolved_parent, resolved_child]),
        ),
        patch("src.api.hierarchies._resolve_preview_connection", new=AsyncMock(return_value=(object(), "postgresql", None))),
        patch("src.api.hierarchies._introspect_batch_via_router", introspect_mock),
    ):
        resp = await client.get(
            f"{PREFIX}/hierarchies/{hierarchy_id}/preview?sample_size=10&expand_level=1&parent_key=EMEA",
            headers={"Authorization": "Bearer test-token"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert any(w["type"] == "cross_table_preview_not_supported" for w in body["warnings"])
    assert body["members"] == []
