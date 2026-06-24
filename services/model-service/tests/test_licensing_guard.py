"""Phase 2: model-service create-cap enforcement glue.

Verifies the default-OFF no-op (full product unchanged) and the ON path
(Community) blocking over-cap creates. Uses a real signed license + key registry.
"""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src import licensing_guard
from shared.licensing.issuer.sign import generate_ed25519_keypair, sign_license


async def _load_license_from_file(monkeypatch, lf):
    """Populate the license manager from a signed license FILE.

    After the License-Manager refactor, ``get_license_manager()`` no longer auto-loads;
    the manager is (re)built by ``reload_license_manager()``, which reads
    ``load_license_doc_from_db()`` (system DB first, file fallback). Unit tests have no
    system DB, so mock that loader to return the file's doc and trigger the reload.
    Pass ``lf=None`` for the no-license case.
    """
    doc = json.loads(open(lf, encoding="utf-8").read()) if lf else None
    monkeypatch.setattr(
        licensing_guard, "load_license_doc_from_db", AsyncMock(return_value=doc)
    )
    await licensing_guard.reload_license_manager()


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
    # licensing_guard caches the manager in a module global (_MANAGER) with an async
    # reload_license_manager(); reset that singleton between tests. (Was .cache_clear()
    # from the old @lru_cache impl, which broke after the License-Manager refactor.)
    licensing_guard._MANAGER = None
    yield
    licensing_guard._MANAGER = None


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
    await _load_license_from_file(monkeypatch, lf)
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
    await _load_license_from_file(monkeypatch, lf)
    with pytest.raises(HTTPException) as exc:
        await licensing_guard.enforce_create_cap("model", lambda: _count(2))
    assert exc.value.status_code == 403
    assert "limit reached" in str(exc.value.detail).lower()


async def test_unactivated_when_enabled_without_license(monkeypatch):
    # enforcement ON but no license -> fail-CLOSED: the unactivated manager DENIES creates
    # (Bug-5474 fix). Not installing a licence must never grant the full product.
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(LICENSE_ENFORCEMENT_ENABLED=True),
    )
    await _load_license_from_file(monkeypatch, None)
    with pytest.raises(HTTPException) as exc:
        await licensing_guard.enforce_create_cap("project", lambda: _count(0))
    assert exc.value.status_code == 403


async def test_no_license_enforcement_on_is_unactivated_not_unlimited(monkeypatch):
    # Regression for Bug-5474 (the breach): enforcement ON + no licence must NOT fall back
    # to the full product. The manager is the fail-closed unactivated stub that denies every
    # create — so caps can't be bypassed by simply never installing a licence.
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(LICENSE_ENFORCEMENT_ENABLED=True),
    )
    await _load_license_from_file(monkeypatch, None)
    mgr = licensing_guard.get_license_manager()
    assert mgr.status()["activated"] is False
    assert mgr.can_create("model", 0).allowed is False
    assert mgr.can_create("user", 0).allowed is False
    assert mgr.can_create("project", 0).allowed is False


def test_no_license_enforcement_off_is_full_product(monkeypatch):
    # Counterpart: enforcement OFF + no licence stays the full product (dev/internal).
    monkeypatch.setattr(licensing_guard, "get_settings", lambda: _settings())
    mgr = licensing_guard.get_license_manager()
    assert mgr.can_create("model", 999).allowed is True


async def test_demo_source_locked_blocks_demo_tenant(tmp_path, monkeypatch):
    lf, pk = _community_license_file(tmp_path, demo_tenant_id="demo-123")
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(LICENSE_ENFORCEMENT_ENABLED=True, LICENSE_FILE=lf, LICENSE_PUBLIC_KEYS=pk),
    )
    await _load_license_from_file(monkeypatch, lf)
    with pytest.raises(HTTPException) as exc:
        licensing_guard.enforce_demo_source_locked("demo-123")
    assert exc.value.status_code == 403
    # the user's own tenant is unaffected
    licensing_guard.enforce_demo_source_locked("own-456")


def test_demo_source_lock_noop_when_enforcement_off(monkeypatch):
    monkeypatch.setattr(licensing_guard, "get_settings", lambda: _settings())
    licensing_guard.enforce_demo_source_locked("demo-123")  # no raise when off
