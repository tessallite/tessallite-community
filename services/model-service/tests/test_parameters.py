"""Tests for model parameter CRUD API (Phase 3, Block D)."""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from src.main import app
from src.auth.middleware import CurrentUser, get_current_user
from .conftest import (
    TEST_TENANT,
    TEST_USER_ID,
    TEST_PROJECT_ID,
    TEST_MODEL_ID,
    NOW,
    async_gen_from,
    make_mock_db,
    make_model,
)


def _make_param(
    param_id: uuid.UUID | None = None,
    model_id: uuid.UUID = TEST_MODEL_ID,
    name: str = "@region",
    param_type: str = "string",
    default_value: str | None = "US",
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=param_id or uuid.uuid4(),
        model_id=model_id,
        name=name,
        display_name="Region Filter",
        param_type=param_type,
        default_value=default_value,
        allowed_values=None,
        description="Filter by region",
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
def auth():
    user = CurrentUser(user_id=TEST_USER_ID, tenant_id=TEST_TENANT, email=TEST_USER_ID)
    app.dependency_overrides[get_current_user] = lambda: user
    yield user
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
async def client(auth):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac


class TestParameterList:

    @pytest.mark.asyncio
    async def test_list_empty(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        with patch("src.api.parameters.get_tenant_db", async_gen_from(db)):
            resp = await client.get(
                f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/parameters"
            )
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_returns_params(self, client):
        db = make_mock_db()
        model = make_model()
        p1 = _make_param(name="@region")
        p2 = _make_param(name="@year", param_type="number", default_value=2026)

        get_results = {TEST_MODEL_ID: model}
        db.get = AsyncMock(side_effect=lambda cls, id: get_results.get(id))

        result = MagicMock()
        result.scalars.return_value.all.return_value = [p1, p2]
        db.execute = AsyncMock(return_value=result)

        with patch("src.api.parameters.get_tenant_db", async_gen_from(db)):
            resp = await client.get(
                f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/parameters"
            )
        assert resp.status_code == 200
        assert len(resp.json()) == 2


class TestParameterCreate:

    @pytest.mark.asyncio
    async def test_create_parameter(self, client):
        db = make_mock_db()
        model = make_model()
        param_id = uuid.uuid4()

        db.get = AsyncMock(return_value=model)

        check_result = MagicMock()
        check_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=check_result)

        async def mock_refresh(obj):
            obj.id = param_id
            obj.model_id = TEST_MODEL_ID
            obj.created_at = NOW
            obj.updated_at = NOW

        db.refresh = AsyncMock(side_effect=mock_refresh)

        with patch("src.api.parameters.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/parameters",
                json={
                    "name": "@region",
                    "param_type": "string",
                    "default_value": "US",
                    "description": "Filter by region",
                },
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "@region"
        assert data["param_type"] == "string"

    @pytest.mark.asyncio
    async def test_create_duplicate_rejected(self, client):
        db = make_mock_db()
        model = make_model()
        existing = _make_param()

        db.get = AsyncMock(return_value=model)

        check_result = MagicMock()
        check_result.scalar_one_or_none.return_value = existing
        db.execute = AsyncMock(return_value=check_result)

        with patch("src.api.parameters.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/parameters",
                json={"name": "@region", "param_type": "string"},
            )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_create_invalid_type_rejected(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        with patch("src.api.parameters.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/parameters",
                json={"name": "@region", "param_type": "invalid_type"},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_create_missing_at_prefix_rejected(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        with patch("src.api.parameters.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/parameters",
                json={"name": "region", "param_type": "string"},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_name", ["@1region", "@a-b", "@a b", "@", "@a.b"])
    async def test_create_unsafe_name_shape_rejected(self, client, bad_name):
        # Bug-1105 / F-029-05 name guard: a ``@``-prefixed name that the
        # query-router lexer cannot recognise as a single ``@name`` placeholder
        # span is rejected at create time, so binding is always unambiguous.
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        with patch("src.api.parameters.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/parameters",
                json={"name": bad_name, "param_type": "string"},
            )
        assert resp.status_code == 422


    @pytest.mark.asyncio
    async def test_create_allowed_values_accepted(self, client):
        # A valid string whitelist passes validation and is stored (F-029-02).
        db = make_mock_db()
        model = make_model()
        param_id = uuid.uuid4()
        db.get = AsyncMock(return_value=model)
        check_result = MagicMock()
        check_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=check_result)

        async def mock_refresh(obj):
            obj.id = param_id
            obj.model_id = TEST_MODEL_ID
            obj.created_at = NOW
            obj.updated_at = NOW

        db.refresh = AsyncMock(side_effect=mock_refresh)
        with patch("src.api.parameters.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/parameters",
                json={
                    "name": "@region",
                    "param_type": "string",
                    "allowed_values": ["EMEA", "APAC"],
                },
            )
        assert resp.status_code == 201
        assert resp.json()["allowed_values"] == ["EMEA", "APAC"]

    @pytest.mark.asyncio
    async def test_create_allowed_values_rejected_for_date_range(self, client):
        # A date_range resolves to a {from,to} object; a discrete whitelist is
        # meaningless and rejected at definition time (F-029-02).
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)
        with patch("src.api.parameters.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/parameters",
                json={
                    "name": "@window",
                    "param_type": "date_range",
                    "allowed_values": ["2026"],
                },
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_create_allowed_values_non_numeric_for_number_rejected(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)
        with patch("src.api.parameters.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/parameters",
                json={
                    "name": "@threshold",
                    "param_type": "number",
                    "allowed_values": [10, "not-a-number"],
                },
            )
        assert resp.status_code == 422


class TestParameterUpdate:

    @pytest.mark.asyncio
    async def test_update_parameter(self, client):
        param = _make_param()
        db = make_mock_db()
        model = make_model()

        db.get = AsyncMock(side_effect=lambda cls, id: model if id == TEST_MODEL_ID else param)

        async def mock_refresh(obj):
            pass

        db.refresh = AsyncMock(side_effect=mock_refresh)

        with patch("src.api.parameters.get_tenant_db", async_gen_from(db)):
            resp = await client.put(
                f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/parameters/{param.id}",
                json={"display_name": "Updated Name", "default_value": "EU"},
            )
        assert resp.status_code == 200
        assert param.display_name == "Updated Name"
        assert param.default_value == "EU"

    @pytest.mark.asyncio
    async def test_update_not_found(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(side_effect=lambda cls, id: model if id == TEST_MODEL_ID else None)

        with patch("src.api.parameters.get_tenant_db", async_gen_from(db)):
            resp = await client.put(
                f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/parameters/{uuid.uuid4()}",
                json={"display_name": "Nope"},
            )
        assert resp.status_code == 404


class TestParameterDelete:

    @pytest.mark.asyncio
    async def test_delete_parameter(self, client):
        param = _make_param()
        db = make_mock_db()
        model = make_model()

        db.get = AsyncMock(side_effect=lambda cls, id: model if id == TEST_MODEL_ID else param)

        with patch("src.api.parameters.get_tenant_db", async_gen_from(db)):
            resp = await client.delete(
                f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/parameters/{param.id}"
            )
        assert resp.status_code == 204
        db.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_not_found(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(side_effect=lambda cls, id: model if id == TEST_MODEL_ID else None)

        with patch("src.api.parameters.get_tenant_db", async_gen_from(db)):
            resp = await client.delete(
                f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/parameters/{uuid.uuid4()}"
            )
        assert resp.status_code == 404
