"""Unit tests for Solidatus config API (Phase 1).

Tests config CRUD + validate endpoints using a fake client.
No live Solidatus instance required. Follows the same mock-DB pattern
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

SOLIDATUS_PREFIX = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/solidatus"
)


def _make_solidatus_connection(
    conn_id: uuid.UUID | None = None,
    model_id: uuid.UUID = TEST_MODEL_ID,
    display_name: str = "Solidatus Dev",
    base_url: str = "https://solidatus.example.com",
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=conn_id or uuid.uuid4(),
        project_id=TEST_PROJECT_ID,
        model_id=model_id,
        display_name=display_name,
        base_url=base_url,
        auth_type="bearer_token",
        encrypted_credentials=b"fake-encrypted",
        workspace_id="ws-123",
        model_ref="tessallite-sales",
        sync_scope="model",
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


class TestListSolidatusConfigs:
    @pytest.mark.anyio
    async def test_list_empty(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        with patch("src.api.solidatus.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{SOLIDATUS_PREFIX}/config")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.anyio
    async def test_list_with_configs(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        configs = [
            _make_solidatus_connection(display_name="Solidatus 1"),
            _make_solidatus_connection(display_name="Solidatus 2"),
        ]
        result = MagicMock()
        result.scalars.return_value.all.return_value = configs
        db.execute = AsyncMock(return_value=result)

        with patch("src.api.solidatus.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{SOLIDATUS_PREFIX}/config")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        for item in data:
            assert "token" not in item
            assert "encrypted_credentials" not in item


# ------------------------------------------------------------------ #
# POST /config
# ------------------------------------------------------------------ #


class TestCreateSolidatusConfig:
    @pytest.mark.anyio
    async def test_create_and_token_not_returned(self, client):
        db = _setup_db_for_cud()
        model = make_model()
        db.get = AsyncMock(return_value=model)
        db.commit = AsyncMock()

        with patch("src.api.solidatus.get_tenant_db", async_gen_from(db)), \
             patch("src.api.solidatus.encrypt_credentials", return_value=b"encrypted"), \
             patch("src.api.solidatus.audit", new_callable=AsyncMock) as audit_mock:
            resp = await client.post(
                f"{SOLIDATUS_PREFIX}/config",
                json={
                    "display_name": "Solidatus Dev",
                    "base_url": "https://solidatus.example.com",
                    "auth_type": "bearer_token",
                    "token": "test-token-secret",
                    "workspace_id": "ws-123",
                    "model_ref": "tessallite-sales",
                },
            )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["display_name"] == "Solidatus Dev"
        assert data["base_url"] == "https://solidatus.example.com"
        assert "token" not in data
        assert "encrypted_credentials" not in data
        audit_mock.assert_awaited_once()
        assert audit_mock.await_args.kwargs["action"] == "solidatus.config.create"

    @pytest.mark.anyio
    async def test_model_not_found(self, client):
        db = make_mock_db()
        db.get = AsyncMock(return_value=None)

        with patch("src.api.solidatus.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                f"{SOLIDATUS_PREFIX}/config",
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


class TestUpdateSolidatusConfig:
    @pytest.mark.anyio
    async def test_update_display_name_and_deactivate(self, client):
        conn_id = uuid.uuid4()
        conn = _make_solidatus_connection(conn_id=conn_id)

        db = _setup_db_for_cud()
        model = make_model()
        db.get = AsyncMock(side_effect=lambda model_cls, pk: (
            model if pk == TEST_MODEL_ID else conn
        ))
        db.commit = AsyncMock()

        with patch("src.api.solidatus.get_tenant_db", async_gen_from(db)), \
             patch("src.api.solidatus.audit", new_callable=AsyncMock) as audit_mock:
            resp = await client.put(
                f"{SOLIDATUS_PREFIX}/config/{conn_id}",
                json={"display_name": "Solidatus Prod", "is_active": False},
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["display_name"] == "Solidatus Prod"
        assert data["is_active"] is False
        audit_mock.assert_awaited_once()
        assert audit_mock.await_args.kwargs["action"] == "solidatus.config.update"

    @pytest.mark.anyio
    async def test_connection_not_found(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(side_effect=lambda model_cls, pk: (
            model if pk == TEST_MODEL_ID else None
        ))

        with patch("src.api.solidatus.get_tenant_db", async_gen_from(db)):
            resp = await client.put(
                f"{SOLIDATUS_PREFIX}/config/{uuid.uuid4()}",
                json={"display_name": "X"},
            )
        assert resp.status_code == 404


# ------------------------------------------------------------------ #
# DELETE /config/{id}
# ------------------------------------------------------------------ #


class TestDeleteSolidatusConfig:
    @pytest.mark.anyio
    async def test_delete_returns_204(self, client):
        conn_id = uuid.uuid4()
        conn = _make_solidatus_connection(conn_id=conn_id)

        db = _setup_db_for_cud()
        model = make_model()
        db.get = AsyncMock(side_effect=lambda model_cls, pk: (
            model if pk == TEST_MODEL_ID else conn
        ))
        db.commit = AsyncMock()

        with patch("src.api.solidatus.get_tenant_db", async_gen_from(db)), \
             patch("src.api.solidatus.audit", new_callable=AsyncMock) as audit_mock:
            resp = await client.delete(f"{SOLIDATUS_PREFIX}/config/{conn_id}")
        assert resp.status_code == 204
        audit_mock.assert_awaited_once()
        assert audit_mock.await_args.kwargs["action"] == "solidatus.config.delete"


# ------------------------------------------------------------------ #
# POST /validate
# ------------------------------------------------------------------ #


class TestValidateSolidatus:
    @pytest.mark.anyio
    async def test_validate_returns_ok(self, client):
        conn_id = uuid.uuid4()
        conn = _make_solidatus_connection(conn_id=conn_id)

        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(side_effect=lambda model_cls, pk: (
            model if pk == TEST_MODEL_ID else conn
        ))

        with patch("src.api.solidatus.get_tenant_db", async_gen_from(db)), \
             patch("src.api.solidatus.decrypt_credentials", return_value={"token": "test-token"}):
            resp = await client.post(
                f"{SOLIDATUS_PREFIX}/validate",
                json={"connection_id": str(conn_id)},
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # F-CSI-01: the placeholder client never contacts Solidatus, so validate
        # must be honest — ok is null (tri-state "unknown"), simulated is true,
        # and a warning explains the connection was not actually verified.
        assert data["ok"] is None
        assert data["simulated"] is True
        assert data["workspace_found"] is None
        assert data["model_ref_found"] is None
        assert len(data["warnings"]) == 1
        assert "simulated" in data["warnings"][0].lower()


# ------------------------------------------------------------------ #
# GET /runs
# ------------------------------------------------------------------ #


class TestListSolidatusRuns:
    @pytest.mark.anyio
    async def test_list_empty(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        with patch("src.api.solidatus.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{SOLIDATUS_PREFIX}/runs")
        assert resp.status_code == 200
        assert resp.json() == []


# ------------------------------------------------------------------ #
# GET /mappings
# ------------------------------------------------------------------ #


class TestListSolidatusMappings:
    @pytest.mark.anyio
    async def test_list_empty(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        with patch("src.api.solidatus.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{SOLIDATUS_PREFIX}/mappings")
        assert resp.status_code == 200
        assert resp.json() == []
