"""Bug-9061 — the per-model metadata fan-out is burst-cached, and fails closed.

``fetch_model_metadata`` issues six model-service GETs PER MODEL. Only the cheap
model LIST was ever cached, so every JDBC connection re-ran the whole
O(models x 6) fan-out before it could answer a query — the ~8 s connection setup
that made ``validate_security.py`` time out on its own test model. It is the
same uncached primitive Bug-9112 hit from the per-query seam.

The filed diagnosis said an in-process cache could not help because "each JDBC
connection is served by its own process". That is wrong, and it is why the
obvious cache was never written: ``PGWireServer._pid`` is a synthetic
per-connection counter used as the PostgreSQL backend pid, not an OS process id.
``test_connections_share_one_process_so_a_module_cache_applies`` pins that,
because the whole fix rests on it.

The cache must FAIL CLOSED — a sibling lane shipped a result cache that returned
"servable" on an unprovable lookup and served stale results indefinitely. These
tests assert the four properties that prevent the same class here:
failure is never cached, degradation is never cached, scope is in the key, and
the CLS revalidation path does not read it.

Execution scope: isolated. Gate tier: T1.
"""
from __future__ import annotations

import pytest

from src import router_client
from src.jdbc.server import PGWireServer

MODEL_ID = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(autouse=True)
def _clear_caches():
    router_client._reset_metadata_caches_for_tests()
    yield
    router_client._reset_metadata_caches_for_tests()


def _count_fanouts(monkeypatch, *, fail_model: bool = False):
    """Stub the model-service calls the fan-out makes, counting invocations."""
    calls = {"n": 0}

    async def _list(tenant_slug, jwt_token):
        return [
            {
                "id": MODEL_ID,
                "slug": "modely",
                "project_id": "p1",
                "project_slug": "project1",
                "deployed_version_id": None,
            }
        ], 0

    async def _dims(mid, tenant, jwt, project_id="", persona_id=None):
        calls["n"] += 1
        if fail_model:
            raise RuntimeError("model-service 503")
        return []

    async def _empty_list(mid, tenant, jwt, project_id="", persona_id=None):
        return []

    async def _empty_dict(mid, tenant, jwt, project_id="", persona_id=None):
        return {}

    monkeypatch.setattr(
        router_client, "_list_all_models_for_tenant_uncached", _list
    )
    monkeypatch.setattr(router_client, "get_model_dimensions", _dims)
    monkeypatch.setattr(router_client, "get_model_measures", _empty_list)
    monkeypatch.setattr(router_client, "get_model_personas", _empty_list)
    monkeypatch.setattr(router_client, "get_model_snapshot", _empty_dict)
    monkeypatch.setattr(router_client, "get_model_kpis", _empty_list)
    return calls


@pytest.mark.asyncio
async def test_second_connection_reuses_the_fan_out(monkeypatch):
    """The fix itself: connection 2 does not repeat connection 1's fan-out."""
    calls = _count_fanouts(monkeypatch)

    first = await router_client.fetch_model_metadata(
        MODEL_ID, "acme", "jwt", use_cache=True
    )
    assert calls["n"] == 1
    second = await router_client.fetch_model_metadata(
        MODEL_ID, "acme", "jwt", use_cache=True
    )
    assert calls["n"] == 1, "the metadata fan-out ran again for a second connection"
    assert second == first


@pytest.mark.asyncio
async def test_the_cache_is_opt_in_so_the_cls_refresh_path_never_reads_it(
    monkeypatch,
):
    """Bug-7043's revalidation defaults to ttl=0 precisely so a tightened
    persona restriction hides columns IMMEDIATELY. Serving that path from a 30 s
    cache would silently undo a security contract, so it must re-fetch."""
    calls = _count_fanouts(monkeypatch)

    await router_client.fetch_model_metadata(MODEL_ID, "acme", "jwt", use_cache=True)
    assert calls["n"] == 1
    # The refresh seam calls WITHOUT use_cache — it must go back to the source.
    await router_client.fetch_model_metadata(MODEL_ID, "acme", "jwt")
    assert calls["n"] == 2, "the CLS refresh path was served from the burst cache"


@pytest.mark.asyncio
async def test_a_degraded_fetch_is_never_cached(monkeypatch):
    """Fail closed: a model whose metadata could not be fetched is DROPPED from
    a broad discovery result. Pinning that partial answer for the TTL would turn
    a transient model-service blip into 30 seconds of "my model vanished"."""
    calls = _count_fanouts(monkeypatch, fail_model=True)

    first = await router_client.fetch_model_metadata(
        None, "acme", "jwt", use_cache=True
    )
    assert first[0] == []  # the model was dropped
    assert calls["n"] == 1

    await router_client.fetch_model_metadata(None, "acme", "jwt", use_cache=True)
    assert calls["n"] == 2, "a degraded result was cached"


@pytest.mark.asyncio
async def test_optional_metadata_failure_is_never_cached(monkeypatch):
    """Bug-9061: a persona/snapshot/KPI failure is still an incomplete fan-out,
    even when dimensions and measures are available for a broad catalogue."""
    calls = _count_fanouts(monkeypatch)

    async def _snapshot_failure(*_args, **_kwargs):
        raise RuntimeError("snapshot service 503")

    monkeypatch.setattr(router_client, "get_model_snapshot", _snapshot_failure)
    first = await router_client.fetch_model_metadata(
        None, "acme", "jwt", use_cache=True
    )
    assert first[0], "the broad path may retain verified base metadata"
    assert calls["n"] == 1

    await router_client.fetch_model_metadata(None, "acme", "jwt", use_cache=True)
    assert calls["n"] == 2, "an optional metadata failure was cached"


@pytest.mark.asyncio
async def test_scope_is_in_the_key(monkeypatch):
    """A re-login, a tenant switch, a different model or a different project
    scope must never read another scope's entry.

    Asserted two ways: each scope pays its own fan-out (nothing was served from
    a neighbour's entry), and the five scopes occupy five distinct cache keys.
    """
    calls = _count_fanouts(monkeypatch)

    scopes = [
        dict(model_id=MODEL_ID, tenant_slug="acme", jwt_token="jwt-1"),
        dict(model_id=MODEL_ID, tenant_slug="acme", jwt_token="jwt-2"),
        dict(model_id=MODEL_ID, tenant_slug="other", jwt_token="jwt-1"),
        dict(model_id=None, tenant_slug="acme", jwt_token="jwt-1"),
        dict(
            model_id=MODEL_ID, tenant_slug="acme", jwt_token="jwt-1",
            project_slug="project1",
        ),
    ]
    for scope in scopes:
        await router_client.fetch_model_metadata(use_cache=True, **scope)

    assert calls["n"] == len(scopes), "a scope was served another scope's entry"
    assert len(router_client._metadata_cache) == len(scopes), (
        "distinct scopes collapsed onto one cache key"
    )

    # Repeating every scope must now hit — proving the keys are STABLE as well
    # as distinct (an unstable key stores an entry nothing ever reads).
    for scope in scopes:
        await router_client.fetch_model_metadata(use_cache=True, **scope)
    assert calls["n"] == len(scopes), "a stored entry was never read back"


@pytest.mark.asyncio
async def test_entry_expires_and_is_refetched(monkeypatch):
    calls = _count_fanouts(monkeypatch)
    await router_client.fetch_model_metadata(MODEL_ID, "acme", "jwt", use_cache=True)
    monkeypatch.setattr(router_client, "_tenant_models_cache_ttl", lambda: 0.0)
    await router_client.fetch_model_metadata(MODEL_ID, "acme", "jwt", use_cache=True)
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_a_metadata_failure_is_never_cached(monkeypatch):
    """A raised ``ModelMetadataUnavailable`` must leave no entry behind."""

    async def _boom(tenant_slug, jwt_token):
        raise RuntimeError("model-service 500")

    monkeypatch.setattr(router_client, "list_all_models_for_tenant", _boom)

    with pytest.raises(router_client.ModelMetadataUnavailable):
        await router_client.fetch_model_metadata(
            MODEL_ID, "acme", "jwt", use_cache=True
        )
    assert router_client._metadata_cache == {}


def test_connections_share_one_process_so_a_module_cache_applies():
    """The premise the whole fix rests on, pinned.

    Bug-9061 recorded "each JDBC connection is served by its own process — every
    log line carries a distinct pid, so in-process caches are cold on every
    connection". Those pids are a module-level COUNTER (the PostgreSQL backend
    pid the gateway reports to the client), and every connection is an asyncio
    task in ONE process. If this ever became untrue, the burst cache above would
    silently stop helping and this test is where that shows up.
    """
    import os

    a, b = PGWireServer(), PGWireServer()
    assert a._pid != b._pid, "backend pids must be unique per connection"
    assert a._pid != os.getpid() or b._pid != os.getpid(), (
        "the backend pid is a synthetic counter, not the OS process id"
    )
    assert b._pid == a._pid + 1, "the counter is monotonic within one process"


def test_bug9061_zero_env_disables_metadata_cache(monkeypatch):
    """Bug-9061: XMLA_METADATA_CACHE_TTL=0 is an explicit cache bypass."""
    monkeypatch.setenv("XMLA_METADATA_CACHE_TTL", "0")
    assert router_client._tenant_models_cache_ttl() == 0.0
