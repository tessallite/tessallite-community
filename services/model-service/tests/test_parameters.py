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


    # ------------------------------------------------------------------
    # Bug-7445: changing param_type must revalidate stored default/allowed
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_type_change_with_incompatible_stored_default_rejects(self, client):
        """Bug-7445: changing type from string to number while stored default
        is a non-numeric string must return 422, not silently succeed."""
        param = _make_param(param_type="string", default_value="EMEA")
        db = make_mock_db()
        model = make_model()

        db.get = AsyncMock(side_effect=lambda cls, id: model if id == TEST_MODEL_ID else param)
        db.refresh = AsyncMock()

        with patch("src.api.parameters.get_tenant_db", async_gen_from(db)):
            resp = await client.put(
                f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/parameters/{param.id}",
                json={"param_type": "number"},
            )
        assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"

    @pytest.mark.asyncio
    async def test_type_change_with_compatible_stored_default_succeeds(self, client):
        """Bug-7445: changing type from string to number when stored default
        is a numeric string should succeed."""
        param = _make_param(param_type="string", default_value="100")
        db = make_mock_db()
        model = make_model()

        db.get = AsyncMock(side_effect=lambda cls, id: model if id == TEST_MODEL_ID else param)
        db.refresh = AsyncMock()

        with patch("src.api.parameters.get_tenant_db", async_gen_from(db)):
            resp = await client.put(
                f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/parameters/{param.id}",
                json={"param_type": "number"},
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_type_change_with_incompatible_stored_allowed_values_rejects(self, client):
        """Bug-7445: changing type to date_range when stored allowed_values
        is non-empty must return 422 (date_range disallows allowed_values)."""
        param = _make_param(param_type="string", default_value="US")
        param.allowed_values = ["US", "EU"]
        db = make_mock_db()
        model = make_model()

        db.get = AsyncMock(side_effect=lambda cls, id: model if id == TEST_MODEL_ID else param)
        db.refresh = AsyncMock()

        with patch("src.api.parameters.get_tenant_db", async_gen_from(db)):
            resp = await client.put(
                f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/parameters/{param.id}",
                json={"param_type": "date_range"},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_type_change_with_null_default_succeeds(self, client):
        """Bug-7445: changing type when stored default is None should succeed."""
        param = _make_param(param_type="string", default_value=None)
        db = make_mock_db()
        model = make_model()

        db.get = AsyncMock(side_effect=lambda cls, id: model if id == TEST_MODEL_ID else param)
        db.refresh = AsyncMock()

        with patch("src.api.parameters.get_tenant_db", async_gen_from(db)):
            resp = await client.put(
                f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/parameters/{param.id}",
                json={"param_type": "number"},
            )
        assert resp.status_code == 200


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


# ---------------------------------------------------------------------------
# F-029-06 / F-029-02 — date_range default is validated at write time
# ---------------------------------------------------------------------------


class TestDateRangeDefaultValidation:
    """`_coerce_default_value` must reject a date_range default that could never
    succeed at query time: missing bounds, non-ISO dates, an inverted range, or
    extra keys. Mirrors the query-router resolver's query-time backstop."""

    def test_valid_iso_from_to_passes(self):
        from src.api.parameters import _coerce_default_value
        rng = {"from": "2024-01-01", "to": "2024-12-31"}
        assert _coerce_default_value("date_range", rng) == rng

    def test_iso_datetime_bounds_pass(self):
        from src.api.parameters import _coerce_default_value
        rng = {"from": "2024-01-01T00:00:00", "to": "2024-12-31T23:59:59"}
        assert _coerce_default_value("date_range", rng) == rng

    def test_missing_bound_rejected(self):
        from fastapi import HTTPException
        from src.api.parameters import _coerce_default_value
        with pytest.raises(HTTPException) as ei:
            _coerce_default_value("date_range", {"from": "2024-01-01"})
        assert ei.value.status_code == 422

    def test_non_iso_bound_rejected(self):
        from fastapi import HTTPException
        from src.api.parameters import _coerce_default_value
        with pytest.raises(HTTPException) as ei:
            _coerce_default_value("date_range", {"from": "banana", "to": "pear"})
        assert ei.value.status_code == 422

    def test_inverted_range_rejected(self):
        from fastapi import HTTPException
        from src.api.parameters import _coerce_default_value
        with pytest.raises(HTTPException) as ei:
            _coerce_default_value(
                "date_range", {"from": "2024-12-31", "to": "2024-01-01"}
            )
        assert ei.value.status_code == 422

    def test_extra_keys_rejected(self):
        from fastapi import HTTPException
        from src.api.parameters import _coerce_default_value
        with pytest.raises(HTTPException) as ei:
            _coerce_default_value(
                "date_range",
                {"from": "2024-01-01", "to": "2024-12-31", "tz": "UTC"},
            )
        assert ei.value.status_code == 422
