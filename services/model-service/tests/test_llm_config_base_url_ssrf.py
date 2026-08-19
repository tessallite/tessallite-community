"""Bug-7108: SSRF allowlist enforcement on LLM config persistence.

The model-service create/update endpoints must reject a ``base_url`` that
is not on the shared SSRF allowlist (``shared.llm.base_url_validator``)
BEFORE persisting it, so a malicious project admin cannot save a config
that later causes AI runs to POST tenant telemetry and the stored API key
to an attacker-controlled URL.

Run from tessallite/services/model-service/:
    pytest tests/test_llm_config_base_url_ssrf.py
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from shared.db.models import LLMProviderConfig, Project
from shared.schemas.pydantic_models import (
    LLMProviderConfigCreate,
    LLMProviderConfigUpdate,
)
from src.api import llm_config as mod
from src.auth.middleware import CurrentUser

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _sync_settings():
    from shared.config.settings import get_settings

    mod.settings = get_settings()
    yield


def _admin_user() -> CurrentUser:
    return CurrentUser(
        user_id="admin@x", tenant_id="t", email="admin@x", role="tenant_admin",
    )


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------

class TestGuardBaseUrl:
    """Direct tests for the _guard_base_url helper."""

    def test_none_passes(self):
        """None means 'use server default' and must be allowed."""
        mod._guard_base_url(None)  # no raise

    def test_allowed_remote_passes(self):
        mod._guard_base_url("https://api.openai.com/v1")  # no raise
        mod._guard_base_url("https://api.anthropic.com/v1")  # no raise

    def test_disallowed_host_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            mod._guard_base_url("https://evil.example.com/steal")
        assert exc.value.status_code == 400
        assert "not in the allowed" in exc.value.detail

    def test_http_on_remote_provider_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            mod._guard_base_url("http://api.openai.com/v1")
        assert exc.value.status_code == 400
        assert "HTTPS" in exc.value.detail

    def test_invalid_scheme_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            mod._guard_base_url("ftp://api.openai.com/v1")
        assert exc.value.status_code == 400

    def test_loopback_allowed_port_passes(self):
        mod._guard_base_url("http://localhost:11434/v1")  # Ollama default

    def test_loopback_disallowed_port_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            mod._guard_base_url("http://localhost:9999")
        assert exc.value.status_code == 400
        assert "not allowed" in exc.value.detail


# ---------------------------------------------------------------------------
# Create endpoint wiring
# ---------------------------------------------------------------------------

class TestCreateRejectsDisallowedBaseUrl:
    """create_llm_config must reject a disallowed base_url before DB access."""

    @pytest.mark.asyncio
    async def test_create_with_disallowed_base_url_returns_400(self):
        body = LLMProviderConfigCreate(
            provider="openai",
            display_name="Evil Config",
            api_key="sk-test-key",
            model_name="gpt-4",
            base_url="https://evil.example.com/steal",
        )
        with pytest.raises(HTTPException) as exc:
            await mod.create_llm_config(
                project_id=uuid.uuid4(),
                body=body,
                current_user=_admin_user(),
            )
        assert exc.value.status_code == 400
        assert "not in the allowed" in exc.value.detail

    @pytest.mark.asyncio
    async def test_create_with_allowed_base_url_passes_guard(self):
        """An allowed base_url should pass the SSRF guard and reach the DB layer.

        We expect a tenant-DB error (mocked away) rather than a 400 SSRF
        rejection, proving the guard did not fire.
        """
        body = LLMProviderConfigCreate(
            provider="openai",
            display_name="Good Config",
            api_key="sk-test-key",
            model_name="gpt-4",
            base_url="https://api.openai.com/v1",
        )
        # The guard should NOT fire; the call will fail at get_tenant_db
        # (no real DB) -- that is fine, we just need to prove it gets past
        # _guard_base_url.
        with patch("src.api.llm_config.get_tenant_db") as mock_gen:
            async def _gen(*a, **k):
                # Yield a mock DB that will cause a controlled error
                db = AsyncMock()
                db.get = AsyncMock(return_value=types.SimpleNamespace(
                    id=uuid.uuid4(),
                ))
                db.add = lambda x: None
                db.commit = AsyncMock()
                db.refresh = AsyncMock()
                yield db
            mock_gen.side_effect = _gen

            with patch.object(mod, "_to_response") as mock_resp:
                mock_resp.return_value = "ok"
                result = await mod.create_llm_config(
                    project_id=uuid.uuid4(),
                    body=body,
                    current_user=_admin_user(),
                )
                assert result == "ok"

    @pytest.mark.asyncio
    async def test_create_with_none_base_url_passes_guard(self):
        """None base_url (server default) must not be rejected."""
        body = LLMProviderConfigCreate(
            provider="openai",
            display_name="Default Config",
            api_key="sk-test-key",
            model_name="gpt-4",
            base_url=None,
        )
        with patch("src.api.llm_config.get_tenant_db") as mock_gen:
            async def _gen(*a, **k):
                db = AsyncMock()
                db.get = AsyncMock(return_value=types.SimpleNamespace(
                    id=uuid.uuid4(),
                ))
                db.add = lambda x: None
                db.commit = AsyncMock()
                db.refresh = AsyncMock()
                yield db
            mock_gen.side_effect = _gen

            with patch.object(mod, "_to_response") as mock_resp:
                mock_resp.return_value = "ok"
                result = await mod.create_llm_config(
                    project_id=uuid.uuid4(),
                    body=body,
                    current_user=_admin_user(),
                )
                assert result == "ok"


# ---------------------------------------------------------------------------
# Update endpoint wiring
# ---------------------------------------------------------------------------

class TestUpdateRejectsDisallowedBaseUrl:
    """update_llm_config must reject a disallowed base_url before persisting."""

    @pytest.mark.asyncio
    async def test_update_to_disallowed_base_url_returns_400(self):
        project_id, config_id = uuid.uuid4(), uuid.uuid4()
        record = types.SimpleNamespace(
            id=config_id, project_id=project_id, provider="openai", config={},
            base_url="https://api.openai.com/v1",
        )

        async def _get(model, _id):
            return (
                types.SimpleNamespace(id=project_id)
                if model is Project
                else record
            )

        db = types.SimpleNamespace(get=AsyncMock(side_effect=_get))
        body = LLMProviderConfigUpdate(
            base_url="https://evil.example.com/steal",
        )

        with patch("src.api.llm_config.get_tenant_db") as mock_gen:
            async def _gen(*a, **k):
                yield db
            mock_gen.side_effect = _gen
            with pytest.raises(HTTPException) as exc:
                await mod.update_llm_config(
                    project_id=project_id,
                    config_id=config_id,
                    body=body,
                    current_user=_admin_user(),
                )
        assert exc.value.status_code == 400
        assert "not in the allowed" in exc.value.detail

    @pytest.mark.asyncio
    async def test_update_to_allowed_base_url_passes_guard(self):
        project_id, config_id = uuid.uuid4(), uuid.uuid4()
        record = types.SimpleNamespace(
            id=config_id, project_id=project_id, provider="openai", config={},
            base_url="https://api.openai.com/v1",
            display_name="X", model_name="gpt-4", max_tokens=4096,
            temperature=0.2, timeout_seconds=60, encrypted_api_key=None,
            created_at=None, updated_at=None,
        )

        async def _get(model, _id):
            return (
                types.SimpleNamespace(id=project_id)
                if model is Project
                else record
            )

        db = types.SimpleNamespace(
            get=AsyncMock(side_effect=_get),
            commit=AsyncMock(),
            refresh=AsyncMock(),
        )
        body = LLMProviderConfigUpdate(
            base_url="https://api.anthropic.com/v1",
        )

        with patch("src.api.llm_config.get_tenant_db") as mock_gen:
            async def _gen(*a, **k):
                yield db
            mock_gen.side_effect = _gen
            with patch.object(mod, "_to_response") as mock_resp:
                mock_resp.return_value = "ok"
                result = await mod.update_llm_config(
                    project_id=project_id,
                    config_id=config_id,
                    body=body,
                    current_user=_admin_user(),
                )
                assert result == "ok"

    @pytest.mark.asyncio
    async def test_update_without_base_url_skips_guard(self):
        """An update that does not touch base_url must not trigger the guard,
        even if the existing record has a legacy disallowed base_url."""
        project_id, config_id = uuid.uuid4(), uuid.uuid4()
        record = types.SimpleNamespace(
            id=config_id, project_id=project_id, provider="openai", config={},
            # Simulate a pre-existing record with a disallowed URL (legacy data).
            base_url="https://evil.example.com/legacy",
            display_name="X", model_name="gpt-4", max_tokens=4096,
            temperature=0.2, timeout_seconds=60, encrypted_api_key=None,
            created_at=None, updated_at=None,
        )

        async def _get(model, _id):
            return (
                types.SimpleNamespace(id=project_id)
                if model is Project
                else record
            )

        db = types.SimpleNamespace(
            get=AsyncMock(side_effect=_get),
            commit=AsyncMock(),
            refresh=AsyncMock(),
        )
        # Update only display_name — base_url is NOT in the update payload.
        body = LLMProviderConfigUpdate(display_name="Renamed")

        with patch("src.api.llm_config.get_tenant_db") as mock_gen:
            async def _gen(*a, **k):
                yield db
            mock_gen.side_effect = _gen
            with patch.object(mod, "_to_response") as mock_resp:
                mock_resp.return_value = "ok"
                result = await mod.update_llm_config(
                    project_id=project_id,
                    config_id=config_id,
                    body=body,
                    current_user=_admin_user(),
                )
                assert result == "ok"
