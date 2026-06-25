"""Tests for JIT user adoption from IdP (Block G).

Verifies that when a non-local auth backend (LDAP, GCP IAM) authenticates a
user who does not yet have a local_users record, the login endpoint auto-creates
one with the configured default role.
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from shared.auth.backend import UserIdentity
from src.main import app
from src.auth.middleware import get_current_user, require_tenant_admin, CurrentUser

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _make_local_user(
    email: str = "user@example.com",
    role: str = "member",
    auth_source: str = "local",
):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        username=email.split("@")[0],
        email=email,
        is_active=True,
        hashed_password="$2b$12$fakehash",
        role=role,
        auth_source=auth_source,
        created_at=NOW,
    )


def _make_identity(
    email: str = "ldap-user@corp.com",
    source_backend: str = "ldap",
    display_name: str = "LDAP User",
    groups: list[str] | None = None,
) -> UserIdentity:
    return UserIdentity(
        email=email,
        display_name=display_name,
        groups=groups or [],
        source_backend=source_backend,
        raw_claims={},
    )


def _mock_chain(identity: UserIdentity | None):
    """Return a mock auth chain whose authenticate() returns *identity*."""
    chain = AsyncMock()
    chain.authenticate = AsyncMock(return_value=identity)
    return chain


def _mock_db_no_user():
    """Mock DB session where local_user lookup returns None (user not found)."""
    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=execute_result)
    db.add = MagicMock()
    db.commit = AsyncMock()

    async def _refresh(obj):
        obj.id = uuid.uuid4()
        obj.created_at = NOW

    db.refresh = _refresh
    return db


def _mock_db_with_user(user):
    """Mock DB session where local_user lookup returns an existing user."""
    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = user
    db.execute = AsyncMock(return_value=execute_result)
    db.add = MagicMock()
    db.flush = AsyncMock()
    return db


def _mock_db_with_setting(key: str, value_json: dict):
    """Mock DB for JIT with configured default role.

    Query order from jit_adopt_user + audit:
    1. User lookup → None
    2. Group role mapping lookup (scalars().all() path)
    3. JIT default role setting lookup → value_json
    4+ Audit level lookups
    """
    db = AsyncMock()
    user_result = MagicMock()
    user_result.scalar_one_or_none.return_value = None

    group_result = MagicMock()
    group_result.scalars.return_value.all.return_value = []

    setting_result = MagicMock()
    setting_result.scalar_one_or_none.return_value = value_json

    audit_result = MagicMock()
    audit_result.scalar_one_or_none.return_value = None

    db.execute = AsyncMock(side_effect=[user_result, setting_result, audit_result, audit_result])
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()

    async def _refresh(obj):
        obj.id = uuid.uuid4()
        obj.created_at = NOW

    db.refresh = _refresh
    return db


async def _yield(value):
    yield value


@pytest.fixture
async def anon_client():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# JIT adoption: LDAP user not in local_users
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_jit_ldap_user_created(anon_client):
    identity = _make_identity(email="new-ldap@corp.com", source_backend="ldap")
    db = _mock_db_no_user()

    with (
        patch("src.api.auth.get_auth_chain", return_value=_mock_chain(identity)),
        patch("src.api.auth.get_tenant_db", lambda tid: _yield(db)),
        patch("src.auth.jit.hash_password", return_value="$2b$12$jit_sentinel"),
    ):
        resp = await anon_client.post(
            "/api/v1/auth/login",
            json={"tenant_id": "acme", "email": "new-ldap@corp.com", "password": "ldap-pw"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "viewer"
    assert db.add.call_count >= 1
    created_user = db.add.call_args_list[0][0][0]
    assert created_user.email == "new-ldap@corp.com"
    assert created_user.auth_source == "ldap"
    assert created_user.role == "viewer"


# ---------------------------------------------------------------------------
# JIT adoption: GCP IAM user not in local_users
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_jit_gcp_iam_user_created(anon_client):
    identity = _make_identity(email="gcp-user@corp.com", source_backend="gcp_iam")
    db = _mock_db_no_user()

    with (
        patch("src.api.auth.get_auth_chain", return_value=_mock_chain(identity)),
        patch("src.api.auth.get_tenant_db", lambda tid: _yield(db)),
        patch("src.auth.jit.hash_password", return_value="$2b$12$jit_sentinel"),
    ):
        resp = await anon_client.post(
            "/api/v1/auth/login",
            json={"tenant_id": "acme", "email": "gcp-user@corp.com", "password": "gcp-token"},
        )

    assert resp.status_code == 200
    assert db.add.call_count >= 1
    created_user = db.add.call_args_list[0][0][0]
    assert created_user.auth_source == "gcp_iam"


# ---------------------------------------------------------------------------
# Existing user: no duplicate creation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_existing_user_no_duplicate(anon_client):
    existing = _make_local_user("existing@corp.com", role="analyst", auth_source="ldap")
    identity = _make_identity(email="existing@corp.com", source_backend="ldap")
    db = _mock_db_with_user(existing)

    with (
        patch("src.api.auth.get_auth_chain", return_value=_mock_chain(identity)),
        patch("src.api.auth.get_tenant_db", lambda tid: _yield(db)),
    ):
        resp = await anon_client.post(
            "/api/v1/auth/login",
            json={"tenant_id": "acme", "email": "existing@corp.com", "password": "ldap-pw"},
        )

    assert resp.status_code == 200
    assert resp.json()["role"] == "analyst"
    for call in db.add.call_args_list:
        assert not hasattr(call[0][0], "hashed_password"), "duplicate user created"


# ---------------------------------------------------------------------------
# JIT user gets configured default role
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_jit_uses_configured_default_role(anon_client):
    identity = _make_identity(email="analyst@corp.com", source_backend="ldap")
    db = _mock_db_with_setting("jit_default_role", {"value": "analyst"})

    with (
        patch("src.api.auth.get_auth_chain", return_value=_mock_chain(identity)),
        patch("src.api.auth.get_tenant_db", lambda tid: _yield(db)),
        patch("src.auth.jit.hash_password", return_value="$2b$12$jit_sentinel"),
    ):
        resp = await anon_client.post(
            "/api/v1/auth/login",
            json={"tenant_id": "acme", "email": "analyst@corp.com", "password": "pw"},
        )

    assert resp.status_code == 200
    # F-021-12: ``analyst`` is a legacy alias and normalizes to viewer (the
    # centralized JIT default-role taxonomy is viewer-equivalent / fail-safe).
    assert resp.json()["role"] == "viewer"
    created_user = db.add.call_args_list[0][0][0]
    assert created_user.role == "viewer"


@pytest.mark.asyncio
async def test_jit_member_default_normalizes_to_viewer(anon_client):
    identity = _make_identity(email="member-default@corp.com", source_backend="ldap")
    db = _mock_db_with_setting("jit_default_role", {"value": "member"})

    with (
        patch("src.api.auth.get_auth_chain", return_value=_mock_chain(identity)),
        patch("src.api.auth.get_tenant_db", lambda tid: _yield(db)),
        patch("src.auth.jit.hash_password", return_value="$2b$12$jit_sentinel"),
    ):
        resp = await anon_client.post(
            "/api/v1/auth/login",
            json={"tenant_id": "acme", "email": "member-default@corp.com", "password": "pw"},
        )

    assert resp.status_code == 200
    assert resp.json()["role"] == "viewer"
    created_user = db.add.call_args_list[0][0][0]
    assert created_user.role == "viewer"


# ---------------------------------------------------------------------------
# JIT user cannot change password
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_jit_user_password_reset_blocked():
    ldap_user = _make_local_user("ldap@corp.com", auth_source="ldap")
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=ldap_user)

    app.dependency_overrides[require_tenant_admin] = lambda: CurrentUser(
        user_id="admin@corp.com", tenant_id="acme",
        email="admin@corp.com", role="tenant_admin",
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.auth.get_tenant_db", lambda tid: _yield(mock_db)):
                resp = await ac.post(
                    f"/api/v1/auth/users/{ldap_user.id}/reset-password",
                    json={"password": "new-password"},
                )
    finally:
        app.dependency_overrides.pop(require_tenant_admin, None)

    assert resp.status_code == 400
    assert "identity provider" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Local user unaffected by JIT logic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_local_user_password_reset_allowed():
    local_user = _make_local_user("local@corp.com", auth_source="local")
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=local_user)
    mock_db.commit = AsyncMock()
    mock_db.add = MagicMock()
    mock_db.flush = AsyncMock()

    async def _refresh(obj):
        pass

    mock_db.refresh = _refresh

    app.dependency_overrides[require_tenant_admin] = lambda: CurrentUser(
        user_id="admin@corp.com", tenant_id="acme",
        email="admin@corp.com", role="tenant_admin",
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with (
                patch("src.api.auth.get_tenant_db", lambda tid: _yield(mock_db)),
                patch("src.api.auth.hash_password", return_value="$2b$12$newhash"),
            ):
                resp = await ac.post(
                    f"/api/v1/auth/users/{local_user.id}/reset-password",
                    json={"password": "new-password"},
                )
    finally:
        app.dependency_overrides.pop(require_tenant_admin, None)

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# F-021-12-01: group-binding rank table must derive from the shared hierarchy
# ---------------------------------------------------------------------------

def test_role_rank_derives_from_shared_hierarchy():
    """``jit._ROLE_RANK`` must not drift from the centralised role hierarchy.

    The group-binding "best role" selector ranks roles higher-is-better; the
    RBAC ``require_role`` path ranks lower-is-more-privileged via
    ``project_role_level``. These are two orderings of the SAME vocabulary, so
    if they ever disagree on relative privilege a tenant-admin group could
    out-rank — or be out-ranked by — a viewer group incorrectly. This pins the
    derivation so a future change to ``PROJECT_ROLE_HIERARCHY`` fails loudly
    here instead of silently diverging.
    """
    from shared.auth.roles import PROJECT_ROLE_HIERARCHY, project_role_level
    from src.auth.jit import _ROLE_RANK

    # 1. Every hierarchy role has a rank; no stray/extra roles in the table.
    assert set(_ROLE_RANK) == set(PROJECT_ROLE_HIERARCHY)

    # 2. Concrete ordering: admin > modeler > viewer (higher-is-better).
    assert _ROLE_RANK["admin"] > _ROLE_RANK["modeler"] > _ROLE_RANK["viewer"]

    # 3. The rank ordering is the inverse of the privilege-level ordering, so
    #    the two tables can never disagree on relative privilege. A more
    #    privileged role (lower level) must always carry a higher rank.
    for a in PROJECT_ROLE_HIERARCHY:
        for b in PROJECT_ROLE_HIERARCHY:
            more_privileged = project_role_level(a) < project_role_level(b)
            higher_rank = _ROLE_RANK[a] > _ROLE_RANK[b]
            assert more_privileged == higher_rank, (
                f"_ROLE_RANK ordering diverged from PROJECT_ROLE_HIERARCHY "
                f"for ({a}, {b})"
            )

    # 4. Unknown roles fall to the least privilege (0), below any real tier.
    assert _ROLE_RANK.get("not-a-role", 0) == 0
    assert all(rank > 0 for rank in _ROLE_RANK.values())
