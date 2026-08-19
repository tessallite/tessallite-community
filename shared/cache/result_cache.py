"""In-memory query result cache with TTL and model-keyed eviction.

Thread-safe via a plain dict (CPython GIL). Single process only.

Cache key (Bug-7038 / Bug-7762 / Bug-8250):
    (model_id, tenant_id, principal_hash, query_hash, force_route,
     include_hidden, deployed_version_id, rls_policy_hash, row_limit,
     deploy_epoch)

Security-policy mutation invalidation contract (Bug-7762)
---------------------------------------------------------

A stale security cache = data leak. Every security-policy mutation path must
ensure the next query reflects the new policy, not a cached stale result.
The contract is enforced by TWO complementary mechanisms:

1. **Cache-key discrimination (cross-replica correct):**
   The ``rls_policy_hash`` component is compiled from the database on every
   cache-lookup attempt (``compile_row_security``). When a row-security rule
   is created, updated, or deleted, the compiled predicate changes and produces
   a different hash. The old cached entry's key no longer matches, so the query
   misses the cache on ALL replicas -- no inter-replica coordination needed.
   Similarly, ``principal_hash`` captures the user's roles, groups, and claims,
   so a changed user principal (re-login with new IdP claims) misses the cache.

2. **Best-effort eviction (single-replica, immediate):**
   ``model-service`` calls ``DELETE /cache/models/{model_id}`` on the
   query-router after row-security mutations (create/update/delete) and after
   model deploy/undeploy/revert. This clears the calling replica's cache
   immediately. On multi-replica deployments, other replicas rely on mechanism
   (1) -- the changed hash causes a natural miss.

Mutation-type coverage:
  - **Row-security rules (RLS, role_predicate type):** Cache-key hash +
    best-effort eviction. Both mechanisms active. Cross-replica correct via
    hash discrimination.
  - **Row-security rules (RLS, user_mapping type):** Cache BYPASSED entirely.
    Mapping-table rows live on the source database and can be mutated
    out-of-band (no Tessallite API hook, no eviction signal). The
    ``policy_hash`` captures only rule IDs + compiled SQL template, NOT the
    mapping-table contents. A revoked mapping row would not change the hash,
    so caching would serve stale pre-revocation results -- a data leak.
    Bypass is the fail-closed choice, matching the persona pattern.
    Active when ``CompiledPredicate.mapping_source_ids`` is non-empty.
  - **CLS / data tags:** Part of the model snapshot. Changes require a model
    deploy, which triggers cache eviction. The ``deployed_version_id`` in the
    cache key ensures a deploy always misses stale entries.
  - **Same-version redeploy / revert (Bug-8250):** ``deployed_version_id`` is
    unchanged by a revert to the currently-deployed version or by a redeploy of
    the same version, so it cannot discriminate those. ``deploy_epoch`` (bumped
    on every deploy, undeploy, and revert -- Bug-7140) is in the key for exactly
    that case. It matters more here than anywhere else because the cache fast
    path returns BEFORE ``route_query``, so neither the aggregate nor the pocket
    ``built_for`` compatibility gate executes on a hit -- the key is the only
    check the result passes through.
  - **Personas:** Persona-scoped queries bypass the cache entirely
    (``if persona_id: cached = None``). No invalidation needed.
  - **User principal changes:** ``principal_hash`` captures identity, roles,
    groups, and claims. A user re-authenticating with different IdP attributes
    produces a different hash and misses the cache.

The TTL (``QUERY_CACHE_TTL_SECONDS``, default 60s) is the upper bound on
staleness for any mutation not covered by the above mechanisms.

Bug-8507 (bounded retention)
----------------------------

Before this fix ``_store`` was a plain unbounded dict. An entry was only removed
when its exact key was looked up again after expiry, when ``evict_model`` was
called for its model_id, or on ``clear()``. Because the key includes
``query_hash``, ``principal_hash``, and ``deployed_version_id``, a distinct
query/user/deployment combination is never looked up again after it expires, so
its rows stayed resident forever. High-cardinality workloads (many BI users,
many ad-hoc queries, frequent deploys) grew the process heap without bound,
holding whole result sets.

Two retention rules now apply, both enforced on ``set()`` (inserts happen on
every cache miss, so the amortised cost is negligible):

1. **Expired-entry sweep:** all entries past their TTL are removed on every
   insert, not merely skipped on lookup.
2. **Hard entry bound:** when the store exceeds ``max_entries`` after insert,
   nearest-to-expiry entries are evicted until the count is within the bound.
   With a uniform TTL this is also the oldest insert (FIFO), but it remains
   correct if the TTL is ever made per-entry.

The query-router supplies ``Settings.QUERY_CACHE_MAX_ENTRIES`` (default 10 000)
at construction. This is deliberately higher than the join-graph cache (256)
because result-cache keys have far higher cardinality (one per query x user x
deployment) and a lower bound would thrash under normal multi-user workloads.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

CacheKey = tuple[str, ...]

class ResultCache:
    def __init__(
        self,
        ttl_seconds: int = 60,
        max_entries: int = 10_000,
    ) -> None:
        if (
            isinstance(max_entries, bool)
            or not isinstance(max_entries, int)
            or max_entries <= 0
        ):
            raise ValueError("max_entries must be a positive integer")
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._store: dict[CacheKey, tuple[float, Any]] = {}

    @staticmethod
    def make_query_hash(bound_query_dict: dict) -> str:
        canonical = json.dumps(bound_query_dict, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    @staticmethod
    def make_principal_hash(principal_dict: dict) -> str:
        canonical = json.dumps(principal_dict, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    @staticmethod
    def make_cache_key(
        *,
        model_id: Any,
        tenant_id: Any,
        principal_hash: Any,
        query_hash: Any,
        force_route: Any,
        include_hidden: Any,
        deployed_version_id: Any,
        rls_policy_hash: Any,
        row_limit: Any,
        deploy_epoch: Any,
    ) -> CacheKey:
        """Build the cache key from every component the contract above names.

        Bug-8250: the key was assembled inline at the single call site while its
        specification lived in this module's docstring. That is how it came to
        omit ``deploy_epoch`` — the component ``Model.deploy_epoch``'s own
        docstring says the router cache uses — without anything failing. Building
        it here puts the contract and its implementation in one place, so a
        missing component is a test failure rather than a documentation lie.

        ``model_id`` MUST stay first: ``evict_model`` matches on ``key[0]``.

        Every component is stringified so a caller passing a UUID and a caller
        passing its string form cannot mint two keys for the same query.
        """
        return (
            str(model_id),
            str(tenant_id),
            str(principal_hash),
            str(query_hash),
            str(force_route or ""),
            str(include_hidden),
            str(deployed_version_id or ""),
            str(rls_policy_hash or ""),
            "" if row_limit is None else str(row_limit),
            "" if deploy_epoch is None else str(deploy_epoch),
        )

    def get(self, key: CacheKey) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, data = entry
        if time.monotonic() > expires_at:
            del self._store[key]
            return None
        return data

    def _sweep_expired(self, now: float) -> None:
        """Retention rule 1 -- drop every entry past its TTL, not just the one
        being looked up.  A key that is never looked up again (different user,
        different query, different deployment) would otherwise stay resident
        forever (Bug-8507)."""
        expired = [k for k, (expires_at, _) in self._store.items()
                   if expires_at <= now]
        for k in expired:
            del self._store[k]

    def _enforce_size_bound(self) -> None:
        """Retention rule 2 -- hard cap on resident entries.

        Evicts nearest-to-expiry first.  With a uniform TTL that is also the
        oldest insert, so this degrades to FIFO under normal operation while
        still doing the right thing if the TTL is ever made per-entry.
        """
        while len(self._store) > self._max_entries:
            oldest = min(
                self._store, key=lambda k: self._store[k][0],
            )
            del self._store[oldest]

    def set(self, key: CacheKey, data: Any) -> None:
        if self._ttl <= 0:
            return  # TTL=0 means caching disabled
        now = time.monotonic()
        self._sweep_expired(now)
        self._store[key] = (now + self._ttl, data)
        self._enforce_size_bound()

    def delete(self, key: CacheKey) -> None:
        self._store.pop(key, None)

    def evict_model(self, model_id: str) -> None:
        stale = [k for k in self._store if k[0] == model_id]
        for k in stale:
            del self._store[k]

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)
