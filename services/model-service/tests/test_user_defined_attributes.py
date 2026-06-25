"""
Unit tests for user-defined attribute routes.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from .conftest import TEST_MODEL_ID, TEST_PROJECT_ID, NOW, async_gen_from, client, make_mock_db

pytestmark = pytest.mark.unit

TABLE_ID = uuid.uuid4()
PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/tables/{TABLE_ID}/user-defined-attributes"


class _ScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items

    def fetchall(self):
        return self._items


def _table() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=TABLE_ID,
        model_id=TEST_MODEL_ID,
        display_name="Payments",
        alias="payments",
        physical_name="public.payments",
    )


def _col(col_id: uuid.UUID, name: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=col_id,
        model_table_id=TABLE_ID,
        column_name=name,
        data_type="text",
        is_nullable=True,
    )


def _scope_aware_get(table: types.SimpleNamespace):
    model = types.SimpleNamespace(id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID)

    async def _get(entity, entity_id):
        if entity_id == TEST_MODEL_ID:
            return model
        if entity_id == TABLE_ID:
            return table
        return None

    return _get


@pytest.mark.asyncio
async def test_create_user_defined_attribute_success(client):
    mock_db = make_mock_db()
    table = _table()
    c1 = _col(uuid.uuid4(), "region_code")
    c2 = _col(uuid.uuid4(), "branch_code")

    mock_db.get = AsyncMock(side_effect=_scope_aware_get(table))
    mock_db.execute = AsyncMock(side_effect=[
        _ScalarResult([c1, c2]),  # load table columns
        _ScalarResult([]),        # delete existing refs
        _ScalarResult([("region_code",), ("branch_code",)]),  # response referenced columns
    ])

    async def _refresh(obj):
        obj.id = uuid.uuid4()
        obj.created_at = NOW
        obj.updated_at = NOW
        obj.validated = True
        obj.validation_error = None

    mock_db.refresh = _refresh

    body = {
        "name": "full_code",
        "expression": "CONCAT(region_code, '-', branch_code)",
        "output_data_type": "varchar",
        "description": "Composite code",
    }

    with patch("src.api.user_defined_attributes.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.post(PREFIX, json=body)

    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "full_code"
    assert data["validated"] is True
    assert data["referenced_columns"] == ["region_code", "branch_code"]


@pytest.mark.asyncio
async def test_validate_user_defined_attribute_rejects_unsupported_function(client):
    mock_db = make_mock_db()
    table = _table()
    c1 = _col(uuid.uuid4(), "account_code")

    mock_db.get = AsyncMock(side_effect=_scope_aware_get(table))
    mock_db.execute = AsyncMock(return_value=_ScalarResult([c1]))

    body = {
        "expression": "REGEXP_EXTRACT(account_code, '([0-9]+)')",
        "output_data_type": "varchar",
    }

    with patch("src.api.user_defined_attributes.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.post(f"{PREFIX}/validate", json=body)

    assert resp.status_code == 200
    data = resp.json()
    assert data["parse_valid"] is False
    assert data["columns_resolved"] is False
    assert data["live_validation"]["success"] is False
    assert "Unsupported function" in (data["live_validation"]["error"] or "")


@pytest.mark.asyncio
async def test_validate_user_defined_attribute_allows_same_table_qualified_reference(client):
    mock_db = make_mock_db()
    table = _table()
    c1 = _col(uuid.uuid4(), "account_code")

    mock_db.get = AsyncMock(side_effect=_scope_aware_get(table))
    mock_db.execute = AsyncMock(return_value=_ScalarResult([c1]))

    body = {
        "expression": "LEFT(payments.account_code, 5)",
        "output_data_type": "varchar",
    }

    with patch("src.api.user_defined_attributes.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.post(f"{PREFIX}/validate", json=body)

    assert resp.status_code == 200
    data = resp.json()
    assert data["parse_valid"] is True
    assert data["columns_resolved"] is True
    assert data["live_validation"]["success"] is True


def _scope_aware_get_with_attr(table, attr):
    model = types.SimpleNamespace(id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID)

    async def _get(entity, entity_id):
        if entity_id == TEST_MODEL_ID:
            return model
        if entity_id == TABLE_ID:
            return table
        if entity_id == attr.id:
            return attr
        return None

    return _get


def _uda_obj(*, name, expression, is_generated):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        table_id=TABLE_ID,
        name=name,
        expression=expression,
        output_data_type="integer",
        description=None,
        validated=True,
        validation_error=None,
        is_generated=is_generated,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.asyncio
async def test_rename_generated_uda_with_extract_expression_succeeds(client):
    """F-016-06: renaming a generated UDA (EXTRACT expression) must not be
    rejected by the user-editor function allow-list."""
    mock_db = make_mock_db()
    table = _table()
    c1 = _col(uuid.uuid4(), "payment_date")
    attr = _uda_obj(
        name="payment_date_year",
        expression="EXTRACT(YEAR FROM (payment_date))",
        is_generated=True,
    )

    mock_db.get = AsyncMock(side_effect=_scope_aware_get_with_attr(table, attr))
    mock_db.execute = AsyncMock(side_effect=[
        _ScalarResult([c1]),               # load table columns (validation)
        _ScalarResult([]),                 # delete existing refs
        _ScalarResult([("payment_date",)]),  # response referenced columns
    ])

    async def _refresh(obj):
        return None

    mock_db.refresh = _refresh

    body = {"name": "fiscal_year"}  # rename only; expression untouched
    with patch("src.api.user_defined_attributes.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.put(f"{PREFIX}/{attr.id}", json=body)

    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "fiscal_year"


@pytest.mark.asyncio
async def test_user_uda_replacing_expression_with_extract_still_rejected(client):
    """A non-generated UDA cannot smuggle EXTRACT in via the editor — the
    allow-list still applies when the expression is genuinely changed."""
    mock_db = make_mock_db()
    table = _table()
    c1 = _col(uuid.uuid4(), "payment_date")
    attr = _uda_obj(
        name="my_attr",
        expression="LEFT(payment_date, 4)",
        is_generated=False,
    )

    mock_db.get = AsyncMock(side_effect=_scope_aware_get_with_attr(table, attr))
    mock_db.execute = AsyncMock(side_effect=[_ScalarResult([c1])])

    body = {"expression": "EXTRACT(YEAR FROM (payment_date))"}
    with patch("src.api.user_defined_attributes.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.put(f"{PREFIX}/{attr.id}", json=body)

    assert resp.status_code == 422
    assert "Unsupported function" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_generated_uda_replacing_expression_with_extract_still_rejected(client):
    """Even a generated UDA, once its expression is genuinely replaced by the
    user, is re-validated against the editor allow-list (skip applies only when
    the expression is unchanged)."""
    mock_db = make_mock_db()
    table = _table()
    c1 = _col(uuid.uuid4(), "payment_date")
    attr = _uda_obj(
        name="payment_date_year",
        expression="EXTRACT(YEAR FROM (payment_date))",
        is_generated=True,
    )

    mock_db.get = AsyncMock(side_effect=_scope_aware_get_with_attr(table, attr))
    mock_db.execute = AsyncMock(side_effect=[_ScalarResult([c1])])

    # User types a different EXTRACT expression — this is a real change, so the
    # allow-list applies and rejects it.
    body = {"expression": "EXTRACT(MONTH FROM (payment_date))"}
    with patch("src.api.user_defined_attributes.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.put(f"{PREFIX}/{attr.id}", json=body)

    assert resp.status_code == 422
    assert "Unsupported function" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_user_defined_attribute_function_catalog_returns_allowlist(client):
    mock_db = make_mock_db()
    table = _table()
    mock_db.get = AsyncMock(side_effect=_scope_aware_get(table))

    with patch("src.api.user_defined_attributes.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.get(f"{PREFIX}/function-catalog")

    assert resp.status_code == 200
    data = resp.json()
    names = {item["name"] for item in data}
    assert "CONCAT" in names
    assert "SPLIT_PART" in names
    assert "NULLIF" in names
