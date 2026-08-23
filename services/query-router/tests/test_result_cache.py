import asyncio
import time
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


# ---------------------------------------------------------------------------
# Bug-8507 — bounded retention: expired sweep + hard max-entry bound
# ---------------------------------------------------------------------------


def test_result_cache_enforces_a_hard_size_bound():
    """Bug-8507: unbounded dict grew the process heap without limit.  Inserting
    more entries than max_entries must trigger eviction so the store stays
    bounded."""
    limit = 5
    cache = ResultCache(ttl_seconds=60, max_entries=limit)
    for i in range(limit + 20):
        cache.set((f"model-{i}", "t", "p", f"q-{i}"), {"rows": [i]})
    assert len(cache) <= limit


def test_result_cache_sweeps_expired_entries_on_set():
    """Bug-8507: an expired entry whose key is never looked up again must be
    reclaimed by the next insert, not left resident until process restart."""
    cache = ResultCache(ttl_seconds=60, max_entries=10_000)
    dead_key = ("model-dead", "t", "p", "q-dead")
    cache.set(dead_key, {"rows": ["stale"]})

    # Manually expire the entry by backdating its expiry.
    cache._store[dead_key] = (time.monotonic() - 1, {"rows": ["stale"]})

    # The next insert triggers the sweep.
    cache.set(("model-live", "t", "p", "q-live"), {"rows": ["fresh"]})
    assert dead_key not in cache._store, (
        "expired entry must be swept on the next set(), not left resident"
    )


def test_size_bound_evicts_nearest_to_expiry_first():
    """The eviction policy removes the entry closest to its TTL deadline
    (oldest insert under uniform TTL), preserving the freshest entries."""
    cache = ResultCache(ttl_seconds=300, max_entries=3)
    k1 = ("m1", "t", "p", "q1")
    k2 = ("m2", "t", "p", "q2")
    k3 = ("m3", "t", "p", "q3")
    k4 = ("m4", "t", "p", "q4")

    cache.set(k1, "first")
    cache.set(k2, "second")
    cache.set(k3, "third")
    # At this point the cache is full (3 entries).
    cache.set(k4, "fourth")
    # k1 was the oldest (nearest to expiry) and should have been evicted.
    assert cache.get(k1) is None, "oldest entry should have been evicted"
    assert cache.get(k4) is not None, "newest entry must be retained"
    assert len(cache) <= 3


def test_sweep_does_not_remove_live_entries():
    """The expired-entry sweep must leave entries that are still within their
    TTL untouched."""
    cache = ResultCache(ttl_seconds=300, max_entries=10_000)
    live_key = ("model-live", "t", "p", "q-live")
    cache.set(live_key, {"rows": ["ok"]})

    # Insert another entry to trigger the sweep.
    cache.set(("model-other", "t", "p", "q-other"), {"rows": ["also ok"]})
    assert live_key in cache._store, (
        "live (non-expired) entry must survive the sweep"
    )


def test_max_entries_constructor_parameter():
    """The max_entries parameter controls the bound — a smaller bound means
    fewer entries retained."""
    cache_small = ResultCache(ttl_seconds=60, max_entries=2)
    cache_large = ResultCache(ttl_seconds=60, max_entries=100)

    for i in range(10):
        key = (f"m{i}", "t", "p", f"q{i}")
        cache_small.set(key, i)
        cache_large.set(key, i)

    assert len(cache_small) <= 2
    assert len(cache_large) == 10


@pytest.mark.parametrize("max_entries", [0, -1, True, 1.5])
def test_max_entries_must_be_a_positive_integer(max_entries):
    with pytest.raises(ValueError, match="positive integer"):
        ResultCache(max_entries=max_entries)
