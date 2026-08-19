"""Bug-6306 / Bug-6352 R3 — the revoke endpoint names the TOKEN's tenant.

Revocation is tenant-scoped: a record only silences a token bearing the same
tenant claim. The endpoint originally filed the record under the CALLING
admin's tenant, which quietly broke the one revocation that matters most.
``mint_embed_token`` deliberately lets a system admin mint for ANY tenant, but
a canonical system admin's own tenant is the ``__system__`` pseudo-tenant, so
their revocation was filed under a tenant no token ever claims: HTTP 204 came
back, the audit record was written, and the token kept working for its full
24-hour lifetime.

These tests assert the tenant the record is written UNDER, which is the thing
that decides whether the revocation has any effect at all.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from src.main import app
from src.auth.middleware import CurrentUser, get_current_user, require_tenant_admin
from tests.conftest import TEST_TENANT, async_gen_from, make_mock_db

pytestmark = pytest.mark.unit

SYSTEM_TENANT = "__system__"


def _as(role: str, tenant: str, email: str):
    user = CurrentUser(user_id=email, tenant_id=tenant, email=email, role=role)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_tenant_admin] = lambda: user
    return user


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(require_tenant_admin, None)


KNOWN_TENANTS = {TEST_TENANT, "acme-demo"}


def _system_db_knowing(slugs):
    """A system-DB stand-in whose SELECT answers "does this slug exist"."""
    class _Result:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _Db:
        async def execute(self, stmt):
            wanted = stmt.compile().params.get("slug_1")
            return _Result(wanted if wanted in slugs else None)

    async def _gen():
        yield _Db()

    return _gen


@pytest.fixture
def revoked():
    """Capture the tenant each revocation is recorded under."""
    calls: list[dict] = []

    async def _spy(**kwargs):
        calls.append(kwargs)

    with (
        patch("src.api.embed.revoke_embed_token", _spy),
        patch("src.api.embed.get_system_db", _system_db_knowing(KNOWN_TENANTS)),
        patch("src.api.embed.get_tenant_db", async_gen_from(make_mock_db())),
        patch("src.api.embed.audit_required", new_callable=AsyncMock),
    ):
        yield calls


@pytest.mark.asyncio
async def test_tenant_admin_revocation_is_filed_under_their_own_tenant(client, revoked):
    _as("tenant_admin", TEST_TENANT, "admin@acme")
    jti = uuid.uuid4()
    resp = await client.delete(f"/api/v1/auth/embed-token/{jti}")
    assert resp.status_code == 204, resp.text
    assert revoked[0]["tenant_id"] == TEST_TENANT


@pytest.mark.asyncio
async def test_tenant_admin_cannot_target_another_tenant(client, revoked):
    _as("tenant_admin", TEST_TENANT, "admin@acme")
    jti = uuid.uuid4()
    resp = await client.delete(
        f"/api/v1/auth/embed-token/{jti}?tenant_id=someone-else"
    )
    assert resp.status_code == 403
    assert revoked == [], "a cross-tenant revocation was recorded"


@pytest.mark.asyncio
async def test_tenant_admin_may_name_their_own_tenant_explicitly(client, revoked):
    _as("tenant_admin", TEST_TENANT, "admin@acme")
    jti = uuid.uuid4()
    resp = await client.delete(
        f"/api/v1/auth/embed-token/{jti}?tenant_id={TEST_TENANT}"
    )
    assert resp.status_code == 204, resp.text
    assert revoked[0]["tenant_id"] == TEST_TENANT


@pytest.mark.asyncio
async def test_system_admin_revocation_is_filed_under_the_named_tenant(client, revoked):
    """The fix: the record must name the tenant the TOKEN belongs to, not the
    platform pseudo-tenant the operator authenticates under."""
    _as("system_admin", SYSTEM_TENANT, "admin@tessallite.local")
    jti = uuid.uuid4()
    resp = await client.delete(
        f"/api/v1/auth/embed-token/{jti}?tenant_id=acme-demo"
    )
    assert resp.status_code == 204, resp.text
    assert revoked[0]["tenant_id"] == "acme-demo", (
        "the system admin's revocation was filed under a tenant no token claims"
    )


@pytest.mark.asyncio
async def test_system_admin_without_a_target_tenant_is_refused_not_ignored(
    client, revoked,
):
    """The failure mode being closed is a SILENT one: 204 with no effect. An
    explicit refusal is the only acceptable alternative."""
    _as("system_admin", SYSTEM_TENANT, "admin@tessallite.local")
    jti = uuid.uuid4()
    resp = await client.delete(f"/api/v1/auth/embed-token/{jti}")
    assert resp.status_code == 422, resp.text
    assert "tenant_id is required" in resp.json()["detail"]
    assert revoked == [], (
        "a revocation was recorded under the platform pseudo-tenant, where it "
        "can never match a token"
    )


@pytest.mark.asyncio
async def test_system_admin_naming_a_tenant_that_does_not_exist_is_refused(
    client, revoked,
):
    """R3 closed "tenant_id omitted -> silent 204". The WRONG-value case is the
    same silent 204 and is the likelier operator error: a mistyped, pasted or
    wrong-case slug files the revocation under a tenant no token can claim, the
    audit write then fails and is swallowed, and the leaked token stays live."""
    _as("system_admin", SYSTEM_TENANT, "admin@tessallite.local")
    jti = uuid.uuid4()
    resp = await client.delete(
        f"/api/v1/auth/embed-token/{jti}?tenant_id=Acme-Demo"
    )
    assert resp.status_code == 422, resp.text
    assert "does not exist" in resp.json()["detail"]
    assert revoked == [], (
        "a revocation was filed under a tenant no token can ever claim"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied", [
    "acme-demo ", " acme-demo", "acme-demo	",
])
async def test_surrounding_whitespace_does_not_produce_a_dead_revocation(
    supplied, client, revoked,
):
    """A slug pasted with a trailing space used to be stored verbatim, so the
    record matched nothing. Trim, then resolve."""
    _as("system_admin", SYSTEM_TENANT, "admin@tessallite.local")
    jti = uuid.uuid4()
    resp = await client.delete(
        f"/api/v1/auth/embed-token/{jti}", params={"tenant_id": supplied},
    )
    assert resp.status_code == 204, resp.text
    assert revoked[0]["tenant_id"] == "acme-demo"


@pytest.mark.asyncio
async def test_an_over_long_tenant_id_is_a_422_not_a_database_error(client, revoked):
    """``RevokedEmbedToken.tenant_id`` is String(64); an unbounded value reached
    the insert and surfaced as a DataError 500."""
    _as("system_admin", SYSTEM_TENANT, "admin@tessallite.local")
    jti = uuid.uuid4()
    resp = await client.delete(
        f"/api/v1/auth/embed-token/{jti}", params={"tenant_id": "x" * 300},
    )
    assert resp.status_code == 422, resp.text
    assert revoked == []
