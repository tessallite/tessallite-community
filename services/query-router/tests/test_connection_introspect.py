"""Bug-6213 / Bug-7167: query-router connection-introspect endpoint tests.

Validates the three new endpoints that centralise connection-level
introspection (discover tables, discover columns, profile) through the
query-router, enforcing the gateway-only-data-access invariant.

Covers: happy path, project-mismatch rejection (403), connection-not-found
(404), source failure propagation (502), empty-tables edge case,
and Bug-7167 RBAC enforcement (viewer denied, modeler allowed).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_PROJECT_ID = uuid4()
_CONNECTION_ID = uuid4()
_TENANT = "test-tenant"


def _fake_connection(project_id=_PROJECT_ID, connection_type="postgresql"):
    conn = MagicMock()
    conn.id = _CONNECTION_ID
    conn.project_id = project_id
    conn.connection_type = connection_type
    conn.encrypted_credentials = b"encrypted"
    conn.config = {}
    return conn


def _fake_user(tenant_id=_TENANT, role="modeler"):
    user = MagicMock()
    user.tenant_id = tenant_id
    user.email = "admin@test.local"
    user.user_id = "admin@test.local"
    user.role = role
    user.capabilities = None  # non-embed user
    return user


def _fake_tenant_db_factory(mock_db):
    async def _fake_tenant_db(tenant_id):
        yield mock_db
    return _fake_tenant_db


def _noop_rbac_patch():
    """Patch ensure_project_model_access to be a no-op (passes RBAC).

    Bug-7167: the connection_introspect endpoints now call
    ensure_project_model_access inside _resolve_connection; happy-path
    tests must patch it out so they only test the endpoint logic, not the
    full RBAC stack (which is tested in its own module).
    """
    return patch(
        "src.api.connection_introspect.ensure_project_model_access",
        new_callable=AsyncMock,
    )


# ---------------------------------------------------------------------------
# discover-tables
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discover_tables_happy_path():
    """discover-tables returns the source_introspection result for a valid connection."""
    from src.api.connection_introspect import discover_tables_via_connection, DiscoverTablesRequest

    fake_conn = _fake_connection()
    fake_user = _fake_user()
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=fake_conn)

    expected = [{"schema": "public", "table": "orders", "type": "BASE TABLE"}]

    body = DiscoverTablesRequest(
        connection_id=str(_CONNECTION_ID),
        project_id=str(_PROJECT_ID),
    )

    with (
        patch("src.api.connection_introspect.get_tenant_db",
              side_effect=_fake_tenant_db_factory(mock_db)),
        patch("src.api.connection_introspect.require_capability",
              return_value=lambda: fake_user),
        _noop_rbac_patch(),
        patch("shared.source_introspection.discover_tables",
              new_callable=AsyncMock, return_value=expected) as mock_discover,
    ):
        result = await discover_tables_via_connection(body=body, current_user=fake_user)

    mock_discover.assert_awaited_once_with(
        fake_conn, schema=None, tenant_session=mock_db,
    )
    assert result == {"tables": expected, "truncated": False}


@pytest.mark.asyncio
async def test_discover_tables_with_schema_filter():
    """schema_filter is forwarded to source_introspection."""
    from src.api.connection_introspect import discover_tables_via_connection, DiscoverTablesRequest

    fake_conn = _fake_connection()
    fake_user = _fake_user()
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=fake_conn)

    body = DiscoverTablesRequest(
        connection_id=str(_CONNECTION_ID),
        project_id=str(_PROJECT_ID),
        schema_filter="sales",
    )

    with (
        patch("src.api.connection_introspect.get_tenant_db",
              side_effect=_fake_tenant_db_factory(mock_db)),
        _noop_rbac_patch(),
        patch("shared.source_introspection.discover_tables",
              new_callable=AsyncMock, return_value=[]) as mock_discover,
    ):
        result = await discover_tables_via_connection(body=body, current_user=fake_user)

    mock_discover.assert_awaited_once_with(
        fake_conn, schema="sales", tenant_session=mock_db,
    )
    assert result == {"tables": [], "truncated": False}


@pytest.mark.asyncio
async def test_discover_tables_connection_not_found():
    """Return 404 when the connection does not exist."""
    from fastapi import HTTPException
    from src.api.connection_introspect import discover_tables_via_connection, DiscoverTablesRequest

    fake_user = _fake_user()
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=None)

    body = DiscoverTablesRequest(
        connection_id=str(uuid4()),
        project_id=str(_PROJECT_ID),
    )

    with (
        patch("src.api.connection_introspect.get_tenant_db",
              side_effect=_fake_tenant_db_factory(mock_db)),
        pytest.raises(HTTPException) as exc_info,
    ):
        await discover_tables_via_connection(body=body, current_user=fake_user)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_discover_tables_project_mismatch():
    """Bug-7592: a cross-project connection returns the uniform 404, not 403.

    Previously a connection that existed under a DIFFERENT project answered
    403, distinguishing "exists elsewhere" from "does not exist" (404) — an
    existence oracle. It now returns the same 404 ``Connection not found``.
    """
    from fastapi import HTTPException
    from src.api.connection_introspect import discover_tables_via_connection, DiscoverTablesRequest

    other_project = uuid4()
    fake_conn = _fake_connection(project_id=other_project)
    fake_user = _fake_user()
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=fake_conn)

    body = DiscoverTablesRequest(
        connection_id=str(_CONNECTION_ID),
        project_id=str(_PROJECT_ID),
    )

    with (
        patch("src.api.connection_introspect.get_tenant_db",
              side_effect=_fake_tenant_db_factory(mock_db)),
        pytest.raises(HTTPException) as exc_info,
    ):
        await discover_tables_via_connection(body=body, current_user=fake_user)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Connection not found"


@pytest.mark.asyncio
async def test_discover_tables_source_failure_returns_502():
    """A source-database error propagates as a 502."""
    from fastapi import HTTPException
    from src.api.connection_introspect import discover_tables_via_connection, DiscoverTablesRequest

    fake_conn = _fake_connection()
    fake_user = _fake_user()
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=fake_conn)

    body = DiscoverTablesRequest(
        connection_id=str(_CONNECTION_ID),
        project_id=str(_PROJECT_ID),
    )

    with (
        patch("src.api.connection_introspect.get_tenant_db",
              side_effect=_fake_tenant_db_factory(mock_db)),
        _noop_rbac_patch(),
        patch("shared.source_introspection.discover_tables",
              new_callable=AsyncMock,
              side_effect=RuntimeError("connection refused")),
        pytest.raises(HTTPException) as exc_info,
    ):
        await discover_tables_via_connection(body=body, current_user=fake_user)

    assert exc_info.value.status_code == 502
    assert "connection refused" in str(exc_info.value.detail)


# ---------------------------------------------------------------------------
# discover-columns
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discover_columns_happy_path():
    """discover-columns returns columns for a valid connection + table."""
    from src.api.connection_introspect import discover_columns_via_connection, DiscoverColumnsRequest

    fake_conn = _fake_connection()
    fake_user = _fake_user()
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=fake_conn)

    expected = [
        {"column_name": "id", "data_type": "integer", "is_nullable": False},
        {"column_name": "name", "data_type": "varchar", "is_nullable": True},
    ]

    body = DiscoverColumnsRequest(
        connection_id=str(_CONNECTION_ID),
        project_id=str(_PROJECT_ID),
        schema="public",
        table="orders",
    )

    with (
        patch("src.api.connection_introspect.get_tenant_db",
              side_effect=_fake_tenant_db_factory(mock_db)),
        _noop_rbac_patch(),
        patch("shared.source_introspection.discover_columns",
              new_callable=AsyncMock, return_value=expected) as mock_discover,
    ):
        result = await discover_columns_via_connection(body=body, current_user=fake_user)

    mock_discover.assert_awaited_once_with(
        fake_conn, schema="public", table="orders", tenant_session=mock_db,
    )
    assert result == expected


@pytest.mark.asyncio
async def test_discover_columns_project_mismatch():
    """Bug-7592: cross-project connection access returns the uniform 404."""
    from fastapi import HTTPException
    from src.api.connection_introspect import discover_columns_via_connection, DiscoverColumnsRequest

    fake_conn = _fake_connection(project_id=uuid4())
    fake_user = _fake_user()
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=fake_conn)

    body = DiscoverColumnsRequest(
        connection_id=str(_CONNECTION_ID),
        project_id=str(_PROJECT_ID),
        schema="public",
        table="orders",
    )

    with (
        patch("src.api.connection_introspect.get_tenant_db",
              side_effect=_fake_tenant_db_factory(mock_db)),
        pytest.raises(HTTPException) as exc_info,
    ):
        await discover_columns_via_connection(body=body, current_user=fake_user)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Connection not found"


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_profile_happy_path():
    """profile returns raw column metadata + row count for each table."""
    from src.api.connection_introspect import profile_tables_via_connection, ProfileRequest

    fake_conn = _fake_connection()
    fake_user = _fake_user()
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=fake_conn)

    profile_columns = [
        {"column_name": "id", "data_type": "integer", "is_nullable": False, "approx_distinct": 1000},
        {"column_name": "amount", "data_type": "numeric", "is_nullable": True, "approx_distinct": 900},
    ]

    body = ProfileRequest(
        connection_id=str(_CONNECTION_ID),
        project_id=str(_PROJECT_ID),
        tables=[{"schema": "public", "table": "orders"}],
    )

    with (
        patch("src.api.connection_introspect.get_tenant_db",
              side_effect=_fake_tenant_db_factory(mock_db)),
        _noop_rbac_patch(),
        patch("shared.source_introspection.profile_table",
              new_callable=AsyncMock,
              return_value=(profile_columns, 1000)) as mock_profile,
    ):
        result = await profile_tables_via_connection(body=body, current_user=fake_user)

    mock_profile.assert_awaited_once_with(
        fake_conn, schema="public", table="orders", tenant_session=mock_db,
    )
    assert len(result) == 1
    assert result[0]["schema"] == "public"
    assert result[0]["table"] == "orders"
    assert result[0]["row_count"] == 1000
    assert result[0]["columns"] == profile_columns


@pytest.mark.asyncio
async def test_profile_empty_tables_returns_empty():
    """An empty tables list returns an empty result without calling source_introspection."""
    from src.api.connection_introspect import profile_tables_via_connection, ProfileRequest

    fake_user = _fake_user()

    body = ProfileRequest(
        connection_id=str(_CONNECTION_ID),
        project_id=str(_PROJECT_ID),
        tables=[],
    )

    with patch("shared.source_introspection.profile_table",
               new_callable=AsyncMock) as mock_profile:
        result = await profile_tables_via_connection(body=body, current_user=fake_user)

    mock_profile.assert_not_awaited()
    assert result == []


@pytest.mark.asyncio
async def test_profile_project_mismatch():
    """Bug-7592: cross-project connection access during profile returns 404."""
    from fastapi import HTTPException
    from src.api.connection_introspect import profile_tables_via_connection, ProfileRequest

    fake_conn = _fake_connection(project_id=uuid4())
    fake_user = _fake_user()
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=fake_conn)

    body = ProfileRequest(
        connection_id=str(_CONNECTION_ID),
        project_id=str(_PROJECT_ID),
        tables=[{"schema": "public", "table": "orders"}],
    )

    with (
        patch("src.api.connection_introspect.get_tenant_db",
              side_effect=_fake_tenant_db_factory(mock_db)),
        pytest.raises(HTTPException) as exc_info,
    ):
        await profile_tables_via_connection(body=body, current_user=fake_user)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Connection not found"


@pytest.mark.asyncio
async def test_profile_source_failure_returns_502():
    """A source-database error during profiling propagates as 502."""
    from fastapi import HTTPException
    from src.api.connection_introspect import profile_tables_via_connection, ProfileRequest

    fake_conn = _fake_connection()
    fake_user = _fake_user()
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=fake_conn)

    body = ProfileRequest(
        connection_id=str(_CONNECTION_ID),
        project_id=str(_PROJECT_ID),
        tables=[{"schema": "public", "table": "orders"}],
    )

    with (
        patch("src.api.connection_introspect.get_tenant_db",
              side_effect=_fake_tenant_db_factory(mock_db)),
        _noop_rbac_patch(),
        patch("shared.source_introspection.profile_table",
              new_callable=AsyncMock,
              side_effect=RuntimeError("timeout")),
        pytest.raises(HTTPException) as exc_info,
    ):
        await profile_tables_via_connection(body=body, current_user=fake_user)

    assert exc_info.value.status_code == 502
    assert "timeout" in str(exc_info.value.detail)


# ---------------------------------------------------------------------------
# Bug-7167 — RBAC enforcement (viewer denied at the query-router boundary)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_tables_viewer_denied():
    """Bug-7167 + Bug-7592: a viewer (no modeler binding) is rejected.

    The RBAC denial now collapses to the uniform 404 ``Connection not found``
    (Bug-7592) rather than a 403 — a 403 here would confirm to an unauthorized
    caller that the connection EXISTS, which is the existence oracle Bug-7592
    closes. The connection remains inaccessible (Bug-7167 still enforced); only
    the leaked status/body changes.
    """
    from fastapi import HTTPException
    from src.api.connection_introspect import discover_tables_via_connection, DiscoverTablesRequest

    fake_conn = _fake_connection()
    fake_user = _fake_user(role="viewer")
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=fake_conn)

    body = DiscoverTablesRequest(
        connection_id=str(_CONNECTION_ID),
        project_id=str(_PROJECT_ID),
    )

    with (
        patch("src.api.connection_introspect.get_tenant_db",
              side_effect=_fake_tenant_db_factory(mock_db)),
        patch(
            "src.api.connection_introspect.ensure_project_model_access",
            new_callable=AsyncMock,
            side_effect=HTTPException(status_code=403, detail="Access denied"),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await discover_tables_via_connection(body=body, current_user=fake_user)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Connection not found"


# ---------------------------------------------------------------------------
# Bug-7157 — profile table-count cap at query-router level
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_profile_exceeds_table_cap():
    """Bug-7157: profile rejects a request exceeding PROFILE_MAX_TABLES."""
    from fastapi import HTTPException
    from src.api.connection_introspect import profile_tables_via_connection, ProfileRequest

    fake_user = _fake_user()

    body = ProfileRequest(
        connection_id=str(_CONNECTION_ID),
        project_id=str(_PROJECT_ID),
        tables=[{"schema": "public", "table": f"t{i}"} for i in range(30)],
    )

    with (
        patch.dict("os.environ", {"PROFILE_MAX_TABLES": "25"}),
        pytest.raises(HTTPException) as exc_info,
    ):
        await profile_tables_via_connection(body=body, current_user=fake_user)

    assert exc_info.value.status_code == 422
    assert "30 requested" in str(exc_info.value.detail)


# ---------------------------------------------------------------------------
# Bug-7592 [SECURITY] — existence oracle: absent vs unauthorized must be
# INDISTINGUISHABLE (identical status AND body). A 403-for-exists /
# 404-for-absent split let an unauthorized caller probe whether any given
# connection_id existed. This test fails pre-fix (unauthorized returned 403).
# ---------------------------------------------------------------------------


async def _introspect_rejection(*, conn, rbac_side_effect=None):
    """Drive discover-tables to its rejection and return (status_code, detail).

    ``conn`` is the value ``db.get`` yields (``None`` = absent connection);
    ``rbac_side_effect`` patches ``ensure_project_model_access`` (e.g. a 403
    denial) so the unauthorized path can be exercised.
    """
    from fastapi import HTTPException
    from src.api.connection_introspect import (
        discover_tables_via_connection,
        DiscoverTablesRequest,
    )

    fake_user = _fake_user(role="viewer")
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=conn)

    body = DiscoverTablesRequest(
        connection_id=str(_CONNECTION_ID),
        project_id=str(_PROJECT_ID),
    )

    rbac_patch = patch(
        "src.api.connection_introspect.ensure_project_model_access",
        new_callable=AsyncMock,
        side_effect=rbac_side_effect,
    )
    with (
        patch("src.api.connection_introspect.get_tenant_db",
              side_effect=_fake_tenant_db_factory(mock_db)),
        rbac_patch,
        pytest.raises(HTTPException) as exc_info,
    ):
        await discover_tables_via_connection(body=body, current_user=fake_user)
    return exc_info.value.status_code, exc_info.value.detail


@pytest.mark.asyncio
async def test_bug_7592_absent_and_unauthorized_are_indistinguishable():
    """Bug-7592: an ABSENT connection and an EXISTING-but-UNAUTHORIZED
    connection return the IDENTICAL status code and response body, so the
    endpoint cannot be used as an existence oracle."""
    from fastapi import HTTPException

    # Case A: the connection does not exist at all.
    absent_status, absent_detail = await _introspect_rejection(conn=None)

    # Case B: the connection EXISTS (right project) but the caller lacks the
    # required project role — ensure_project_model_access raises 403.
    existing_conn = _fake_connection()
    unauth_status, unauth_detail = await _introspect_rejection(
        conn=existing_conn,
        rbac_side_effect=HTTPException(status_code=403, detail="Access denied"),
    )

    # The whole point: the two responses are byte-for-byte the same.
    assert absent_status == unauth_status == 404
    assert absent_detail == unauth_detail == "Connection not found"


@pytest.mark.asyncio
async def test_bug_7592_cross_project_matches_absent():
    """Bug-7592: a connection that exists under a DIFFERENT project is also
    indistinguishable from an absent one (same 404 + body)."""
    absent_status, absent_detail = await _introspect_rejection(conn=None)

    other_project_conn = _fake_connection(project_id=uuid4())
    cross_status, cross_detail = await _introspect_rejection(conn=other_project_conn)

    assert absent_status == cross_status == 404
    assert absent_detail == cross_detail == "Connection not found"


@pytest.mark.asyncio
async def test_test_stored_connection_happy_path():
    """F-014-04: stored /test delegates to source_introspection.test_connection."""
    from src.api.connection_introspect import (
        test_stored_connection,
        TestStoredConnectionRequest,
    )

    fake_conn = _fake_connection()
    fake_user = _fake_user()
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=fake_conn)
    body = TestStoredConnectionRequest(
        connection_id=str(_CONNECTION_ID),
        project_id=str(_PROJECT_ID),
    )
    with (
        patch("src.api.connection_introspect.get_tenant_db",
              side_effect=_fake_tenant_db_factory(mock_db)),
        _noop_rbac_patch(),
        patch("shared.source_introspection.test_connection",
              new_callable=AsyncMock, return_value=(True, None)) as mock_test,
    ):
        result = await test_stored_connection(body=body, current_user=fake_user)
    mock_test.assert_awaited_once_with(fake_conn, tenant_session=mock_db)
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_test_draft_connection_happy_path():
    """F-014-04: draft /test-draft delegates to test_connection_raw."""
    from src.api.connection_introspect import (
        test_draft_connection,
        TestDraftConnectionRequest,
    )

    fake_user = _fake_user()
    mock_db = AsyncMock()
    body = TestDraftConnectionRequest(
        project_id=str(_PROJECT_ID),
        connection_type="postgresql",
        credentials={"host": "h", "username": "u", "password": "p"},
        config={},
    )
    with (
        patch("src.api.connection_introspect.get_tenant_db",
              side_effect=_fake_tenant_db_factory(mock_db)),
        _noop_rbac_patch(),
        patch("shared.source_introspection.test_connection_raw",
              new_callable=AsyncMock, return_value=(True, None)) as mock_raw,
    ):
        result = await test_draft_connection(body=body, current_user=fake_user)
    mock_raw.assert_awaited_once()
    assert result == {"ok": True}
