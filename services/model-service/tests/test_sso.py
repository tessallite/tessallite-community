"""Tests for Block B — SAML/OIDC SSO and group-to-role mapping."""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from shared.auth.backend import UserIdentity
from src.main import app
from src.auth.middleware import CurrentUser, get_current_user, require_tenant_admin
from .conftest import (
    TEST_TENANT,
    TEST_USER_ID,
    NOW,
    async_gen_from,
    make_mock_db,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _empty_sso_overlay(monkeypatch):
    async def _empty(_tid: str) -> dict:
        return {}
    monkeypatch.setattr("src.auth.sso_overlay.load_tenant_overlay", _empty)


# Bug-6307: the SSO callback origin is no longer reconstructed from
# client-supplied Host / X-Forwarded-Host headers. The autouse
# ``configure_public_base_url`` fixture in conftest.py makes every route test
# run as a correctly configured deployment; the guard itself is covered in
# test_sso_base_url_injection.py.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _saml_result(identity, assertion_id: str = "assert-1", not_on_or_after=None):
    """F-021-03: wrap a UserIdentity in the SamlAssertionResult that
    process_saml_response now returns (identity + replay-ledger fields)."""
    from src.auth.saml_backend import SamlAssertionResult
    return SamlAssertionResult(
        identity=identity,
        assertion_id=assertion_id,
        not_on_or_after=not_on_or_after,
    )


def _admin_override():
    user = CurrentUser(
        user_id=TEST_USER_ID, tenant_id=TEST_TENANT,
        email=TEST_USER_ID, role="tenant_admin",
    )
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_tenant_admin] = lambda: user
    return user


def _clear_overrides():
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(require_tenant_admin, None)


# ---------------------------------------------------------------------------
# Auth backends discovery
# ---------------------------------------------------------------------------

class TestBackendsDiscovery:

    @pytest.mark.asyncio
    async def test_list_backends_default(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            resp = await ac.get("/api/v1/auth/backends")
        assert resp.status_code == 200
        data = resp.json()
        assert "backends" in data
        assert isinstance(data["saml_enabled"], bool)
        assert isinstance(data["oidc_enabled"], bool)

    @pytest.mark.asyncio
    async def test_list_backends_with_saml_oidc(self):
        with patch("shared.config.settings.get_settings") as mock_settings:
            s = MagicMock()
            s.AUTH_BACKENDS = "local,saml,oidc"
            mock_settings.return_value = s
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as ac:
                resp = await ac.get("/api/v1/auth/backends")
        assert resp.status_code == 200
        data = resp.json()
        assert data["saml_enabled"] is True
        assert data["oidc_enabled"] is True


# ---------------------------------------------------------------------------
# SAML endpoints
# ---------------------------------------------------------------------------

class TestSamlEndpoints:

    @pytest.mark.asyncio
    async def test_saml_metadata_not_configured(self):
        with patch("src.auth.saml_backend.get_settings") as mock_s:
            s = MagicMock()
            s.SAML_IDP_METADATA_URL = ""
            s.SAML_IDP_METADATA_XML = ""
            mock_s.return_value = s
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as ac:
                resp = await ac.get("/api/v1/auth/saml/metadata")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_saml_login_not_configured(self):
        with (
            patch("src.auth.saml_backend.get_settings") as mock_s,
            patch("src.api.sso.create_state", new_callable=AsyncMock,
                  return_value=("st", "n", "on", "cv")),
            # F-021-03: an unconfigured backend fails the build; the just-created
            # orphan state is discarded. Patch the discard so it doesn't touch
            # the system DB in this unit test.
            patch("src.api.sso.discard_state", new_callable=AsyncMock),
        ):
            s = MagicMock()
            s.SAML_IDP_METADATA_URL = ""
            s.SAML_IDP_METADATA_XML = ""
            mock_s.return_value = s
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                follow_redirects=False,
            ) as ac:
                resp = await ac.get("/api/v1/auth/saml/login?tenant_id=acme")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_saml_acs_invalid_state(self):
        with patch("src.api.sso.consume_state", new_callable=AsyncMock, return_value=None):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as ac:
                resp = await ac.post(
                    "/api/v1/auth/saml/acs",
                    data={"SAMLResponse": "invalid", "RelayState": "bad-state"},
                )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_saml_acs_success(self):
        identity = UserIdentity(
            email="saml-user@corp.com", display_name="SAML User",
            groups=["engineers"], source_backend="saml", raw_claims={},
        )
        db = make_mock_db()
        async def _refresh(obj):
            obj.id = uuid.uuid4()
            obj.created_at = NOW
        db.refresh = _refresh

        state_key = "test-state-saml"

        with (
            patch("src.api.sso.consume_state", new_callable=AsyncMock,
                  return_value=("acme", None, "req-1", None)),
            patch("src.api.sso.process_saml_response",
                  return_value=_saml_result(identity)),
            patch("src.api.sso.record_assertion_or_reject",
                  new_callable=AsyncMock, return_value=True),
            patch("src.api.sso.get_tenant_db", lambda tid: _yield(db)),
            patch("src.api.sso.jit_adopt_user", new_callable=AsyncMock) as jit_mock,
            patch("src.api.sso.audit", new_callable=AsyncMock) as audit_mock,
        ):
            jit_mock.return_value = (
                types.SimpleNamespace(
                    id=uuid.uuid4(), email="saml-user@corp.com", token_version=0
                ),
                "member",
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                follow_redirects=False,
            ) as ac:
                resp = await ac.post(
                    "/api/v1/auth/saml/acs",
                    data={"SAMLResponse": "valid-assertion", "RelayState": state_key},
                )

        assert resp.status_code == 302
        location = resp.headers["location"]
        assert "/sso/callback" in location
        assert "token=" not in location, "token must not leak in redirect URL"
        assert resp.cookies.get("access_token"), "access_token cookie must be set"

    @pytest.mark.asyncio
    async def test_saml_acs_denies_unadmitted_external_identity(self):
        identity = UserIdentity(
            email="outsider@corp.com", display_name="Outsider",
            groups=[], source_backend="saml", raw_claims={},
        )
        db = make_mock_db()
        no_row = MagicMock()
        no_row.scalar_one_or_none.return_value = None
        no_row.first.return_value = None
        db.execute = AsyncMock(return_value=no_row)

        with (
            patch("src.api.sso.consume_state", new_callable=AsyncMock,
                  return_value=("acme", None, "req-1", None)),
            patch("src.api.sso.process_saml_response",
                  return_value=_saml_result(identity)),
            patch("src.api.sso.record_assertion_or_reject",
                  new_callable=AsyncMock, return_value=True),
            patch("src.api.sso.get_tenant_db", lambda tid: _yield(db)),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                follow_redirects=False,
            ) as ac:
                resp = await ac.post(
                    "/api/v1/auth/saml/acs",
                    data={"SAMLResponse": "valid-assertion", "RelayState": "state"},
                )

        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# OIDC endpoints
# ---------------------------------------------------------------------------

class TestOidcEndpoints:

    @pytest.mark.asyncio
    async def test_oidc_login_not_configured(self):
        with (
            patch("src.api.sso._oidc_configured", return_value=False),
            patch("src.auth.oidc_backend.get_settings") as mock_s,
            patch("src.api.sso.create_state", new_callable=AsyncMock,
                  return_value=("st", "n", "on", "cv")),
            patch("src.api.sso.discard_state", new_callable=AsyncMock) as mock_discard,
        ):
            s = MagicMock()
            s.OIDC_ISSUER = ""
            s.OIDC_CLIENT_ID = ""
            mock_s.return_value = s
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                follow_redirects=False,
            ) as ac:
                resp = await ac.get("/api/v1/auth/oidc/login?tenant_id=acme")
        assert resp.status_code == 404
        # Not-configured fails closed before create_state, so there is no
        # orphan row to discard (same as SAML login not-configured).
        mock_discard.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_oidc_callback_invalid_state(self):
        with patch("src.api.sso.consume_state", new_callable=AsyncMock, return_value=None):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as ac:
                resp = await ac.get("/api/v1/auth/oidc/callback?code=abc&state=bad-state")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_oidc_callback_success(self):
        identity = UserIdentity(
            email="oidc-user@corp.com", display_name="OIDC User",
            groups=["devops"], source_backend="oidc", raw_claims={},
        )
        db = make_mock_db()
        async def _refresh(obj):
            obj.id = uuid.uuid4()
            obj.created_at = NOW
        db.refresh = _refresh

        state_key = "test-state-oidc"

        with (
            patch("src.api.sso.consume_state", new_callable=AsyncMock,
                  return_value=("acme", None, None, "cv-oidc")),
            patch("src.api.sso.exchange_code", new_callable=AsyncMock, return_value=identity),
            patch("src.api.sso.get_tenant_db", lambda tid: _yield(db)),
            patch("src.auth.jit.hash_password", return_value="$2b$12$fakehash"),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                follow_redirects=False,
            ) as ac:
                resp = await ac.get(f"/api/v1/auth/oidc/callback?code=test-code&state={state_key}")

        assert resp.status_code == 302
        location = resp.headers["location"]
        assert "/sso/callback" in location
        assert "token=" not in location, "token must not leak in redirect URL"
        assert resp.cookies.get("access_token"), "access_token cookie must be set"


# ---------------------------------------------------------------------------
# JIT with group-to-role mapping
# ---------------------------------------------------------------------------

class TestJitGroupMapping:

    @pytest.mark.asyncio
    async def test_resolve_group_role_with_match(self):
        from src.auth.jit import resolve_group_role
        db = make_mock_db()
        mapping = types.SimpleNamespace(
            id=uuid.uuid4(), idp_group_name="engineers",
            project_id=None, role="modeler",
        )
        result = MagicMock()
        result.scalars.return_value.all.return_value = [mapping]
        db.execute = AsyncMock(return_value=result)

        role = await resolve_group_role(db, ["engineers"])
        assert role == "modeler"

    @pytest.mark.asyncio
    async def test_resolve_group_role_no_match(self):
        from src.auth.jit import resolve_group_role
        db = make_mock_db()
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=result)

        role = await resolve_group_role(db, ["unknown-group"])
        assert role is None

    @pytest.mark.asyncio
    async def test_resolve_group_role_empty_groups(self):
        from src.auth.jit import resolve_group_role
        db = make_mock_db()
        role = await resolve_group_role(db, [])
        assert role is None

    @pytest.mark.asyncio
    async def test_resolve_group_role_highest_wins(self):
        from src.auth.jit import resolve_group_role
        db = make_mock_db()
        mappings = [
            types.SimpleNamespace(id=uuid.uuid4(), idp_group_name="viewers", project_id=None, role="viewer"),
            types.SimpleNamespace(id=uuid.uuid4(), idp_group_name="admins", project_id=None, role="admin"),
        ]
        result = MagicMock()
        result.scalars.return_value.all.return_value = mappings
        db.execute = AsyncMock(return_value=result)

        role = await resolve_group_role(db, ["viewers", "admins"])
        assert role == "admin"

    @pytest.mark.asyncio
    async def test_jit_adopt_uses_group_role(self):
        from src.auth.jit import jit_adopt_user
        db = make_mock_db()

        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = None

        mapping = types.SimpleNamespace(
            id=uuid.uuid4(), idp_group_name="eng", project_id=None, role="modeler",
        )
        group_result = MagicMock()
        group_result.scalars.return_value.all.return_value = [mapping]

        # Call sequence (F-021-03 + Bug-6303): user lookup, tenant-wide
        # group-role resolution, the project-scoped binding sync query (empty
        # here, the mapping is tenant-wide), then the sso_group revocation scan
        # (also empty — nothing to reconcile away).
        empty_bindings = MagicMock()
        empty_bindings.scalars.return_value.all.return_value = []
        revoke_scan = MagicMock()
        revoke_scan.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(
            side_effect=[user_result, group_result, empty_bindings, revoke_scan]
        )

        async def _refresh(obj):
            obj.id = uuid.uuid4()
            obj.created_at = NOW
        db.refresh = _refresh

        identity = UserIdentity(
            email="eng@corp.com", display_name="Engineer",
            groups=["eng"], source_backend="oidc", raw_claims={},
        )

        with patch("src.auth.jit.hash_password", return_value="$2b$12$fakehash"):
            user, role = await jit_adopt_user(db, identity, "acme")

        # Tenant-wide modeler mapping keeps the cosmetic LocalUser.role.
        assert role == "modeler"
        assert user.role == "modeler"

    @pytest.mark.asyncio
    async def test_jit_adopt_tenant_wide_admin_elevates_to_tenant_admin(self):
        """F-021-03: a tenant-wide ``admin`` group mapping grants real access by
        elevating the user to ``tenant_admin`` (which require_role honours),
        instead of a cosmetic ``admin`` string that no check recognises."""
        from src.auth.jit import jit_adopt_user
        db = make_mock_db()

        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = None
        mapping = types.SimpleNamespace(
            id=uuid.uuid4(), idp_group_name="admins", project_id=None, role="admin",
        )
        group_result = MagicMock()
        group_result.scalars.return_value.all.return_value = [mapping]
        empty_bindings = MagicMock()
        empty_bindings.scalars.return_value.all.return_value = []
        # Bug-6666: the new-user tenant_admin path now emits an audit event
        # via _audit_sso_reconcile, which calls audit() -> _get_audit_level()
        # (one db.execute for the TenantSetting lookup).
        audit_level_result = MagicMock()
        audit_level_result.scalar_one_or_none.return_value = None
        # Bug-6303: trailing sso_group revocation scan (empty).
        revoke_scan = MagicMock()
        revoke_scan.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(
            side_effect=[
                user_result, group_result,
                audit_level_result,  # Bug-6666: audit level query
                empty_bindings, revoke_scan,
            ]
        )

        async def _refresh(obj):
            obj.id = uuid.uuid4()
            obj.created_at = NOW
        db.refresh = _refresh

        identity = UserIdentity(
            email="boss@corp.com", display_name="Boss",
            groups=["admins"], source_backend="oidc", raw_claims={},
        )
        with patch("src.auth.jit.hash_password", return_value="$2b$12$fakehash"):
            user, role = await jit_adopt_user(db, identity, "acme")
        assert role == "tenant_admin"
        assert user.role == "tenant_admin"

    @pytest.mark.asyncio
    async def test_jit_adopt_project_scoped_mapping_creates_binding(self):
        """F-021-03: a project-scoped group mapping materialises a real
        ``UserAccessBinding`` so the SSO user actually gains the mapped role on
        that project — previously these rows were stored but never consulted."""
        from src.auth.jit import jit_adopt_user
        from shared.db.models import UserAccessBinding
        db = make_mock_db()
        project_id = uuid.uuid4()

        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = None
        # Tenant-wide resolution: no tenant-wide mapping matches.
        tenant_group_result = MagicMock()
        tenant_group_result.scalars.return_value.all.return_value = []
        setting_result = MagicMock()
        setting_result.scalar_one_or_none.return_value = None
        # Project-scoped binding sync: one project-scoped modeler mapping.
        proj_mapping = types.SimpleNamespace(
            id=uuid.uuid4(), idp_group_name="eng", project_id=project_id, role="modeler",
        )
        proj_mappings_result = MagicMock()
        proj_mappings_result.scalars.return_value.all.return_value = [proj_mapping]
        # Existing-binding lookup for that project: none.
        existing_binding_result = MagicMock()
        existing_binding_result.scalar_one_or_none.return_value = None
        # Bug-6303: trailing sso_group revocation scan (empty — no stale grants).
        revoke_scan = MagicMock()
        revoke_scan.scalars.return_value.all.return_value = []

        db.execute = AsyncMock(side_effect=[
            user_result, tenant_group_result, setting_result,
            proj_mappings_result, existing_binding_result, revoke_scan,
        ])

        async def _refresh(obj):
            obj.id = uuid.uuid4()
            obj.created_at = NOW
        db.refresh = _refresh

        identity = UserIdentity(
            email="eng2@corp.com", display_name="Eng2",
            groups=["eng"], source_backend="oidc", raw_claims={},
        )
        with patch("src.auth.jit.hash_password", return_value="$2b$12$fakehash"):
            await jit_adopt_user(db, identity, "acme")

        added_bindings = [
            c.args[0] for c in db.add.call_args_list
            if isinstance(c.args[0], UserAccessBinding)
        ]
        assert len(added_bindings) == 1
        b = added_bindings[0]
        assert str(b.project_id) == str(project_id)
        assert b.role == "modeler"
        assert b.model_id is None
        assert b.user_identity == "eng2@corp.com"
        # Bug-6303: group-materialised bindings are stamped sso_group so they
        # can be reconciled/revoked on de-provisioning without touching manual
        # grants.
        assert b.source == "sso_group"

    @pytest.mark.asyncio
    async def test_jit_adopt_fallback_default_role(self):
        from src.auth.jit import jit_adopt_user
        db = make_mock_db()

        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = None

        setting_result = MagicMock()
        setting_result.scalar_one_or_none.return_value = None

        db.execute = AsyncMock(side_effect=[user_result, setting_result])

        async def _refresh(obj):
            obj.id = uuid.uuid4()
            obj.created_at = NOW
        db.refresh = _refresh

        identity = UserIdentity(
            email="nogroup@corp.com", display_name="No Group",
            groups=[], source_backend="saml", raw_claims={},
        )

        with patch("src.auth.jit.hash_password", return_value="$2b$12$fakehash"):
            user, role = await jit_adopt_user(db, identity, "acme")

        assert role == "viewer"


# ---------------------------------------------------------------------------
# Group mappings CRUD API
# ---------------------------------------------------------------------------

class TestGroupMappingsApi:

    @pytest.mark.asyncio
    async def test_list_empty(self):
        _admin_override()
        db = make_mock_db()
        try:
            with patch("src.api.group_mappings.get_tenant_db", async_gen_from(db)):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://testserver"
                ) as ac:
                    resp = await ac.get("/api/v1/admin/group-mappings")
            assert resp.status_code == 200
            assert resp.json() == []
        finally:
            _clear_overrides()

    @pytest.mark.asyncio
    async def test_create_mapping(self):
        _admin_override()
        db = make_mock_db()

        existing_result = MagicMock()
        existing_result.scalar_one_or_none.return_value = None

        audit_result = MagicMock()
        audit_result.scalar_one_or_none.return_value = None

        db.execute = AsyncMock(side_effect=[existing_result, audit_result])

        async def _refresh(obj):
            obj.id = uuid.uuid4()
            obj.created_at = NOW
        db.refresh = _refresh

        try:
            with patch("src.api.group_mappings.get_tenant_db", async_gen_from(db)):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://testserver"
                ) as ac:
                    resp = await ac.post(
                        "/api/v1/admin/group-mappings",
                        json={"idp_group_name": "engineers", "role": "modeler"},
                    )
            assert resp.status_code == 201
            assert resp.json()["idp_group_name"] == "engineers"
            assert resp.json()["role"] == "modeler"
        finally:
            _clear_overrides()

    @pytest.mark.asyncio
    async def test_create_mapping_invalid_role(self):
        _admin_override()
        db = make_mock_db()
        try:
            with patch("src.api.group_mappings.get_tenant_db", async_gen_from(db)):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://testserver"
                ) as ac:
                    resp = await ac.post(
                        "/api/v1/admin/group-mappings",
                        json={"idp_group_name": "eng", "role": "superadmin"},
                    )
            assert resp.status_code == 400
        finally:
            _clear_overrides()

    @pytest.mark.asyncio
    async def test_delete_mapping(self):
        _admin_override()
        db = make_mock_db()

        mapping = types.SimpleNamespace(
            id=uuid.uuid4(), idp_group_name="old-group",
            project_id=None, role="viewer",
        )
        db.get = AsyncMock(return_value=mapping)

        audit_result = MagicMock()
        audit_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=audit_result)

        try:
            with patch("src.api.group_mappings.get_tenant_db", async_gen_from(db)):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://testserver"
                ) as ac:
                    resp = await ac.delete(f"/api/v1/admin/group-mappings/{mapping.id}")
            assert resp.status_code == 204
        finally:
            _clear_overrides()

    @pytest.mark.asyncio
    async def test_update_mapping(self):
        _admin_override()
        db = make_mock_db()

        mapping = types.SimpleNamespace(
            id=uuid.uuid4(), idp_group_name="eng",
            project_id=None, role="viewer", created_at=NOW,
        )
        db.get = AsyncMock(return_value=mapping)

        audit_result = MagicMock()
        audit_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=audit_result)

        async def _refresh(obj):
            pass
        db.refresh = _refresh

        try:
            with patch("src.api.group_mappings.get_tenant_db", async_gen_from(db)):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://testserver"
                ) as ac:
                    resp = await ac.put(
                        f"/api/v1/admin/group-mappings/{mapping.id}",
                        json={"role": "admin"},
                    )
            assert resp.status_code == 200
            assert resp.json()["role"] == "admin"
        finally:
            _clear_overrides()


# ---------------------------------------------------------------------------
# Auth chain registration
# ---------------------------------------------------------------------------

class TestAuthChainRegistration:

    def test_saml_oidc_not_added_to_credential_chain(self):
        with patch("src.auth.chain.get_settings") as mock_s:
            s = MagicMock()
            s.AUTH_BACKENDS = "local,saml,oidc"
            mock_s.return_value = s

            from src.auth.chain import _build_backends
            backends = _build_backends()
            names = [b.name for b in backends]
            assert "local" in names
            assert "saml" not in names
            assert "oidc" not in names


# ---------------------------------------------------------------------------
# Async generator helper (same as conftest)
# ---------------------------------------------------------------------------

async def _yield(value):
    yield value


class TestModelTechnicalGrantPath:
    """H-1 (B1 deep review): the model_technical audience role must be
    grantable — through the SSO group-mapping API here, and through the
    local-user role validator (see test_auth.py)."""

    @pytest.mark.asyncio
    async def test_create_mapping_accepts_model_technical(self):
        _admin_override()
        db = make_mock_db()

        existing_result = MagicMock()
        existing_result.scalar_one_or_none.return_value = None
        audit_result = MagicMock()
        audit_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(side_effect=[existing_result, audit_result])

        async def _refresh(obj):
            obj.id = uuid.uuid4()
            obj.created_at = NOW
        db.refresh = _refresh

        try:
            with patch("src.api.group_mappings.get_tenant_db", async_gen_from(db)):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://testserver"
                ) as ac:
                    resp = await ac.post(
                        "/api/v1/admin/group-mappings",
                        json={
                            "idp_group_name": "data-engineers",
                            "role": "model_technical",
                        },
                    )
            assert resp.status_code == 201
            assert resp.json()["role"] == "model_technical"
        finally:
            _clear_overrides()


# ---------------------------------------------------------------------------
# Bug-1072 — JWT claim bounding on SSO callbacks
# ---------------------------------------------------------------------------

def _make_sso_db(referenced_claim_names: list[str]):
    """make_mock_db variant whose execute() answers the row_security_rules
    claim-name query with *referenced_claim_names* and everything else with
    the empty defaults."""
    db = make_mock_db()

    async def _refresh(obj):
        obj.id = uuid.uuid4()
        obj.created_at = NOW
    db.refresh = _refresh

    default = MagicMock()
    default.scalar_one_or_none.return_value = None
    default.scalars.return_value.all.return_value = []
    rules = MagicMock()
    rules.scalars.return_value.all.return_value = list(referenced_claim_names)

    async def _execute(stmt, *args, **kwargs):
        if "row_security_rules" in str(stmt).lower():
            return rules
        return default

    db.execute = _execute
    return db


class TestJwtClaimBounding:
    """The SSO callbacks must not embed an unbounded IdP attribute set in
    the signed JWT: unreferenced attributes are dropped (with a log) and
    an oversized referenced set fails the login cleanly."""

    def test_unreferenced_attributes_are_dropped_with_log(self, caplog):
        from src.auth.claim_bounds import bound_token_claims
        raw = {"department": "sales", "memberships": ["g1", "g2"], "phone": "x"}
        with caplog.at_level("INFO", logger="src.auth.claim_bounds"):
            kept = bound_token_claims(
                raw, {"department"}, backend="saml", subject="a@x",
            )
        assert kept == {"department": "sales"}
        assert any(
            "dropped 2 IdP attribute" in r.getMessage() for r in caplog.records
        ), "dropping attributes must be logged"

    def test_allowlist_keeps_extra_claims(self):
        from src.auth.claim_bounds import bound_token_claims
        with patch("src.auth.claim_bounds.get_settings") as gs:
            s = MagicMock()
            s.AUTH_JWT_CLAIMS_ALLOWLIST = "department, locale"
            s.AUTH_JWT_CLAIMS_MAX_BYTES = 4096
            gs.return_value = s
            kept = bound_token_claims(
                {"department": "d", "locale": "en", "junk": "x"},
                set(), backend="oidc", subject="a@x",
            )
        assert kept == {"department": "d", "locale": "en"}

    def test_oversized_referenced_claims_raise(self):
        from src.auth.claim_bounds import ClaimsTooLargeError, bound_token_claims
        with pytest.raises(ClaimsTooLargeError):
            bound_token_claims(
                {"blob": "x" * 10000}, {"blob"}, backend="saml", subject="a@x",
            )

    @pytest.mark.asyncio
    async def test_saml_acs_filters_token_claims_to_referenced(self):
        """Oversized *unreferenced* attribute set: login succeeds and the
        token carries only the rule-referenced claim."""
        raw = {"department": "sales"}
        raw.update({f"attr_{i}": "v" * 50 for i in range(60)})
        identity = UserIdentity(
            email="saml-user@corp.com", display_name="SAML User",
            groups=["engineers"], source_backend="saml", raw_claims=raw,
        )
        db = _make_sso_db(["department"])

        state_key = "test-state-saml-bound"

        with (
            patch("src.api.sso.consume_state", new_callable=AsyncMock,
                  return_value=("acme", None, "req-1", None)),
            patch("src.api.sso.process_saml_response",
                  return_value=_saml_result(identity)),
            patch("src.api.sso.record_assertion_or_reject",
                  new_callable=AsyncMock, return_value=True),
            patch("src.api.sso.get_tenant_db", lambda tid: _yield(db)),
            patch("src.auth.jit.hash_password", return_value="$2b$12$fakehash"),
            patch(
                "src.api.sso.create_access_token", return_value="fake-token"
            ) as mock_token,
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                follow_redirects=False,
            ) as ac:
                resp = await ac.post(
                    "/api/v1/auth/saml/acs",
                    data={"SAMLResponse": "valid", "RelayState": state_key},
                )

        assert resp.status_code == 302
        assert mock_token.call_args.kwargs["claims"] == {"department": "sales"}

    @pytest.mark.asyncio
    async def test_saml_acs_oversized_referenced_set_returns_413(self):
        """Oversized *referenced* attribute set: dropping it could widen row
        access, so the login fails with a clean 413 instead."""
        identity = UserIdentity(
            email="saml-user@corp.com", display_name="SAML User",
            groups=["engineers"], source_backend="saml",
            raw_claims={"blob": "x" * 10000},
        )
        db = _make_sso_db(["blob"])

        state_key = "test-state-saml-oversize"

        with (
            patch("src.api.sso.consume_state", new_callable=AsyncMock,
                  return_value=("acme", None, "req-1", None)),
            patch("src.api.sso.process_saml_response",
                  return_value=_saml_result(identity)),
            patch("src.api.sso.record_assertion_or_reject",
                  new_callable=AsyncMock, return_value=True),
            patch("src.api.sso.get_tenant_db", lambda tid: _yield(db)),
            patch("src.api.sso.jit_adopt_user", new_callable=AsyncMock) as jit_mock,
            patch("src.api.sso.audit", new_callable=AsyncMock) as audit_mock,
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                follow_redirects=False,
            ) as ac:
                resp = await ac.post(
                    "/api/v1/auth/saml/acs",
                    data={"SAMLResponse": "valid", "RelayState": state_key},
                )

        assert resp.status_code == 413
        assert "AUTH_JWT_CLAIMS_MAX_BYTES" in resp.json()["detail"]
        jit_mock.assert_not_awaited()
        audit_mock.assert_not_awaited()
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_oidc_callback_filters_token_claims_to_referenced(self):
        raw = {"scope": "openid reports:read", "picture": "p" * 500}
        identity = UserIdentity(
            email="oidc-user@corp.com", display_name="OIDC User",
            groups=["devops"], source_backend="oidc", raw_claims=raw,
        )
        db = _make_sso_db(["scope"])

        state_key = "test-state-oidc-bound"

        with (
            patch("src.api.sso.consume_state", new_callable=AsyncMock,
                  return_value=("acme", None, None, "cv-oidc")),
            patch("src.api.sso.exchange_code", new_callable=AsyncMock, return_value=identity),
            patch("src.api.sso.get_tenant_db", lambda tid: _yield(db)),
            patch("src.auth.jit.hash_password", return_value="$2b$12$fakehash"),
            patch(
                "src.api.sso.create_access_token", return_value="fake-token"
            ) as mock_token,
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                follow_redirects=False,
            ) as ac:
                resp = await ac.get(
                    f"/api/v1/auth/oidc/callback?code=test-code&state={state_key}"
                )

        assert resp.status_code == 302
        assert mock_token.call_args.kwargs["claims"] == {
            "scope": "openid reports:read"
        }

    @pytest.mark.asyncio
    async def test_oidc_callback_oversized_referenced_set_returns_413_without_jit(self):
        identity = UserIdentity(
            email="oidc-user@corp.com", display_name="OIDC User",
            groups=["devops"], source_backend="oidc",
            raw_claims={"scope": "x" * 10000},
        )
        db = _make_sso_db(["scope"])

        state_key = "test-state-oidc-oversize"

        with (
            patch("src.api.sso.consume_state", new_callable=AsyncMock,
                  return_value=("acme", None, None, "cv-oidc")),
            patch("src.api.sso.exchange_code", new_callable=AsyncMock, return_value=identity),
            patch("src.api.sso.get_tenant_db", lambda tid: _yield(db)),
            patch("src.api.sso.jit_adopt_user", new_callable=AsyncMock) as jit_mock,
            patch("src.api.sso.audit", new_callable=AsyncMock) as audit_mock,
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                follow_redirects=False,
            ) as ac:
                resp = await ac.get(
                    f"/api/v1/auth/oidc/callback?code=test-code&state={state_key}"
                )

        assert resp.status_code == 413
        assert "AUTH_JWT_CLAIMS_MAX_BYTES" in resp.json()["detail"]
        jit_mock.assert_not_awaited()
        audit_mock.assert_not_awaited()
        db.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# Bug-9302: OIDC token_endpoint must use SSRF-safe transport
# ---------------------------------------------------------------------------

class TestOidcTokenEndpointSsrf:

    @pytest.mark.asyncio
    async def test_bug_9302_oidc_token_endpoint_localhost_never_fetched(
        self, monkeypatch
    ):
        """A tenant-admin-set token_endpoint of http://localhost:9999 must not
        be fetched. exchange_code returns None.
        """
        from src.auth import oidc_backend

        fetched: list[str] = []

        monkeypatch.setattr(
            oidc_backend,
            "get_oidc_config",
            lambda: {
                "issuer": "https://idp.example.com",
                "client_id": "cid",
                "client_secret": "sec",
                "scopes": "openid",
                "groups_claim": "groups",
            },
        )

        async def _disc(_issuer):
            return {
                "token_endpoint": "http://localhost:9999",
                "jwks_uri": "https://idp.example.com/jwks",
            }

        monkeypatch.setattr(oidc_backend, "_discover", _disc)

        class SpyClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, **kwargs):
                fetched.append(str(url))
                raise AssertionError(f"must not POST to {url}")

            async def get(self, url, **kwargs):
                fetched.append(str(url))
                raise AssertionError(f"must not GET {url}")

        monkeypatch.setattr(oidc_backend.httpx, "AsyncClient", SpyClient)

        result = await oidc_backend.exchange_code(
            "https://app.example.com", "the-code"
        )
        assert result is None
        assert fetched == []
        assert not any("localhost:9999" in u for u in fetched)


# ---------------------------------------------------------------------------
# Bug-9325: PUT /sso-config must PRESERVE overlay keys the form omits
# ---------------------------------------------------------------------------

def _written_sso_payload(insert_stmt) -> dict:
    """Pull the value_json (sso.config) dict bound into the pg INSERT."""
    from sqlalchemy.dialects import postgresql

    params = insert_stmt.compile(dialect=postgresql.dialect()).params
    for value in params.values():
        if isinstance(value, dict) and ("oidc" in value or "saml" in value):
            return value
    raise AssertionError("no sso.config payload bound in insert statement")


def _put_sso_config_db(existing_overlay: dict):
    """A mock AsyncSession whose FOR UPDATE select returns *existing_overlay*
    and whose INSERT is captured for inspection. audit_required is patched out
    by the caller, so only the overlay select and the config insert reach here.
    """
    db = make_mock_db()
    captured: dict = {}

    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = types.SimpleNamespace(
        value_json=existing_overlay
    )

    async def _execute(stmt, *args, **kwargs):
        if getattr(stmt, "is_select", False):
            captured["select"] = stmt
            return existing_result
        if getattr(stmt, "is_insert", False):
            captured["insert"] = stmt
            return MagicMock()
        default = MagicMock()
        default.scalar_one_or_none.return_value = None
        return default

    db.execute = _execute
    return db, captured


class TestPutSsoConfigPreserveOverlay:
    """Bug-9325 (G-021-02 overlay round-trip): the Identity-provider Save form
    submits only a subset of overlay keys. The backend must preserve the keys
    it omits (oidc.scopes, oidc.groups_claim, saml.idp_metadata_xml, and the
    stored client_secret_enc), which the OIDC/SAML backends consume, and must
    do the read+write in one FOR UPDATE-locked transaction."""

    @pytest.mark.asyncio
    async def test_put_sso_config_preserves_unedited_overlay_keys_and_secret(self):
        _admin_override()
        existing = {
            "oidc": {
                "issuer": "https://old-issuer.example",
                "client_id": "old-client",
                "scopes": "openid profile email",
                "groups_claim": "groups",
                "client_secret_enc": "PRESERVED_ENC_BLOB",
            },
            "saml": {
                "idp_metadata_url": "https://old-idp.example/meta",
                "idp_metadata_xml": "<EntityDescriptor>preserved</EntityDescriptor>",
            },
        }
        db, captured = _put_sso_config_db(existing)
        # UI subset PUT: new issuer/client_id, new idp_metadata_url, no secret.
        body = {
            "oidc": {
                "issuer": "https://new-issuer.example",
                "client_id": "new-client",
            },
            "saml": {"idp_metadata_url": "https://new-idp.example/meta"},
        }
        try:
            with (
                patch("src.api.sso.get_tenant_db", lambda tid: _yield(db)),
                patch("src.api.sso.audit_required", new_callable=AsyncMock),
            ):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as ac:
                    resp = await ac.put("/api/v1/auth/sso-config", json=body)
        finally:
            _clear_overrides()

        assert resp.status_code == 200

        payload = _written_sso_payload(captured["insert"])
        # Omitted keys survive unchanged (the actual bug).
        assert payload["oidc"]["scopes"] == "openid profile email"
        assert payload["oidc"]["groups_claim"] == "groups"
        assert payload["saml"]["idp_metadata_xml"] == (
            "<EntityDescriptor>preserved</EntityDescriptor>"
        )
        # Stored secret preserved because no new one was supplied.
        assert payload["oidc"]["client_secret_enc"] == "PRESERVED_ENC_BLOB"
        # Submitted keys override.
        assert payload["oidc"]["issuer"] == "https://new-issuer.example"
        assert payload["oidc"]["client_id"] == "new-client"
        assert payload["saml"]["idp_metadata_url"] == "https://new-idp.example/meta"

        # Read+write serialise: the overlay row is locked FOR UPDATE.
        from sqlalchemy.dialects import postgresql
        select_sql = str(
            captured["select"].compile(dialect=postgresql.dialect())
        ).lower()
        assert "for update" in select_sql

        # Response never leaks the secret but reports it is set.
        data = resp.json()
        assert "client_secret_enc" not in data["oidc"]
        assert "client_secret" not in data["oidc"]
        assert data["oidc"]["client_secret_set"] is True
        assert data["oidc"]["scopes"] == "openid profile email"

    @pytest.mark.asyncio
    async def test_put_sso_config_supplied_secret_replaces_enc(self):
        import base64
        from shared.security.credential_crypto import decrypt_str

        _admin_override()
        existing = {
            "oidc": {
                "issuer": "https://issuer.example",
                "client_id": "client",
                "scopes": "openid",
                "client_secret_enc": "OLD_ENC_BLOB",
            },
            "saml": {},
        }
        db, captured = _put_sso_config_db(existing)
        body = {
            "oidc": {
                "issuer": "https://issuer.example",
                "client_id": "client",
                "client_secret": "brand-new-secret",
            },
            "saml": {},
        }
        try:
            with (
                patch("src.api.sso.get_tenant_db", lambda tid: _yield(db)),
                patch("src.api.sso.audit_required", new_callable=AsyncMock),
            ):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as ac:
                    resp = await ac.put("/api/v1/auth/sso-config", json=body)
        finally:
            _clear_overrides()

        assert resp.status_code == 200
        payload = _written_sso_payload(captured["insert"])
        new_enc = payload["oidc"]["client_secret_enc"]
        assert new_enc != "OLD_ENC_BLOB"
        # The new secret is what the OIDC backend will decrypt at login.
        assert decrypt_str(base64.b64decode(new_enc)) == "brand-new-secret"
        # Non-secret submitted plaintext must never be persisted.
        assert "client_secret" not in payload["oidc"]
        # Unedited key still preserved alongside the secret rotation.
        assert payload["oidc"]["scopes"] == "openid"

    @pytest.mark.asyncio
    async def test_put_sso_config_first_ever_save_inserts_subset(self):
        """First-ever save (no existing row): the FOR UPDATE select returns
        None and the submitted subset is written as-is."""
        _admin_override()
        db = make_mock_db()
        captured: dict = {}

        none_result = MagicMock()
        none_result.scalar_one_or_none.return_value = None

        async def _execute(stmt, *args, **kwargs):
            if getattr(stmt, "is_insert", False):
                captured["insert"] = stmt
                return MagicMock()
            return none_result

        db.execute = _execute
        body = {
            "oidc": {"issuer": "https://i.example", "client_id": "c"},
            "saml": {},
        }
        try:
            with (
                patch("src.api.sso.get_tenant_db", lambda tid: _yield(db)),
                patch("src.api.sso.audit_required", new_callable=AsyncMock),
            ):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as ac:
                    resp = await ac.put("/api/v1/auth/sso-config", json=body)
        finally:
            _clear_overrides()

        assert resp.status_code == 200
        payload = _written_sso_payload(captured["insert"])
        assert payload["oidc"] == {"issuer": "https://i.example", "client_id": "c"}
        assert payload["saml"] == {}
