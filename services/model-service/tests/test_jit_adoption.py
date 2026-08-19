"""Tests for JIT user adoption from IdP (Block G).

Verifies that when a non-local auth backend (LDAP, GCP IAM) authenticates a
user who does not yet have a local_users record, the login endpoint auto-creates
one with the configured default role.
"""
from __future__ import annotations

import base64
import json
import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from shared.auth.backend import UserIdentity
from shared.licensing.issuer.sign import generate_ed25519_keypair, sign_license
from src.main import app
from src.auth.middleware import get_current_user, require_tenant_admin, CurrentUser
from src import licensing_guard

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _issuer_context(monkeypatch):
    """This module builds signed licence fixtures via ``sign_license``, which is
    fail-closed to a sanctioned issuer context (Bug-6547). Scope the marker to THIS
    file only — the rest of the model-service suite runs guard-live, so an accidental
    in-product mint would fail loudly there rather than being masked."""
    monkeypatch.setenv("TESSALLITE_LICENSE_ISSUER", "1")


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
    execute_result.first.return_value = object()
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
    user_result.first.return_value = None

    admission_group_result = MagicMock()
    admission_group_result.first.return_value = object()

    group_result = MagicMock()
    group_result.scalars.return_value.all.return_value = []

    setting_result = MagicMock()
    setting_result.scalar_one_or_none.return_value = value_json

    audit_result = MagicMock()
    audit_result.scalar_one_or_none.return_value = None

    db.execute = AsyncMock(
        side_effect=[
            user_result,
            admission_group_result,
            user_result,
            group_result,
            setting_result,
            group_result,
            group_result,
            audit_result,
            audit_result,
        ]
    )
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


def _mock_system_db():
    """In-memory stand-in so G-021-04 login lockout (CP-08, get_system_db ->
    login_lockouts) does not open Postgres in these unit tests."""
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
    # CP-08 added a system-DB login-lockout check on the login path; mirror
    # test_auth.py so these pre-existing JIT login tests do not hit a real
    # (unmigrated) system DB. Matches the established file-local pattern.
    db = _mock_system_db()
    with patch("src.api.auth.get_system_db", lambda: _yield(db)):
        yield


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
    identity = _make_identity(
        email="new-ldap@corp.com",
        source_backend="ldap",
        groups=["admitted"],
    )
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
    identity = _make_identity(
        email="gcp-user@corp.com",
        source_backend="gcp_iam",
        groups=["admitted"],
    )
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
# F-021-01: SSO must NOT silently take over an existing account with the same
# email but a different auth_source (local, or a different external provider).
# Test escape: the existing-user JIT test used the SAME provider on both sides,
# so it never asked whether a local / different-provider account may be linked.
# Guard: jit_adopt_user refuses cross-provider adoption with an audited 403.
# Tier: T1 (identity-linking security contract).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sso_refuses_takeover_of_local_account():
    """An external (SAML) identity whose email matches a LOCAL account is
    refused: no session is issued and a critical audit event is written."""
    from fastapi import HTTPException
    from src.auth.jit import jit_adopt_user

    local_alice = _make_local_user("alice@x.com", role="admin", auth_source="local")
    db = _mock_db_with_user(local_alice)
    db.commit = AsyncMock()
    identity = _make_identity(email="alice@x.com", source_backend="saml")

    with (
        patch("src.auth.jit.resolve_group_role", new_callable=AsyncMock,
              return_value=None),
        patch("src.auth.jit._sync_group_bindings", new_callable=AsyncMock),
        patch("src.auth.jit.audit", new_callable=AsyncMock) as audit_mock,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await jit_adopt_user(db, identity, "acme")

    assert exc_info.value.status_code == 403
    assert audit_mock.await_count == 1
    assert audit_mock.await_args.kwargs["action"] == "auth.sso_account_link_refused"


@pytest.mark.asyncio
async def test_sso_refuses_takeover_of_different_provider_account():
    """A SAML identity must not adopt an account provisioned by a DIFFERENT
    external provider (oidc) even though the email matches."""
    from fastapi import HTTPException
    from src.auth.jit import jit_adopt_user

    oidc_bob = _make_local_user("bob@x.com", role="viewer", auth_source="oidc")
    db = _mock_db_with_user(oidc_bob)
    db.commit = AsyncMock()
    identity = _make_identity(email="bob@x.com", source_backend="saml")

    with (
        patch("src.auth.jit.resolve_group_role", new_callable=AsyncMock,
              return_value=None),
        patch("src.auth.jit._sync_group_bindings", new_callable=AsyncMock),
        patch("src.auth.jit.audit", new_callable=AsyncMock),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await jit_adopt_user(db, identity, "acme")

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_sso_same_provider_returning_user_allowed():
    """The legitimate case still works: a SAML identity adopting a SAML account
    is a returning user and is NOT refused."""
    from src.auth.jit import jit_adopt_user

    saml_carol = _make_local_user("carol@x.com", role="viewer", auth_source="saml")
    saml_carol.token_version = 0
    db = _mock_db_with_user(saml_carol)
    db.commit = AsyncMock()

    async def _refresh(obj):
        return None
    db.refresh = _refresh
    identity = _make_identity(email="carol@x.com", source_backend="saml")

    with (
        patch("src.auth.jit.resolve_group_role", new_callable=AsyncMock,
              return_value=None),
        patch("src.auth.jit._sync_group_bindings", new_callable=AsyncMock),
        patch("src.auth.jit._reconcile_sso_tenant_role", new_callable=AsyncMock),
    ):
        user, role = await jit_adopt_user(db, identity, "acme")

    assert user is saml_carol
    assert role == "viewer"


# ---------------------------------------------------------------------------
# JIT user gets configured default role
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_jit_uses_configured_default_role(anon_client):
    identity = _make_identity(
        email="analyst@corp.com",
        source_backend="ldap",
        groups=["admitted"],
    )
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
    identity = _make_identity(
        email="member-default@corp.com",
        source_backend="ldap",
        groups=["admitted"],
    )
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
                    json={"password": "NewPassword1!"},
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
    token_version_result = MagicMock()
    token_version_result.scalar_one_or_none.return_value = 1
    audit_result = MagicMock()
    audit_result.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(side_effect=[token_version_result, audit_result])

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
                    json={"password": "NewPassword1!"},
                )
    finally:
        app.dependency_overrides.pop(require_tenant_admin, None)

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# JIT sentinel password must hash with the REAL bcrypt (no mock).
#
# Test escape guard: every endpoint-level JIT test above patches
# ``src.auth.jit.hash_password``, so bcrypt's 72-byte hard limit was never
# exercised — ``token_urlsafe(64)`` (86 chars) made every first-time SSO/LDAP
# login raise ValueError, swallowed into an opaque 401. This test runs the
# exact sentinel expression through the real hasher.
# ---------------------------------------------------------------------------

def test_jit_sentinel_password_hashes_with_real_bcrypt():
    from src.auth.jit import _jit_sentinel_password
    from src.auth.local_backend import hash_password

    sentinel = _jit_sentinel_password()
    assert len(sentinel.encode()) <= 72, (
        f"JIT sentinel is {len(sentinel.encode())} bytes; bcrypt >= 4.1 "
        "raises ValueError beyond 72 bytes"
    )
    hashed = hash_password(sentinel)  # must not raise
    assert hashed.startswith("$2")


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


# ---------------------------------------------------------------------------
# Bug-6435: JIT user provisioning must enforce the licensed users cap
# ---------------------------------------------------------------------------


def _community_license_file(tmp_path, users=2):
    """Create a signed Community license with a configurable users cap."""
    priv, pub = generate_ed25519_keypair()
    doc = {
        "schema_version": 1,
        "license_id": "lic_jit_test",
        "key_id": "k1",
        "issuer": "tessallite.io",
        "edition": "community",
        "issued_at": "2026-06-22T00:00:00Z",
        "expires_at": None,
        "product": "tessallite-community",
        "entitlements": {
            "own_tenants": 1,
            "models": 2,
            "users": users,
            "features": "all",
        },
    }
    signed = sign_license(doc, priv)
    path = tmp_path / "license.json"
    path.write_text(json.dumps(signed), encoding="utf-8")
    pub_spec = "k1:" + base64.b64encode(pub).decode("ascii")
    return str(path), pub_spec


def _settings_for_cap(**kw):
    base = dict(
        LICENSE_ENFORCEMENT_ENABLED=False, LICENSE_FILE="", LICENSE_PUBLIC_KEYS=""
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


async def _load_license(monkeypatch, lf):
    doc = json.loads(open(lf, encoding="utf-8").read()) if lf else None
    monkeypatch.setattr(
        licensing_guard, "load_license_doc_from_db", AsyncMock(return_value=doc)
    )
    await licensing_guard.reload_license_manager()


def _mock_db_for_jit_cap(user_count: int):
    """Mock DB session for JIT adoption with a configurable user count.

    Query order from jit_adopt_user when local_user is None and groups=[]:
    1. User lookup -> None (no existing local user)
       (resolve_group_role returns None immediately when groups=[], no query)
    2. Advisory lock (Bug-6567: pg_advisory_xact_lock) -> no-op
    3. User count for cap check (enforce_create_cap) -> user_count
       -- if cap exceeded, HTTPException is raised here; queries 4+ are skipped
    4. JIT default role setting lookup -> None (use default viewer)
    5+ Audit queries from the login endpoint (scalar_one_or_none -> None)
    """
    db = AsyncMock()

    # 1. User lookup -> None
    user_result = MagicMock()
    user_result.scalar_one_or_none.return_value = None
    user_result.first.return_value = None

    admission_group_result = MagicMock()
    admission_group_result.first.return_value = object()

    # 2. Advisory lock result (Bug-6567: no-op for mocked DB)
    advisory_lock_result = MagicMock()

    # 3. User count for enforce_create_cap -> the supplied count
    count_result = MagicMock()
    count_result.scalar.return_value = user_count

    # 4. JIT default role setting -> None (default viewer)
    setting_result = MagicMock()
    setting_result.scalar_one_or_none.return_value = None

    # 5+ Audit queries
    audit_result = MagicMock()
    audit_result.scalar_one_or_none.return_value = None

    group_result = MagicMock()
    group_result.scalars.return_value.all.return_value = []

    db.execute = AsyncMock(
        side_effect=[
            user_result,
            admission_group_result,
            user_result,
            group_result,
            advisory_lock_result,
            count_result,
            setting_result,
            group_result,
            group_result,
            audit_result,
            audit_result,
        ]
    )
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()

    async def _refresh(obj):
        obj.id = uuid.uuid4()
        obj.created_at = NOW

    db.refresh = _refresh
    return db


@pytest.mark.asyncio
async def test_jit_rejects_user_at_cap(tmp_path, monkeypatch, anon_client):
    """Bug-6435: JIT provisioning must be REJECTED (403) when the licensed
    users cap is reached. The Community license allows 2 users; with 2
    already present, a new SSO login must not silently create a third."""
    lf, pk = _community_license_file(tmp_path, users=2)
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings_for_cap(
            LICENSE_ENFORCEMENT_ENABLED=True, LICENSE_FILE=lf, LICENSE_PUBLIC_KEYS=pk,
        ),
    )
    await _load_license(monkeypatch, lf)

    identity = _make_identity(
        email="over-cap@corp.com",
        source_backend="ldap",
        groups=["admitted"],
    )
    db = _mock_db_for_jit_cap(user_count=2)  # at cap

    with (
        patch("src.api.auth.get_auth_chain", return_value=_mock_chain(identity)),
        patch("src.api.auth.get_tenant_db", lambda tid: _yield(db)),
        patch("src.auth.jit.hash_password", return_value="$2b$12$jit_sentinel"),
    ):
        resp = await anon_client.post(
            "/api/v1/auth/login",
            json={
                "tenant_id": "acme",
                "email": "over-cap@corp.com",
                "password": "ldap-pw",
            },
        )

    assert resp.status_code == 403, (
        f"Expected 403 when users cap reached, got {resp.status_code}: {resp.text}"
    )
    # Verify no user was created (db.add should NOT have been called with a
    # LocalUser -- the cap check should have raised before reaching that point).
    for call in db.add.call_args_list:
        obj = call[0][0]
        assert not hasattr(obj, "hashed_password"), (
            "A LocalUser was created despite the users cap being reached"
        )


@pytest.mark.asyncio
async def test_jit_allows_user_under_cap(tmp_path, monkeypatch, anon_client):
    """Bug-6435: JIT provisioning must be ALLOWED when the user count is
    below the licensed cap. The Community license allows 2 users; with 1
    already present, a new SSO login should succeed."""
    lf, pk = _community_license_file(tmp_path, users=2)
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings_for_cap(
            LICENSE_ENFORCEMENT_ENABLED=True, LICENSE_FILE=lf, LICENSE_PUBLIC_KEYS=pk,
        ),
    )
    await _load_license(monkeypatch, lf)

    identity = _make_identity(
        email="under-cap@corp.com",
        source_backend="ldap",
        groups=["admitted"],
    )
    db = _mock_db_for_jit_cap(user_count=1)  # under cap

    with (
        patch("src.api.auth.get_auth_chain", return_value=_mock_chain(identity)),
        patch("src.api.auth.get_tenant_db", lambda tid: _yield(db)),
        patch("src.auth.jit.hash_password", return_value="$2b$12$jit_sentinel"),
    ):
        resp = await anon_client.post(
            "/api/v1/auth/login",
            json={
                "tenant_id": "acme",
                "email": "under-cap@corp.com",
                "password": "ldap-pw",
            },
        )

    assert resp.status_code == 200, (
        f"Expected 200 when under users cap, got {resp.status_code}: {resp.text}"
    )
    # Verify the user was actually created.
    created = False
    for call in db.add.call_args_list:
        obj = call[0][0]
        if hasattr(obj, "hashed_password") and getattr(obj, "email", None) == "under-cap@corp.com":
            created = True
            break
    assert created, "Expected a LocalUser to be created when under the users cap"


# ---------------------------------------------------------------------------
# Bug-6303: SSO group binding provenance + revocation on de-provisioning
# ---------------------------------------------------------------------------
#
# These exercise ``_sync_group_bindings`` directly with a fake AsyncSession
# that records add/delete and returns query results in the order the function
# issues them:
#   1. project-scoped IdpGroupRoleMapping lookup       -> scalars().all()
#   2. (per mapped project) existing binding lookup    -> scalar_one_or_none()
#   3. all source="sso_group" bindings for the user    -> scalars().all()


class _FakeResult:
    def __init__(self, scalar=None, all_=None):
        self._scalar = scalar
        self._all = all_ or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        s = MagicMock()
        s.all.return_value = self._all
        return s


class _FakeDB:
    """Minimal async session recording add/delete and replaying results."""

    def __init__(self, results):
        self._results = list(results)
        self.executed = []
        self.added = []
        self.deleted = []

    async def execute(self, stmt):
        self.executed.append(stmt)
        if self._results:
            return self._results.pop(0)
        return _FakeResult()

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)


def _mapping(project_id: str, role: str):
    return types.SimpleNamespace(project_id=project_id, role=role)


def _binding(project_id: str, role: str, source: str, model_id=None):
    return types.SimpleNamespace(
        project_id=project_id, role=role, source=source, model_id=model_id,
    )


P1 = "11111111-1111-1111-1111-111111111111"
P2 = "22222222-2222-2222-2222-222222222222"


@pytest.mark.asyncio
async def test_sso_group_binding_revoked_on_deprovision():
    """(a) A user removed from the mapping's IdP group has their stale
    ``sso_group`` binding revoked on next login. Current groups are non-empty
    and authoritative but no longer map to project P1."""
    from src.auth.jit import _sync_group_bindings

    stale = _binding(P1, "modeler", "sso_group")
    db = _FakeDB([
        _FakeResult(all_=[]),          # no project mapping matches current groups
        _FakeResult(all_=[stale]),     # existing sso_group bindings for user
    ])

    await _sync_group_bindings(db, "user@corp.com", ["employees"])

    assert db.deleted == [stale], "de-provisioned sso_group binding must be revoked"
    assert db.added == []


@pytest.mark.asyncio
async def test_manual_binding_never_revoked_or_touched():
    """(b) A manual binding on a mapped project is left untouched: no role
    change, no revoke, no duplicate row. The revocation query filters on
    source='sso_group' so a manual row can never be selected for deletion."""
    from src.auth.jit import _sync_group_bindings

    manual = _binding(P1, "viewer", "manual")
    db = _FakeDB([
        _FakeResult(all_=[_mapping(P1, "modeler")]),  # group maps analysts->modeler on P1
        _FakeResult(scalar=manual),                   # existing binding on P1 is MANUAL
        _FakeResult(all_=[]),                          # no sso_group bindings for user
    ])

    await _sync_group_bindings(db, "user@corp.com", ["analysts"])

    assert manual.role == "viewer", "manual binding role must not be upgraded by SSO sync"
    assert db.added == [], "no duplicate sso_group row on a manual scope"
    assert db.deleted == [], "manual binding must never be revoked"

    # The revocation SELECT must constrain source='sso_group' so manual rows are
    # excluded at the DB level (defence in depth vs. the in-loop check).
    revoke_sql = str(
        db.executed[-1].compile(compile_kwargs={"literal_binds": True})
    )
    assert "source" in revoke_sql and "sso_group" in revoke_sql


@pytest.mark.asyncio
async def test_empty_group_set_does_not_mass_revoke():
    """(c) Fail-closed: an empty/undetermined group set must not revoke any
    binding. The function returns before issuing a single query."""
    from src.auth.jit import _sync_group_bindings

    db = _FakeDB([])
    await _sync_group_bindings(db, "user@corp.com", [])

    assert db.executed == [], "no queries may run on an empty group set"
    assert db.deleted == [], "empty group set must never mass-revoke"
    assert db.added == []


@pytest.mark.asyncio
async def test_new_sso_binding_stamped_with_source():
    """A newly materialised group binding is stamped source='sso_group' so it is
    later distinguishable from a manual grant (the provenance the revocation
    path depends on)."""
    from src.auth.jit import _sync_group_bindings
    from shared.db.models import UserAccessBinding

    db = _FakeDB([
        _FakeResult(all_=[_mapping(P1, "modeler")]),  # mapping matches
        _FakeResult(scalar=None),                     # no existing binding on P1
        _FakeResult(all_=[]),                          # no sso_group bindings yet
    ])

    await _sync_group_bindings(db, "user@corp.com", ["analysts"])

    assert len(db.added) == 1
    created = db.added[0]
    assert isinstance(created, UserAccessBinding)
    assert created.source == "sso_group"
    assert created.role == "modeler"
    assert str(created.project_id) == P1
    assert created.model_id is None
    assert db.deleted == []


@pytest.mark.asyncio
async def test_sso_binding_kept_and_role_refreshed_when_still_mapped():
    """A still-mapped sso_group binding is retained (not revoked) and its role
    is refreshed to the currently mapped role."""
    from src.auth.jit import _sync_group_bindings

    existing = _binding(P1, "viewer", "sso_group")
    db = _FakeDB([
        _FakeResult(all_=[_mapping(P1, "modeler")]),  # mapping now grants modeler
        _FakeResult(scalar=existing),                 # existing sso_group binding
        _FakeResult(all_=[existing]),                  # revocation scan sees it
    ])

    await _sync_group_bindings(db, "user@corp.com", ["analysts"])

    assert existing.role == "modeler", "sso_group role must refresh to mapped role"
    assert db.deleted == [], "still-mapped sso_group binding must not be revoked"
    assert db.added == []


@pytest.mark.asyncio
async def test_partial_deprovision_revokes_only_unmapped_project():
    """A user still mapped to P1 but de-provisioned from the group mapping P2
    keeps the P1 sso_group binding and loses only the P2 one."""
    from src.auth.jit import _sync_group_bindings

    keep = _binding(P1, "modeler", "sso_group")
    drop = _binding(P2, "viewer", "sso_group")
    db = _FakeDB([
        _FakeResult(all_=[_mapping(P1, "modeler")]),  # only P1 still mapped
        _FakeResult(scalar=keep),                     # existing binding on P1
        _FakeResult(all_=[keep, drop]),                # both sso_group bindings
    ])

    await _sync_group_bindings(db, "user@corp.com", ["analysts"])

    assert db.deleted == [drop], "only the unmapped-project sso_group binding is revoked"
    assert keep not in db.deleted


# ---------------------------------------------------------------------------
# F-021-04: present-empty groups claim (authoritative de-provisioning) MUST
# revoke SSO-derived grants; an ABSENT groups claim must NOT.
# Test escape: prior tests encoded empty groups as indeterminate and preserved
# access, never modelling "IdP present, returned no groups". Guard: the
# groups_claim_present flag threads through _sync_group_bindings /
# _reconcile_sso_tenant_role. Tier: T1 (SSO deprovisioning contract).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_present_empty_groups_revokes_sso_bindings():
    """When the IdP RETURNED the groups claim and it is empty, every sso_group
    binding is revoked — the user's current groups authoritatively map to no
    project. This is the de-provisioning signal a present-empty claim carries."""
    from src.auth.jit import _sync_group_bindings

    stale_p1 = _binding(P1, "modeler", "sso_group")
    stale_p2 = _binding(P2, "viewer", "sso_group")
    db = _FakeDB([
        # groups=[] → IdpGroupRoleMapping.in_([]) returns nothing
        _FakeResult(all_=[]),
        # revocation scan sees both stale sso_group bindings
        _FakeResult(all_=[stale_p1, stale_p2]),
    ])

    # groups_claim_present=True with groups=[] is the authoritative "no groups".
    await _sync_group_bindings(
        db, "user@corp.com", [], groups_claim_present=True,
    )

    assert db.deleted == [stale_p1, stale_p2], (
        "a present-empty groups claim must revoke ALL sso_group bindings"
    )
    assert db.added == []


@pytest.mark.asyncio
async def test_absent_groups_claim_retains_sso_bindings():
    """When the IdP did NOT return the groups claim (indeterminate), sso_group
    bindings are retained — fail closed, never revoke on an omitted claim."""
    from src.auth.jit import _sync_group_bindings

    db = _FakeDB([])
    await _sync_group_bindings(
        db, "user@corp.com", [], groups_claim_present=False,
    )

    assert db.executed == [], "absent claim must not run any query"
    assert db.deleted == [], "absent groups claim must never revoke"


@pytest.mark.asyncio
async def test_present_empty_groups_demotes_sso_tenant_admin():
    """F-021-04: an SSO-elevated tenant_admin whose groups claim is now
    present-but-empty is demoted (Bug-6597 path), provided another admin exists."""
    from src.auth.jit import _reconcile_sso_tenant_role

    admin = types.SimpleNamespace(
        id=uuid.uuid4(), email="admin@corp.com", role="tenant_admin",
        role_source="sso", token_version=0,
    )
    db = _FakeDB([])

    with (
        patch("src.auth.jit.other_active_tenant_admin_exists",
              new_callable=AsyncMock, return_value=True),
        patch("src.auth.jit.bump_local_user_token_version",
              new_callable=AsyncMock, return_value=1),
        patch("src.auth.jit.resolve_jit_default_role",
              new_callable=AsyncMock, return_value="viewer"),
        patch("src.auth.jit._audit_sso_reconcile", new_callable=AsyncMock),
    ):
        await _reconcile_sso_tenant_role(
            db, admin, mapped_tenant_role=None, tenant_group_role=None,
            groups=[], groups_claim_present=True,
        )

    assert admin.role == "viewer", (
        "present-empty groups must demote an SSO-elevated tenant_admin"
    )


@pytest.mark.asyncio
async def test_absent_groups_claim_keeps_sso_tenant_admin():
    """An ABSENT groups claim must NOT demote an SSO tenant_admin (indeterminate)."""
    from src.auth.jit import _reconcile_sso_tenant_role

    admin = types.SimpleNamespace(
        id=uuid.uuid4(), email="admin@corp.com", role="tenant_admin",
        role_source="sso", token_version=0,
    )
    db = _FakeDB([])

    await _reconcile_sso_tenant_role(
        db, admin, mapped_tenant_role=None, tenant_group_role=None,
        groups=[], groups_claim_present=False,
    )

    assert admin.role == "tenant_admin", (
        "absent groups claim must keep the SSO tenant_admin (fail closed)"
    )
