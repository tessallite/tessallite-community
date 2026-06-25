"""Unit tests for Collibra config API (Phase 1).

Tests config CRUD + validate endpoints using a fake client.
No live Collibra instance required. Follows the same mock-DB pattern
as test_downstream_assets.py.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .conftest import (
    TEST_PROJECT_ID,
    TEST_MODEL_ID,
    NOW,
    async_gen_from,
    make_mock_db,
    make_model,
)

COLLIBRA_PREFIX = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/collibra"
)


def _make_collibra_connection(
    conn_id: uuid.UUID | None = None,
    model_id: uuid.UUID = TEST_MODEL_ID,
    display_name: str = "Collibra Production",
    base_url: str = "https://company.collibra.com",
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=conn_id or uuid.uuid4(),
        project_id=TEST_PROJECT_ID,
        model_id=model_id,
        display_name=display_name,
        base_url=base_url,
        auth_type="bearer_token",
        encrypted_credentials=b"fake-encrypted",
        community_id="community-123",
        domain_id="domain-456",
        asset_type_mapping={},
        relation_type_mapping={},
        responsibility_mapping={},
        sync_scope="model",
        sync_mode="rest_api",
        is_active=True,
        created_at=NOW,
        updated_at=NOW,
    )


async def _fake_refresh(obj):
    """Populate ORM defaults that would normally come from the DB."""
    if obj.id is None:
        object.__setattr__(obj, "id", uuid.uuid4())
    if obj.created_at is None:
        object.__setattr__(obj, "created_at", NOW)
    if obj.updated_at is None:
        object.__setattr__(obj, "updated_at", NOW)
    if obj.is_active is None:
        object.__setattr__(obj, "is_active", True)


def _setup_db_for_cud():
    """Return a mock DB suitable for create/update/delete operations."""
    db = make_mock_db()
    db.add = MagicMock()
    db.delete = AsyncMock()
    db.refresh = AsyncMock(side_effect=_fake_refresh)
    return db


# ------------------------------------------------------------------ #
# GET /config
# ------------------------------------------------------------------ #


class TestListCollibraConfigs:
    @pytest.mark.anyio
    async def test_list_empty(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        with patch("src.api.collibra.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{COLLIBRA_PREFIX}/config")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.anyio
    async def test_list_with_configs(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        configs = [
            _make_collibra_connection(display_name="Collibra 1"),
            _make_collibra_connection(display_name="Collibra 2"),
        ]
        result = MagicMock()
        result.scalars.return_value.all.return_value = configs
        db.execute = AsyncMock(return_value=result)

        with patch("src.api.collibra.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{COLLIBRA_PREFIX}/config")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        for item in data:
            assert "token" not in item
            assert "encrypted_credentials" not in item


# ------------------------------------------------------------------ #
# POST /config
# ------------------------------------------------------------------ #


class TestCreateCollibraConfig:
    @pytest.mark.anyio
    async def test_create_and_token_not_returned(self, client):
        db = _setup_db_for_cud()
        model = make_model()
        db.get = AsyncMock(return_value=model)
        db.commit = AsyncMock()

        with patch("src.api.collibra.get_tenant_db", async_gen_from(db)), \
             patch("src.api.collibra.encrypt_credentials", return_value=b"encrypted"), \
             patch("src.api.collibra.audit", new_callable=AsyncMock) as audit_mock:
            resp = await client.post(
                f"{COLLIBRA_PREFIX}/config",
                json={
                    "display_name": "Collibra Production",
                    "base_url": "https://company.collibra.com",
                    "auth_type": "bearer_token",
                    "token": "test-token-secret",
                    "community_id": "community-123",
                    "domain_id": "domain-456",
                    "asset_type_mapping": {"semantic_model": "Semantic Model"},
                    "relation_type_mapping": {"contains": "contains"},
                    "responsibility_mapping": {"owner": "Business Owner"},
                },
            )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["display_name"] == "Collibra Production"
        assert data["base_url"] == "https://company.collibra.com"
        assert data["community_id"] == "community-123"
        assert data["domain_id"] == "domain-456"
        assert data["sync_scope"] == "model"
        assert data["sync_mode"] == "rest_api"
        assert "token" not in data
        assert "encrypted_credentials" not in data
        audit_mock.assert_awaited_once()
        assert audit_mock.await_args.kwargs["action"] == "collibra.config.create"

    @pytest.mark.anyio
    async def test_model_not_found(self, client):
        db = make_mock_db()
        db.get = AsyncMock(return_value=None)

        with patch("src.api.collibra.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                f"{COLLIBRA_PREFIX}/config",
                json={
                    "display_name": "Test",
                    "base_url": "https://test.example.com",
                    "token": "test-token",
                },
            )
        assert resp.status_code == 404


# ------------------------------------------------------------------ #
# PUT /config/{id}
# ------------------------------------------------------------------ #


class TestUpdateCollibraConfig:
    @pytest.mark.anyio
    async def test_update_display_name_and_deactivate(self, client):
        conn_id = uuid.uuid4()
        conn = _make_collibra_connection(conn_id=conn_id)

        db = _setup_db_for_cud()
        model = make_model()
        db.get = AsyncMock(side_effect=lambda model_cls, pk: (
            model if pk == TEST_MODEL_ID else conn
        ))
        db.commit = AsyncMock()

        with patch("src.api.collibra.get_tenant_db", async_gen_from(db)), \
             patch("src.api.collibra.audit", new_callable=AsyncMock) as audit_mock:
            resp = await client.put(
                f"{COLLIBRA_PREFIX}/config/{conn_id}",
                json={"display_name": "Collibra Staging", "is_active": False, "domain_id": "domain-789"},
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["display_name"] == "Collibra Staging"
        assert data["domain_id"] == "domain-789"
        assert data["is_active"] is False
        audit_mock.assert_awaited_once()
        assert audit_mock.await_args.kwargs["action"] == "collibra.config.update"

    @pytest.mark.anyio
    async def test_connection_not_found(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(side_effect=lambda model_cls, pk: (
            model if pk == TEST_MODEL_ID else None
        ))

        with patch("src.api.collibra.get_tenant_db", async_gen_from(db)):
            resp = await client.put(
                f"{COLLIBRA_PREFIX}/config/{uuid.uuid4()}",
                json={"display_name": "X"},
            )
        assert resp.status_code == 404


# ------------------------------------------------------------------ #
# DELETE /config/{id}
# ------------------------------------------------------------------ #


class TestDeleteCollibraConfig:
    @pytest.mark.anyio
    async def test_delete_returns_204(self, client):
        conn_id = uuid.uuid4()
        conn = _make_collibra_connection(conn_id=conn_id)

        db = _setup_db_for_cud()
        model = make_model()
        db.get = AsyncMock(side_effect=lambda model_cls, pk: (
            model if pk == TEST_MODEL_ID else conn
        ))
        db.commit = AsyncMock()

        with patch("src.api.collibra.get_tenant_db", async_gen_from(db)), \
             patch("src.api.collibra.audit", new_callable=AsyncMock) as audit_mock:
            resp = await client.delete(f"{COLLIBRA_PREFIX}/config/{conn_id}")
        assert resp.status_code == 204
        audit_mock.assert_awaited_once()
        assert audit_mock.await_args.kwargs["action"] == "collibra.config.delete"


# ------------------------------------------------------------------ #
# POST /validate
# ------------------------------------------------------------------ #


class TestValidateCollibra:
    @pytest.mark.anyio
    async def test_validate_returns_ok(self, client):
        conn_id = uuid.uuid4()
        conn = _make_collibra_connection(conn_id=conn_id)

        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(side_effect=lambda model_cls, pk: (
            model if pk == TEST_MODEL_ID else conn
        ))

        with patch("src.api.collibra.get_tenant_db", async_gen_from(db)), \
             patch("src.api.collibra.decrypt_credentials", return_value={"token": "test-token"}):
            resp = await client.post(
                f"{COLLIBRA_PREFIX}/validate",
                json={"connection_id": str(conn_id)},
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # F-CSI-01: the placeholder client never contacts Collibra, so validate
        # must be honest — ok is null (tri-state "unknown"), simulated is true,
        # and a warning explains the connection was not actually verified.
        assert data["ok"] is None
        assert data["simulated"] is True
        assert data["community_found"] is None
        assert data["domain_found"] is None
        assert data["missing_asset_types"] == []
        assert data["missing_relation_types"] == []
        assert len(data["warnings"]) == 1
        assert "simulated" in data["warnings"][0].lower()


# ------------------------------------------------------------------ #
# GET /runs
# ------------------------------------------------------------------ #


class TestListCollibraRuns:
    @pytest.mark.anyio
    async def test_list_empty(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        with patch("src.api.collibra.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{COLLIBRA_PREFIX}/runs")
        assert resp.status_code == 200
        assert resp.json() == []


# ------------------------------------------------------------------ #
# GET /mappings
# ------------------------------------------------------------------ #


class TestListCollibraMappings:
    @pytest.mark.anyio
    async def test_list_empty(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        with patch("src.api.collibra.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{COLLIBRA_PREFIX}/mappings")
        assert resp.status_code == 200
        assert resp.json() == []
