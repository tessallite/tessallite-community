"""Endpoint guards for cycles spanning KPI expressions and composite ownership."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from .conftest import async_gen_from, make_mock_db
from .test_kpi_composite_indicators import PREFIX, _kpi


class _Rows:
    def __init__(self, *kpis):
        self._rows = [
            (
                kpi.id,
                kpi.name,
                kpi.expression,
                kpi.target_expression,
                kpi.parent_kpi_id,
            )
            for kpi in kpis
        ]

    def __iter__(self):
        return iter(self._rows)


@pytest.mark.asyncio
async def test_create_rejects_child_target_reference_to_composite_parent(client):
    """A target edge back to the owner closes parent -> child -> parent."""
    parent = _kpi(
        name="Create Cycle Parent",
        kpi_type="composite",
        expression="literal(0)",
    )
    db = make_mock_db()
    db.get = AsyncMock(return_value=parent)
    db.execute = AsyncMock(return_value=_Rows(parent))

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.acquire_model_definition_lock", new_callable=AsyncMock),
        patch("src.api.kpis._validate_kpi_expression", new_callable=AsyncMock, return_value=None),
    ):
        response = await client.post(
            PREFIX,
            json={
                "name": "Create Cycle Child",
                "expression": "literal(50)",
                "target_type": "expression",
                "target_expression": 'kpi("Create Cycle Parent")',
                "parent_kpi_id": str(parent.id),
            },
        )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["message"] == "KPI definition would create a dependency cycle"
    assert any(
        "Create Cycle Parent" in cycle and "Create Cycle Child" in cycle
        for cycle in detail["cycles"]
    )


@pytest.mark.asyncio
async def test_create_rejects_target_only_reference_to_composite_parent(client):
    """A missing value expression must not bypass the combined graph check."""
    parent = _kpi(
        name="Target Only Parent",
        kpi_type="composite",
        expression="literal(0)",
    )
    db = make_mock_db()
    db.get = AsyncMock(return_value=parent)
    db.execute = AsyncMock(return_value=_Rows(parent))

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.acquire_model_definition_lock", new_callable=AsyncMock),
        patch("src.api.kpis._validate_kpi_expression", new_callable=AsyncMock, return_value=None),
    ):
        response = await client.post(
            PREFIX,
            json={
                "name": "Target Only Child",
                "target_type": "expression",
                "target_expression": 'kpi("Target Only Parent")',
                "parent_kpi_id": str(parent.id),
            },
        )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["message"] == "KPI definition would create a dependency cycle"
    assert any(
        "Target Only Parent" in cycle and "Target Only Child" in cycle
        for cycle in detail["cycles"]
    )
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_rejects_child_value_reference_to_composite_parent(client):
    """An existing ownership edge is considered with the prospective value."""
    parent = _kpi(
        name="Update Cycle Parent",
        kpi_type="composite",
        expression="literal(0)",
    )
    child = _kpi(
        name="Update Cycle Child",
        expression="literal(50)",
        parent_kpi_id=parent.id,
    )
    db = make_mock_db()
    db.get = AsyncMock(return_value=child)
    db.execute = AsyncMock(return_value=_Rows(parent, child))

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.acquire_model_definition_lock", new_callable=AsyncMock),
        patch("src.api.kpis._validate_kpi_expression", new_callable=AsyncMock, return_value=None),
    ):
        response = await client.patch(
            f"{PREFIX}/{child.id}",
            json={"expression": 'kpi("Update Cycle Parent")'},
        )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["message"] == "KPI definition would create a dependency cycle"
    assert any(
        "Update Cycle Parent" in cycle and "Update Cycle Child" in cycle
        for cycle in detail["cycles"]
    )


@pytest.mark.asyncio
async def test_update_rejects_parent_only_combined_cycle(client):
    """A parent-only PATCH must include the child's stored expression edge."""
    parent = _kpi(
        name="Parent Only Owner",
        kpi_type="composite",
        expression="literal(0)",
    )
    child = _kpi(
        name="Parent Only Child",
        expression='kpi("Parent Only Owner")',
        parent_kpi_id=None,
    )
    db = make_mock_db()
    db.get = AsyncMock(side_effect=[child, parent, parent])
    db.execute = AsyncMock(return_value=_Rows(parent, child))

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.acquire_model_definition_lock", new_callable=AsyncMock),
    ):
        response = await client.patch(
            f"{PREFIX}/{child.id}",
            json={"parent_kpi_id": str(parent.id)},
        )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["message"] == "KPI definition would create a dependency cycle"
    assert any(
        "Parent Only Owner" in cycle and "Parent Only Child" in cycle
        for cycle in detail["cycles"]
    )
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_allows_reference_when_parent_is_explicitly_removed(client):
    """A null parent update must remove the prospective ownership edge."""
    parent = _kpi(
        name="Former Owner",
        kpi_type="composite",
        expression="literal(0)",
    )
    child = _kpi(
        name="Former Child",
        expression="literal(50)",
        parent_kpi_id=parent.id,
    )
    db = make_mock_db()
    db.get = AsyncMock(return_value=child)
    db.execute = AsyncMock(return_value=_Rows(parent, child))

    with (
        patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
        patch("src.api.kpis.acquire_model_definition_lock", new_callable=AsyncMock),
        patch("src.api.kpis._validate_kpi_expression", new_callable=AsyncMock, return_value=None),
        patch("src.api.kpis._create_kpi_version", new_callable=AsyncMock),
        patch("src.api.kpis.audit", new_callable=AsyncMock),
        patch("src.api.kpis._invalidate_kpi_and_dependents", new_callable=AsyncMock),
    ):
        response = await client.patch(
            f"{PREFIX}/{child.id}",
            json={
                "parent_kpi_id": None,
                "expression": 'kpi("Former Owner")',
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["parent_kpi_id"] is None
    assert child.parent_kpi_id is None
    db.commit.assert_awaited_once()
