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
from .result_fakes import FakeScalarResult

from shared.auth.backend import UserIdentity
from src.main import app
from src.auth.local_backend import decode_access_token
from src.auth.middleware import (
    CurrentEmbedUser,
    CurrentServiceUser,
    CurrentUser,
    get_current_user,
    require_tenant_admin,
)

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
        role_source="manual",
        token_version=0,
        created_at=NOW,
    )


def _admin_user() -> CurrentUser:
    return CurrentUser(
        user_id="admin@example.com",
        tenant_id="acme",
        email="admin@example.com",
        role="tenant_admin",
    )


def _system_admin_user() -> CurrentUser:
    return CurrentUser(
        user_id="admin@tessallite.local",
        tenant_id="__system__",
        email="admin@tessallite.local",
        role="system_admin",
    )


def test_resolve_target_tenant_requires_canonical_human_system_admin():
    from src.api.auth import _resolve_target_tenant_id

    assert _resolve_target_tenant_id(_system_admin_user(), "other") == "other"

    real_tenant_spoof = CurrentUser(
        user_id="spoof@example.com",
        tenant_id="acme",
        email="spoof@example.com",
        role="system_admin",
    )
    service = CurrentServiceUser(
        principal="data-quality-validator",
        tenant_id="acme",
        role="system_admin",
        scopes=["query-router.data-quality"],
    )
    embed = CurrentEmbedUser(
        user_id="embed@example.com",
        tenant_id="acme",
        email="embed@example.com",
    )

    for user in (real_tenant_spoof, service, embed):
        with pytest.raises(Exception) as exc:
            _resolve_target_tenant_id(user, "other")
        assert getattr(exc.value, "status_code", None) == 403


@pytest.fixture
async def anon_client():
    """Client with no auth override (login endpoints don't require auth)."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac


def _mock_system_db():
    """In-memory stand-in so G-021-04 lockout does not open Postgres."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.add = MagicMock()
    db.delete = AsyncMock()
    db.flush = AsyncMock()
    return db


@pytest.fixture(autouse=True)
def _lockout_system_db():
    db = _mock_system_db()
    with patch("src.api.auth.get_system_db", lambda: _yield(db)):
        yield


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_system_login_mints_system_admin_with_default_modeler_role():
    from shared.schemas.domains.auth import SystemLoginRequest
    from src.api.auth import system_login

    body = SystemLoginRequest(email="admin@example.com", password="secret")
    request = MagicMock()
    request.client.host = "127.0.0.1"
    request.headers = {}
    with (
        patch("src.api.auth.authenticate_system_admin", return_value=True),
        patch("src.api.auth.create_access_token", return_value="signed") as mint,
        patch("src.api.auth.get_system_admin_token_version", AsyncMock(return_value=0)),
        patch("src.api.auth.system_audit", AsyncMock()),
        patch("src.api.auth.assert_not_locked", AsyncMock()),
        patch("src.api.auth.record_login_success", AsyncMock()),
    ):
        response = await system_login(body, request)

    assert response.status_code == 200
    assert mint.call_args.kwargs["role"] == "system_admin"
    assert mint.call_args.kwargs["roles"] == ["system_admin", "modeler"]

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
async def test_logout_invalidates_bearer_jwt(anon_client):
    """F-021-01: logout then Bearer GET /users/me is 401 (cookie clear is not enough)."""
    user = _make_local_user()
    identity = UserIdentity(
        email=user.email, display_name="Test User",
        source_backend="local", raw_claims={"role": "member"},
    )
    mock_db = _mock_db_with_user(user)

    async def _bump(_db, local_user):
        local_user.token_version = int(getattr(local_user, "token_version", 0)) + 1
        return local_user.token_version

    with (
        patch("src.api.auth.get_auth_chain", return_value=_mock_chain(identity)),
        patch("src.api.auth.get_tenant_db", lambda tid: _yield(mock_db)),
        patch("src.api.auth.bump_local_user_token_version", _bump),
        patch("src.api.auth.audit_required", AsyncMock()),
        patch("shared.auth.middleware.get_tenant_db", lambda tid: _yield(mock_db)),
    ):
        login = await anon_client.post(
            "/api/v1/auth/login",
            json={"tenant_id": "acme", "email": user.email, "password": "secret"},
        )
        assert login.status_code == 200
        token = login.cookies.get("access_token") or login.json().get("access_token")
        assert token
        logout = await anon_client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert logout.status_code == 200
        me = await anon_client.get(
            "/api/v1/auth/users/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me.status_code == 401


@pytest.mark.asyncio
async def test_login_uses_canonical_local_user_email_as_subject(anon_client):
    # F-021-01: the existing row must have auth_source="ldap" so this is a
    # legitimate SAME-provider returning user (a mixed-case LDAP login resolving
    # to its existing lowercased record) rather than an LDAP identity taking
    # over a local account. The test's intent — canonical email is the JWT
    # subject — is preserved; only the provenance is aligned to be valid.
    user = _make_local_user(email="user@example.com", auth_source="ldap")
    identity = UserIdentity(
        email="User@Example.com",
        display_name="LDAP User",
        groups=["tenant-users"],
        source_backend="ldap",
        raw_claims={"ldap_dn": "cn=user"},
    )
    mock_db = _mock_db_with_user(user)
    with (
        patch("src.api.auth.get_auth_chain", return_value=_mock_chain(identity)),
        patch("src.api.auth.get_tenant_db", lambda tid: _yield(mock_db)),
    ):
        resp = await anon_client.post(
            "/api/v1/auth/login",
            json={"tenant_id": "acme", "email": "User@Example.com", "password": "secret"},
        )
    assert resp.status_code == 200
    payload = decode_access_token(resp.cookies.get("access_token"))
    assert payload["sub"] == "user@example.com"


@pytest.mark.asyncio
async def test_login_with_mixed_case_local_user_mints_canonical_subject(anon_client):
    user = _make_local_user(email="user@example.com")
    identity = UserIdentity(
        email="USER@EXAMPLE.COM",
        display_name="User",
        source_backend="local",
        raw_claims={},
    )
    mock_db = _mock_db_with_user(user)
    with (
        patch("src.api.auth.get_auth_chain", return_value=_mock_chain(identity)),
        patch("src.api.auth.get_tenant_db", lambda tid: _yield(mock_db)),
    ):
        resp = await anon_client.post(
            "/api/v1/auth/login",
            json={"tenant_id": "acme", "email": "USER@EXAMPLE.COM", "password": "secret"},
        )
    assert resp.status_code == 200
    payload = decode_access_token(resp.cookies.get("access_token"))
    assert payload["sub"] == "user@example.com"


@pytest.mark.asyncio
async def test_external_login_denies_unadmitted_identity(anon_client):
    identity = UserIdentity(
        email="intruder@example.com",
        display_name="Intruder",
        groups=[],
        source_backend="ldap",
        raw_claims={},
    )
    mock_db = AsyncMock()
    no_row = MagicMock()
    no_row.scalar_one_or_none.return_value = None
    no_row.first.return_value = None
    mock_db.execute = AsyncMock(return_value=no_row)
    with (
        patch("src.api.auth.get_auth_chain", return_value=_mock_chain(identity)),
        patch("src.api.auth.get_tenant_db", lambda tid: _yield(mock_db)),
    ):
        resp = await anon_client.post(
            "/api/v1/auth/login",
            json={"tenant_id": "victim", "email": "intruder@example.com", "password": "secret"},
        )
    assert resp.status_code == 403


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
                    json={"username": "newuser", "email": "new@example.com", "password": "Password12345"},
                )
    finally:
        app.dependency_overrides.pop(require_tenant_admin, None)

    assert resp.status_code == 201
    assert resp.json()["email"] == "new@example.com"
    assert resp.json()["role"] == "member"
    assert resp.json()["auth_source"] == "local"


@pytest.mark.asyncio
async def test_create_user_normalizes_mixed_case_email():
    created = {}
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=_scalar(None))
    mock_db.add = lambda obj: created.setdefault("user", obj)
    mock_db.commit = AsyncMock()

    async def _refresh(obj):
        obj.id = uuid.uuid4()
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
                    json={"username": "mixed", "email": "Mixed@Example.COM", "password": "Password12345"},
                )
    finally:
        app.dependency_overrides.pop(require_tenant_admin, None)

    assert resp.status_code == 201
    assert resp.json()["email"] == "mixed@example.com"
    assert created["user"].email == "mixed@example.com"


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
                    json={"username": "u", "email": "dup@example.com", "password": "Password12345"},
                )
    finally:
        app.dependency_overrides.pop(require_tenant_admin, None)

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_create_user_duplicate_email_is_case_insensitive():
    existing = _make_local_user("existing@example.com")
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
                    json={"username": "u", "email": "Existing@Example.COM", "password": "Password12345"},
                )
    finally:
        app.dependency_overrides.pop(require_tenant_admin, None)

    assert resp.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user",
    [
        CurrentUser(
            user_id="spoof@example.com",
            tenant_id="acme",
            email="spoof@example.com",
            role="system_admin",
        ),
        CurrentServiceUser(
            principal="data-quality-validator",
            tenant_id="acme",
            role="system_admin",
            scopes=["query-router.data-quality"],
        ),
        CurrentEmbedUser(
            user_id="embed@example.com",
            tenant_id="acme",
            email="embed@example.com",
        ),
    ],
)
async def test_user_management_route_rejects_noncanonical_cross_tenant(user):
    app.dependency_overrides[require_tenant_admin] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            resp = await ac.get("/api/v1/auth/users?tenant_id=other")
    finally:
        app.dependency_overrides.pop(require_tenant_admin, None)

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_user_management_route_allows_canonical_system_admin_cross_tenant():
    user = _make_local_user("tenant@example.com")
    mock_db = AsyncMock()
    rows = MagicMock()
    rows.scalars.return_value.all.return_value = [user]
    mock_db.execute = AsyncMock(return_value=rows)
    app.dependency_overrides[require_tenant_admin] = _system_admin_user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.auth.get_tenant_db", lambda tid: _yield(mock_db)):
                resp = await ac.get("/api/v1/auth/users?tenant_id=other")
    finally:
        app.dependency_overrides.pop(require_tenant_admin, None)

    assert resp.status_code == 200
    assert resp.json()[0]["email"] == "tenant@example.com"


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
                json={"username": "x", "email": "x@x.com", "password": "Password12345"},
            )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_reset_password_bumps_token_version():
    user = _make_local_user("reset@example.com")
    user.token_version = 0
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=user)
    mock_db.commit = AsyncMock()
    mock_db.refresh = AsyncMock()
    mock_db.add = MagicMock()
    audit_level = MagicMock()
    audit_level.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(side_effect=[_scalar(1), audit_level])

    app.dependency_overrides[require_tenant_admin] = _admin_user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with (
                patch("src.api.auth.get_tenant_db", lambda tid: _yield(mock_db)),
                patch("src.api.auth.hash_password", return_value="$2b$12$newhash"),
            ):
                resp = await ac.post(
                    f"/api/v1/auth/users/{user.id}/reset-password",
                    json={"password": "NewPass12345"},
                )
    finally:
        app.dependency_overrides.pop(require_tenant_admin, None)

    assert resp.status_code == 200
    assert user.token_version == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload, expected_field, expected_value",
    [
        ({"role": "model_technical"}, "role", "model_technical"),
        ({"is_active": False}, "is_active", False),
        ({"email": "renamed@example.com"}, "email", "renamed@example.com"),
    ],
)
async def test_update_user_security_fields_bump_token_version(
    payload,
    expected_field,
    expected_value,
):
    user = _make_local_user("target@example.com")
    admin_id = uuid.uuid4()
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=user)
    mock_db.commit = AsyncMock()
    mock_db.refresh = AsyncMock()
    mock_db.add = MagicMock()
    audit_level = _scalar(None)

    execute_results = []
    if "email" in payload:
        execute_results.append(_scalar(None))
        execute_results.append(_scalars([]))
    if "role" in payload or "is_active" in payload:
        execute_results.append(
            _rows(
                [
                    (user.id, user.role, user.is_active),
                    (admin_id, "tenant_admin", True),
                ]
            )
        )
    execute_results.extend([_scalar(1), audit_level])
    mock_db.execute = AsyncMock(side_effect=execute_results)

    app.dependency_overrides[require_tenant_admin] = _admin_user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.auth.get_tenant_db", lambda tid: _yield(mock_db)):
                resp = await ac.patch(
                    f"/api/v1/auth/users/{user.id}",
                    json=payload,
                )
    finally:
        app.dependency_overrides.pop(require_tenant_admin, None)

    assert resp.status_code == 200
    assert getattr(user, expected_field) == expected_value
    assert user.token_version == 1
    assert resp.json()["token_version"] == 1


@pytest.mark.asyncio
async def test_update_user_non_security_field_does_not_bump_token_version():
    user = _make_local_user("target@example.com")
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=user)
    mock_db.commit = AsyncMock()
    mock_db.refresh = AsyncMock()
    mock_db.add = MagicMock()
    mock_db.execute = AsyncMock(side_effect=[_scalar(None), _scalar(None)])

    app.dependency_overrides[require_tenant_admin] = _admin_user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.auth.get_tenant_db", lambda tid: _yield(mock_db)):
                resp = await ac.patch(
                    f"/api/v1/auth/users/{user.id}",
                    json={"username": "newname"},
                )
    finally:
        app.dependency_overrides.pop(require_tenant_admin, None)

    assert resp.status_code == 200
    assert user.username == "newname"
    assert user.token_version == 0
    assert resp.json()["token_version"] == 0


@pytest.mark.asyncio
async def test_update_user_email_canonicalizes_and_preserves_bindings():
    user = _make_local_user("old@example.com")
    binding = types.SimpleNamespace(user_identity="Old@Example.COM")
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=user)
    mock_db.commit = AsyncMock()
    mock_db.refresh = AsyncMock()
    mock_db.add = MagicMock()
    mock_db.execute = AsyncMock(
        side_effect=[
            _scalar(None),
            _scalars([binding]),
            _scalar(1),
            _scalar(None),
        ]
    )

    app.dependency_overrides[require_tenant_admin] = _admin_user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.auth.get_tenant_db", lambda tid: _yield(mock_db)):
                resp = await ac.patch(
                    f"/api/v1/auth/users/{user.id}",
                    json={"email": "New@Example.COM"},
                )
    finally:
        app.dependency_overrides.pop(require_tenant_admin, None)

    assert resp.status_code == 200
    assert user.email == "new@example.com"
    assert binding.user_identity == "new@example.com"
    assert user.token_version == 1


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

    def first(self):
        return self._value


class _rows:
    """Minimal mock for db.execute(...).all()."""
    def __init__(self, values):
        self._values = values

    def all(self):
        return self._values


class _scalars:
    """Minimal mock for db.execute(...).scalars().all()."""
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return FakeScalarResult(self._values)

    def all(self):
        return self._values


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
                        "password": "Password12345",
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
                    "password": "Password12345",
                    "role": "superuser",
                },
            )
    finally:
        app.dependency_overrides.pop(require_tenant_admin, None)

    assert resp.status_code == 422
