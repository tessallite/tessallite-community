"""Phase 2: model-service create-cap enforcement glue.

Verifies the default-OFF no-op (full product unchanged) and the ON path
(Community) blocking over-cap creates. Uses a real signed license + key registry.
"""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src import licensing_guard
from shared.licensing.issuer.sign import generate_ed25519_keypair, sign_license


def _settings(**kw):
    base = dict(
        LICENSE_ENFORCEMENT_ENABLED=False, LICENSE_FILE="", LICENSE_PUBLIC_KEYS=""
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _community_license_file(tmp_path, models=2, demo_tenant_id=None):
    priv, pub = generate_ed25519_keypair()
    doc = {
        "schema_version": 1,
        "license_id": "lic_community_1",
        "key_id": "k1",
        "issuer": "tessallite.io",
        "edition": "community",
        "issued_at": "2026-06-22T00:00:00Z",
        "expires_at": None,
        "product": "tessallite-community",
        "entitlements": {
            "own_tenants": 1,
            "models": models,
            "users": 2,
            "features": "all",
        },
    }
    if demo_tenant_id:
        doc["entitlements"]["demo_tenant"] = {"enabled": True, "tenant_id": demo_tenant_id}
    signed = sign_license(doc, priv)
    path = tmp_path / "license.json"
    path.write_text(json.dumps(signed), encoding="utf-8")
    pub_spec = "k1:" + base64.b64encode(pub).decode("ascii")
    return str(path), pub_spec


@pytest.fixture(autouse=True)
def _clear_cache():
    licensing_guard.get_license_manager.cache_clear()
    yield
    licensing_guard.get_license_manager.cache_clear()


async def _count(n: int) -> int:
    return n


async def test_enforcement_off_is_noop(monkeypatch):
    monkeypatch.setattr(licensing_guard, "get_settings", lambda: _settings())
    queried = {"called": False}

    async def count_fn() -> int:
        queried["called"] = True
        return 999

    # no raise even though count would be way over any cap
    await licensing_guard.enforce_create_cap("model", count_fn)
    # count not even queried when enforcement is off
    assert queried["called"] is False
    assert licensing_guard.get_license_manager().status()["edition"] == "enterprise"


async def test_enforcement_on_allows_under_cap(tmp_path, monkeypatch):
    lf, pk = _community_license_file(tmp_path, models=2)
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(LICENSE_ENFORCEMENT_ENABLED=True, LICENSE_FILE=lf, LICENSE_PUBLIC_KEYS=pk),
    )
    # 0 and 1 existing models -> allowed (cap 2)
    await licensing_guard.enforce_create_cap("model", lambda: _count(0))
    await licensing_guard.enforce_create_cap("model", lambda: _count(1))


async def test_enforcement_on_blocks_at_cap(tmp_path, monkeypatch):
    lf, pk = _community_license_file(tmp_path, models=2)
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(LICENSE_ENFORCEMENT_ENABLED=True, LICENSE_FILE=lf, LICENSE_PUBLIC_KEYS=pk),
    )
    with pytest.raises(HTTPException) as exc:
        await licensing_guard.enforce_create_cap("model", lambda: _count(2))
    assert exc.value.status_code == 403
    assert "limit reached" in str(exc.value.detail).lower()


async def test_unactivated_when_enabled_without_license(monkeypatch):
    # enforcement on but no license configured -> stub denies (Community unactivated)
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(LICENSE_ENFORCEMENT_ENABLED=True),
    )
    with pytest.raises(HTTPException) as exc:
        await licensing_guard.enforce_create_cap("project", lambda: _count(0))
    assert exc.value.status_code == 403


def test_demo_source_locked_blocks_demo_tenant(tmp_path, monkeypatch):
    lf, pk = _community_license_file(tmp_path, demo_tenant_id="demo-123")
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(LICENSE_ENFORCEMENT_ENABLED=True, LICENSE_FILE=lf, LICENSE_PUBLIC_KEYS=pk),
    )
    with pytest.raises(HTTPException) as exc:
        licensing_guard.enforce_demo_source_locked("demo-123")
    assert exc.value.status_code == 403
    # the user's own tenant is unaffected
    licensing_guard.enforce_demo_source_locked("own-456")


def test_demo_source_lock_noop_when_enforcement_off(monkeypatch):
    monkeypatch.setattr(licensing_guard, "get_settings", lambda: _settings())
    licensing_guard.enforce_demo_source_locked("demo-123")  # no raise when off
