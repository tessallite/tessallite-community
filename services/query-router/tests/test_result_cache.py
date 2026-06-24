import asyncio
import pytest
from shared.cache.result_cache import ResultCache


# ---------------------------------------------------------------------------
# Core get/set/evict
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_miss_then_hit():
    cache = ResultCache(ttl_seconds=60)
    key = ("model1", "tenant1", "princHash", "queryHash")
    assert cache.get(key) is None

    data = {"columns": ["a"], "rows": [[1]]}
    cache.set(key, data)
    assert cache.get(key) is data


@pytest.mark.asyncio
async def test_ttl_zero_means_caching_disabled():
    """TTL=0 means caching is disabled — set is a no-op."""
    cache = ResultCache(ttl_seconds=0)
    key = ("m", "t", "p", "q")
    cache.set(key, {"rows": []})
    assert cache.get(key) is None


@pytest.mark.asyncio
async def test_evict_by_model_id():
    cache = ResultCache(ttl_seconds=60)
    k1 = ("model1", "t", "p", "q1")
    k2 = ("model1", "t", "p", "q2")
    k3 = ("model2", "t", "p", "q3")
    cache.set(k1, {"rows": []})
    cache.set(k2, {"rows": []})
    cache.set(k3, {"rows": []})
    cache.evict_model("model1")
    assert cache.get(k1) is None
    assert cache.get(k2) is None
    assert cache.get(k3) is not None


# ---------------------------------------------------------------------------
# clear()
# ---------------------------------------------------------------------------

def test_clear_removes_all_entries():
    cache = ResultCache(ttl_seconds=60)
    for i in range(5):
        cache.set((f"model{i}", "t", "p", "q"), {"rows": []})
    assert len(cache) == 5
    cache.clear()
    assert len(cache) == 0


def test_len_tracks_stored_entries():
    cache = ResultCache(ttl_seconds=60)
    assert len(cache) == 0
    cache.set(("m", "t", "p", "q"), {"rows": []})
    assert len(cache) == 1
    cache.set(("m2", "t", "p", "q"), {"rows": []})
    assert len(cache) == 2


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------

def test_make_query_hash_is_deterministic():
    d = {"model_id": "abc", "measures": ["revenue"], "dims": ["region"]}
    h1 = ResultCache.make_query_hash(d)
    h2 = ResultCache.make_query_hash(d)
    assert h1 == h2


def test_make_query_hash_different_for_different_queries():
    h1 = ResultCache.make_query_hash({"measures": ["a"]})
    h2 = ResultCache.make_query_hash({"measures": ["b"]})
    assert h1 != h2


def test_make_principal_hash_is_deterministic():
    d = {"user_id": "u1", "groups": ["admin"]}
    h1 = ResultCache.make_principal_hash(d)
    h2 = ResultCache.make_principal_hash(d)
    assert h1 == h2


def test_make_principal_hash_different_for_different_principals():
    h1 = ResultCache.make_principal_hash({"user_id": "u1"})
    h2 = ResultCache.make_principal_hash({"user_id": "u2"})
    assert h1 != h2


def test_make_principal_hash_separates_same_identity_different_subject():
    """F-007-10: roles/groups/claims drive RLS rule matching — the same
    identity with a different matching subject must never share a cache
    entry (mirrors the dict shape built in routes._handle_execute)."""
    base = {
        "user_identity": "alice@x",
        "roles": ["viewer"],
        "groups": [],
        "claims": {},
    }
    h_base = ResultCache.make_principal_hash(base)
    h_roles = ResultCache.make_principal_hash({**base, "roles": ["admin"]})
    h_groups = ResultCache.make_principal_hash({**base, "groups": ["g1"]})
    h_claims = ResultCache.make_principal_hash(
        {**base, "claims": {"department": "sales-emea"}}
    )
    assert len({h_base, h_roles, h_groups, h_claims}) == 4


def test_make_query_hash_key_order_independent():
    """sort_keys=True in JSON serialisation ensures key order doesn't matter."""
    d1 = {"a": 1, "b": 2}
    d2 = {"b": 2, "a": 1}
    assert ResultCache.make_query_hash(d1) == ResultCache.make_query_hash(d2)


# ---------------------------------------------------------------------------
# evict_model with no matching keys is safe
# ---------------------------------------------------------------------------

def test_evict_nonexistent_model_is_safe():
    cache = ResultCache(ttl_seconds=60)
    cache.evict_model("nonexistent-model")  # must not raise
    assert len(cache) == 0
