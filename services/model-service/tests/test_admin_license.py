"""System-admin License Manager — upload/verify/persist to the system DB (Bug-5466).

The license is fed from the UI, verified with the built-in public key, and stored
in the system DB (no file/env), so it applies immediately and works on read-only
hosts. These tests cover the endpoint behaviour and the manager-load fallback.

Run from tessallite/services/model-service/:
    pytest tests/test_admin_license.py
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from shared.licensing.errors import InvalidSignature
from src.api import admin as mod
from src import licensing_guard as guard

pytestmark = pytest.mark.unit


def _admin():
    from src.auth.middleware import CurrentUser
    return CurrentUser(user_id="root", tenant_id="__system__", email="root@x", role="system_admin")


@pytest.fixture
def patched(monkeypatch):
    mgr = MagicMock()
    mgr.status.return_value = {"edition": "enterprise", "activated": True}
    mgr.entitlements.return_value = {"users": 50}
    monkeypatch.setattr(mod, "get_license_manager", MagicMock(return_value=mgr))
    monkeypatch.setattr(mod, "build_registry", lambda keys: {"registry": keys})
    monkeypatch.setattr(mod, "has_installed_license", AsyncMock(return_value=True))
    monkeypatch.setattr(mod, "store_license_doc", AsyncMock())
    monkeypatch.setattr(mod, "license_public_keys", lambda: "k:v")
    return mgr


# ---------------------------------------------------------------------------
# Upload endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_install_rejects_invalid_signature(monkeypatch, patched):
    def _raise(*a, **k):
        raise InvalidSignature("does not verify")
    monkeypatch.setattr(mod, "verify_license", _raise)
    with pytest.raises(HTTPException) as exc:
        await mod.install_license(body={"bad": 1}, current_user=_admin())
    assert exc.value.status_code == 400
    assert "rejected" in exc.value.detail.lower()
    mod.store_license_doc.assert_not_awaited()


@pytest.mark.asyncio
async def test_install_happy_path_persists_to_db(monkeypatch, patched):
    monkeypatch.setattr(mod, "verify_license", lambda *a, **k: types.SimpleNamespace(license_id="L-OK"))
    body = {"license_id": "L-OK", "edition": "enterprise", "signature": "ed25519:abc"}

    out = await mod.install_license(body=body, current_user=_admin())

    assert out["status"] == "installed"
    # Persisted to the system DB (not a file), with the installer recorded.
    mod.store_license_doc.assert_awaited_once_with(body, installed_by="root@x")
    assert out["license"]["edition"] == "enterprise"
    assert out["license"]["has_license"] is True


@pytest.mark.asyncio
async def test_status_reports_edition_and_install_state(monkeypatch, patched):
    out = await mod.get_license_status()
    assert out["edition"] == "enterprise"
    assert out["has_license"] is True
    assert "enforcement_enabled" in out


# ---------------------------------------------------------------------------
# licensing_guard: built-in key + no-license fallback
# ---------------------------------------------------------------------------

def test_built_in_public_key_used_when_env_unset(monkeypatch):
    from shared.config.settings import get_settings
    monkeypatch.setattr(get_settings(), "LICENSE_PUBLIC_KEYS", "")
    assert guard.license_public_keys() == guard.BUILTIN_PUBLIC_KEYS


def test_env_public_key_overrides_built_in(monkeypatch):
    from shared.config.settings import get_settings
    monkeypatch.setattr(get_settings(), "LICENSE_PUBLIC_KEYS", "custom:key")
    assert guard.license_public_keys() == "custom:key"


@pytest.mark.asyncio
async def test_reload_with_no_license_is_full_product(monkeypatch):
    monkeypatch.setattr(guard, "load_license_doc_from_db", AsyncMock(return_value=None))
    mgr = await guard.reload_license_manager()
    assert mgr.status().get("edition") == "enterprise"
    assert mgr.can_create("model", 999).allowed is True


@pytest.mark.asyncio
async def test_reload_with_license_builds_from_doc(monkeypatch):
    doc = {"license_id": "L1", "edition": "community"}
    monkeypatch.setattr(guard, "load_license_doc_from_db", AsyncMock(return_value=doc))
    built = MagicMock()
    built.status.return_value = {"edition": "community"}
    monkeypatch.setattr(guard, "load_manager", lambda **kw: built)
    monkeypatch.setattr(guard, "build_registry", lambda keys: {"r": keys})
    mgr = await guard.reload_license_manager()
    assert mgr.status().get("edition") == "community"