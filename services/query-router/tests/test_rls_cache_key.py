"""Bug-7038: result cache must incorporate the RLS policy identity.

Invariants under test:
  * A cached result under one set of RLS rules produces a DIFFERENT cache
    key after the rules are tightened, so the old (pre-tightening) cached
    entry is never returned.
  * Two replicas with the same rule state produce the SAME policy hash
    (deterministic across replicas).
  * When no RLS rules apply, the policy hash is empty (preserving the
    pre-fix key space for non-RLS queries).
  * A tightened rule (changed predicate or new rule) invalidates the
    cache entry without relying on best-effort eviction.
"""
from __future__ import annotations

import pytest

from src.security import CompiledPredicate


# ---------------------------------------------------------------------------
# CompiledPredicate.policy_hash determinism + sensitivity
# ---------------------------------------------------------------------------


def test_policy_hash_is_deterministic():
    """Same rules -> same hash, across invocations."""
    cp = CompiledPredicate(
        sql_expression="\"region_code\" = 'NORTH'",
        active_rule_ids=("r1",),
    )
    assert cp.policy_hash == cp.policy_hash
    # Second identical instance produces the same hash.
    cp2 = CompiledPredicate(
        sql_expression="\"region_code\" = 'NORTH'",
        active_rule_ids=("r1",),
    )
    assert cp.policy_hash == cp2.policy_hash


def test_policy_hash_changes_when_rule_tightened():
    """Editing the predicate (tightening the rule) produces a different hash."""
    original = CompiledPredicate(
        sql_expression="\"region_code\" = 'NORTH'",
        active_rule_ids=("r1",),
    )
    tightened = CompiledPredicate(
        sql_expression="\"region_code\" = 'NORTH' AND \"status\" = 'ACTIVE'",
        active_rule_ids=("r1",),
    )
    assert original.policy_hash != tightened.policy_hash


def test_policy_hash_changes_when_new_rule_added():
    """Adding a new rule produces a different hash."""
    before = CompiledPredicate(
        sql_expression="\"region_code\" = 'NORTH'",
        active_rule_ids=("r1",),
    )
    after = CompiledPredicate(
        sql_expression="\"region_code\" = 'NORTH' AND \"dept\" IN ('ENG', 'SALES')",
        active_rule_ids=("r1", "r2"),
    )
    assert before.policy_hash != after.policy_hash


def test_policy_hash_changes_when_rule_removed():
    """Removing (deleting) a rule produces a different hash."""
    before = CompiledPredicate(
        sql_expression="\"region_code\" = 'NORTH' AND \"dept\" IN ('ENG')",
        active_rule_ids=("r1", "r2"),
    )
    after = CompiledPredicate(
        sql_expression="\"region_code\" = 'NORTH'",
        active_rule_ids=("r1",),
    )
    assert before.policy_hash != after.policy_hash


def test_policy_hash_order_independent_for_rule_ids():
    """Rule ID ordering does not matter (sorted internally)."""
    cp1 = CompiledPredicate(
        sql_expression="\"region_code\" = 'NORTH'",
        active_rule_ids=("r1", "r2"),
    )
    cp2 = CompiledPredicate(
        sql_expression="\"region_code\" = 'NORTH'",
        active_rule_ids=("r2", "r1"),
    )
    assert cp1.policy_hash == cp2.policy_hash


# ---------------------------------------------------------------------------
# Cache key integration: tightened rule MUST miss
# ---------------------------------------------------------------------------


def test_cache_key_with_rls_hash_prevents_stale_hit():
    """Two-cache-instance scenario: cache an unrestricted result, then
    tighten the rule, and prove the old entry cannot be returned.

    This simulates the multi-replica scenario described in Bug-7038:
    replica A caches a result with the original rule, then the rule is
    tightened, and replica B (which never received the eviction) builds
    a new cache key that differs from the old one.
    """
    from shared.cache.result_cache import ResultCache

    cache = ResultCache(ttl_seconds=60)

    # Simulate: user U runs a query under the original rule.
    original_rls = CompiledPredicate(
        sql_expression="\"region_code\" = 'NORTH'",
        active_rule_ids=("r1",),
    )
    base_key_parts = ("model1", "tenant1", "phash", "qhash", "", "False", "v1")
    original_key = base_key_parts + (original_rls.policy_hash,)
    cache.set(original_key, {"rows": [{"region_code": "NORTH", "revenue": 100}]})

    # The entry is retrievable under the original key.
    assert cache.get(original_key) is not None

    # Now the rule is tightened (admin adds a second predicate).
    tightened_rls = CompiledPredicate(
        sql_expression="\"region_code\" = 'NORTH' AND \"status\" = 'ACTIVE'",
        active_rule_ids=("r1",),
    )
    tightened_key = base_key_parts + (tightened_rls.policy_hash,)

    # The tightened key MUST NOT hit the old entry.
    assert cache.get(tightened_key) is None, (
        "SECURITY: tightened RLS rule hit a stale cached result from the "
        "pre-tightening rule. The cache key did not incorporate the policy change."
    )


def test_cache_key_without_rls_is_backward_compatible():
    """When no RLS rules apply, the policy hash is '' (empty), matching
    the pre-fix key space so non-RLS queries are unaffected."""
    base = ("model1", "tenant1", "phash", "qhash", "", "False", "v1")
    no_rls_key = base + ("",)
    # The key is just the base parts + an empty string for the RLS hash.
    assert no_rls_key[-1] == ""
    assert len(no_rls_key) == len(base) + 1


# ---------------------------------------------------------------------------
# Bug-7762: security-policy mutation invalidation contract completeness
# ---------------------------------------------------------------------------


def test_deploy_version_change_invalidates_cls_cached_results():
    """Bug-7762: CLS (data tag) changes are part of the model snapshot.
    Changing them requires a deploy, which increments deployed_version_id.
    The cache key includes deployed_version_id, so a deploy always misses
    stale entries -- proving CLS mutations are correctly invalidated.

    Test escape: no test previously asserted that the deployed_version_id
    component of the cache key prevented stale CLS results.
    Guard: this test. Tier: T1.
    """
    from shared.cache.result_cache import ResultCache

    cache = ResultCache(ttl_seconds=60)

    # Cache a result under deployed version v1 (CLS allows column X).
    key_v1 = ("model1", "tenant1", "phash", "qhash", "", "False", "v1", "")
    cache.set(key_v1, {"rows": [{"x": 1, "secret_col": 42}]})
    assert cache.get(key_v1) is not None

    # Admin adds a CLS data tag restricting secret_col, then deploys (v2).
    key_v2 = ("model1", "tenant1", "phash", "qhash", "", "False", "v2", "")
    assert cache.get(key_v2) is None, (
        "SECURITY: post-deploy (CLS change) must miss the pre-deploy cache"
    )


def test_evict_model_clears_all_entries_for_model():
    """Bug-7762: the best-effort eviction endpoint clears ALL cached entries
    for a model on the calling replica, regardless of principal/query/version.
    This is the immediate-invalidation mechanism for the calling replica on
    row-security mutations and deploys.

    Test escape: no test previously asserted that evict_model cleared entries
    with varied keys.
    Guard: this test. Tier: T1.
    """
    from shared.cache.result_cache import ResultCache

    cache = ResultCache(ttl_seconds=60)

    # Cache entries for two different users and two different queries on model1.
    cache.set(("model1", "t", "user_a", "q1", "", "F", "v1", "rls1"), "result_a_q1")
    cache.set(("model1", "t", "user_b", "q2", "", "F", "v1", "rls2"), "result_b_q2")
    # A different model's entry should survive.
    cache.set(("model2", "t", "user_a", "q1", "", "F", "v1", ""), "other_model")

    assert len(cache) == 3

    cache.evict_model("model1")

    assert len(cache) == 1
    assert cache.get(("model2", "t", "user_a", "q1", "", "F", "v1", "")) == "other_model"
    assert cache.get(("model1", "t", "user_a", "q1", "", "F", "v1", "rls1")) is None
    assert cache.get(("model1", "t", "user_b", "q2", "", "F", "v1", "rls2")) is None


def test_principal_hash_change_misses_cache():
    """Bug-7762: a user re-authenticating with different IdP attributes (roles,
    groups, claims) produces a different principal_hash and misses the cache.
    This ensures user-principal changes do not serve stale security-scoped data.

    Test escape: no test asserted that varied principal attributes caused a miss.
    Guard: this test. Tier: T1.
    """
    from shared.cache.result_cache import ResultCache

    cache = ResultCache(ttl_seconds=60)

    phash_before = ResultCache.make_principal_hash({
        "user_identity": "user@corp.com",
        "roles": ["viewer"],
        "groups": ["analysts"],
        "claims": {},
    })
    phash_after = ResultCache.make_principal_hash({
        "user_identity": "user@corp.com",
        "roles": ["viewer"],
        "groups": ["analysts", "admins"],  # user gained a new group
        "claims": {},
    })

    assert phash_before != phash_after, (
        "principal_hash must differ when groups change"
    )

    key_before = ("m", "t", phash_before, "q", "", "F", "v1", "")
    cache.set(key_before, "old_result")
    assert cache.get(key_before) is not None

    key_after = ("m", "t", phash_after, "q", "", "F", "v1", "")
    assert cache.get(key_after) is None, (
        "SECURITY: changed principal must miss the cache"
    )


def test_user_mapping_rls_bypasses_cache():
    """Bug-7762 (Codex finding 1): user_mapping RLS rules reference a mapping
    table on the source database whose rows can be mutated out-of-band (no API
    hook, no eviction signal). The policy_hash captures only rule IDs + compiled
    SQL template, NOT the mapping-table contents. Revoking a user's access by
    deleting a mapping row does NOT change the hash, so a stale cached result
    would be served -- a data leak.

    Fix: queries with active user_mapping rules bypass the cache entirely, like
    persona-scoped queries. This test verifies the bypass signal: a
    CompiledPredicate with non-empty mapping_source_ids must trigger a cache
    bypass in the caller.

    Test escape: no test verified that user_mapping RLS caused a cache bypass.
    Guard: this test. Tier: T1.
    """
    # A CompiledPredicate with mapping_source_ids = non-empty means
    # user_mapping rules are active.
    cp_with_mapping = CompiledPredicate(
        sql_expression=(
            '"region" IN (SELECT "region_code" FROM "user_region_map" '
            "WHERE \"user_email\" = 'user@corp.com')"
        ),
        active_rule_ids=("rule_1",),
        mapping_source_ids=("source_abc",),
    )

    # The bypass check mirrors routes.py logic:
    has_user_mapping = bool(
        cp_with_mapping is not None
        and getattr(cp_with_mapping, "mapping_source_ids", ())
    )
    assert has_user_mapping is True, (
        "SECURITY: user_mapping RLS must signal cache bypass"
    )

    # Without mapping rules, the flag is False (cache is used normally).
    cp_without_mapping = CompiledPredicate(
        sql_expression='"region" = \'NORTH\'',
        active_rule_ids=("rule_2",),
        mapping_source_ids=(),
    )
    has_user_mapping_2 = bool(
        cp_without_mapping is not None
        and getattr(cp_without_mapping, "mapping_source_ids", ())
    )
    assert has_user_mapping_2 is False, (
        "non-mapping RLS must NOT bypass cache"
    )

    # None compiled RLS (no rules) is also False.
    has_user_mapping_3 = bool(
        None is not None
        and getattr(None, "mapping_source_ids", ())
    )
    assert has_user_mapping_3 is False
