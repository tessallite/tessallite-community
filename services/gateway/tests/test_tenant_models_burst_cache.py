"""Bug-5534 follow-up: tenant catalog burst cache (Excel metadata slowness).

Every authenticated XMLA request re-enumerated projects + per-project models
(1 + N model-service GETs) even within one Excel discovery burst, dominating
metadata latency. ``list_all_models_for_tenant`` now burst-caches per
(tenant, JWT) for a short TTL.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import router_client


@pytest.fixture(autouse=True)
def _clear_cache():
    router_client._tenant_models_cache.clear()
    yield
    router_client._tenant_models_cache.clear()


def _patch_uncached(monkeypatch):
    calls = {"n": 0}

    async def _fake(tenant_slug, jwt_token):
        calls["n"] += 1
        return [{"id": "m1", "name": "modely", "project_id": "p1"}]

    monkeypatch.setattr(
        router_client, "_list_all_models_for_tenant_uncached", _fake
    )
    return calls


@pytest.mark.asyncio
async def test_second_call_within_ttl_served_from_cache(monkeypatch):
    calls = _patch_uncached(monkeypatch)
    a = await router_client.list_all_models_for_tenant("acme", "jwt-1")
    b = await router_client.list_all_models_for_tenant("acme", "jwt-1")
    assert calls["n"] == 1
    assert a == b
    # Cached copies are independent — a caller mutating its list must not
    # poison the cache.
    a[0]["name"] = "mutated"
    c = await router_client.list_all_models_for_tenant("acme", "jwt-1")
    assert c[0]["name"] == "modely"


@pytest.mark.asyncio
async def test_different_jwt_or_tenant_not_shared(monkeypatch):
    calls = _patch_uncached(monkeypatch)
    await router_client.list_all_models_for_tenant("acme", "jwt-1")
    await router_client.list_all_models_for_tenant("acme", "jwt-2")
    await router_client.list_all_models_for_tenant("other", "jwt-1")
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_expired_entry_refetches(monkeypatch):
    calls = _patch_uncached(monkeypatch)
    monkeypatch.setattr(router_client, "_tenant_models_cache_ttl", lambda: 0.0)
    await router_client.list_all_models_for_tenant("acme", "jwt-1")
    await router_client.list_all_models_for_tenant("acme", "jwt-1")
    assert calls["n"] == 2
