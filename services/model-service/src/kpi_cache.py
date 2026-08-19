"""Bounded TTL-based in-process cache for KPI evaluation results.

Spec: Section 16.1
- 300-second TTL per entry and a bounded LRU entry count
- Cache key: (tenant_id, model_id, kpi_id, calc_agg_mode, filters_hash, time_context_hash)
- Invalidation: model publish/undeploy (evict all model entries), KPI update/delete (evict KPI + dependents)
- Ad-hoc evaluation bypasses the cache
- Models with enabled row-security rules bypass this outer cache; the
  query-router remains authoritative for principal and policy identity
- Prometheus counters: kpi_eval_cache_hit_total, kpi_eval_cache_miss_total

Limitation: this is an in-process cache. In multi-instance deployments
(e.g. Cloud Run auto-scaling, gunicorn multi-worker), each instance
maintains its own cache and local invalidation only clears that instance.
KPI definition changes still miss on another instance after its next model-row
read because the endpoint's definition version includes the relevant draft
dependency closure. For other invalidation-only uses, a shared cache with
pub/sub invalidation remains the production option.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
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
DEFAULT_MAX_ENTRIES = 10_000


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
    definition_version: str | None = None,
    data_epoch: int | None = None,
) -> str:
    """Build a cache key from the evaluation parameters.

    ``user_id`` is included because KPI SQL executes through query-router
    with the caller's bearer token, which carries row-security policies.
    Different users may receive different numeric results for the same KPI.

    ``persona_id`` is included because query-router applies persona-specific
    default filters, row-security bypass rules, and column restrictions that
    affect numeric results. The same user under different personas can get
    different values.

    This key is used only for models without enabled row-security rules.
    Roles, groups, claims, mutable rule definitions, and user-mapping rows all
    affect RLS, so the endpoint layer bypasses this outer cache whenever RLS may
    apply instead of duplicating the query-router's policy compiler here.

    Bug-7242: ``definition_version`` makes the key unique per definition
    revision. For a DEPLOYED KPI this is the deployed ``(version_id, epoch)``
    (F-017-01) so the cached value is pinned to the deployed definition and
    only changes on redeploy. For an undeployed target-dependent draft, the
    endpoint supplies a deterministic fingerprint of its value/target
    dependency closure as part of this version. Either way a stale replica
    whose DB read returns the new definition naturally misses the old entry,
    bounding cross-replica staleness to the DB read, not the TTL.

    F-017-03 (Bug-7989): ``data_epoch`` is the model's monotonic
    data-freshness counter (``Model.data_epoch``), bumped on every successful
    aggregate/pocket/source data refresh. Folding it into the key means the
    next evaluation after a refresh reads the new epoch, forms a new key, and
    misses the pre-refresh entry — on EVERY replica, without a cross-process
    event bus (same cross-replica bound as ``definition_version``). A KPI
    scorecard is therefore never stale past the DB read after a refresh.
    """
    return (
        f"{tenant_id}:{model_id}:{kpi_id}:"
        f"{user_id or '_'}:{persona_id or '_'}:"
        f"{calc_agg_mode or 'auto'}:"
        f"{_hash_filters(filters)}:"
        f"{_hash_time_context(time_context)}:"
        f"{definition_version or '_'}:"
        f"{data_epoch if data_epoch is not None else '_'}"
    )


class KpiEvalCache:
    """In-process TTL cache for KPI evaluation results.

    Thread-safe for single-threaded async use (GIL-protected dict ops).
    Each model-service replica maintains its own cache instance.
    """

    def __init__(
        self,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._store: dict[str, _CacheEntry] = {}
        # Secondary index: model_id -> set of cache keys
        self._model_keys: dict[str, set[str]] = {}
        # Secondary index: kpi_id -> set of cache keys
        self._kpi_keys: dict[str, set[str]] = {}
        # Reverse index: cache key -> (model_id_str, kpi_id_str) for O(1) removal
        self._key_owners: dict[str, tuple[str, str]] = {}
        # Access order supports bounded memory without leaving stale secondary
        # index entries behind when the least-recently-used entry is evicted.
        self._recency: OrderedDict[str, None] = OrderedDict()

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
        definition_version: str | None = None,
        data_epoch: int | None = None,
    ) -> Any | None:
        """Return cached result or None if not cached / expired."""
        key = _make_key(tenant_id, model_id, kpi_id, calc_agg_mode, filters, time_context, user_id, persona_id, definition_version=definition_version, data_epoch=data_epoch)
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
        self._recency.move_to_end(key)
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
        definition_version: str | None = None,
        data_epoch: int | None = None,
    ) -> None:
        """Store a result in the cache."""
        key = _make_key(tenant_id, model_id, kpi_id, calc_agg_mode, filters, time_context, user_id, persona_id, definition_version=definition_version, data_epoch=data_epoch)
        # Replacing an existing entry must not leave its old index membership
        # behind. Then evict LRU entries until this write is within the bound.
        self._remove_key(key)
        while len(self._store) >= self._max_entries:
            oldest_key, _ = self._recency.popitem(last=False)
            self._remove_key(oldest_key)
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
        self._recency[key] = None

    def invalidate_model(self, model_id: UUID) -> int:
        """Evict all cached entries for a model. Returns count evicted."""
        mid = str(model_id)
        keys = self._model_keys.pop(mid, set())
        count = 0
        for key in keys:
            if key in self._store:
                count += 1
            self._remove_key(key)
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
                count += 1
            self._remove_key(key)
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
        self._recency.clear()

    @property
    def size(self) -> int:
        """Current number of entries (including potentially expired)."""
        return len(self._store)

    def _remove_key(self, key: str) -> None:
        """Remove a single key from store and indexes (O(1) via reverse index)."""
        self._store.pop(key, None)
        self._recency.pop(key, None)
        owners = self._key_owners.pop(key, None)
        if owners:
            mid, kid = owners
            mid_keys = self._model_keys.get(mid)
            if mid_keys is not None:
                mid_keys.discard(key)
                if not mid_keys:
                    self._model_keys.pop(mid, None)
            kid_keys = self._kpi_keys.get(kid)
            if kid_keys is not None:
                kid_keys.discard(key)
                if not kid_keys:
                    self._kpi_keys.pop(kid, None)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_cache = KpiEvalCache()


def get_kpi_cache() -> KpiEvalCache:
    """Return the module-level KPI evaluation cache."""
    return _cache
