"""Demo source lock on test-connection endpoints (SSRF prevention).

Verifies that enforce_demo_source_locked gates all three test-connection
endpoints so a locked demo tenant cannot use them to probe arbitrary hosts:

- test_connection         (POST /{connection_id}/test)
- test_connection_payload (POST /test)
- test_connection_merged  (POST /{connection_id}/test_edit)

A locked demo tenant must receive a 403 BEFORE any outbound connection is
attempted. Normal (unlisted) tenants must pass through unblocked.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException


class _DummyRequest:
    headers = {"authorization": "Bearer dummy"}
    cookies: dict = {}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROJECT_ID = uuid4()
_CONNECTION_ID = uuid4()
_LOCKED_TENANT = "acme-demo"
_NORMAL_TENANT = "customer-tenant"


def _fake_connection(project_id=_PROJECT_ID, connection_type="postgresql"):
    conn = MagicMock()
    conn.id = _CONNECTION_ID
    conn.project_id = project_id
    conn.connection_type = connection_type
    conn.encrypted_credentials = b"encrypted"
    conn.config = {}
    return conn


def _fake_user(tenant_id):
    user = MagicMock()
    user.tenant_id = tenant_id
    user.email = "admin@test.local"
    return user


# ---------------------------------------------------------------------------
# Locked demo tenant: connection mutations must reject with 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_connection_rejects_locked_demo_tenant(monkeypatch):
    """POST /connections must reject a locked demo tenant before DB access."""
    from src.api.connections import create_connection
    from shared.schemas.pydantic_models import ConnectionCreate

    monkeypatch.setenv("DEMO_SOURCE_LOCKED_TENANTS", _LOCKED_TENANT)
    user = _fake_user(_LOCKED_TENANT)
    body = ConnectionCreate(
        display_name="Blocked",
        connection_type="postgresql",
        credentials={"host": "db", "database": "d", "username": "u"},
        config={},
    )

    with (
        patch("src.api.connections.get_tenant_db") as mock_db,
        pytest.raises(HTTPException) as exc_info,
    ):
        await create_connection(
            project_id=_PROJECT_ID,
            body=body,
            current_user=user,
        )

    assert exc_info.value.status_code == 403
    assert "fixed and read-only" in str(exc_info.value.detail)
    mock_db.assert_not_called()


@pytest.mark.asyncio
async def test_update_connection_rejects_locked_demo_tenant(monkeypatch):
    """PATCH /connections/{id} must reject a locked demo tenant before DB access."""
    from src.api.connections import update_connection
    from shared.schemas.pydantic_models import ConnectionUpdate

    monkeypatch.setenv("DEMO_SOURCE_LOCKED_TENANTS", _LOCKED_TENANT)
    user = _fake_user(_LOCKED_TENANT)
    body = ConnectionUpdate(display_name="Blocked edit")

    with (
        patch("src.api.connections.get_tenant_db") as mock_db,
        pytest.raises(HTTPException) as exc_info,
    ):
        await update_connection(
            project_id=_PROJECT_ID,
            connection_id=_CONNECTION_ID,
            body=body,
            current_user=user,
        )

    assert exc_info.value.status_code == 403
    assert "fixed and read-only" in str(exc_info.value.detail)
    mock_db.assert_not_called()


@pytest.mark.asyncio
async def test_delete_connection_rejects_locked_demo_tenant(monkeypatch):
    """DELETE /connections/{id} must reject a locked demo tenant before DB access."""
    from src.api.connections import delete_connection

    monkeypatch.setenv("DEMO_SOURCE_LOCKED_TENANTS", _LOCKED_TENANT)
    user = _fake_user(_LOCKED_TENANT)

    with (
        patch("src.api.connections.get_tenant_db") as mock_db,
        pytest.raises(HTTPException) as exc_info,
    ):
        await delete_connection(
            project_id=_PROJECT_ID,
            connection_id=_CONNECTION_ID,
            current_user=user,
        )

    assert exc_info.value.status_code == 403
    assert "fixed and read-only" in str(exc_info.value.detail)
    mock_db.assert_not_called()


@pytest.mark.asyncio
async def test_project_import_rejects_locked_demo_tenant(monkeypatch):
    """Project import can create/update/prune connections and must be locked."""
    from src.api.project_import_export import ProjectImportRequest, import_project_endpoint

    monkeypatch.setenv("DEMO_SOURCE_LOCKED_TENANTS", _LOCKED_TENANT)
    user = _fake_user(_LOCKED_TENANT)
    body = ProjectImportRequest(
        bundle={
            "credentials_included": False,
            "included_sections": ["connections"],
            "project": {"slug": "imported", "display_name": "Imported"},
            "connections": [],
            "models": [],
        },
        mode="create",
    )

    with (
        patch("src.api.project_import_export.get_tenant_db") as mock_db,
        patch("src.api.project_import_export.import_project", AsyncMock()) as mock_import,
        pytest.raises(HTTPException) as exc_info,
    ):
        await import_project_endpoint(body=body, current_user=user)

    assert exc_info.value.status_code == 403
    mock_db.assert_not_called()
    mock_import.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_name", "func_name", "kwargs"),
    [
        ("src.api.dbt_import", "import_dbt_semantic_models", {"file": MagicMock()}),
        ("src.api.cube_import", "import_cube_models", {"file": MagicMock()}),
        ("src.api.atscale_import", "import_atscale_sml", {"file": MagicMock()}),
        ("src.api.yaml_export", "import_project_yaml", {"file": MagicMock()}),
        (
            "src.api.catalog_import",
            "import_from_catalog",
            {"body": MagicMock(catalog_type="datahub")},
        ),
    ],
)
async def test_placeholder_importers_reject_locked_demo_tenant(
    monkeypatch, module_name, func_name, kwargs
):
    """Placeholder-import endpoints must reject locked demo tenants before DB access."""
    import importlib

    module = importlib.import_module(module_name)
    func = getattr(module, func_name)
    monkeypatch.setenv("DEMO_SOURCE_LOCKED_TENANTS", _LOCKED_TENANT)
    user = _fake_user(_LOCKED_TENANT)

    with (
        patch(f"{module_name}.get_tenant_db") as mock_db,
        pytest.raises(HTTPException) as exc_info,
    ):
        await func(
            project_id=_PROJECT_ID,
            current_user=user,
            **kwargs,
        )

    assert exc_info.value.status_code == 403
    mock_db.assert_not_called()


# ---------------------------------------------------------------------------
# Locked demo tenant: all 3 test endpoints must reject with 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_test_connection_rejects_locked_demo_tenant(monkeypatch):
    """POST /{connection_id}/test must reject a locked demo tenant with 403
    BEFORE any DB lookup or outbound connection attempt."""
    from src.api.connections import test_connection

    monkeypatch.setenv("DEMO_SOURCE_LOCKED_TENANTS", _LOCKED_TENANT)
    user = _fake_user(_LOCKED_TENANT)

    mock_run = AsyncMock()
    with (
        patch("src.api.connections._run_connection_test", mock_run),
        patch("src.api.connections.get_tenant_db") as mock_db,
        pytest.raises(HTTPException) as exc_info,
    ):
        await test_connection(
            request=_DummyRequest(),
            project_id=_PROJECT_ID,
            connection_id=_CONNECTION_ID,
            current_user=user,
        )

    assert exc_info.value.status_code == 403
    assert "fixed and read-only" in str(exc_info.value.detail)
    # No outbound connection was attempted.
    mock_run.assert_not_awaited()
    # No DB session was even opened.
    mock_db.assert_not_called()


@pytest.mark.asyncio
async def test_test_connection_payload_rejects_locked_demo_tenant(monkeypatch):
    """POST /test (ad-hoc payload) must reject a locked demo tenant with 403
    BEFORE any outbound connection attempt."""
    from src.api.connections import test_connection_payload
    from shared.schemas.pydantic_models import ConnectionTestRequest

    monkeypatch.setenv("DEMO_SOURCE_LOCKED_TENANTS", _LOCKED_TENANT)
    user = _fake_user(_LOCKED_TENANT)

    body = ConnectionTestRequest(
        connection_type="postgresql",
        credentials={"host": "evil.internal", "port": 5432,
                      "database": "probe", "username": "x", "password": "y"},
    )

    mock_run = AsyncMock()
    with (
        patch("src.api.connections._run_connection_test", mock_run),
        patch("src.api.connections.get_tenant_db") as mock_db,
        pytest.raises(HTTPException) as exc_info,
    ):
        await test_connection_payload(
            request=_DummyRequest(),
            project_id=_PROJECT_ID,
            body=body,
            current_user=user,
        )

    assert exc_info.value.status_code == 403
    assert "fixed and read-only" in str(exc_info.value.detail)
    mock_run.assert_not_awaited()
    mock_db.assert_not_called()


@pytest.mark.asyncio
async def test_test_connection_merged_rejects_locked_demo_tenant(monkeypatch):
    """POST /{connection_id}/test_edit must reject a locked demo tenant with
    403 BEFORE any DB lookup or outbound connection attempt."""
    from src.api.connections import test_connection_merged
    from shared.schemas.pydantic_models import ConnectionUpdate

    monkeypatch.setenv("DEMO_SOURCE_LOCKED_TENANTS", _LOCKED_TENANT)
    user = _fake_user(_LOCKED_TENANT)

    body = ConnectionUpdate(credentials={"host": "evil.internal"})

    mock_run = AsyncMock()
    with (
        patch("src.api.connections._run_connection_test", mock_run),
        patch("src.api.connections.get_tenant_db") as mock_db,
        pytest.raises(HTTPException) as exc_info,
    ):
        await test_connection_merged(
            request=_DummyRequest(),
            project_id=_PROJECT_ID,
            connection_id=_CONNECTION_ID,
            body=body,
            current_user=user,
        )

    assert exc_info.value.status_code == 403
    assert "fixed and read-only" in str(exc_info.value.detail)
    mock_run.assert_not_awaited()
    mock_db.assert_not_called()


# ---------------------------------------------------------------------------
# Normal tenant: all 3 endpoints must allow through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_test_connection_allows_normal_tenant(monkeypatch):
    """POST /{connection_id}/test must allow a normal (unlisted) tenant
    through to the actual connection test."""
    from src.api.connections import test_connection

    monkeypatch.setenv("DEMO_SOURCE_LOCKED_TENANTS", _LOCKED_TENANT)
    user = _fake_user(_NORMAL_TENANT)
    fake_conn = _fake_connection()

    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=fake_conn)

    async def _fake_tenant_db(tenant_id):
        yield mock_db

    mock_router = AsyncMock(return_value={"ok": True})
    with (
        patch("src.api.connections.get_tenant_db", side_effect=_fake_tenant_db),
        patch("src.api.connections._connection_introspect_via_router", mock_router),
    ):
        result = await test_connection(
            request=_DummyRequest(),
            project_id=_PROJECT_ID,
            connection_id=_CONNECTION_ID,
            current_user=user,
        )

    assert result == {"ok": True}
    mock_router.assert_awaited_once()
    assert mock_router.await_args[0][0] == "test"


@pytest.mark.asyncio
async def test_test_connection_payload_allows_normal_tenant(monkeypatch):
    """POST /test (ad-hoc payload) must allow a normal tenant through."""
    from src.api.connections import test_connection_payload
    from shared.schemas.pydantic_models import ConnectionTestRequest

    monkeypatch.setenv("DEMO_SOURCE_LOCKED_TENANTS", _LOCKED_TENANT)
    user = _fake_user(_NORMAL_TENANT)

    body = ConnectionTestRequest(
        connection_type="postgresql",
        credentials={"host": "my-db.internal", "port": 5432,
                      "database": "mydb", "username": "u", "password": "p"},
    )

    mock_db = AsyncMock()

    async def _fake_tenant_db(tenant_id):
        yield mock_db

    mock_run = AsyncMock(return_value={"ok": True})
    with (
        patch("src.api.connections.get_tenant_db", side_effect=_fake_tenant_db),
        patch("src.api.connections._run_connection_test", mock_run),
    ):
        result = await test_connection_payload(
            request=_DummyRequest(),
            project_id=_PROJECT_ID,
            body=body,
            current_user=user,
        )

    assert result == {"ok": True}
    mock_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_test_connection_merged_allows_normal_tenant(monkeypatch):
    """POST /{connection_id}/test_edit must allow a normal tenant through."""
    from src.api.connections import test_connection_merged
    from shared.schemas.pydantic_models import ConnectionUpdate

    monkeypatch.setenv("DEMO_SOURCE_LOCKED_TENANTS", _LOCKED_TENANT)
    user = _fake_user(_NORMAL_TENANT)
    fake_conn = _fake_connection()

    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=fake_conn)

    async def _fake_tenant_db(tenant_id):
        yield mock_db

    body = ConnectionUpdate(credentials={"host": "my-db.internal"})

    mock_run = AsyncMock(return_value={"ok": True})
    with (
        patch("src.api.connections.get_tenant_db", side_effect=_fake_tenant_db),
        patch("src.api.connections._run_connection_test", mock_run),
        patch("src.api.connections._decrypt", return_value={"host": "db"}),
    ):
        result = await test_connection_merged(
            request=_DummyRequest(),
            project_id=_PROJECT_ID,
            connection_id=_CONNECTION_ID,
            body=body,
            current_user=user,
        )

    assert result == {"ok": True}
    mock_run.assert_awaited_once()
