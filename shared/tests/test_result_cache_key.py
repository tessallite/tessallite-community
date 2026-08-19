"""Bug-8250 (finding 5) — the result-cache key must discriminate deploy_epoch.

The query-router's result-cache fast path returns BEFORE ``route_query``, so a
cache hit runs neither the aggregate nor the pocket ``built_for`` compatibility
gate. The key is therefore the only check a replayed result passes through.

It keyed on ``deployed_version_id`` alone. A revert to the currently-deployed
version, and a redeploy of the same version after draft edits, both leave that
id unchanged while bumping ``deploy_epoch`` (Bug-7140) — so the pre-move result
still matched its key and was served with the old numbers. Best-effort eviction
does not cover it: ``DELETE /cache/models/{id}`` clears only the replica that
receives the call, while key discrimination misses on every replica at once.

These tests pin every component of the documented key, so a future component
being dropped from the builder fails here rather than silently degrading a
cross-replica invalidation guarantee into a TTL.
"""
from __future__ import annotations

import uuid

import pytest

from shared.cache.result_cache import ResultCache

_BASE = dict(
    model_id="model-1",
    tenant_id="acme",
    principal_hash="ph",
    query_hash="qh",
    force_route=None,
    include_hidden=False,
    deployed_version_id="v1",
    rls_policy_hash="",
    row_limit=None,
    deploy_epoch=3,
)


def _key(**overrides):
    return ResultCache.make_cache_key(**{**_BASE, **overrides})


def test_same_inputs_produce_the_same_key():
    assert _key() == _key()


def test_deploy_epoch_alone_changes_the_key():
    """A revert to the SAME version bumps only the epoch; the key must move."""
    assert _key(deploy_epoch=3) != _key(deploy_epoch=4)


def test_a_revert_to_the_same_version_misses_the_cached_entry():
    """End-to-end through the cache, not just the key tuple."""
    cache = ResultCache(ttl_seconds=300)
    before = _key(deployed_version_id="v1", deploy_epoch=3)
    cache.set(before, {"rows": ["stale numbers"]})
    assert cache.get(before) is not None

    after_revert = _key(deployed_version_id="v1", deploy_epoch=4)
    assert cache.get(after_revert) is None, (
        "a same-version revert must miss the pre-revert cached result"
    )


@pytest.mark.parametrize(
    "field,other",
    [
        ("model_id", "model-2"),
        ("tenant_id", "other-tenant"),
        ("principal_hash", "ph2"),
        ("query_hash", "qh2"),
        ("force_route", "aggregate"),
        ("include_hidden", True),
        ("deployed_version_id", "v2"),
        ("rls_policy_hash", "rls2"),
        ("row_limit", 100),
        ("deploy_epoch", 99),
    ],
)
def test_every_documented_component_participates(field, other):
    """Each component named in the module contract must change the key.

    A component present in the docstring but absent from the builder is exactly
    the ``deploy_epoch`` defect: the invalidation guarantee reads as satisfied
    while nothing enforces it.
    """
    assert _key() != _key(**{field: other})


def test_model_id_stays_first_so_evict_model_still_works():
    """``evict_model`` matches on ``key[0]``; reordering would silently break it."""
    cache = ResultCache(ttl_seconds=300)
    cache.set(_key(model_id="model-1"), {"rows": []})
    cache.set(_key(model_id="model-2"), {"rows": []})
    cache.evict_model("model-1")
    assert cache.get(_key(model_id="model-1")) is None
    assert cache.get(_key(model_id="model-2")) is not None


def test_uuid_and_string_forms_do_not_mint_two_keys():
    """A caller passing a UUID and one passing its string form must collide."""
    mid = uuid.uuid4()
    vid = uuid.uuid4()
    assert _key(model_id=mid, deployed_version_id=vid) == _key(
        model_id=str(mid), deployed_version_id=str(vid)
    )


def test_absent_row_limit_and_epoch_are_stable_empty_slots():
    assert _key(row_limit=None)[8] == ""
    assert _key(deploy_epoch=None)[9] == ""
