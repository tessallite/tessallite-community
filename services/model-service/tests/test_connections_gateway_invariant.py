"""Bug-6213: discover/profile endpoints route through the query-router.

These tests verify the gateway-only-data-access invariant: discover_tables,
discover_columns, and profile_tables must call the query-router's
connection-introspect endpoints via HTTP, never opening a direct connection
to the customer's source database from the model-service.

Test strategy:
- Mock the HTTP call to the query-router and assert it is invoked with the
  correct endpoint/body.
- Patch ``shared.source_introspection`` functions and assert they are NEVER
  called from the model-service side (direct source access would bypass the
  invariant).
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROJECT_ID = uuid4()
_CONNECTION_ID = uuid4()
_TENANT = "test-tenant"
_BEARER = "test-bearer-token"


def _fake_connection(project_id=_PROJECT_ID, connection_type="postgresql"):
    """Return a minimal mock ProjectConnection."""
    conn = MagicMock()
    conn.id = _CONNECTION_ID
    conn.project_id = project_id
    conn.connection_type = connection_type
    conn.encrypted_credentials = b"encrypted"
    conn.config = {}
    return conn


def _fake_user(tenant_id=_TENANT):
    user = MagicMock()
    user.tenant_id = tenant_id
    user.email = "admin@test.local"
    return user


class _FakeRequest:
    """Minimal Request stand-in that carries an Authorization header."""

    def __init__(self, bearer: str = _BEARER):
        self.headers = {"authorization": f"Bearer {bearer}"}
        self.cookies = {}


# ---------------------------------------------------------------------------
# Test: discover_tables routes through the query-router
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discover_tables_calls_query_router_not_source_introspection():
    """discover_tables must POST to the query-router's
    /introspect/connection/discover-tables endpoint and must NOT import or
    call shared.source_introspection.discover_tables."""
    from src.api.connections import discover_tables

    fake_conn = _fake_connection()
    fake_user = _fake_user()
    fake_request = _FakeRequest()

    # Mock the tenant DB session + connection lookup.
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=fake_conn)

    expected_tables = [
        {"schema": "public", "table": "orders", "type": "BASE TABLE"},
    ]
    expected_payload = {"tables": expected_tables, "truncated": False}

    async def _fake_tenant_db(tenant_id):
        yield mock_db

    with (
        patch(
            "src.api.connections.get_tenant_db", side_effect=_fake_tenant_db,
        ),
        patch(
            "src.api.connections.forbid_embed_user",
            return_value=fake_user,
        ),
        patch(
            "src.api.connections._connection_introspect_via_router",
            new_callable=AsyncMock,
            return_value=expected_payload,
        ) as mock_router_call,
        patch(
            "shared.source_introspection.discover_tables",
            new_callable=AsyncMock,
        ) as mock_direct,
    ):
        result = await discover_tables(
            request=fake_request,
            project_id=_PROJECT_ID,
            connection_id=_CONNECTION_ID,
            schema_filter=None,
            current_user=fake_user,
        )

    # The query-router path was called.
    mock_router_call.assert_awaited_once()
    call_args = mock_router_call.call_args
    assert call_args[0][0] == "discover-tables"
    body = call_args[0][1]
    assert body["connection_id"] == str(_CONNECTION_ID)
    assert body["project_id"] == str(_PROJECT_ID)

    # The direct source_introspection path was NOT called.
    mock_direct.assert_not_awaited()

    # The result is the query-router response (structured truncation payload).
    assert result == expected_payload


# ---------------------------------------------------------------------------
# Test: discover_columns routes through the query-router
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discover_columns_calls_query_router_not_source_introspection():
    """discover_columns must POST to the query-router's
    /introspect/connection/discover-columns endpoint."""
    from src.api.connections import discover_columns

    fake_conn = _fake_connection()
    fake_user = _fake_user()
    fake_request = _FakeRequest()

    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=fake_conn)

    expected_columns = [
        {"column_name": "id", "data_type": "integer", "is_nullable": False},
    ]

    async def _fake_tenant_db(tenant_id):
        yield mock_db

    with (
        patch(
            "src.api.connections.get_tenant_db", side_effect=_fake_tenant_db,
        ),
        patch(
            "src.api.connections.forbid_embed_user",
            return_value=fake_user,
        ),
        patch(
            "src.api.connections._connection_introspect_via_router",
            new_callable=AsyncMock,
            return_value=expected_columns,
        ) as mock_router_call,
        patch(
            "shared.source_introspection.discover_columns",
            new_callable=AsyncMock,
        ) as mock_direct,
    ):
        result = await discover_columns(
            request=fake_request,
            project_id=_PROJECT_ID,
            connection_id=_CONNECTION_ID,
            schema="public",
            table="orders",
            current_user=fake_user,
        )

    mock_router_call.assert_awaited_once()
    call_args = mock_router_call.call_args
    assert call_args[0][0] == "discover-columns"
    body = call_args[0][1]
    assert body["connection_id"] == str(_CONNECTION_ID)
    assert body["schema"] == "public"
    assert body["table"] == "orders"

    mock_direct.assert_not_awaited()
    assert result == expected_columns


# ---------------------------------------------------------------------------
# Test: profile_tables routes through the query-router
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_profile_tables_calls_query_router_not_source_introspection():
    """profile_tables must POST to the query-router's
    /introspect/connection/profile endpoint; classification and role
    suggestion logic runs locally in the model-service."""
    from src.api.connections import profile_tables
    from shared.schemas.pydantic_models import ProfileTablesRequest

    fake_conn = _fake_connection()
    fake_user = _fake_user()
    fake_request = _FakeRequest()

    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=fake_conn)

    # Raw profile data that the query-router would return.
    raw_profile = [
        {
            "schema": "public",
            "table": "orders",
            "columns": [
                {
                    "column_name": "order_id",
                    "data_type": "bigint",
                    "is_nullable": False,
                    "approx_distinct": 100_000,
                },
                {
                    "column_name": "amount",
                    "data_type": "numeric",
                    "is_nullable": True,
                    "approx_distinct": 80_000,
                },
                {
                    "column_name": "order_date",
                    "data_type": "timestamp",
                    "is_nullable": False,
                    "approx_distinct": 365,
                },
            ],
            "row_count": 100_000,
        },
    ]

    async def _fake_tenant_db(tenant_id):
        yield mock_db

    body = ProfileTablesRequest(
        tables=[{"schema": "public", "table": "orders"}],
    )

    with (
        patch(
            "src.api.connections.get_tenant_db", side_effect=_fake_tenant_db,
        ),
        patch(
            "src.api.connections.forbid_embed_user",
            return_value=fake_user,
        ),
        patch(
            "src.api.connections._connection_introspect_via_router",
            new_callable=AsyncMock,
            return_value=raw_profile,
        ) as mock_router_call,
        patch(
            "shared.source_introspection.profile_table",
            new_callable=AsyncMock,
        ) as mock_direct,
    ):
        result = await profile_tables(
            request=fake_request,
            project_id=_PROJECT_ID,
            connection_id=_CONNECTION_ID,
            body=body,
            current_user=fake_user,
        )

    # The query-router path was called with the profile endpoint.
    mock_router_call.assert_awaited_once()
    call_args = mock_router_call.call_args
    assert call_args[0][0] == "profile"
    body_sent = call_args[0][1]
    assert body_sent["connection_id"] == str(_CONNECTION_ID)
    assert len(body_sent["tables"]) == 1
    assert body_sent["tables"][0]["table"] == "orders"

    # The direct source_introspection path was NOT called.
    mock_direct.assert_not_awaited()

    # The result contains classification (applied locally).
    assert len(result) == 1
    assert result[0]["table"] == "orders"
    assert result[0]["classification"] == "fact"
    assert result[0]["row_count"] == 100_000
    # Role suggestions are applied.
    cols = result[0]["columns"]
    assert any(c["suggested_role"] == "time_dimension" for c in cols)


@pytest.mark.asyncio
async def test_profile_tables_rejects_oversized_batch(monkeypatch):
    """Bug-6214: a batch larger than PROFILE_MAX_TABLES is rejected at the
    boundary (422) before any source-DB cardinality scan is issued."""
    from fastapi import HTTPException
    from src.api import connections as conn_mod
    from src.api.connections import profile_tables
    from shared.schemas.pydantic_models import ProfileTablesRequest

    monkeypatch.setenv("PROFILE_MAX_TABLES", "2")

    body = ProfileTablesRequest(
        tables=[{"schema": "public", "table": f"t{i}"} for i in range(3)],
    )

    mock_router = AsyncMock()
    with (
        patch.object(conn_mod, "_connection_introspect_via_router", mock_router),
    ):
        with pytest.raises(HTTPException) as exc:
            await profile_tables(
                request=_FakeRequest(),
                project_id=_PROJECT_ID,
                connection_id=_CONNECTION_ID,
                body=body,
                current_user=_fake_user(),
            )

    assert exc.value.status_code == 422
    # The runaway scan was never dispatched.
    mock_router.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test: _connection_introspect_via_router calls the correct URL
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_introspect_via_router_posts_to_query_router_url():
    """_connection_introspect_via_router must POST to the query-router's
    /api/v1/introspect/connection/<path> URL with the bearer token."""
    from src.api.connections import _connection_introspect_via_router

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = [{"schema": "public", "table": "t"}]

    with patch("src.api.connections.httpx.AsyncClient") as MockClient:
        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(return_value=mock_response)
        MockClient.return_value.__aenter__ = AsyncMock(
            return_value=mock_client_instance,
        )
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await _connection_introspect_via_router(
            "discover-tables",
            {"connection_id": "abc", "project_id": "xyz"},
            "my-token",
        )

    mock_client_instance.post.assert_awaited_once()
    call_args = mock_client_instance.post.call_args
    url = call_args[0][0]
    assert "/api/v1/introspect/connection/discover-tables" in url
    headers = call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer my-token"


# ---------------------------------------------------------------------------
# Test: discover_tables rejects wrong-project connection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discover_tables_rejects_wrong_project():
    """If the connection belongs to a different project, return 404."""
    from fastapi import HTTPException
    from src.api.connections import discover_tables

    other_project = uuid4()
    fake_conn = _fake_connection(project_id=other_project)
    fake_user = _fake_user()
    fake_request = _FakeRequest()

    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=fake_conn)

    async def _fake_tenant_db(tenant_id):
        yield mock_db

    with (
        patch("src.api.connections.get_tenant_db", side_effect=_fake_tenant_db),
        patch("src.api.connections.forbid_embed_user", return_value=fake_user),
        pytest.raises(HTTPException) as exc_info,
    ):
        await discover_tables(
            request=fake_request,
            project_id=_PROJECT_ID,
            connection_id=_CONNECTION_ID,
            schema_filter=None,
            current_user=fake_user,
        )

    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Test: /test routes through the query-router (F-014-04)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_test_connection_calls_query_router_not_source_introspection():
    """Stored /test must POST to query-router /introspect/connection/test."""
    from src.api.connections import test_connection

    fake_conn = _fake_connection()
    fake_user = _fake_user()
    fake_request = _FakeRequest()
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=fake_conn)

    async def _fake_tenant_db(tenant_id):
        yield mock_db

    with (
        patch("src.api.connections.get_tenant_db", side_effect=_fake_tenant_db),
        patch(
            "src.api.connections._connection_introspect_via_router",
            new_callable=AsyncMock,
            return_value={"ok": True},
        ) as mock_router_call,
        patch(
            "shared.source_introspection.test_connection",
            new_callable=AsyncMock,
        ) as mock_direct,
        patch(
            "shared.source_introspection.test_connection_raw",
            new_callable=AsyncMock,
        ) as mock_raw,
    ):
        result = await test_connection(
            request=fake_request,
            project_id=_PROJECT_ID,
            connection_id=_CONNECTION_ID,
            current_user=fake_user,
        )

    mock_router_call.assert_awaited_once()
    assert mock_router_call.call_args[0][0] == "test"
    mock_direct.assert_not_awaited()
    mock_raw.assert_not_awaited()
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_test_connection_payload_calls_query_router_not_source_introspection():
    """Draft /test must POST to query-router /introspect/connection/test-draft."""
    from src.api.connections import test_connection_payload
    from shared.schemas.pydantic_models import ConnectionTestRequest

    fake_user = _fake_user()
    fake_request = _FakeRequest()
    body = ConnectionTestRequest(
        connection_type="postgresql",
        credentials={"host": "h", "username": "u", "password": "p"},
    )

    with (
        patch(
            "src.api.connections._connection_introspect_via_router",
            new_callable=AsyncMock,
            return_value={"ok": True},
        ) as mock_router_call,
        patch(
            "shared.source_introspection.test_connection_raw",
            new_callable=AsyncMock,
        ) as mock_raw,
    ):
        result = await test_connection_payload(
            request=fake_request,
            project_id=_PROJECT_ID,
            body=body,
            current_user=fake_user,
        )

    mock_router_call.assert_awaited_once()
    assert mock_router_call.call_args[0][0] == "test-draft"
    mock_raw.assert_not_awaited()
    assert result == {"ok": True}
