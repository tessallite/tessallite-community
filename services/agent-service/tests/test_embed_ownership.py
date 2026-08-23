"""Tests for embed user conversation ownership and management API denial."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import uuid

import pytest
from fastapi import HTTPException

from shared.auth.middleware import (
    CurrentEmbedUser,
    CurrentServiceUser,
    CurrentUser,
    forbid_embed_user,
)
from src.api.conversations import (
    ConversationCreate,
    _enforce_conversation_ownership,
    create_conversation,
)
from src.api.personas import (
    _require_project_modeller as _require_persona_modeller,
    _require_project_viewer as _require_persona_viewer,
)

pytestmark = pytest.mark.unit


def _make_conv(caller_ref: str, project_id: str = "p1") -> MagicMock:
    conv = MagicMock()
    conv.caller_ref = caller_ref
    conv.project_id = project_id
    return conv


class TestConversationOwnership:
    def test_embed_user_can_access_own_conversation(self):
        user = CurrentEmbedUser(
            user_id="viewer@customer.com", tenant_id="t", email="viewer@customer.com",
        )
        conv = _make_conv("viewer@customer.com")
        _enforce_conversation_ownership(conv, user)

    def test_embed_user_blocked_from_other_conversation(self):
        user = CurrentEmbedUser(
            user_id="viewer@customer.com", tenant_id="t", email="viewer@customer.com",
        )
        conv = _make_conv("other@customer.com")
        with pytest.raises(HTTPException) as exc_info:
            _enforce_conversation_ownership(conv, user)
        assert exc_info.value.status_code == 404

    def test_tenant_admin_can_access_other_conversation_for_admin_surfaces(self):
        user = CurrentUser(
            user_id="admin@tenant.com", tenant_id="t", email="admin@tenant.com",
            role="tenant_admin",
        )
        conv = _make_conv("other@tenant.com")
        _enforce_conversation_ownership(conv, user)

    def test_service_principal_cannot_access_other_conversation_by_admin_role(self):
        user = CurrentServiceUser(
            principal="pocket-refresh",
            tenant_id="t",
            role="system_admin",
            scopes=["query-router.pocket-refresh"],
        )
        conv = _make_conv("other@tenant.com")
        with pytest.raises(HTTPException) as exc_info:
            _enforce_conversation_ownership(conv, user)
        assert exc_info.value.status_code == 404

    def test_regular_user_blocked_from_other_conversation(self):
        user = CurrentUser(
            user_id="viewer@tenant.com", tenant_id="t", email="viewer@tenant.com",
            role="member",
        )
        conv = _make_conv("other@tenant.com")
        with pytest.raises(HTTPException) as exc_info:
            _enforce_conversation_ownership(conv, user)
        assert exc_info.value.status_code == 404

    def test_regular_user_can_access_own_conversation(self):
        user = CurrentUser(
            user_id="viewer@tenant.com", tenant_id="t", email="viewer@tenant.com",
            role="member",
        )
        conv = _make_conv("viewer@tenant.com")
        _enforce_conversation_ownership(conv, user)

    def test_embed_user_blocked_from_tenant_user_conversation(self):
        user = CurrentEmbedUser(
            user_id="embed@customer.com", tenant_id="t", email="embed@customer.com",
        )
        conv = _make_conv("admin@tenant.com")
        with pytest.raises(HTTPException) as exc_info:
            _enforce_conversation_ownership(conv, user)
        assert exc_info.value.status_code == 404


class TestEmbedPersonaOverride:
    @pytest.mark.asyncio
    async def test_create_conversation_uses_project_persona_claim_only(self):
        """Bug-9196/F01: agent ProjectPersona is not query-router Persona.

        A legacy/simultaneous embed ``persona_id`` claim remains available to
        query-router as the model Persona lock. Agent-service must ignore that
        namespace and apply only ``project_persona_id`` to conversation field
        scope.
        """
        project_id = uuid.uuid4()
        model_persona = "11111111-1111-1111-1111-111111111111"
        project_persona = uuid.uuid4()
        body_persona = uuid.uuid4()
        saved = {}

        db = AsyncMock()

        def _add(conv):
            saved["conversation"] = conv

        async def _refresh(conv):
            conv.id = uuid.uuid4()
            conv.created_at = datetime.now(timezone.utc)
            conv.updated_at = datetime.now(timezone.utc)
            conv.last_active_at = None
            conv.title = None
            conv.state = {}
            conv.summary = None
            conv.started_at = datetime.now(timezone.utc)

        db.add = _add
        db.commit = AsyncMock()
        db.refresh = AsyncMock(side_effect=_refresh)

        async def _tenant_db(_tenant_id: str):
            yield db

        current_user = CurrentEmbedUser(
            user_id="embed@customer.com",
            tenant_id="t",
            email="embed@customer.com",
            persona_id=model_persona,
            project_persona_id=str(project_persona),
            project_ids=[str(project_id)],
            capabilities=["chat"],
        )

        with patch("src.api.conversations.get_tenant_db", _tenant_db), \
             patch("src.api.conversations._require_project_access_and_agent", AsyncMock()), \
             patch("src.api.conversations._validate_persona", AsyncMock()) as validate_persona, \
             patch("src.api.conversations.dispatch_event", lambda **_kwargs: None), \
             patch("src.api.conversations._spawn_background", lambda _coro: None):
            response = await create_conversation(
                project_id,
                ConversationCreate(persona_id=body_persona),
                current_user,
            )

        conv = saved["conversation"]
        assert conv.persona_id == str(project_persona)
        assert conv.persona_id != model_persona
        assert conv.persona_id != body_persona
        validate_persona.assert_awaited_once_with(db, project_id, str(project_persona))
        assert response.persona_id == project_persona


class TestForbidEmbedUser:
    async def test_rejects_embed_user(self):
        user = CurrentEmbedUser(
            user_id="embed@customer.com", tenant_id="t", email="embed@customer.com",
        )
        with pytest.raises(HTTPException) as exc_info:
            await forbid_embed_user(user)
        assert exc_info.value.status_code == 403
        assert "management" in exc_info.value.detail.lower()

    async def test_allows_regular_user(self):
        user = CurrentUser(
            user_id="admin@tenant.com", tenant_id="t", email="admin@tenant.com",
            role="tenant_admin",
        )
        result = await forbid_embed_user(user)
        assert result is user


def _db_without_binding():
    db = AsyncMock()
    no_binding = MagicMock()
    no_binding.scalar_one_or_none.return_value = None
    some_binding_exists = MagicMock()
    some_binding_exists.scalar_one_or_none.return_value = object()
    db.execute = AsyncMock(side_effect=[no_binding, some_binding_exists])
    return db


def _gen(db):
    async def _g(*_args, **_kwargs):
        yield db
    return _g


class TestPersonaHumanAdminGates:
    @pytest.mark.asyncio
    async def test_service_principal_does_not_bypass_persona_modeller_gate(self):
        user = CurrentServiceUser(
            principal="glossary-bootstrap-service",
            tenant_id="t",
            role="tenant_admin",
            scopes=["optimizer.stats-refresh"],
        )
        with patch("src.api.personas.get_tenant_db", _gen(_db_without_binding())):
            with pytest.raises(HTTPException) as exc_info:
                await _require_persona_modeller(uuid.uuid4(), user)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_service_principal_does_not_bypass_persona_viewer_gate(self):
        user = CurrentServiceUser(
            principal="data-quality-validator",
            tenant_id="t",
            role="system_admin",
            scopes=["query-router.data-quality"],
        )
        with patch("src.api.personas.get_tenant_db", _gen(_db_without_binding())):
            with pytest.raises(HTTPException) as exc_info:
                await _require_persona_viewer(uuid.uuid4(), user)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_human_tenant_admin_still_bypasses_persona_gate(self):
        user = CurrentUser(
            user_id="admin@tenant.com",
            tenant_id="t",
            email="admin@tenant.com",
            role="tenant_admin",
        )
        async def _explode(*_args, **_kwargs):
            raise AssertionError("human tenant_admin should not need binding lookup")
            yield

        with patch("src.api.personas.get_tenant_db", _explode):
            await _require_persona_modeller(uuid.uuid4(), user)
            await _require_persona_viewer(uuid.uuid4(), user)


class TestProjectScope:
    def test_empty_project_ids_denies_all(self):
        from uuid import uuid4
        from src.api.conversations import _enforce_project_scope
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
            project_ids=[],
        )
        with pytest.raises(HTTPException) as exc_info:
            _enforce_project_scope(uuid4(), user)
        assert exc_info.value.status_code == 403

    def test_none_project_ids_allows_all(self):
        from uuid import uuid4
        from src.api.conversations import _enforce_project_scope
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
            project_ids=None,
        )
        _enforce_project_scope(uuid4(), user)

    def test_scoped_project_ids_blocks_other(self):
        from uuid import UUID, uuid4
        from src.api.conversations import _enforce_project_scope
        allowed = str(uuid4())
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
            project_ids=[allowed],
        )
        with pytest.raises(HTTPException) as exc_info:
            _enforce_project_scope(uuid4(), user)
        assert exc_info.value.status_code == 403

    def test_scoped_project_ids_allows_listed(self):
        from uuid import UUID
        from src.api.conversations import _enforce_project_scope
        pid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
            project_ids=[pid],
        )
        _enforce_project_scope(UUID(pid), user)

    def test_regular_user_bypasses_project_scope(self):
        from uuid import uuid4
        from src.api.conversations import _enforce_project_scope
        user = CurrentUser(
            user_id="admin", tenant_id="t", email="admin",
            role="tenant_admin",
        )
        _enforce_project_scope(uuid4(), user)


class TestCapabilityOnConversationEndpoints:
    async def test_query_only_token_blocked_from_chat(self):
        from shared.auth.middleware import require_capability
        dep = require_capability("chat")
        user = CurrentEmbedUser(
            user_id="v", tenant_id="t", email="v",
            capabilities=["query"],
        )
        with pytest.raises(HTTPException) as exc_info:
            await dep(user)
        assert exc_info.value.status_code == 403
        assert "chat" in exc_info.value.detail
