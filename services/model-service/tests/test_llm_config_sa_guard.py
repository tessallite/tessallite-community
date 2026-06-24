"""Anti-exploitation guard for non-API-key (service-account / OAuth) LLM auth.

The LLM-config CRUD must only accept bring-your-own API-key auth unless an
operator sets ``LLM_ALLOW_SERVICE_ACCOUNT_AUTH=true``. Otherwise a project admin
could point the agent at Google Vertex AI mode (ADC), billing the deployment's
cloud project instead of a customer key (the 2026-06-23 cost-leak vector).

Run from tessallite/services/model-service/:
    pytest tests/test_llm_config_sa_guard.py
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from shared.db.models import Project
from shared.llm import sa_auth
from shared.schemas.pydantic_models import (
    LLMProviderConfigCreate,
    LLMProviderConfigUpdate,
)
from src.api import llm_config as mod
from src.auth.middleware import CurrentUser

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _sync_settings():
    # The SA guard reads shared.config.settings.get_settings() (a cached singleton), while
    # these tests patch mod.settings. They are normally the same object, but another test in
    # the full suite can clear the settings cache -> a NEW singleton, leaving mod.settings
    # stale so the monkeypatch misses (order-dependent flake). Re-sync mod.settings to the
    # live singleton before each test so the patch always hits the object the guard reads.
    from shared.config.settings import get_settings

    mod.settings = get_settings()
    yield


def _admin_user() -> CurrentUser:
    return CurrentUser(user_id="admin@x", tenant_id="t", email="admin@x", role="tenant_admin")


# ---------------------------------------------------------------------------
# Pure detection: which configs use cloud service-account / OAuth (non-key) auth
# ---------------------------------------------------------------------------

def test_detects_vertex_ai_as_service_account_auth():
    assert mod._uses_service_account_auth("google", {"google_mode": "vertex_ai"}) is True
    assert mod._uses_service_account_auth("gemini", {"google_mode": "vertex_ai"}) is True
    assert mod._uses_service_account_auth("GOOGLE", {"google_mode": "vertex_ai"}) is True


def test_api_key_and_other_providers_are_not_service_account():
    assert mod._uses_service_account_auth("google", {"google_mode": "api_key"}) is False
    assert mod._uses_service_account_auth("google", {}) is False
    assert mod._uses_service_account_auth("google", None) is False
    # vertex_ai key is meaningless for non-google providers → not SA auth here
    assert mod._uses_service_account_auth("anthropic", {"google_mode": "vertex_ai"}) is False
    assert mod._uses_service_account_auth("openai", {"google_mode": "vertex_ai"}) is False
    assert mod._uses_service_account_auth(None, None) is False


# ---------------------------------------------------------------------------
# CRUD guard: reject SA auth unless explicitly enabled
# ---------------------------------------------------------------------------

def test_guard_blocks_vertex_when_flag_disabled(monkeypatch):
    monkeypatch.setattr(mod.settings, "LLM_ALLOW_SERVICE_ACCOUNT_AUTH", False)
    with pytest.raises(HTTPException) as exc:
        mod._guard_service_account_auth("google", {"google_mode": "vertex_ai"})
    assert exc.value.status_code == 403


def test_guard_allows_vertex_when_flag_enabled(monkeypatch):
    monkeypatch.setattr(mod.settings, "LLM_ALLOW_SERVICE_ACCOUNT_AUTH", True)
    mod._guard_service_account_auth("google", {"google_mode": "vertex_ai"})  # no raise


def test_guard_allows_api_key_regardless_of_flag(monkeypatch):
    monkeypatch.setattr(mod.settings, "LLM_ALLOW_SERVICE_ACCOUNT_AUTH", False)
    mod._guard_service_account_auth("google", {"google_mode": "api_key"})  # no raise
    mod._guard_service_account_auth("anthropic", {})  # no raise
    mod._guard_service_account_auth("openai", None)  # no raise


# ---------------------------------------------------------------------------
# Defense in depth: the Google adapter refuses Vertex/ADC when disabled, so a
# pre-existing/imported vertex_ai row can't be exploited even past the CRUD.
# ---------------------------------------------------------------------------

def test_google_adapter_refuses_vertex_when_disabled(monkeypatch):
    from shared.config.settings import get_settings
    from shared.llm.providers import google as g

    monkeypatch.setattr(get_settings(), "LLM_ALLOW_SERVICE_ACCOUNT_AUTH", False)
    cfg = types.SimpleNamespace(
        config={"google_mode": "vertex_ai", "google_project": "p", "google_location": "l"},
        api_key=None,
    )
    with pytest.raises(ValueError, match="disabled"):
        g._client(cfg)


# ---------------------------------------------------------------------------
# Endpoint wiring: the guard is actually invoked by the create/update handlers
# (regression-locks the wiring, not just the helper — F2 in deep review).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_handler_rejects_vertex_when_disabled(monkeypatch):
    """create_llm_config must 403 a vertex_ai body before touching the DB."""
    monkeypatch.setattr(mod.settings, "LLM_ALLOW_SERVICE_ACCOUNT_AUTH", False)
    body = LLMProviderConfigCreate(
        provider="google",
        display_name="x",
        api_key="unused-for-vertex",
        model_name="gemini-2.5-pro",
        config={"google_mode": "vertex_ai"},
    )
    # No get_tenant_db patch needed: the guard runs before any DB access.
    with pytest.raises(HTTPException) as exc:
        await mod.create_llm_config(
            project_id=uuid.uuid4(), body=body, current_user=_admin_user()
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_update_handler_rejects_switch_to_vertex_when_disabled(monkeypatch):
    """update_llm_config must 403 when an update switches an existing api-key row
    to vertex_ai (effective post-update config), proving the effective-config
    guard is wired."""
    monkeypatch.setattr(mod.settings, "LLM_ALLOW_SERVICE_ACCOUNT_AUTH", False)
    project_id, config_id = uuid.uuid4(), uuid.uuid4()
    record = types.SimpleNamespace(
        id=config_id, project_id=project_id, provider="google", config={}
    )

    async def _get(model, _id):
        return types.SimpleNamespace(id=project_id) if model is Project else record

    db = types.SimpleNamespace(get=AsyncMock(side_effect=_get))
    body = LLMProviderConfigUpdate(config={"google_mode": "vertex_ai"})

    with patch("src.api.llm_config.get_tenant_db") as mock_gen:
        async def _gen(*a, **k):
            yield db
        mock_gen.side_effect = _gen
        with pytest.raises(HTTPException) as exc:
            await mod.update_llm_config(
                project_id=project_id, config_id=config_id, body=body,
                current_user=_admin_user(),
            )
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Import path: a bundled vertex_ai config is neutralised (F1 in deep review).
# ---------------------------------------------------------------------------

def test_neutralise_strips_service_account_keys():
    cfg = {"google_mode": "vertex_ai", "google_project": "p", "google_location": "l", "x": 1}
    out = sa_auth.neutralise_service_account_config(cfg)
    assert out == {"x": 1}
    # original is not mutated
    assert "google_mode" in cfg


def test_neutralised_config_is_no_longer_service_account_auth():
    cfg = {"google_mode": "vertex_ai", "google_project": "p"}
    out = sa_auth.neutralise_service_account_config(cfg)
    assert sa_auth.uses_service_account_auth("google", out) is False
