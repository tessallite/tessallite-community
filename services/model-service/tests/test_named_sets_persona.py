"""Bug-5963: named-set listing and preview must honour the active persona.

Tests that:
1. Listing filters out a named set built on a dimension outside the
   persona's ``included_dimension_ids`` allow-list.
2. Listing keeps a named set whose referenced dimension is in scope.
3. Preview 404s for an out-of-scope named set (same gate KPI evaluation
   uses for out-of-scope KPIs).
4. A dynamic (topN/filter) preview forwards persona_id to the router so
   the query executes under the same row-level security as KPI/plugin
   execution instead of running persona-blind.
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
    routed_execute,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/named-sets"


def _make_persona(*, included_dimension_ids: list | None = None):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name="restricted",
        slug="restricted",
        included_dimension_ids=included_dimension_ids or [],
        includes_hidden_columns=False,
        audience_roles=[],
        default_filters={},
    )


def _named_set(*, expression: str = "{ [Customer].[Customer].Members }"):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name="Customer Set",
        display_name="Customer Set",
        description=None,
        display_folder=None,
        scope=1,
        expression=expression,
        dimensions=None,
        builder_definition=None,
        list_type="advanced_mdx",
        certification_status="draft",
        owner_user_id=None,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.asyncio
async def test_list_named_sets_filters_out_of_persona_scope(client):
    ns = _named_set(expression="{ [Customer].[Customer].Members }")
    excluded_dim_id = uuid.uuid4()
    customer_dim = types.SimpleNamespace(id=excluded_dim_id, name="Customer")
    persona = _make_persona(included_dimension_ids=[str(uuid.uuid4())])  # excludes Customer

    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())
    db.execute = AsyncMock(
        side_effect=routed_execute(
            named_sets=[ns],
            dimensions=[(customer_dim.id, customer_dim.name)],
        )
    )

    with (
        patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
        patch("src.api.named_sets.resolve_effective_persona", new=AsyncMock(return_value=persona)),
    ):
        resp = await client.get(f"{PREFIX}?persona_id={persona.id}")

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_named_sets_keeps_in_scope_set(client):
    customer_dim_id = uuid.uuid4()
    ns = _named_set(expression="{ [Customer].[Customer].Members }")
    customer_dim = types.SimpleNamespace(id=customer_dim_id, name="Customer")
    persona = _make_persona(included_dimension_ids=[str(customer_dim_id)])

    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())
    db.execute = AsyncMock(
        side_effect=routed_execute(
            named_sets=[ns],
            dimensions=[(customer_dim.id, customer_dim.name)],
        )
    )

    with (
        patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
        patch("src.api.named_sets.resolve_effective_persona", new=AsyncMock(return_value=persona)),
    ):
        resp = await client.get(f"{PREFIX}?persona_id={persona.id}")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "Customer Set"


@pytest.mark.asyncio
async def test_preview_named_set_404_when_persona_excludes_dimension(client):
    ns = _named_set(expression="{ [Customer].[Customer].Members }")
    excluded_dim_id = uuid.uuid4()
    customer_dim = types.SimpleNamespace(id=excluded_dim_id, name="Customer")
    persona = _make_persona(included_dimension_ids=[str(uuid.uuid4())])  # excludes Customer

    db = make_mock_db()
    db.get = AsyncMock(side_effect=[make_model(), ns])
    db.execute = AsyncMock(
        side_effect=routed_execute(dimensions=[(customer_dim.id, customer_dim.name)])
    )

    with (
        patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
        patch("src.api.named_sets.resolve_effective_persona", new=AsyncMock(return_value=persona)),
    ):
        resp = await client.post(f"{PREFIX}/{ns.id}/preview?persona_id={persona.id}")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_preview_named_set_forwards_persona_id_to_router(client):
    """A dynamic (topN) preview must execute under the caller's persona so
    row-level security applies to the member universe, not the
    unrestricted default context (F-025-03)."""
    ns = _named_set()
    ns.builder_definition = {
        "type": "topN", "entity": "Customer", "count": 5,
        "measure": "Revenue", "direction": "top",
    }
    persona = _make_persona(included_dimension_ids=[])  # unrestricted

    db = make_mock_db()
    db.get = AsyncMock(side_effect=[make_model(), ns])
    dim_result = types.SimpleNamespace(scalar=lambda: "Customer")
    dim_result.scalars = lambda: types.SimpleNamespace(all=lambda: [])
    meas_result = types.SimpleNamespace()
    meas_result.all = lambda: [("Revenue", "sum")]

    async def db_execute_side_effect(*_args, **_kwargs):
        return dim_result

    captured: dict = {}

    async def fake_router(model_id, sql, bearer, timeout_s=30.0, persona_id=None):
        captured["persona_id"] = persona_id
        return {"rows": [{"customer": "Acme"}]}

    with (
        patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
        patch("src.api.named_sets.resolve_effective_persona", new=AsyncMock(return_value=persona)),
        patch("src.api.named_sets._resolve_dim_name", new=AsyncMock(return_value="Customer")),
        patch("src.api.named_sets._resolve_measure", new=AsyncMock(return_value=("Revenue", "sum"))),
        patch("src.api.named_sets._execute_via_router", side_effect=fake_router),
    ):
        resp = await client.post(f"{PREFIX}/{ns.id}/preview?persona_id={persona.id}")

    assert resp.status_code == 200
    assert captured["persona_id"] == str(persona.id)
