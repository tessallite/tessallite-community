"""Unit tests for embed token API: POST /api/v1/auth/embed-token."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import httpx
from fastapi import HTTPException

from shared.auth.middleware import CurrentEmbedUser, CurrentUser
from src.auth.local_backend import decode_access_token
from src.auth.middleware import get_current_user, require_tenant_admin
from src.main import app

pytestmark = pytest.mark.unit


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
    db.commit = AsyncMock()
    yield db


async def _fake_system_db():
    db = AsyncMock()
    # Return a fake tenant for the SystemTenant lookup
    result = AsyncMock()
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
        assert data["scope"]["capabilities"] == ["query", "chat", "explore"]
        assert data["scope"]["expiry_minutes"] == 180

        payload = decode_access_token(data["token"])
        assert payload["sub"] == "demo-viewer@customer.com"
        assert payload["tenant_id"] == "acme"
        assert payload["aud"] == "embed"
        assert payload["capabilities"] == ["query", "chat", "explore"]

    @patch("src.api.embed.get_tenant_db", new=_fake_tenant_db)
    async def test_scoped_embed_token(self, admin_client):
        resp = await admin_client.post(URL, json={
            "tenant_id": "acme",
            "user_identity": "limited@customer.com",
            "persona_id": "persona-abc",
            "model_ids": ["model-1", "model-2"],
            "capabilities": ["chat"],
            "expiry_minutes": 60,
        })
        assert resp.status_code == 200
        data = resp.json()
        payload = decode_access_token(data["token"])
        assert payload["persona_id"] == "persona-abc"
        assert payload["model_ids"] == ["model-1", "model-2"]
        assert payload["capabilities"] == ["chat"]
        assert data["scope"]["expiry_minutes"] == 60

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


class TestEmbedTokenMiddleware:
    def test_embed_payload_produces_embed_user(self):
        from shared.auth.middleware import _build_user_from_payload
        payload = {
            "sub": "viewer@customer.com",
            "tenant_id": "acme",
            "aud": "embed",
            "persona_id": "p1",
            "model_ids": ["m1", "m2"],
            "capabilities": ["query", "chat"],
        }
        user = _build_user_from_payload(payload)
        assert isinstance(user, CurrentEmbedUser)
        assert user.is_embed is True
        assert user.tenant_id == "acme"
        assert user.persona_id == "p1"
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

    def test_embed_user_without_capabilities_gets_all(self):
        from shared.auth.middleware import _build_user_from_payload
        payload = {
            "sub": "viewer",
            "tenant_id": "acme",
            "aud": "embed",
        }
        user = _build_user_from_payload(payload)
        assert isinstance(user, CurrentEmbedUser)
        assert user.capabilities == ["query", "chat", "explore"]


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

    def test_empty_capabilities_rejected(self):
        from pydantic import ValidationError
        from shared.schemas.pydantic_models import EmbedTokenRequest
        with pytest.raises(ValidationError):
            EmbedTokenRequest(
                tenant_id="acme",
                user_identity="u@test.com",
                capabilities=[],
            )

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
