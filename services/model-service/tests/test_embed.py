"""Unit tests for embed token API: POST /api/v1/auth/embed-token."""
from __future__ import annotations

import uuid as _uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx
from fastapi import HTTPException

from shared.auth.middleware import CurrentEmbedUser, CurrentUser
from src.auth.local_backend import create_embed_token, decode_access_token
from src.auth.middleware import get_current_user, require_tenant_admin
from src.main import app

pytestmark = pytest.mark.unit

# Stable UUIDs for scope validation tests (Bug-5943)
_PERSONA_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_MODEL_UUID_1 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_MODEL_UUID_2 = "cccccccc-cccc-cccc-cccc-cccccccccccc"
_PROJECT_UUID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
_PROJECT_PERSONA_UUID = "99999999-9999-9999-9999-999999999999"


def _admin_user(tenant_id: str = "acme") -> CurrentUser:
    return CurrentUser(
        user_id="admin@example.com",
        tenant_id=tenant_id,
        email="admin@example.com",
        role="tenant_admin",
    )


def _system_admin() -> CurrentUser:
    return CurrentUser(
        user_id="sysadmin@tessallite.local",
        tenant_id="__system__",
        email="sysadmin@tessallite.local",
        role="system_admin",
    )


async def _fake_tenant_db(tenant_id: str = ""):
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    audit_result = MagicMock()
    audit_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=audit_result)
    yield db


async def _fake_tenant_db_with_scope(tenant_id: str = ""):
    """Fake tenant DB that returns mock objects for Bug-5943 scope validation.

    The ``get()`` method returns a mock for any known test UUID, or None for
    unknowns, so the embed-token scope validation can verify existence.
    """
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    audit_result = MagicMock()
    audit_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=audit_result)

    _known: dict[str, set[str]] = {
        "Persona": {_PERSONA_UUID},
        "ProjectPersona": {_PROJECT_PERSONA_UUID},
        "Project": {_PROJECT_UUID},
        "Model": {_MODEL_UUID_1, _MODEL_UUID_2},
    }

    async def _get(model_cls, pk):
        cls_name = model_cls.__name__
        if str(pk) in _known.get(cls_name, set()):
            obj = MagicMock()
            obj.id = pk
            if cls_name == "Model":
                obj.project_id = _uuid.UUID(_PROJECT_UUID)
            if cls_name == "Persona":
                # Bug-8253: the persona belongs to model M1 (project P).
                obj.model_id = _uuid.UUID(_MODEL_UUID_1)
            if cls_name == "ProjectPersona":
                obj.project_id = _uuid.UUID(_PROJECT_UUID)
            return obj
        return None

    db.get = _get
    yield db


# Bug-8253: a second project/model the persona does NOT belong to.
_PROJECT_UUID_2 = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
_MODEL_UUID_3 = "ffffffff-ffff-ffff-ffff-ffffffffffff"


async def _fake_tenant_db_persona_scope(tenant_id: str = ""):
    """Fake tenant DB for Bug-8253: persona belongs to M1/P1; M3 is in P2."""
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    audit_result = MagicMock()
    audit_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=audit_result)

    async def _get(model_cls, pk):
        cls_name = model_cls.__name__
        pk_s = str(pk)
        if cls_name == "Persona" and pk_s == _PERSONA_UUID:
            obj = MagicMock()
            obj.id = pk
            obj.model_id = _uuid.UUID(_MODEL_UUID_1)  # persona lives in M1/P1
            return obj
        if cls_name == "Model":
            obj = MagicMock()
            obj.id = pk
            if pk_s in (_MODEL_UUID_1, _MODEL_UUID_2):
                obj.project_id = _uuid.UUID(_PROJECT_UUID)
            elif pk_s == _MODEL_UUID_3:
                obj.project_id = _uuid.UUID(_PROJECT_UUID_2)
            else:
                return None
            return obj
        if cls_name == "Project" and pk_s in (_PROJECT_UUID, _PROJECT_UUID_2):
            obj = MagicMock()
            obj.id = pk
            return obj
        if cls_name == "ProjectPersona" and pk_s == _PROJECT_PERSONA_UUID:
            obj = MagicMock()
            obj.id = pk
            obj.project_id = _uuid.UUID(_PROJECT_UUID)
            return obj
        return None

    db.get = _get
    yield db


async def _fake_system_db():
    db = AsyncMock()
    # Return a fake tenant for the SystemTenant lookup
    result = MagicMock()
    result.scalar_one_or_none.return_value = AsyncMock(slug="acme", id="fake-id")
    db.execute = AsyncMock(return_value=result)
    yield db


@pytest.fixture
async def admin_client():
    admin = _admin_user()
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[require_tenant_admin] = lambda: admin
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def sysadmin_client():
    admin = _system_admin()
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[require_tenant_admin] = lambda: admin
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def member_client():
    user = CurrentUser(
        user_id="member@example.com",
        tenant_id="acme",
        email="member@example.com",
        role="member",
    )
    app.dependency_overrides[get_current_user] = lambda: user
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


URL = "/api/v1/auth/embed-token"


@patch("src.api.embed.get_system_db", new=_fake_system_db)
class TestMintEmbedToken:
    @patch("src.api.embed.get_tenant_db", new=_fake_tenant_db)
    async def test_basic_embed_token(self, admin_client):
        resp = await admin_client.post(URL, json={
            "tenant_id": "acme",
            "user_identity": "demo-viewer@customer.com",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "token" in data
        assert "expires_at" in data
        assert data["scope"]["tenant_id"] == "acme"
        assert data["scope"]["user_identity"] == "demo-viewer@customer.com"
        assert data["scope"]["capabilities"] == []
        assert data["scope"]["expiry_minutes"] == 180

        payload = decode_access_token(data["token"])
        assert payload["sub"] == "demo-viewer@customer.com"
        assert payload["tenant_id"] == "acme"
        assert payload["aud"] == "embed"
        assert payload["capabilities"] == []

    @patch("src.api.embed.get_tenant_db", new=_fake_tenant_db_with_scope)
    async def test_scoped_embed_token(self, admin_client):
        # Bug-5943: scope IDs must be valid UUIDs that exist in the tenant DB.
        # Bug-9196/F01: model Personas and agent ProjectPersonas are distinct
        # namespaces. The signed embed token carries both claims separately so
        # query-router keeps consuming ``persona_id`` while agent-service uses
        # only ``project_persona_id``.
        with patch("src.api.embed.emit_webhook", AsyncMock()):
            resp = await admin_client.post(URL, json={
                "tenant_id": "acme",
                "user_identity": "limited@customer.com",
                "persona_id": _PERSONA_UUID,
                "project_persona_id": _PROJECT_PERSONA_UUID,
                "project_ids": [_PROJECT_UUID],
                "model_ids": [_MODEL_UUID_1, _MODEL_UUID_2],
                "capabilities": ["chat"],
                "expiry_minutes": 60,
            })
        assert resp.status_code == 200
        data = resp.json()
        payload = decode_access_token(data["token"])
        assert payload["persona_id"] == _PERSONA_UUID
        assert payload["project_persona_id"] == _PROJECT_PERSONA_UUID
        assert payload["project_ids"] == [_PROJECT_UUID]
        assert payload["model_ids"] == [_MODEL_UUID_1, _MODEL_UUID_2]
        assert payload["capabilities"] == ["chat"]
        assert data["scope"]["persona_id"] == _PERSONA_UUID
        assert data["scope"]["project_persona_id"] == _PROJECT_PERSONA_UUID
        assert data["scope"]["expiry_minutes"] == 60

        from shared.auth.middleware import _build_user_from_payload

        user = _build_user_from_payload(payload)
        assert isinstance(user, CurrentEmbedUser)
        assert user.persona_id == _PERSONA_UUID
        assert user.project_persona_id == _PROJECT_PERSONA_UUID

    @patch("src.api.embed.get_tenant_db", new=_fake_tenant_db_with_scope)
    async def test_nonexistent_model_rejected(self, admin_client):
        """Bug-5943: nonexistent model IDs are rejected at mint time."""
        resp = await admin_client.post(URL, json={
            "tenant_id": "acme",
            "user_identity": "limited@customer.com",
            "model_ids": ["eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"],
        })
        assert resp.status_code == 422
        assert "does not exist" in resp.json()["detail"]

    @patch("src.api.embed.get_tenant_db", new=_fake_tenant_db_with_scope)
    async def test_nonexistent_persona_rejected(self, admin_client):
        """Bug-5943: nonexistent persona IDs are rejected at mint time."""
        resp = await admin_client.post(URL, json={
            "tenant_id": "acme",
            "user_identity": "limited@customer.com",
            "persona_id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
        })
        assert resp.status_code == 422
        assert "does not exist" in resp.json()["detail"]

    @patch("src.api.embed.get_tenant_db", new=_fake_tenant_db)
    async def test_default_expiry_is_180_minutes(self, admin_client):
        resp = await admin_client.post(URL, json={
            "tenant_id": "acme",
            "user_identity": "viewer",
        })
        assert resp.status_code == 200
        assert resp.json()["scope"]["expiry_minutes"] == 180

    async def test_missing_user_identity(self, admin_client):
        resp = await admin_client.post(URL, json={
            "tenant_id": "acme",
        })
        assert resp.status_code == 422

    async def test_expiry_too_short(self, admin_client):
        resp = await admin_client.post(URL, json={
            "tenant_id": "acme",
            "user_identity": "viewer",
            "expiry_minutes": 2,
        })
        assert resp.status_code == 422

    async def test_expiry_too_long(self, admin_client):
        resp = await admin_client.post(URL, json={
            "tenant_id": "acme",
            "user_identity": "viewer",
            "expiry_minutes": 2000,
        })
        assert resp.status_code == 422

    async def test_invalid_capability(self, admin_client):
        resp = await admin_client.post(URL, json={
            "tenant_id": "acme",
            "user_identity": "viewer",
            "capabilities": ["delete_everything"],
        })
        assert resp.status_code == 422

    async def test_member_cannot_create_embed_token(self, member_client):
        resp = await member_client.post(URL, json={
            "tenant_id": "acme",
            "user_identity": "viewer",
        })
        assert resp.status_code == 403

    @patch("src.api.embed.get_tenant_db", new=_fake_tenant_db)
    async def test_tenant_admin_cannot_create_for_other_tenant(self, admin_client):
        resp = await admin_client.post(URL, json={
            "tenant_id": "other-tenant",
            "user_identity": "viewer",
        })
        assert resp.status_code == 403

    @patch("src.api.embed.get_tenant_db", new=_fake_tenant_db)
    async def test_system_admin_can_create_for_any_tenant(self, sysadmin_client):
        resp = await sysadmin_client.post(URL, json={
            "tenant_id": "any-tenant",
            "user_identity": "viewer",
        })
        assert resp.status_code == 200
        assert resp.json()["scope"]["tenant_id"] == "any-tenant"


@patch("src.api.embed.get_system_db", new=_fake_system_db)
class TestEmbedRlsSubject:
    """Bug-7995 / F-024-01: an embed token carries an admin-authored row-security
    subject (role/groups/claims) so attribute/role RLS rules fire for the
    embedded session. Guard: the mint writes the subject into the signed token
    under the same claim names an interactive token uses. Tier: T1 (security
    producer/consumer contract). Test escape: prior mint tests never asserted an
    RLS subject was carried into the JWT."""

    @patch("src.api.embed.get_tenant_db", new=_fake_tenant_db)
    async def test_mint_carries_rls_subject_into_token(self, admin_client):
        resp = await admin_client.post(URL, json={
            "tenant_id": "acme",
            "user_identity": "finance-user@customer.com",
            "rls": {
                "role": "finance",
                "groups": ["finance", "emea"],
                "claims": {"department": "sales"},
            },
        })
        assert resp.status_code == 200
        data = resp.json()
        # Response scope echoes exactly what was signed.
        assert data["scope"]["rls"]["role"] == "finance"
        assert data["scope"]["rls"]["groups"] == ["finance", "emea"]
        assert data["scope"]["rls"]["claims"] == {"department": "sales"}
        # The signed token carries the subject under interactive claim names.
        payload = decode_access_token(data["token"])
        assert payload["aud"] == "embed"
        assert payload["role"] == "finance"
        assert payload["groups"] == ["finance", "emea"]
        assert payload["claims"] == {"department": "sales"}

    @patch("src.api.embed.get_tenant_db", new=_fake_tenant_db)
    async def test_mint_without_rls_omits_subject_claims(self, admin_client):
        resp = await admin_client.post(URL, json={
            "tenant_id": "acme",
            "user_identity": "viewer@customer.com",
        })
        assert resp.status_code == 200
        payload = decode_access_token(resp.json()["token"])
        # A bare embed token carries no RLS subject -> fails closed downstream.
        assert "role" not in payload
        assert "groups" not in payload
        assert "claims" not in payload
        assert resp.json()["scope"]["rls"] is None

    @patch("src.api.embed.get_tenant_db", new=_fake_tenant_db)
    async def test_mint_groups_only_subject(self, admin_client):
        resp = await admin_client.post(URL, json={
            "tenant_id": "acme",
            "user_identity": "u@customer.com",
            "rls": {"groups": ["finance"]},
        })
        assert resp.status_code == 200
        payload = decode_access_token(resp.json()["token"])
        assert payload["groups"] == ["finance"]
        assert "role" not in payload

    def test_empty_rls_groups_rejected(self):
        from pydantic import ValidationError
        from shared.schemas.pydantic_models import EmbedRlsSubject
        with pytest.raises(ValidationError):
            EmbedRlsSubject(groups=[])

    def test_empty_rls_claims_rejected(self):
        from pydantic import ValidationError
        from shared.schemas.pydantic_models import EmbedRlsSubject
        with pytest.raises(ValidationError):
            EmbedRlsSubject(claims={})

    def test_all_empty_rls_subject_rejected(self):
        from pydantic import ValidationError
        from shared.schemas.pydantic_models import EmbedRlsSubject
        with pytest.raises(ValidationError):
            EmbedRlsSubject()

    def test_sentinel_role_embed_rejected(self):
        """Opus review hardening: the literal string 'embed' is reserved as the
        internal sentinel and must be rejected at mint — it would be silently
        dropped downstream and never match any rule."""
        from pydantic import ValidationError
        from shared.schemas.pydantic_models import EmbedRlsSubject
        with pytest.raises(ValidationError, match="reserved"):
            EmbedRlsSubject(role="embed")


class TestEmbedTokenMiddleware:
    @pytest.mark.asyncio
    async def test_signed_embed_token_keeps_model_and_project_personas_distinct(self):
        """Bug-9196/F01: exercise the real signer, decoder, and QR resolver.

        ``persona_id`` remains the model-service/query-router Persona claim;
        ``project_persona_id`` is the only claim that can lock an agent
        ProjectPersona.  A legacy signed token has no project claim and must
        therefore leave the agent side unbound.
        """
        from shared.auth.middleware import _build_user_from_payload
        from shared.security.persona_resolver import resolve_effective_persona

        model_persona_id = _PERSONA_UUID
        project_persona_id = _PROJECT_PERSONA_UUID
        assert model_persona_id != project_persona_id
        token, _ = create_embed_token(
            user_identity="signed-viewer@customer.com",
            tenant_id="acme",
            persona_id=model_persona_id,
            project_persona_id=project_persona_id,
        )
        payload = decode_access_token(token)
        user = _build_user_from_payload(payload)
        assert user.persona_id == model_persona_id
        assert user.project_persona_id == project_persona_id

        sentinel = object()
        loader = AsyncMock(return_value=sentinel)
        db = AsyncMock()
        with patch(
            "shared.security.persona_resolver.load_persona_or_fail",
            new=loader,
        ):
            resolved = await resolve_effective_persona(
                db,
                current_user=user,
                model_id=_uuid.UUID(_MODEL_UUID_1),
                requested_persona_id=None,
            )
        assert resolved is sentinel
        loader.assert_awaited_once_with(
            db,
            model_persona_id,
            _uuid.UUID(_MODEL_UUID_1),
        )

        legacy_token, _ = create_embed_token(
            user_identity="legacy-viewer@customer.com",
            tenant_id="acme",
            persona_id=model_persona_id,
        )
        legacy_payload = decode_access_token(legacy_token)
        legacy_user = _build_user_from_payload(legacy_payload)
        assert legacy_user.persona_id == model_persona_id
        assert legacy_user.project_persona_id is None

    def test_embed_payload_produces_embed_user(self):
        from shared.auth.middleware import _build_user_from_payload
        payload = {
            "sub": "viewer@customer.com",
            "tenant_id": "acme",
            "aud": "embed",
            "persona_id": "p1",
            "project_persona_id": "agent-p1",
            "model_ids": ["m1", "m2"],
            "capabilities": ["query", "chat"],
        }
        user = _build_user_from_payload(payload)
        assert isinstance(user, CurrentEmbedUser)
        assert user.is_embed is True
        assert user.tenant_id == "acme"
        assert user.persona_id == "p1"
        assert user.project_persona_id == "agent-p1"
        assert user.model_ids == ["m1", "m2"]
        assert user.capabilities == ["query", "chat"]

    def test_regular_payload_produces_regular_user(self):
        from shared.auth.middleware import _build_user_from_payload
        payload = {
            "sub": "admin@example.com",
            "tenant_id": "acme",
            "role": "tenant_admin",
            "groups": ["admins"],
        }
        user = _build_user_from_payload(payload)
        assert not isinstance(user, CurrentEmbedUser)
        assert user.is_embed is False
        assert user.role == "tenant_admin"

    def test_embed_user_without_capabilities_gets_none(self):
        from shared.auth.middleware import _build_user_from_payload
        payload = {
            "sub": "viewer",
            "tenant_id": "acme",
            "aud": "embed",
        }
        user = _build_user_from_payload(payload)
        assert isinstance(user, CurrentEmbedUser)
        assert user.capabilities == []


class TestModelScopeEnforcement:
    def test_enforce_model_scope_blocks_out_of_scope(self):
        from shared.auth.middleware import enforce_model_scope
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
            model_ids=["m1", "m2"],
        )
        with pytest.raises(Exception) as exc_info:
            enforce_model_scope(user, "m3")
        assert exc_info.value.status_code == 403

    def test_enforce_model_scope_allows_in_scope(self):
        from shared.auth.middleware import enforce_model_scope
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
            model_ids=["m1", "m2"],
        )
        enforce_model_scope(user, "m1")

    def test_enforce_model_scope_allows_all_when_no_restriction(self):
        from shared.auth.middleware import enforce_model_scope
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
        )
        enforce_model_scope(user, "any-model")

    def test_enforce_model_scope_ignores_regular_users(self):
        from shared.auth.middleware import enforce_model_scope
        user = CurrentUser(
            user_id="u", tenant_id="t", email="u", role="member",
        )
        enforce_model_scope(user, "any-model")


class TestAudienceValidation:
    def test_wrong_audience_rejected(self):
        from jose import jwt as jose_jwt, JWTError
        from shared.config.settings import get_settings
        from shared.auth.jwt import decode_access_token
        s = get_settings()
        token = jose_jwt.encode(
            {"sub": "u", "tenant_id": "t", "aud": "wrong"},
            s.JWT_SECRET_KEY,
            algorithm=s.JWT_ALGORITHM,
        )
        with pytest.raises(JWTError, match="Unexpected audience"):
            decode_access_token(token)

    def test_embed_audience_accepted(self):
        from jose import jwt as jose_jwt
        from shared.config.settings import get_settings
        from shared.auth.jwt import decode_access_token
        s = get_settings()
        token = jose_jwt.encode(
            {"sub": "u", "tenant_id": "t", "aud": "embed"},
            s.JWT_SECRET_KEY,
            algorithm=s.JWT_ALGORITHM,
        )
        payload = decode_access_token(token)
        assert payload["aud"] == "embed"

    def test_no_audience_accepted(self):
        from jose import jwt as jose_jwt
        from shared.config.settings import get_settings
        from shared.auth.jwt import decode_access_token
        s = get_settings()
        token = jose_jwt.encode(
            {"sub": "u", "tenant_id": "t"},
            s.JWT_SECRET_KEY,
            algorithm=s.JWT_ALGORITHM,
        )
        payload = decode_access_token(token)
        assert "aud" not in payload


class TestModelServiceEmbedScope:
    @patch("src.api.models.get_tenant_db", new=_fake_tenant_db)
    async def test_get_model_rejects_out_of_scope(self):
        """Embed user with model_ids restriction gets 403 for out-of-scope model."""
        embed_user = CurrentEmbedUser(
            user_id="v@customer.com", tenant_id="acme", email="v@customer.com",
            model_ids=["model-1"],
        )
        app.dependency_overrides[get_current_user] = lambda: embed_user
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            resp = await ac.get("/api/v1/projects/00000000-0000-0000-0000-000000000001/models/00000000-0000-0000-0000-000000000099")
            assert resp.status_code == 403
        app.dependency_overrides.clear()


class TestEmbedRbacBypass:
    def test_embed_viewer_level_allowed(self):
        """Embed users pass through viewer-level role checks without UserAccessBinding."""
        from src.auth.rbac import _role_level
        assert _role_level("viewer") >= _role_level("viewer")

    def test_embed_modeler_level_blocked(self):
        """Embed users are blocked from modeler-level operations."""
        from shared.auth.middleware import CurrentEmbedUser
        from src.auth.rbac import _role_level
        assert _role_level("modeler") < _role_level("viewer")

    async def test_forbid_embed_user_rejects_embed(self):
        from shared.auth.middleware import forbid_embed_user
        embed_user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
        )
        with pytest.raises(Exception) as exc_info:
            await forbid_embed_user(embed_user)
        assert exc_info.value.status_code == 403

    async def test_forbid_embed_user_allows_regular(self):
        from shared.auth.middleware import forbid_embed_user
        user = CurrentUser(
            user_id="u", tenant_id="t", email="u", role="member",
        )
        result = await forbid_embed_user(user)
        assert result is user


class TestEmptyProjectIdsRejected:
    """Verify that project_ids=[] is rejected at validation time."""

    def test_empty_project_ids_rejected(self):
        from pydantic import ValidationError
        from shared.schemas.pydantic_models import EmbedTokenRequest
        with pytest.raises(ValidationError) as exc_info:
            EmbedTokenRequest(
                tenant_id="acme",
                user_identity="u@test.com",
                project_ids=[],
            )
        assert "non-empty list" in str(exc_info.value).lower() or "unrestricted" in str(exc_info.value).lower()

    def test_empty_model_ids_rejected(self):
        from pydantic import ValidationError
        from shared.schemas.pydantic_models import EmbedTokenRequest
        with pytest.raises(ValidationError) as exc_info:
            EmbedTokenRequest(
                tenant_id="acme",
                user_identity="u@test.com",
                model_ids=[],
            )
        assert "non-empty list" in str(exc_info.value).lower() or "unrestricted" in str(exc_info.value).lower()

    def test_empty_capabilities_allowed_deny_all(self):
        from shared.schemas.pydantic_models import EmbedTokenRequest
        req = EmbedTokenRequest(
            tenant_id="acme",
            user_identity="u@test.com",
            capabilities=[],
        )
        assert req.capabilities == []

    def test_none_project_ids_accepted(self):
        from shared.schemas.pydantic_models import EmbedTokenRequest
        req = EmbedTokenRequest(
            tenant_id="acme",
            user_identity="u@test.com",
            project_ids=None,
        )
        assert req.project_ids is None


class TestLowerLayerScopeDenyAll:
    def test_enforce_model_scope_empty_list_denies(self):
        from shared.auth.middleware import CurrentEmbedUser, enforce_model_scope
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
            model_ids=[],
        )
        with pytest.raises(HTTPException) as exc_info:
            enforce_model_scope(user, "any-model")
        assert exc_info.value.status_code == 403

    def test_enforce_model_scope_none_allows(self):
        from shared.auth.middleware import CurrentEmbedUser, enforce_model_scope
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
            model_ids=None,
        )
        enforce_model_scope(user, "any-model")

    async def test_require_capability_empty_list_denies(self):
        from shared.auth.middleware import CurrentEmbedUser, require_capability
        dep = require_capability("query")
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
            capabilities=[],
        )
        with pytest.raises(HTTPException) as exc_info:
            await dep(user)
        assert exc_info.value.status_code == 403


class TestEmbedProjectScopeEnforcement:
    """F-021-02 / Bug-7992: a project-scoped embed token with model_ids=null must
    not be able to read another project's model metadata. The escape was that
    enforce_model_scope only checked model_ids (a no-op when null), leaving the
    token's project_ids unenforced on the generic model-service viewer routes.
    Guard: enforce_model_scope now also checks project_ids when a project_id is
    supplied. Tier: T1 (producer/consumer contract). Test escape: prior tests
    only asserted the model_ids path, never the project_ids-only escape.
    """

    def test_project_scoped_token_model_ids_null_blocked_on_other_project(self):
        from shared.auth.middleware import CurrentEmbedUser, enforce_model_scope
        # Token scoped to P1 only; model_ids is null (unrestricted at model level).
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
            project_ids=["p1"], model_ids=None,
        )
        # Reading a model in P2 must be refused on the project half of the scope.
        with pytest.raises(HTTPException) as exc_info:
            enforce_model_scope(user, "m-in-p2", project_id="p2")
        assert exc_info.value.status_code == 403
        assert "project" in exc_info.value.detail.lower()

    def test_project_scoped_token_allows_in_scope_project(self):
        from shared.auth.middleware import CurrentEmbedUser, enforce_model_scope
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
            project_ids=["p1"], model_ids=None,
        )
        # In-scope project passes (model unrestricted).
        enforce_model_scope(user, "any-model", project_id="p1")

    def test_project_and_model_both_enforced(self):
        from shared.auth.middleware import CurrentEmbedUser, enforce_model_scope
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
            project_ids=["p1"], model_ids=["m1"],
        )
        # In-scope project but out-of-scope model → refused on model half.
        with pytest.raises(HTTPException) as exc_info:
            enforce_model_scope(user, "m2", project_id="p1")
        assert exc_info.value.status_code == 403
        assert "model" in exc_info.value.detail.lower()
        # Both in scope → allowed.
        enforce_model_scope(user, "m1", project_id="p1")

    def test_case_insensitive_project_match(self):
        from shared.auth.middleware import CurrentEmbedUser, enforce_model_scope
        # project_ids are lowercased at token build; the helper lowercases the
        # incoming id too, so an upper-case path segment still matches.
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
            project_ids=["abc-123"], model_ids=None,
        )
        enforce_model_scope(user, "m", project_id="ABC-123")

    @patch("src.api.measures.get_tenant_db", new=_fake_tenant_db)
    async def test_measures_route_blocks_cross_project_embed(self):
        """End-to-end route check: token scoped to P1 (model_ids null) is 403 on
        a measures list under a DIFFERENT project P2."""
        embed_user = CurrentEmbedUser(
            user_id="v@customer.com", tenant_id="acme", email="v@customer.com",
            project_ids=["00000000-0000-0000-0000-000000000001"],
            model_ids=None,
        )
        app.dependency_overrides[get_current_user] = lambda: embed_user
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as ac:
                resp = await ac.get(
                    "/api/v1/projects/00000000-0000-0000-0000-000000000002"
                    "/models/00000000-0000-0000-0000-000000000099/measures"
                )
                assert resp.status_code == 403
        finally:
            app.dependency_overrides.clear()


@patch("src.api.embed.get_system_db", new=_fake_system_db)
class TestEmbedPersonaScopeAtMint:
    """Bug-8253: minting must validate the locked persona falls within the
    token's project/model scope. Guard: mint_embed_token checks persona.model_id
    against model_ids and the persona model's project against project_ids.
    Tier: T2 (fixed-bug regression). Test escape: mint only validated persona
    existence, never that the persona belonged to the scoped project/model.
    """

    @patch("src.api.embed.get_tenant_db", new=_fake_tenant_db_persona_scope)
    async def test_persona_in_scope_accepted(self, admin_client):
        resp = await admin_client.post(URL, json={
            "tenant_id": "acme",
            "user_identity": "u@customer.com",
            "persona_id": _PERSONA_UUID,
            "project_ids": [_PROJECT_UUID],
            "model_ids": [_MODEL_UUID_1],
        })
        assert resp.status_code == 200

    @patch("src.api.embed.get_tenant_db", new=_fake_tenant_db_persona_scope)
    async def test_project_persona_in_scope_accepted(self, admin_client):
        """Bug-9196/F01: the agent ProjectPersona claim is ownership checked."""
        resp = await admin_client.post(URL, json={
            "tenant_id": "acme",
            "user_identity": "u@customer.com",
            "project_persona_id": _PROJECT_PERSONA_UUID,
            "project_ids": [_PROJECT_UUID],
        })
        assert resp.status_code == 200
        payload = decode_access_token(resp.json()["token"])
        assert payload["project_persona_id"] == _PROJECT_PERSONA_UUID

    @patch("src.api.embed.get_tenant_db", new=_fake_tenant_db_persona_scope)
    async def test_project_persona_out_of_project_scope_rejected(self, admin_client):
        resp = await admin_client.post(URL, json={
            "tenant_id": "acme",
            "user_identity": "u@customer.com",
            "project_persona_id": _PROJECT_PERSONA_UUID,
            "project_ids": [_PROJECT_UUID_2],
        })
        assert resp.status_code == 422
        assert "project_ids" in resp.json()["detail"]

    @patch("src.api.embed.get_tenant_db", new=_fake_tenant_db_persona_scope)
    async def test_persona_out_of_model_scope_rejected(self, admin_client):
        # Persona belongs to M1 but token is scoped to M2 only.
        resp = await admin_client.post(URL, json={
            "tenant_id": "acme",
            "user_identity": "u@customer.com",
            "persona_id": _PERSONA_UUID,
            "model_ids": [_MODEL_UUID_2],
        })
        assert resp.status_code == 422
        assert "model_ids" in resp.json()["detail"]

    @patch("src.api.embed.get_tenant_db", new=_fake_tenant_db_persona_scope)
    async def test_persona_out_of_project_scope_rejected(self, admin_client):
        # Persona belongs to M1/P1 but token is scoped to P2 only.
        resp = await admin_client.post(URL, json={
            "tenant_id": "acme",
            "user_identity": "u@customer.com",
            "persona_id": _PERSONA_UUID,
            "project_ids": [_PROJECT_UUID_2],
        })
        assert resp.status_code == 422
        assert "project_ids" in resp.json()["detail"]


class TestManagementEndpointGuard:
    """Verify management API files import forbid_embed_user, not get_current_user."""

    def test_management_files_use_forbid_embed_user(self):
        import importlib
        management_modules = [
            "src.api.connections",
            "src.api.access",
            "src.api.row_security",
            "src.api.llm_config",
            "src.api.logs",
            "src.api.project_settings",
            "src.api.scheduler_config",
            "src.api.alerts",
            "src.api.notifications",
            "src.api.analytics",
            "src.api.sources",
            "src.api.calendar",
            "src.api.versions",
            "src.api.tables",
            "src.api.aggregates",
            "src.api.pockets",
        ]
        for module_name in management_modules:
            mod = importlib.import_module(module_name)
            source = open(mod.__file__).read()
            assert "forbid_embed_user" in source, (
                f"{module_name} should use forbid_embed_user"
            )
            assert "Depends(get_current_user)" not in source, (
                f"{module_name} should not use Depends(get_current_user)"
            )


@patch("src.api.embed.get_system_db", new=_fake_system_db)
@patch("src.api.embed.audit_required", new=AsyncMock())
@patch("src.api.embed.emit_webhook", new=AsyncMock())
async def test_bug_9315_f_021_08_mint_persists_embed_token_mint_row(admin_client):
    """Bug-9315 / F-021-08: mint must INSERT embed_token_mints then SELECT by jti.

    A get_tenant_db mocked past the INSERT cannot satisfy this test: the row is
    created on TenantBase metadata (sqlite stand-in) and read back by primary key.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from shared.db.models import EmbedTokenMint
    from src.api import embed as embed_mod

    @compiles(JSONB, "sqlite")
    def _jsonb_sqlite(element, compiler, **kw):  # noqa: ARG001
        return "TEXT"

    @compiles(PGUUID, "sqlite")
    def _uuid_sqlite(element, compiler, **kw):  # noqa: ARG001
        return "CHAR(36)"

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    EmbedTokenMint.__table__.create(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    class _AsyncSession:
        def __init__(self, sync_session):
            self._s = sync_session

        def add(self, obj):
            self._s.add(obj)

        async def commit(self):
            self._s.commit()

        async def get(self, model, pk):
            return self._s.get(model, pk)

        async def execute(self, stmt):
            return self._s.execute(stmt)

        async def flush(self):
            self._s.flush()

    async def _persist_tenant_db(_tenant_id: str = ""):
        session = SessionLocal()
        try:
            yield _AsyncSession(session)
        finally:
            session.close()

    with patch.object(embed_mod, "get_tenant_db", new=_persist_tenant_db):
        resp = await admin_client.post(URL, json={
            "tenant_id": "acme",
            "user_identity": "demo-viewer@customer.com",
        })
    assert resp.status_code == 200, resp.text
    jti = _uuid.UUID(str(decode_access_token(resp.json()["token"])["jti"]))
    with SessionLocal() as db:
        row = db.get(EmbedTokenMint, jti)
        assert row is not None, "mint did not persist EmbedTokenMint (get_tenant_db mocked past INSERT?)"
        assert row.user_identity == "demo-viewer@customer.com"
        assert row.actor_email == "admin@example.com"
        assert row.capabilities == []
    engine.dispose()
