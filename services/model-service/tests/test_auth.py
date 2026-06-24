"""
Unit tests for auth routes: POST /api/v1/auth/login, POST /api/v1/auth/users.

Strategy:
  - get_auth_chain() is patched to return a mock chain for login tests.
  - Token decoding validated by calling decode_access_token on the Set-Cookie.
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
from src.auth.local_backend import decode_access_token
from src.auth.middleware import get_current_user, require_tenant_admin, CurrentUser

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _make_local_user(
    email: str = "user@example.com",
    is_active: bool = True,
    role: str = "member",
    auth_source: str = "local",
):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        username="testuser",
        email=email,
        is_active=is_active,
        hashed_password="$2b$12$fakehash",
        role=role,
        auth_source=auth_source,
        created_at=NOW,
    )


def _admin_user() -> CurrentUser:
    return CurrentUser(
        user_id="admin@example.com",
        tenant_id="acme",
        email="admin@example.com",
        role="tenant_admin",
    )


@pytest.fixture
async def anon_client():
    """Client with no auth override (login endpoints don't require auth)."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_login_success(anon_client):
    user = _make_local_user()
    identity = UserIdentity(
        email=user.email, display_name="Test User",
        source_backend="local", raw_claims={"role": "member"},
    )
    mock_db = _mock_db_with_user(user)
    with (
        patch("src.api.auth.get_auth_chain", return_value=_mock_chain(identity)),
        patch("src.api.auth.get_tenant_db", lambda tid: _yield(mock_db)),
    ):
        resp = await anon_client.post(
            "/api/v1/auth/login",
            json={"tenant_id": "acme", "email": user.email, "password": "secret"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "member"
    token = resp.cookies.get("access_token")
    assert token, "access_token cookie must be set"
    payload = decode_access_token(token)
    assert payload["sub"] == user.email
    assert payload["tenant_id"] == "acme"
    assert payload["role"] == "member"


@pytest.mark.asyncio
async def test_login_wrong_password(anon_client):
    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    mock_db.flush = AsyncMock()
    mock_db.commit = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = {"value": "info"}
    mock_db.execute = AsyncMock(return_value=result)

    with (
        patch("src.api.auth.get_auth_chain", return_value=_mock_chain(None)),
        patch("src.api.auth.get_tenant_db", lambda tid: _yield(mock_db)),
    ):
        resp = await anon_client.post(
            "/api/v1/auth/login",
            json={"tenant_id": "acme", "email": "x@x.com", "password": "wrong"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_inactive_user(anon_client):
    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    mock_db.flush = AsyncMock()
    mock_db.commit = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = {"value": "info"}
    mock_db.execute = AsyncMock(return_value=result)

    with (
        patch("src.api.auth.get_auth_chain", return_value=_mock_chain(None)),
        patch("src.api.auth.get_tenant_db", lambda tid: _yield(mock_db)),
    ):
        resp = await anon_client.post(
            "/api/v1/auth/login",
            json={"tenant_id": "acme", "email": "inactive@x.com", "password": "pw"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_unknown_tenant_returns_401(anon_client):
    identity = UserIdentity(
        email="x@x.com", source_backend="local", raw_claims={},
    )

    async def _raise_unknown_tenant():
        raise ValueError("Tenant not found")
        yield  # pragma: no cover

    with (
        patch("src.api.auth.get_auth_chain", return_value=_mock_chain(identity)),
        patch("src.api.auth.get_tenant_db", lambda tid: _raise_unknown_tenant()),
    ):
        resp = await anon_client.post(
            "/api/v1/auth/login",
            json={"tenant_id": "missing", "email": "x@x.com", "password": "pw"},
        )

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid tenant or credentials"


# ---------------------------------------------------------------------------
# Create user
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_user_success():
    new_user = _make_local_user("new@example.com")
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=_scalar(None))
    mock_db.add = lambda x: None
    mock_db.commit = AsyncMock()

    async def _refresh(obj):
        obj.id = new_user.id
        obj.created_at = NOW
        if not hasattr(obj, "auth_source"):
            obj.auth_source = "local"

    mock_db.refresh = _refresh
    app.dependency_overrides[require_tenant_admin] = _admin_user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with (
                patch("src.api.auth.get_tenant_db", lambda tid: _yield(mock_db)),
                patch("src.api.auth.hash_password", return_value="$2b$12$fakehash"),
            ):
                resp = await ac.post(
                    "/api/v1/auth/users",
                    json={"username": "newuser", "email": "new@example.com", "password": "pw123"},
                )
    finally:
        app.dependency_overrides.pop(require_tenant_admin, None)

    assert resp.status_code == 201
    assert resp.json()["email"] == "new@example.com"
    assert resp.json()["role"] == "member"
    assert resp.json()["auth_source"] == "local"


@pytest.mark.asyncio
async def test_create_user_duplicate_email():
    existing = _make_local_user("dup@example.com")
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=_scalar(existing))
    app.dependency_overrides[require_tenant_admin] = _admin_user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.auth.get_tenant_db", lambda tid: _yield(mock_db)):
                resp = await ac.post(
                    "/api/v1/auth/users",
                    json={"username": "u", "email": "dup@example.com", "password": "pw"},
                )
    finally:
        app.dependency_overrides.pop(require_tenant_admin, None)

    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Token content
# ---------------------------------------------------------------------------

def test_token_contains_email_and_tenant():
    from src.auth.local_backend import create_access_token
    token = create_access_token(sub="alice@co.com", tenant_id="acme")
    payload = decode_access_token(token)
    assert payload["sub"] == "alice@co.com"
    assert payload["tenant_id"] == "acme"
    assert "exp" in payload


def test_token_carries_role_when_provided():
    from src.auth.local_backend import create_access_token
    token = create_access_token(
        sub="admin@co.com", tenant_id="acme", role="tenant_admin"
    )
    payload = decode_access_token(token)
    assert payload["role"] == "tenant_admin"


def test_token_carries_groups_and_claims_to_current_user():
    """F-007-02: IdP groups + claims survive issuance → decode →
    CurrentUser → Principal, so claim/scope row-security rules can be
    enforced at query time."""
    from shared.auth.middleware import _build_user_from_payload
    from shared.security import Principal
    from src.auth.local_backend import create_access_token

    token = create_access_token(
        sub="alice@co.com",
        tenant_id="acme",
        role="member",
        groups=["finance-team"],
        claims={"department": "sales-emea", "scope": "openid reports:read"},
    )
    payload = decode_access_token(token)
    user = _build_user_from_payload(payload)
    assert user.groups == ["finance-team"]
    assert user.claims == {
        "department": "sales-emea", "scope": "openid reports:read",
    }

    principal = Principal.from_current_user(user)
    assert principal.groups == frozenset({"finance-team"})
    assert principal.claims["department"] == "sales-emea"


def test_token_without_claims_yields_empty_claims():
    """Tokens issued before the claims field existed must decode cleanly."""
    from shared.auth.middleware import _build_user_from_payload
    from src.auth.local_backend import create_access_token

    token = create_access_token(sub="bob@co.com", tenant_id="acme", role="member")
    user = _build_user_from_payload(decode_access_token(token))
    assert user.claims == {}


@pytest.mark.asyncio
async def test_create_user_rejected_for_member():
    """A plain member without the tenant_admin role must get 403 from POST /users."""
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id="member@example.com",
        tenant_id="acme",
        email="member@example.com",
        role="member",
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            resp = await ac.post(
                "/api/v1/auth/users",
                json={"username": "x", "email": "x@x.com", "password": "pw"},
            )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_login_returns_tenant_admin_role():
    """Login by a tenant_admin user must include role=tenant_admin in the JWT."""
    user = _make_local_user("admin@example.com", role="tenant_admin")
    identity = UserIdentity(
        email=user.email, display_name="Admin",
        source_backend="local", raw_claims={"role": "member"},
    )
    mock_db = _mock_db_with_user(user)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        with (
            patch("src.api.auth.get_auth_chain", return_value=_mock_chain(identity)),
            patch("src.api.auth.get_tenant_db", lambda tid: _yield(mock_db)),
        ):
            resp = await ac.post(
                "/api/v1/auth/login",
                json={"tenant_id": "acme", "email": user.email, "password": "secret"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "tenant_admin"
    token = resp.cookies.get("access_token")
    assert token, "access_token cookie must be set"
    payload = decode_access_token(token)
    assert payload["role"] == "tenant_admin"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _yield(value):
    yield value


def _mock_chain(identity):
    chain = AsyncMock()
    chain.authenticate = AsyncMock(return_value=identity)
    return chain


def _mock_db_with_user(user):
    db = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    db.execute = AsyncMock(return_value=result)
    return db


class _scalar:
    """Minimal mock for db.execute(...).scalar_one_or_none()."""
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


@pytest.mark.asyncio
async def test_create_user_with_model_technical_role():
    """H-1 (B1 deep review): the model_technical audience role is grantable
    through the local-user API, so a regular user can be designated a
    technical-view holder."""
    new_user = _make_local_user("engineer@example.com", role="model_technical")
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=_scalar(None))
    mock_db.add = lambda x: None
    mock_db.commit = AsyncMock()

    async def _refresh(obj):
        obj.id = new_user.id
        obj.created_at = NOW
        if not hasattr(obj, "auth_source"):
            obj.auth_source = "local"

    mock_db.refresh = _refresh
    app.dependency_overrides[require_tenant_admin] = _admin_user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with (
                patch("src.api.auth.get_tenant_db", lambda tid: _yield(mock_db)),
                patch("src.api.auth.hash_password", return_value="$2b$12$fakehash"),
            ):
                resp = await ac.post(
                    "/api/v1/auth/users",
                    json={
                        "username": "engineer",
                        "email": "engineer@example.com",
                        "password": "pw123",
                        "role": "model_technical",
                    },
                )
    finally:
        app.dependency_overrides.pop(require_tenant_admin, None)

    assert resp.status_code == 201
    assert resp.json()["role"] == "model_technical"


@pytest.mark.asyncio
async def test_create_user_with_unknown_role_rejected():
    """The role vocabulary stays closed: anything outside the shared
    allowed set is a 422."""
    app.dependency_overrides[require_tenant_admin] = _admin_user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            resp = await ac.post(
                "/api/v1/auth/users",
                json={
                    "username": "x",
                    "email": "x@example.com",
                    "password": "pw123",
                    "role": "superuser",
                },
            )
    finally:
        app.dependency_overrides.pop(require_tenant_admin, None)

    assert resp.status_code == 422
