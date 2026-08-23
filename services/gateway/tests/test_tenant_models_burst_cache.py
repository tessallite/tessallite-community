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
    router_client._reset_metadata_caches_for_tests()
    yield
    router_client._reset_metadata_caches_for_tests()


def _patch_uncached(monkeypatch, degraded: int = 0):
    """Stub the uncached lister.

    Bug-9218 changed this helper's contract to ``(models, degraded_project_count)``
    so a caller resolving ONE named model can tell a partial listing apart from
    a missing model. The stub returns the same pair.
    """
    calls = {"n": 0}

    async def _fake(tenant_slug, jwt_token):
        calls["n"] += 1
        return [{"id": "m1", "name": "modely", "project_id": "p1"}], degraded

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


@pytest.mark.asyncio
async def test_cache_hit_reports_the_listing_as_degraded_too(monkeypatch):
    """Bug-9218: the degraded count must travel WITH the cached entry.

    A partial listing that is cached and then re-served must still report
    itself as partial. If the count were recomputed only on a cache MISS, the
    second connection inside the TTL window would read a partial list and
    believe it was complete — and a model living in the dropped project would
    be reported to the BI client as ``Unknown model`` rather than as a service
    fault.
    """
    calls = _patch_uncached(monkeypatch, degraded=1)

    await router_client.list_all_models_for_tenant("acme", "jwt-1")
    assert router_client.tenant_listing_degraded("acme", "jwt-1") == 1

    # Second call is a cache HIT (the stub is not re-entered) and must still
    # report the listing as incomplete.
    await router_client.list_all_models_for_tenant("acme", "jwt-1")
    assert calls["n"] == 1, "expected a cache hit"
    assert router_client.tenant_listing_degraded("acme", "jwt-1") == 1


@pytest.mark.asyncio
async def test_complete_listing_reports_zero_degraded(monkeypatch):
    _patch_uncached(monkeypatch, degraded=0)
    await router_client.list_all_models_for_tenant("acme", "jwt-1")
    assert router_client.tenant_listing_degraded("acme", "jwt-1") == 0
    # An unknown scope has never been listed, so it is not "degraded".
    assert router_client.tenant_listing_degraded("other", "jwt-9") == 0
