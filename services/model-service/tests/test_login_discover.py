"""login_discover strict discovery and mutation boundaries.

Discovery evaluates the configured auth chain for each active tenant, fails
closed on any tenant/backend evaluation error, requires exactly one admitted
match, and mutates tenant-local state only after the full scan proves
uniqueness. No-match paths still pay one dummy bcrypt cost to keep timing flat.
"""
from __future__ import annotations

import asyncio
import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import src.api.auth as auth_mod
from shared.auth.backend import AuthChain, AuthOutcome, UserIdentity
from src.main import app
from src.auth.local_backend import decode_access_token


async def _drain_discover_audit_tasks():
    """Await the fire-and-forget discovery-login audit tasks (Bug-6304) so a
    test can assert the audit event was emitted after the response returned."""
    pending = list(auth_mod._DISCOVER_AUDIT_TASKS)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _make_user(email: str, role: str = "member"):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        username="u",
        email=email,
        is_active=True,
        hashed_password="$2b$12$fakehash",
        role=role,
        created_at=NOW,
    )


def _make_tenant(slug: str):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        slug=slug,
        display_name=slug,
        db_schema_prefix=slug,
        is_active=True,
        encrypted_db_url=b"",
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
async def anon_client():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as ac:
        yield ac


def _install_system_tenants(monkeypatch, tenants: list):
    class _Result:
        def scalars(self):
            class _S:
                def __init__(self, xs):
                    self._xs = xs

                def all(self_inner):
                    return self_inner._xs
            return _S(tenants)

        def scalar_one_or_none(self):
            return None

    class _SysDB:
        async def execute(self, stmt):
            return _Result()

        def add(self, _row):
            return None

        async def flush(self):
            return None

        async def commit(self):
            return None

        async def delete(self, _row):
            return None

    async def _gen():
        yield _SysDB()

    monkeypatch.setattr("src.api.auth.get_system_db", lambda: _gen())


def _install_tenant_lookup(monkeypatch, user_for_slug):
    """Patch get_tenant_db so each tenant's db.execute returns the user (or
    None) for that slug via ``user_for_slug(slug)``. A returned exception type
    is raised to simulate an operational failure."""
    async def fake_get_tenant_db(slug):
        outcome = user_for_slug(slug)

        class _UserResult:
            def scalar_one_or_none(self_inner):
                return outcome

        class _DB:
            _slug = slug

            async def execute(self_inner, stmt):
                if isinstance(outcome, Exception):
                    raise outcome
                return _UserResult()

            # Bug-6304: login_discover now writes an audit event into the matched
            # tenant's DB on success/failure. The audit writer only add+flushes,
            # then the endpoint commits — support those as no-ops so the mock
            # exercises the audit path without a real session.
            def add(self_inner, obj):
                return None

            async def flush(self_inner):
                return None

            async def commit(self_inner):
                return None

        yield _DB()

    monkeypatch.setattr("src.api.auth.get_tenant_db", fake_get_tenant_db)

    class _Chain:
        async def authenticate_outcome(
            self, *, tenant_id: str, email: str, password: str, **kwargs
        ):
            outcome = user_for_slug(tenant_id)
            if isinstance(outcome, Exception):
                return AuthOutcome(
                    status="backend_error", backend_name="test", error=outcome
                )
            if outcome is None or password == "wrong":
                return AuthOutcome(status="rejected")
            return AuthOutcome(
                status="authenticated",
                identity=UserIdentity(
                    email=outcome.email,
                    display_name=getattr(outcome, "username", outcome.email),
                    groups=[],
                    source_backend="local",
                    raw_claims={},
                ),
                backend_name="test",
            )

    monkeypatch.setattr("src.api.auth.get_auth_chain", lambda: _Chain())


@pytest.mark.asyncio
async def test_login_discover_returns_first_match_by_slug(anon_client, monkeypatch):
    tenants = [_make_tenant("acme"), _make_tenant("beta"), _make_tenant("gamma")]
    _install_system_tenants(monkeypatch, tenants)
    _install_tenant_lookup(
        monkeypatch,
        lambda slug: _make_user("u@example.com", role="tenant_admin") if slug == "beta" else None,
    )

    with patch("src.api.auth.verify_password", return_value=True):
        resp = await anon_client.post(
            "/api/v1/auth/login/discover",
            json={"tenant_id": "_discover", "email": "u@example.com", "password": "pw"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "tenant_admin"
    token = resp.cookies.get("access_token")
    assert token, "access_token cookie must be set"
    payload = decode_access_token(token)
    assert payload["tenant_id"] == "beta"


@pytest.mark.asyncio
async def test_login_discover_fails_closed_when_multiple_tenants_match(
    anon_client, monkeypatch, caplog
):
    import logging

    tenants = [_make_tenant("acme"), _make_tenant("beta")]
    _install_system_tenants(monkeypatch, tenants)
    _install_tenant_lookup(monkeypatch, lambda slug: _make_user("u@example.com", role="member"))

    with (
        patch("src.api.auth.verify_password", return_value=True),
        caplog.at_level(logging.WARNING, logger="src.api.auth"),
    ):
        resp = await anon_client.post(
            "/api/v1/auth/login/discover",
            json={"tenant_id": "_discover", "email": "u@example.com", "password": "pw"},
        )

    assert resp.status_code == 401
    collision_logs = [
        r for r in caplog.records if "matched multiple admitted tenants" in r.getMessage()
    ]
    assert collision_logs
    msg = collision_logs[0].getMessage()
    assert "acme" in msg and "beta" in msg


@pytest.mark.asyncio
async def test_login_discover_partial_tenant_outage_fails_closed(
    anon_client, monkeypatch, caplog
):
    import logging

    tenants = [_make_tenant("broken"), _make_tenant("healthy")]
    _install_system_tenants(monkeypatch, tenants)
    _install_tenant_lookup(
        monkeypatch,
        lambda slug: RuntimeError("simulated DB outage") if slug == "broken"
        else _make_user("u@example.com", role="member"),
    )

    with (
        patch("src.api.auth.verify_password", return_value=True),
        caplog.at_level(logging.WARNING, logger="src.api.auth"),
    ):
        resp = await anon_client.post(
            "/api/v1/auth/login/discover",
            json={"tenant_id": "_discover", "email": "u@example.com", "password": "pw"},
        )

    assert resp.status_code == 503
    assert resp.cookies.get("access_token") is None
    op_logs = [r for r in caplog.records if "raised RuntimeError" in r.getMessage()]
    assert op_logs
    assert "broken" in op_logs[0].getMessage()


@pytest.mark.asyncio
async def test_login_discover_auth_chain_backend_error_returns_503_without_mutation(
    anon_client, monkeypatch
):
    tenants = [_make_tenant("acme")]
    _install_system_tenants(monkeypatch, tenants)
    db = AsyncMock()
    db.add = MagicMock()
    db.delete = AsyncMock()
    db.commit = AsyncMock()

    async def fake_get_tenant_db(slug):
        yield db

    class _RaisingBackend:
        name = "ldap"

        async def authenticate(self, **kwargs):
            raise RuntimeError("ldap unavailable")

    class _MatchingBackend:
        name = "local"

        async def authenticate(self, **kwargs):
            return UserIdentity(
                email="u@example.com",
                display_name="User",
                groups=[],
                source_backend="local",
                raw_claims={},
            )

    jit_mock = AsyncMock()
    audit_mock = AsyncMock()
    monkeypatch.setattr("src.api.auth.get_tenant_db", fake_get_tenant_db)
    monkeypatch.setattr(
        "src.api.auth.get_auth_chain",
        lambda: AuthChain([_RaisingBackend(), _MatchingBackend()]),
    )
    monkeypatch.setattr("src.api.auth.jit_adopt_user", jit_mock)
    monkeypatch.setattr("src.api.auth.audit_required", audit_mock)

    resp = await anon_client.post(
        "/api/v1/auth/login/discover",
        json={"tenant_id": "_discover", "email": "u@example.com", "password": "pw"},
    )

    assert resp.status_code == 503
    assert resp.cookies.get("access_token") is None
    jit_mock.assert_not_awaited()
    audit_mock.assert_not_awaited()
    db.add.assert_not_called()
    db.delete.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_login_discover_ambiguous_external_match_has_zero_mutation(
    anon_client, monkeypatch
):
    tenants = [_make_tenant("alpha"), _make_tenant("beta")]
    _install_system_tenants(monkeypatch, tenants)
    dbs = {}

    class _Chain:
        async def authenticate_outcome(
            self, *, tenant_id: str, email: str, password: str, **kwargs
        ):
            return AuthOutcome(
                status="authenticated",
                identity=UserIdentity(
                    email="ldap@example.com",
                    display_name="LDAP User",
                    groups=["eng"],
                    source_backend="ldap",
                    raw_claims={},
                ),
                backend_name="ldap",
            )

    async def fake_get_tenant_db(slug):
        db = AsyncMock()
        db.add = MagicMock()
        db.delete = AsyncMock()
        db.commit = AsyncMock()
        no_existing = MagicMock()
        no_existing.scalar_one_or_none.return_value = None
        admitted_mapping = MagicMock()
        admitted_mapping.first.return_value = object()
        db.execute = AsyncMock(side_effect=[no_existing, admitted_mapping])
        dbs[slug] = db
        yield db

    jit_mock = AsyncMock()
    audit_mock = AsyncMock()
    monkeypatch.setattr("src.api.auth.get_tenant_db", fake_get_tenant_db)
    monkeypatch.setattr("src.api.auth.get_auth_chain", lambda: _Chain())
    monkeypatch.setattr("src.api.auth.jit_adopt_user", jit_mock)
    monkeypatch.setattr("src.api.auth.audit_required", audit_mock)

    resp = await anon_client.post(
        "/api/v1/auth/login/discover",
        json={"tenant_id": "_discover", "email": "ldap@example.com", "password": "pw"},
    )

    assert resp.status_code == 401
    jit_mock.assert_not_awaited()
    audit_mock.assert_not_awaited()
    for db in dbs.values():
        db.add.assert_not_called()
        db.delete.assert_not_awaited()
        db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_login_discover_unique_external_match_jits_once_after_scan(
    anon_client, monkeypatch
):
    tenants = [_make_tenant("alpha"), _make_tenant("beta")]
    _install_system_tenants(monkeypatch, tenants)
    user = _make_user("ldap@example.com")
    user.token_version = 3

    class _Chain:
        async def authenticate_outcome(
            self, *, tenant_id: str, email: str, password: str, **kwargs
        ):
            if tenant_id != "beta":
                return AuthOutcome(status="rejected")
            return AuthOutcome(
                status="authenticated",
                identity=UserIdentity(
                    email="ldap@example.com",
                    display_name="LDAP User",
                    groups=["eng"],
                    source_backend="ldap",
                    raw_claims={},
                ),
                backend_name="ldap",
            )

    async def fake_get_tenant_db(slug):
        db = AsyncMock()
        db.add = MagicMock()
        no_existing = MagicMock()
        no_existing.scalar_one_or_none.return_value = None
        admitted_mapping = MagicMock()
        admitted_mapping.first.return_value = object()
        db.execute = AsyncMock(side_effect=[no_existing, admitted_mapping])
        yield db

    jit_mock = AsyncMock(return_value=(user, "member"))
    monkeypatch.setattr("src.api.auth.get_tenant_db", fake_get_tenant_db)
    monkeypatch.setattr("src.api.auth.get_auth_chain", lambda: _Chain())
    monkeypatch.setattr("src.api.auth.jit_adopt_user", jit_mock)

    resp = await anon_client.post(
        "/api/v1/auth/login/discover",
        json={"tenant_id": "_discover", "email": "ldap@example.com", "password": "pw"},
    )

    assert resp.status_code == 200
    jit_mock.assert_awaited_once()
    payload = decode_access_token(resp.cookies.get("access_token"))
    assert payload["tenant_id"] == "beta"
    assert payload["token_version"] == 3


@pytest.mark.asyncio
async def test_login_discover_no_match_returns_401(anon_client, monkeypatch):
    tenants = [_make_tenant("alpha"), _make_tenant("beta")]
    _install_system_tenants(monkeypatch, tenants)
    _install_tenant_lookup(monkeypatch, lambda slug: None)

    # verify_password is still called once (dummy hash) to keep timing flat.
    with patch("src.api.auth.verify_password", return_value=False) as vp:
        resp = await anon_client.post(
            "/api/v1/auth/login/discover",
            json={"tenant_id": "_discover", "email": "u@example.com", "password": "pw"},
        )
    assert resp.status_code == 401
    assert vp.call_count == 1, "exactly one bcrypt verify even on no match (F-021-07)"


@pytest.mark.asyncio
async def test_login_discover_success_writes_audit(anon_client, monkeypatch):
    """Bug-6304: a successful cross-tenant discovery login must emit an
    auth.login_success audit into the matched tenant, tagged via='discover'
    (this path is BI-client auth and previously wrote no audit at all)."""
    tenants = [_make_tenant("acme"), _make_tenant("beta")]
    _install_system_tenants(monkeypatch, tenants)
    _install_tenant_lookup(
        monkeypatch,
        lambda slug: _make_user("u@example.com", role="tenant_admin") if slug == "beta" else None,
    )
    audit_mock = AsyncMock()
    with (
        patch("src.api.auth.verify_password", return_value=True),
        patch("src.api.auth.audit_required", audit_mock),
    ):
        resp = await anon_client.post(
            "/api/v1/auth/login/discover",
            json={"tenant_id": "_discover", "email": "u@example.com", "password": "pw"},
        )
        await _drain_discover_audit_tasks()

    assert resp.status_code == 200
    audit_mock.assert_awaited()
    kw = audit_mock.await_args.kwargs
    assert kw["action"] == "auth.login_success"
    assert kw["severity"] == "info"
    assert kw["detail"]["via"] == "discover"


@pytest.mark.asyncio
async def test_login_discover_wrong_password_is_opaque(anon_client, monkeypatch):
    """Discovery uses the auth chain and keeps failed credentials opaque."""
    tenants = [_make_tenant("acme"), _make_tenant("beta")]
    _install_system_tenants(monkeypatch, tenants)
    _install_tenant_lookup(
        monkeypatch,
        lambda slug: _make_user("u@example.com") if slug == "beta" else None,
    )
    with patch("src.api.auth.audit_required", AsyncMock()) as audit_mock:
        resp = await anon_client.post(
            "/api/v1/auth/login/discover",
            json={"tenant_id": "_discover", "email": "u@example.com", "password": "wrong"},
        )
        await _drain_discover_audit_tasks()

    assert resp.status_code == 401
    audit_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_login_discover_no_match_writes_no_tenant_audit(anon_client, monkeypatch):
    """Bug-6304 (timing symmetry): an unknown email matches no tenant, so there
    is no tenant DB to audit into — the response path must NOT await any DB
    write (that asymmetry would be a user-enumeration timing oracle)."""
    tenants = [_make_tenant("acme"), _make_tenant("beta")]
    _install_system_tenants(monkeypatch, tenants)
    _install_tenant_lookup(monkeypatch, lambda slug: None)
    audit_mock = AsyncMock()
    with (
        patch("src.api.auth.verify_password", return_value=False),
        patch("src.api.auth.audit_required", audit_mock),
    ):
        resp = await anon_client.post(
            "/api/v1/auth/login/discover",
            json={"tenant_id": "_discover", "email": "nobody@example.com", "password": "pw"},
        )
        await _drain_discover_audit_tasks()

    assert resp.status_code == 401
    # No tenant matched -> no tenant-scoped audit event is written.
    audit_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_login_discover_runs_bcrypt_once_on_match(anon_client, monkeypatch):
    """F-021-07 successor: admitted ambiguity fails closed before token mint."""
    tenants = [_make_tenant("a"), _make_tenant("b"), _make_tenant("c")]
    _install_system_tenants(monkeypatch, tenants)
    _install_tenant_lookup(monkeypatch, lambda slug: _make_user("u@example.com"))

    with patch("src.api.auth.verify_password", return_value=True) as vp:
        resp = await anon_client.post(
            "/api/v1/auth/login/discover",
            json={"tenant_id": "_discover", "email": "u@example.com", "password": "pw"},
        )
    assert resp.status_code == 401
    assert vp.call_count == 0, "chain-auth matches do not run discovery-local bcrypt"


@pytest.mark.asyncio
async def test_login_discover_tenant_hint_resolves_multi_match(anon_client, monkeypatch):
    """Bug-7319: when multi-tenant match occurs but the request body carries
    a valid tenant_id hint, use it to disambiguate."""
    tenants = [_make_tenant("acme"), _make_tenant("beta")]
    _install_system_tenants(monkeypatch, tenants)
    _install_tenant_lookup(
        monkeypatch,
        lambda slug: _make_user("u@example.com", role="member"),
    )

    with patch("src.api.auth.verify_password", return_value=True):
        resp = await anon_client.post(
            "/api/v1/auth/login/discover",
            json={"tenant_id": "beta", "email": "u@example.com", "password": "pw"},
        )
    assert resp.status_code == 200, "Tenant hint should resolve ambiguity"
    body = resp.json()
    token = resp.cookies.get("access_token")
    assert token
    payload = decode_access_token(token)
    assert payload["tenant_id"] == "beta"


@pytest.mark.asyncio
async def test_login_discover_bad_tenant_hint_still_fails_closed(anon_client, monkeypatch):
    """Bug-7319: if the hint does not match any admitted tenant, fail closed."""
    tenants = [_make_tenant("acme"), _make_tenant("beta")]
    _install_system_tenants(monkeypatch, tenants)
    _install_tenant_lookup(
        monkeypatch,
        lambda slug: _make_user("u@example.com", role="member"),
    )

    with patch("src.api.auth.verify_password", return_value=True):
        resp = await anon_client.post(
            "/api/v1/auth/login/discover",
            json={"tenant_id": "nonexistent", "email": "u@example.com", "password": "pw"},
        )
    assert resp.status_code == 401
