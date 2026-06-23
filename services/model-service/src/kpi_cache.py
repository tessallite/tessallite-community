"""TTL-based in-process cache for KPI evaluation results.

Spec: Section 16.1
- 300-second TTL per entry
- Cache key: (tenant_id, model_id, kpi_id, calc_agg_mode, filters_hash, time_context_hash)
- Invalidation: model publish/undeploy (evict all model entries), KPI update/delete (evict KPI + dependents)
- Ad-hoc evaluation bypasses the cache
- Prometheus counters: kpi_eval_cache_hit_total, kpi_eval_cache_miss_total

Limitation: this is an in-process cache. In multi-instance deployments
(e.g. Cloud Run auto-scaling, gunicorn multi-worker), each instance
maintains its own cache. Invalidation events only clear the local
instance; other instances rely on TTL expiry for staleness bounds.
For production multi-instance deployments, consider Redis-backed
caching with pub/sub invalidation.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from prometheus_client import Counter

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

KPI_CACHE_HIT = Counter(
    "tessallite_kpi_eval_cache_hit_total",
    "KPI evaluation cache hits",
    ["tenant_id", "model_id"],
)

KPI_CACHE_MISS = Counter(
    "tessallite_kpi_eval_cache_miss_total",
    "KPI evaluation cache misses",
    ["tenant_id", "model_id"],
)

# ---------------------------------------------------------------------------
# Cache entry
# ---------------------------------------------------------------------------

DEFAULT_TTL_SECONDS = 300


@dataclass(slots=True)
class _CacheEntry:
    value: Any
    expires_at: float


# ---------------------------------------------------------------------------
# Cache implementation
# ---------------------------------------------------------------------------


def _hash_filters(filters: list[dict] | None) -> str:
    """Produce a deterministic hash of the filters list."""
    if not filters:
        return ""
    canonical = json.dumps(filters, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _hash_time_context(time_context: dict | None) -> str:
    """Produce a deterministic hash of the time context dict."""
    if not time_context:
        return ""
    canonical = json.dumps(time_context, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _make_key(
    tenant_id: str,
    model_id: UUID,
    kpi_id: UUID,
    calc_agg_mode: str | None,
    filters: list[dict] | None,
    time_context: dict | None,
    user_id: str | None = None,
    persona_id: str | None = None,
) -> str:
    """Build a cache key from the evaluation parameters.

    ``user_id`` is included because KPI SQL executes through query-router
    with the caller's bearer token, which carries row-security policies.
    Different users may receive different numeric results for the same KPI.

    ``persona_id`` is included because query-router applies persona-specific
    default filters, row-security bypass rules, and column restrictions that
    affect numeric results. The same user under different personas can get
    different values.
    """
    return (
        f"{tenant_id}:{model_id}:{kpi_id}:"
        f"{user_id or '_'}:{persona_id or '_'}:"
        f"{calc_agg_mode or 'auto'}:"
        f"{_hash_filters(filters)}:"
        f"{_hash_time_context(time_context)}"
    )


class KpiEvalCache:
    """In-process TTL cache for KPI evaluation results.

    Thread-safe for single-threaded async use (GIL-protected dict ops).
    Each model-service replica maintains its own cache instance.
    """

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, _CacheEntry] = {}
        # Secondary index: model_id -> set of cache keys
        self._model_keys: dict[str, set[str]] = {}
        # Secondary index: kpi_id -> set of cache keys
        self._kpi_keys: dict[str, set[str]] = {}
        # Reverse index: cache key -> (model_id_str, kpi_id_str) for O(1) removal
        self._key_owners: dict[str, tuple[str, str]] = {}

    def get(
        self,
        tenant_id: str,
        model_id: UUID,
        kpi_id: UUID,
        calc_agg_mode: str | None = None,
        filters: list[dict] | None = None,
        time_context: dict | None = None,
        user_id: str | None = None,
        persona_id: str | None = None,
    ) -> Any | None:
        """Return cached result or None if not cached / expired."""
        key = _make_key(tenant_id, model_id, kpi_id, calc_agg_mode, filters, time_context, user_id, persona_id)
        entry = self._store.get(key)
        if entry is None:
            KPI_CACHE_MISS.labels(tenant_id=tenant_id, model_id=str(model_id)).inc()
            return None
        if time.monotonic() > entry.expires_at:
            # Expired — remove and report miss
            self._remove_key(key)
            KPI_CACHE_MISS.labels(tenant_id=tenant_id, model_id=str(model_id)).inc()
            return None
        KPI_CACHE_HIT.labels(tenant_id=tenant_id, model_id=str(model_id)).inc()
        return entry.value

    def put(
        self,
        tenant_id: str,
        model_id: UUID,
        kpi_id: UUID,
        value: Any,
        calc_agg_mode: str | None = None,
        filters: list[dict] | None = None,
        time_context: dict | None = None,
        user_id: str | None = None,
        persona_id: str | None = None,
    ) -> None:
        """Store a result in the cache."""
        key = _make_key(tenant_id, model_id, kpi_id, calc_agg_mode, filters, time_context, user_id, persona_id)
        self._store[key] = _CacheEntry(
            value=value,
            expires_at=time.monotonic() + self._ttl,
        )
        # Update secondary indexes
        mid = str(model_id)
        kid = str(kpi_id)
        self._model_keys.setdefault(mid, set()).add(key)
        self._kpi_keys.setdefault(kid, set()).add(key)
        self._key_owners[key] = (mid, kid)

    def invalidate_model(self, model_id: UUID) -> int:
        """Evict all cached entries for a model. Returns count evicted."""
        mid = str(model_id)
        keys = self._model_keys.pop(mid, set())
        count = 0
        for key in keys:
            if key in self._store:
                del self._store[key]
                count += 1
            # Clean kpi_keys via reverse index (O(1) per key)
            owners = self._key_owners.pop(key, None)
            if owners:
                _, kid = owners
                kid_keys = self._kpi_keys.get(kid)
                if kid_keys is not None:
                    kid_keys.discard(key)
        if count > 0:
            log.debug("Evicted %d KPI cache entries for model %s", count, mid)
        return count

    def invalidate_kpi(self, kpi_id: UUID) -> int:
        """Evict cached entries for a single KPI. Returns count evicted."""
        kid = str(kpi_id)
        keys = self._kpi_keys.pop(kid, set())
        count = 0
        for key in keys:
            if key in self._store:
                del self._store[key]
                count += 1
            # Clean model_keys via reverse index (O(1) per key)
            owners = self._key_owners.pop(key, None)
            if owners:
                mid, _ = owners
                mid_keys = self._model_keys.get(mid)
                if mid_keys is not None:
                    mid_keys.discard(key)
        if count > 0:
            log.debug("Evicted %d KPI cache entries for KPI %s", count, kid)
        return count

    def invalidate_kpis(self, kpi_ids: list[UUID]) -> int:
        """Evict cached entries for multiple KPIs. Returns total count evicted."""
        total = 0
        for kpi_id in kpi_ids:
            total += self.invalidate_kpi(kpi_id)
        return total

    def clear(self) -> None:
        """Remove all entries."""
        self._store.clear()
        self._model_keys.clear()
        self._kpi_keys.clear()
        self._key_owners.clear()

    @property
    def size(self) -> int:
        """Current number of entries (including potentially expired)."""
        return len(self._store)

    def _remove_key(self, key: str) -> None:
        """Remove a single key from store and indexes (O(1) via reverse index)."""
        self._store.pop(key, None)
        owners = self._key_owners.pop(key, None)
        if owners:
            mid, kid = owners
            mid_keys = self._model_keys.get(mid)
            if mid_keys is not None:
                mid_keys.discard(key)
            kid_keys = self._kpi_keys.get(kid)
            if kid_keys is not None:
                kid_keys.discard(key)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_cache = KpiEvalCache()


def get_kpi_cache() -> KpiEvalCache:
    """Return the module-level KPI evaluation cache."""
    return _cache
