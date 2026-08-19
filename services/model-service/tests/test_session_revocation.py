"""Middleware-level tests for session revocation enforcement (Bug-7322).

Verifies that ``_validate_regular_session`` in shared/auth/middleware.py
rejects tokens for deactivated users, role-demoted users, and tokens with
stale ``token_version`` claims. These tests exercise the middleware boundary
directly (not through mocked routes) so the security property is proven at
the enforcement point, not inferred from write-side tests alone.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from shared.auth.middleware import CurrentUser, _validate_regular_session

pytestmark = pytest.mark.unit

AUTH_EXC = HTTPException(status_code=401, detail="Invalid or expired token")


def _local_user(
    email: str = "user@example.com",
    is_active: bool = True,
    role: str = "member",
    token_version: int = 0,
):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        email=email,
        is_active=is_active,
        role=role,
        token_version=token_version,
    )


def _mock_tenant_db(local_user):
    """Patch get_tenant_db to yield a mock session returning *local_user*."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = local_user
    db.execute = AsyncMock(return_value=result)

    async def _gen(tenant_id):
        yield db

    return _gen


# -----------------------------------------------------------------------
# Deactivated user rejection
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deactivated_user_token_rejected():
    """A deactivated user's existing token must be rejected immediately."""
    user = CurrentUser(
        user_id="user@example.com", tenant_id="acme",
        email="user@example.com", role="member",
    )
    payload = {"sub": "user@example.com", "tenant_id": "acme", "role": "member", "token_version": 0}
    local = _local_user(is_active=False)
    with patch("shared.auth.middleware.get_tenant_db", _mock_tenant_db(local)):
        with pytest.raises(HTTPException) as exc:
            await _validate_regular_session(user, payload, AUTH_EXC)
        assert exc.value.status_code == 401


# -----------------------------------------------------------------------
# Role demotion rejection
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_demoted_user_token_rejected():
    """A user demoted from tenant_admin to member has their existing
    tenant_admin token rejected on the next request."""
    user = CurrentUser(
        user_id="admin@example.com", tenant_id="acme",
        email="admin@example.com", role="tenant_admin",
    )
    payload = {"sub": "admin@example.com", "tenant_id": "acme", "role": "tenant_admin", "token_version": 0}
    # DB says role is now "member" (demoted)
    local = _local_user(email="admin@example.com", role="member", token_version=0)
    with patch("shared.auth.middleware.get_tenant_db", _mock_tenant_db(local)):
        with pytest.raises(HTTPException) as exc:
            await _validate_regular_session(user, payload, AUTH_EXC)
        assert exc.value.status_code == 401


# -----------------------------------------------------------------------
# Token version mismatch rejection
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stale_token_version_rejected():
    """After password reset or deactivation, the DB token_version is bumped.
    An old token with the prior version must be rejected."""
    user = CurrentUser(
        user_id="user@example.com", tenant_id="acme",
        email="user@example.com", role="member",
    )
    # Token carries version 0, but DB was bumped to 1 (e.g. password reset)
    payload = {"sub": "user@example.com", "tenant_id": "acme", "role": "member", "token_version": 0}
    local = _local_user(token_version=1)
    with patch("shared.auth.middleware.get_tenant_db", _mock_tenant_db(local)):
        with pytest.raises(HTTPException) as exc:
            await _validate_regular_session(user, payload, AUTH_EXC)
        assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_current_token_version_accepted():
    """A token whose version matches the DB version is accepted."""
    user = CurrentUser(
        user_id="user@example.com", tenant_id="acme",
        email="user@example.com", role="member",
    )
    payload = {"sub": "user@example.com", "tenant_id": "acme", "role": "member", "token_version": 3}
    local = _local_user(token_version=3)
    with patch("shared.auth.middleware.get_tenant_db", _mock_tenant_db(local)):
        # Should NOT raise
        await _validate_regular_session(user, payload, AUTH_EXC)


# -----------------------------------------------------------------------
# Legacy token (no token_version claim) compatibility
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_legacy_token_accepted_at_version_zero():
    """A pre-migration token without a token_version claim is accepted
    when the DB version is still at the initial value (0)."""
    user = CurrentUser(
        user_id="user@example.com", tenant_id="acme",
        email="user@example.com", role="member",
    )
    payload = {"sub": "user@example.com", "tenant_id": "acme", "role": "member"}
    local = _local_user(token_version=0)
    with patch("shared.auth.middleware.get_tenant_db", _mock_tenant_db(local)):
        # Should NOT raise
        await _validate_regular_session(user, payload, AUTH_EXC)


@pytest.mark.asyncio
async def test_legacy_token_rejected_after_first_invalidation():
    """A pre-migration token without a token_version claim is rejected
    once the DB version has been bumped above 0 (first security event)."""
    user = CurrentUser(
        user_id="user@example.com", tenant_id="acme",
        email="user@example.com", role="member",
    )
    payload = {"sub": "user@example.com", "tenant_id": "acme", "role": "member"}
    local = _local_user(token_version=1)
    with patch("shared.auth.middleware.get_tenant_db", _mock_tenant_db(local)):
        with pytest.raises(HTTPException) as exc:
            await _validate_regular_session(user, payload, AUTH_EXC)
        assert exc.value.status_code == 401


# -----------------------------------------------------------------------
# role=None rejection (R1-F1 defence-in-depth)
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_role_none_token_rejected():
    """A token without a role claim must be rejected (R1-F1 fix)."""
    user = CurrentUser(
        user_id="user@example.com", tenant_id="acme",
        email="user@example.com", role=None,
    )
    payload = {"sub": "user@example.com", "tenant_id": "acme"}
    with pytest.raises(HTTPException) as exc:
        await _validate_regular_session(user, payload, AUTH_EXC)
    assert exc.value.status_code == 401


# -----------------------------------------------------------------------
# Deleted user (no LocalUser row) rejection
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deleted_user_token_rejected():
    """A deleted user (no LocalUser row found) must have their token rejected."""
    user = CurrentUser(
        user_id="user@example.com", tenant_id="acme",
        email="user@example.com", role="member",
    )
    payload = {"sub": "user@example.com", "tenant_id": "acme", "role": "member", "token_version": 0}
    with patch("shared.auth.middleware.get_tenant_db", _mock_tenant_db(None)):
        with pytest.raises(HTTPException) as exc:
            await _validate_regular_session(user, payload, AUTH_EXC)
        assert exc.value.status_code == 401


# -----------------------------------------------------------------------
# System admin bypass
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_canonical_system_admin_bypasses_validation():
    """A canonical system admin (role=system_admin, tenant_id=__system__) skips
    the tenant-local LocalUser validation. CP-08 added a durable system-admin
    token_version check on this path (get_system_admin_token_version, read from
    tess_system.system_settings); mock it to the unset value (0) so a token
    without a version claim is accepted, exercising the accept path without a
    live system DB."""
    user = CurrentUser(
        user_id="admin@tessallite.local", tenant_id="__system__",
        email="admin@tessallite.local", role="system_admin",
    )
    payload = {"sub": "admin@tessallite.local", "tenant_id": "__system__", "role": "system_admin"}
    with patch(
        "shared.auth.middleware.get_system_admin_token_version",
        AsyncMock(return_value=0),
    ):
        # Should NOT raise: canonical admin, version-0 system, no version claim.
        await _validate_regular_session(user, payload, AUTH_EXC)


@pytest.mark.asyncio
async def test_spoofed_system_admin_rejected():
    """A token claiming system_admin but with a real tenant_id (not __system__)
    must be rejected."""
    user = CurrentUser(
        user_id="spoof@example.com", tenant_id="acme",
        email="spoof@example.com", role="system_admin",
    )
    payload = {"sub": "spoof@example.com", "tenant_id": "acme", "role": "system_admin"}
    with pytest.raises(HTTPException) as exc:
        await _validate_regular_session(user, payload, AUTH_EXC)
    assert exc.value.status_code == 401
