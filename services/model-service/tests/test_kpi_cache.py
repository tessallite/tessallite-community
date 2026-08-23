"""Tests for KPI evaluation cache."""
from __future__ import annotations

import time
import uuid
from unittest.mock import patch

import pytest

from src.kpi_cache import KpiEvalCache

pytestmark = pytest.mark.unit


def _uid() -> uuid.UUID:
    return uuid.uuid4()


TENANT = "test-tenant"
MODEL_ID = _uid()
KPI_ID_A = _uid()
KPI_ID_B = _uid()


class TestCacheGetPut:
    def test_cache_miss_returns_none(self):
        cache = KpiEvalCache()
        assert cache.get(TENANT, MODEL_ID, KPI_ID_A) is None

    def test_cache_hit_returns_value(self):
        cache = KpiEvalCache()
        cache.put(TENANT, MODEL_ID, KPI_ID_A, {"value": 42})
        result = cache.get(TENANT, MODEL_ID, KPI_ID_A)
        assert result == {"value": 42}

    def test_different_kpis_are_separate(self):
        cache = KpiEvalCache()
        cache.put(TENANT, MODEL_ID, KPI_ID_A, {"value": 1})
        cache.put(TENANT, MODEL_ID, KPI_ID_B, {"value": 2})
        assert cache.get(TENANT, MODEL_ID, KPI_ID_A) == {"value": 1}
        assert cache.get(TENANT, MODEL_ID, KPI_ID_B) == {"value": 2}

    def test_different_calc_agg_modes_are_separate(self):
        cache = KpiEvalCache()
        cache.put(TENANT, MODEL_ID, KPI_ID_A, {"v": 1}, calc_agg_mode="automatic")
        cache.put(TENANT, MODEL_ID, KPI_ID_A, {"v": 2}, calc_agg_mode="aggregate_first")
        assert cache.get(TENANT, MODEL_ID, KPI_ID_A, calc_agg_mode="automatic") == {"v": 1}
        assert cache.get(TENANT, MODEL_ID, KPI_ID_A, calc_agg_mode="aggregate_first") == {"v": 2}

    def test_different_filters_are_separate(self):
        cache = KpiEvalCache()
        f1 = [{"dim": "country", "value": "US"}]
        f2 = [{"dim": "country", "value": "UK"}]
        cache.put(TENANT, MODEL_ID, KPI_ID_A, {"v": "us"}, filters=f1)
        cache.put(TENANT, MODEL_ID, KPI_ID_A, {"v": "uk"}, filters=f2)
        assert cache.get(TENANT, MODEL_ID, KPI_ID_A, filters=f1) == {"v": "us"}
        assert cache.get(TENANT, MODEL_ID, KPI_ID_A, filters=f2) == {"v": "uk"}

    def test_size_reflects_entries(self):
        cache = KpiEvalCache()
        assert cache.size == 0
        cache.put(TENANT, MODEL_ID, KPI_ID_A, "x")
        assert cache.size == 1
        cache.put(TENANT, MODEL_ID, KPI_ID_B, "y")
        assert cache.size == 2

    def test_different_users_are_separate(self):
        cache = KpiEvalCache()
        cache.put(TENANT, MODEL_ID, KPI_ID_A, {"v": "alice"}, user_id="alice")
        cache.put(TENANT, MODEL_ID, KPI_ID_A, {"v": "bob"}, user_id="bob")

        assert cache.get(TENANT, MODEL_ID, KPI_ID_A, user_id="alice") == {"v": "alice"}
        assert cache.get(TENANT, MODEL_ID, KPI_ID_A, user_id="bob") == {"v": "bob"}

    def test_different_personas_are_separate(self):
        cache = KpiEvalCache()
        persona_a = str(_uid())
        persona_b = str(_uid())
        cache.put(TENANT, MODEL_ID, KPI_ID_A, {"v": "finance"}, user_id="alice", persona_id=persona_a)
        cache.put(TENANT, MODEL_ID, KPI_ID_A, {"v": "sales"}, user_id="alice", persona_id=persona_b)

        assert cache.get(TENANT, MODEL_ID, KPI_ID_A, user_id="alice", persona_id=persona_a) == {"v": "finance"}
        assert cache.get(TENANT, MODEL_ID, KPI_ID_A, user_id="alice", persona_id=persona_b) == {"v": "sales"}
        assert cache.get(TENANT, MODEL_ID, KPI_ID_A, user_id="alice") is None

    def test_different_definition_versions_are_separate(self):
        # F-017-01: the served definition version (deployed version:epoch) keys
        # the cache; a new deployed definition must miss the old entry.
        cache = KpiEvalCache()
        cache.put(TENANT, MODEL_ID, KPI_ID_A, {"v": "v1"}, definition_version="ver1:1")
        assert cache.get(TENANT, MODEL_ID, KPI_ID_A, definition_version="ver1:1") == {"v": "v1"}
        # A redeploy advances the epoch -> new key -> miss.
        assert cache.get(TENANT, MODEL_ID, KPI_ID_A, definition_version="ver1:2") is None

    def test_data_epoch_bump_invalidates_entry(self):
        # F-017-03 (Bug-7989): a data refresh bumps Model.data_epoch, which is
        # folded into the cache key. The next evaluation reads the new epoch,
        # forms a new key, and misses the pre-refresh entry -- on every replica,
        # without waiting out the TTL and with no cross-process eviction.
        cache = KpiEvalCache()
        cache.put(TENANT, MODEL_ID, KPI_ID_A, {"value": 100}, data_epoch=7)
        # Same epoch still hits.
        assert cache.get(TENANT, MODEL_ID, KPI_ID_A, data_epoch=7) == {"value": 100}
        # After a refresh bumped the epoch, the stale entry is a miss.
        assert cache.get(TENANT, MODEL_ID, KPI_ID_A, data_epoch=8) is None

    def test_data_epoch_absent_matches_absent(self):
        # Backward compatibility: entries stored without a data_epoch (legacy /
        # non-model paths) still round-trip when data_epoch is omitted.
        cache = KpiEvalCache()
        cache.put(TENANT, MODEL_ID, KPI_ID_A, {"value": 5})
        assert cache.get(TENANT, MODEL_ID, KPI_ID_A) == {"value": 5}
        # An entry keyed with an epoch is distinct from one without.
        cache.put(TENANT, MODEL_ID, KPI_ID_B, {"value": 6}, data_epoch=0)
        assert cache.get(TENANT, MODEL_ID, KPI_ID_B) is None
        assert cache.get(TENANT, MODEL_ID, KPI_ID_B, data_epoch=0) == {"value": 6}


class TestCacheTTL:
    def test_expired_entry_returns_none(self):
        cache = KpiEvalCache(ttl_seconds=1)
        cache.put(TENANT, MODEL_ID, KPI_ID_A, {"value": 42})

        # Fast-forward monotonic clock past TTL
        original_entry = cache._store[list(cache._store.keys())[0]]
        original_entry.expires_at = time.monotonic() - 1

        assert cache.get(TENANT, MODEL_ID, KPI_ID_A) is None

    def test_not_expired_returns_value(self):
        cache = KpiEvalCache(ttl_seconds=300)
        cache.put(TENANT, MODEL_ID, KPI_ID_A, {"value": 42})
        assert cache.get(TENANT, MODEL_ID, KPI_ID_A) == {"value": 42}


class TestCacheInvalidation:
    def test_invalidate_model_clears_all_kpis(self):
        cache = KpiEvalCache()
        cache.put(TENANT, MODEL_ID, KPI_ID_A, "a")
        cache.put(TENANT, MODEL_ID, KPI_ID_B, "b")
        other_model = _uid()
        cache.put(TENANT, other_model, KPI_ID_A, "other")

        evicted = cache.invalidate_model(MODEL_ID)
        assert evicted == 2
        assert cache.get(TENANT, MODEL_ID, KPI_ID_A) is None
        assert cache.get(TENANT, MODEL_ID, KPI_ID_B) is None
        # Other model unaffected
        assert cache.get(TENANT, other_model, KPI_ID_A) == "other"

    def test_invalidate_kpi_clears_single(self):
        cache = KpiEvalCache()
        cache.put(TENANT, MODEL_ID, KPI_ID_A, "a")
        cache.put(TENANT, MODEL_ID, KPI_ID_B, "b")

        evicted = cache.invalidate_kpi(KPI_ID_A)
        assert evicted == 1
        assert cache.get(TENANT, MODEL_ID, KPI_ID_A) is None
        assert cache.get(TENANT, MODEL_ID, KPI_ID_B) == "b"

    def test_invalidate_kpis_clears_multiple(self):
        cache = KpiEvalCache()
        kpi_c = _uid()
        cache.put(TENANT, MODEL_ID, KPI_ID_A, "a")
        cache.put(TENANT, MODEL_ID, KPI_ID_B, "b")
        cache.put(TENANT, MODEL_ID, kpi_c, "c")

        evicted = cache.invalidate_kpis([KPI_ID_A, KPI_ID_B])
        assert evicted == 2
        assert cache.get(TENANT, MODEL_ID, KPI_ID_A) is None
        assert cache.get(TENANT, MODEL_ID, KPI_ID_B) is None
        assert cache.get(TENANT, MODEL_ID, kpi_c) == "c"

    def test_invalidate_nonexistent_returns_zero(self):
        cache = KpiEvalCache()
        assert cache.invalidate_kpi(_uid()) == 0
        assert cache.invalidate_model(_uid()) == 0

    def test_clear_removes_all(self):
        cache = KpiEvalCache()
        cache.put(TENANT, MODEL_ID, KPI_ID_A, "a")
        cache.put(TENANT, MODEL_ID, KPI_ID_B, "b")
        cache.clear()
        assert cache.size == 0
        assert cache.get(TENANT, MODEL_ID, KPI_ID_A) is None

    def test_invalidate_model_after_deploy(self):
        """Simulate deploy: cache entries exist, then model is deployed, cache cleared."""
        cache = KpiEvalCache()
        cache.put(TENANT, MODEL_ID, KPI_ID_A, {"value": 100})
        cache.put(TENANT, MODEL_ID, KPI_ID_B, {"value": 200})

        # After deploy, invalidate model
        cache.invalidate_model(MODEL_ID)
        assert cache.get(TENANT, MODEL_ID, KPI_ID_A) is None
        assert cache.get(TENANT, MODEL_ID, KPI_ID_B) is None

    def test_invalidate_kpi_with_multiple_filter_entries(self):
        """A single KPI with different filters should all be invalidated."""
        cache = KpiEvalCache()
        cache.put(TENANT, MODEL_ID, KPI_ID_A, "no-filter")
        cache.put(TENANT, MODEL_ID, KPI_ID_A, "us", filters=[{"dim": "country", "value": "US"}])
        cache.put(TENANT, MODEL_ID, KPI_ID_A, "uk", filters=[{"dim": "country", "value": "UK"}])

        evicted = cache.invalidate_kpi(KPI_ID_A)
        assert evicted == 3
        assert cache.get(TENANT, MODEL_ID, KPI_ID_A) is None
        assert cache.get(TENANT, MODEL_ID, KPI_ID_A, filters=[{"dim": "country", "value": "US"}]) is None


class TestCachePrometheusMetrics:
    def test_hit_increments_counter(self):
        cache = KpiEvalCache()
        cache.put(TENANT, MODEL_ID, KPI_ID_A, "x")

        from src.kpi_cache import KPI_CACHE_HIT, KPI_CACHE_MISS
        hit_before = KPI_CACHE_HIT.labels(tenant_id=TENANT, model_id=str(MODEL_ID))._value.get()
        cache.get(TENANT, MODEL_ID, KPI_ID_A)
        hit_after = KPI_CACHE_HIT.labels(tenant_id=TENANT, model_id=str(MODEL_ID))._value.get()
        assert hit_after == hit_before + 1

    def test_miss_increments_counter(self):
        cache = KpiEvalCache()

        from src.kpi_cache import KPI_CACHE_MISS
        miss_before = KPI_CACHE_MISS.labels(tenant_id=TENANT, model_id=str(MODEL_ID))._value.get()
        cache.get(TENANT, MODEL_ID, _uid())
        miss_after = KPI_CACHE_MISS.labels(tenant_id=TENANT, model_id=str(MODEL_ID))._value.get()
        assert miss_after == miss_before + 1


class TestKpiCacheKeyComponents:
    """F-017-28: the single-KPI evaluate endpoint derives the cache key's
    calc_agg_mode / filters / time_context from the KPI's own definition so the
    documented key contract is satisfied (correct by construction), not merely
    safe-because-empty."""

    def test_components_from_calc_mode_only(self):
        from types import SimpleNamespace
        from src.api.kpis import _kpi_cache_key_components

        kpi = SimpleNamespace(
            calc_agg_mode="per_row_then_aggregate", business_definition=None,
            updated_at=None,
        )
        calc_mode, filters, time_ctx, def_ver = _kpi_cache_key_components(kpi)
        assert calc_mode == "per_row_then_aggregate"
        assert filters is None
        assert time_ctx is None
        assert def_ver is None  # Bug-7242: no updated_at -> None

    def test_components_pull_filters_and_time_window(self):
        from types import SimpleNamespace
        from src.api.kpis import _kpi_cache_key_components

        bd = {
            "filters": [{"dimension_id": "d1", "op": "eq", "value": "EU"}],
            "time_window": {"type": "relative", "grain": "month", "n": 3},
            "_compiled": {"where_clause": "region = 'EU'"},  # volatile, excluded
        }
        kpi = SimpleNamespace(
            calc_agg_mode="automatic", business_definition=bd,
            updated_at=None,
        )
        calc_mode, filters, time_ctx, def_ver = _kpi_cache_key_components(kpi)
        assert calc_mode == "automatic"
        assert filters == bd["filters"]
        assert time_ctx == bd["time_window"]

    def test_two_kpis_differing_only_in_filters_key_distinctly(self):
        """The derived components feed the cache key, so two KPIs that differ
        only in their definitional filters must not share a cache slot even if
        someone passed the same kpi_id (defensive: request-level filters)."""
        from src.kpi_cache import KpiEvalCache

        cache = KpiEvalCache()
        f_eu = [{"dimension_id": "d1", "op": "eq", "value": "EU"}]
        f_us = [{"dimension_id": "d1", "op": "eq", "value": "US"}]
        cache.put(TENANT, MODEL_ID, KPI_ID_A, {"v": "eu"}, filters=f_eu)
        cache.put(TENANT, MODEL_ID, KPI_ID_A, {"v": "us"}, filters=f_us)
        assert cache.get(TENANT, MODEL_ID, KPI_ID_A, filters=f_eu) == {"v": "eu"}
        assert cache.get(TENANT, MODEL_ID, KPI_ID_A, filters=f_us) == {"v": "us"}


class TestTargetDependencyCacheVersions:
    """Cross-replica cache identity for target-only KPI dependency closures."""

    @staticmethod
    def _kpi(kpi_id, name, *, expression="literal(1)", target_expression=None):
        from datetime import datetime, timezone
        from types import SimpleNamespace

        return SimpleNamespace(
            id=kpi_id,
            name=name,
            expression=expression,
            target_expression=target_expression,
            updated_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
        )

    def _target_only_chain(self):
        leaf = self._kpi(_uid(), "Leaf")
        middle = self._kpi(
            _uid(), "Middle", target_expression='kpi("Leaf")',
        )
        consumer = self._kpi(
            _uid(), "Consumer", target_expression='kpi("Middle")',
        )
        return consumer, middle, leaf

    def test_update_to_transitive_target_dependency_misses_warmed_entry(self):
        from src.api.kpis import _kpi_dependency_cache_version

        consumer, middle, leaf = self._target_only_chain()
        before = _kpi_dependency_cache_version(
            consumer, {k.name: k for k in (consumer, middle, leaf)},
            {k.id: k for k in (consumer, middle, leaf)}, "consumer-v1",
        )
        cache = KpiEvalCache()
        cache.put(TENANT, MODEL_ID, consumer.id, "warm", definition_version=before)

        leaf.expression = "literal(2)"
        after = _kpi_dependency_cache_version(
            consumer, {k.name: k for k in (consumer, middle, leaf)},
            {k.id: k for k in (consumer, middle, leaf)}, "consumer-v1",
        )

        assert after != before
        assert cache.get(TENANT, MODEL_ID, consumer.id, definition_version=after) is None

    def test_delete_of_transitive_target_dependency_misses_warmed_entry(self):
        from src.api.kpis import _kpi_dependency_cache_version

        consumer, middle, leaf = self._target_only_chain()
        before = _kpi_dependency_cache_version(
            consumer, {k.name: k for k in (consumer, middle, leaf)},
            {k.id: k for k in (consumer, middle, leaf)}, "consumer-v1",
        )
        cache = KpiEvalCache()
        cache.put(TENANT, MODEL_ID, consumer.id, "warm", definition_version=before)

        after = _kpi_dependency_cache_version(
            consumer, {k.name: k for k in (consumer, middle)},
            {k.id: k for k in (consumer, middle)}, "consumer-v1",
        )

        assert after != before
        assert cache.get(TENANT, MODEL_ID, consumer.id, definition_version=after) is None

    def test_revert_of_target_dependency_misses_warmed_entry(self):
        from src.api.kpis import _kpi_dependency_cache_version

        consumer, middle, leaf = self._target_only_chain()
        before = _kpi_dependency_cache_version(
            consumer, {k.name: k for k in (consumer, middle, leaf)},
            {k.id: k for k in (consumer, middle, leaf)}, "consumer-v2",
        )
        cache = KpiEvalCache()
        cache.put(TENANT, MODEL_ID, consumer.id, "warm", definition_version=before)

        middle.target_expression = 'kpi("Prior Leaf")'
        after = _kpi_dependency_cache_version(
            consumer, {k.name: k for k in (consumer, middle, leaf)},
            {k.id: k for k in (consumer, middle, leaf)}, "consumer-v2",
        )

        assert after != before
        assert cache.get(TENANT, MODEL_ID, consumer.id, definition_version=after) is None

    def test_composite_child_scoring_change_misses_warmed_parent_entry(self):
        from src.api.kpis import _kpi_dependency_cache_version

        parent = self._kpi(_uid(), "Composite", expression="literal(0)")
        parent.kpi_type = "composite"
        child = self._kpi(_uid(), "Composite child", expression="literal(50)")
        child.parent_kpi_id = parent.id
        child.weight = 1.0
        child.direction = "higher_is_better"
        child.target_value = 100.0
        consumer = self._kpi(
            _uid(), "Composite consumer", expression='kpi("Composite")',
        )
        all_kpis = (parent, child, consumer)
        before = _kpi_dependency_cache_version(
            consumer, {k.name: k for k in all_kpis},
            {k.id: k for k in all_kpis}, "composite-v1",
        )
        cache = KpiEvalCache()
        cache.put(TENANT, MODEL_ID, consumer.id, "warm", definition_version=before)

        child.weight = 0.25
        after = _kpi_dependency_cache_version(
            consumer, {k.name: k for k in all_kpis},
            {k.id: k for k in all_kpis}, "composite-v1",
        )

        assert after != before
        assert cache.get(TENANT, MODEL_ID, consumer.id, definition_version=after) is None


class TestCacheCapacity:
    def test_lru_eviction_bounds_entries_and_cleans_secondary_indexes(self):
        cache = KpiEvalCache(max_entries=2)
        cache.put(TENANT, MODEL_ID, KPI_ID_A, "a")
        cache.put(TENANT, MODEL_ID, KPI_ID_B, "b")
        assert cache.get(TENANT, MODEL_ID, KPI_ID_A) == "a"

        third = _uid()
        cache.put(TENANT, MODEL_ID, third, "c")

        assert cache.size == 2
        assert cache.get(TENANT, MODEL_ID, KPI_ID_B) is None
        assert cache.get(TENANT, MODEL_ID, KPI_ID_A) == "a"
        assert cache.get(TENANT, MODEL_ID, third) == "c"
        assert str(KPI_ID_B) not in cache._kpi_keys
        assert all(str(KPI_ID_B) not in owners for owners in cache._key_owners.values())

    def test_cache_capacity_requires_a_positive_limit(self):
        with pytest.raises(ValueError, match="max_entries"):
            KpiEvalCache(max_entries=0)
