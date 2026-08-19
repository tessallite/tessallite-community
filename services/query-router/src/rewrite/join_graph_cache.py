"""Join-graph cache for the query rewriter.

Model metadata (tables, joins, columns) is static per deployed model version.
Cache for a short TTL to avoid 3-5 DB round-trips per query.

F-013-01 / Bug-7979: the cache is keyed by
``(model_id, deployed_version_id, deploy_epoch)`` so the graph is pinned to
the deployed version. A deploy/revert changes the version_id/epoch, so the
next query naturally rebuilds from the new snapshot -- no explicit
invalidation is required for correctness. Explicit invalidation via
``invalidate_join_graph_cache`` (wired into the ``DELETE /cache/models``
endpoint) is an optimization that clears the stale entry eagerly.

Runtime-state declaration
-------------------------

* Durable authority: the deployed snapshot. This dict is only a derived cache.
* Scope: one in-process dict per query-router replica.
* Key: ``(model_id, deployed_version_id, deploy_epoch)``. ``deploy_epoch`` is
  monotonically increasing per model (model-service bumps it inside the same
  transaction that moves the deployed pointer), so it is a total order over a
  model's deployments.
* TTL: ``_JOIN_GRAPH_TTL`` seconds (default 60, override with
  ``QUERY_ROUTER_JOIN_GRAPH_TTL_SECONDS``).
* Hard bound: ``_JOIN_GRAPH_MAX_ENTRIES`` (default 256, override with
  ``QUERY_ROUTER_JOIN_GRAPH_MAX_ENTRIES``).
* Invalidation: key change on deploy/revert (correctness), plus the eviction
  endpoint and the retention rules below (memory).

Bug-8503 (bounded retention). Before this fix an entry was only ever removed
when its OWN key was looked up again after expiry, when ``evict_model`` was
called, or on restart. Because the key carries the deployment identity, a
superseded key is BY DEFINITION never looked up again -- so every deploy or
revert stranded a full table/column/join/UDA graph in memory permanently. Three
retention rules now apply, all enforced on insert (inserts only happen on a
cache miss, so the amortised cost is negligible):

1. Expired entries are swept, not merely skipped on lookup.
2. Superseded entries are dropped: inserting ``(model, V2, E2)`` removes every
   entry for the same model with a STRICTLY LOWER epoch.
3. A hard entry bound evicts nearest-to-expiry entries when exceeded, so the
   cache cannot grow without limit even under pathological key churn.

Bug-8251 (distributed invalidation). Correctness across replicas never depended
on broadcast eviction: the epoch key-change makes a stale sibling entry
UNREACHABLE, not merely un-evicted. The residual was operational -- a sibling
replica that never received ``DELETE /cache/models/{id}`` retained the stale
graph. Retention rule 2 closes that at root cause without new infrastructure:
the first post-deploy request on ANY replica inserts the new deployment's key
and, in doing so, evicts that replica's superseded entries for the model. Every
replica therefore self-heals on its first post-deploy query instead of waiting
for a TTL that only reclaims memory on a lookup that never comes. Rule 2 uses a
STRICTLY-LOWER epoch comparison so an in-flight pre-commit request that inserts
its own (older) key afterwards cannot evict the newer entry and start a
refill/evict thrash loop; that older entry is reclaimed by rule 1 or 3.

Extracted from query_rewriter.py (Phase 3 decomposition).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID


def _env_int(name: str, default: int, *, minimum: int) -> int:
    """Read a positive integer tuning value from the environment.

    An unset, non-numeric, or out-of-range value falls back to ``default`` so a
    typo in deployment configuration can never disable the bound entirely.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


_JOIN_GRAPH_TTL = _env_int(
    "QUERY_ROUTER_JOIN_GRAPH_TTL_SECONDS", 60, minimum=1,
)
_JOIN_GRAPH_MAX_ENTRIES = _env_int(
    "QUERY_ROUTER_JOIN_GRAPH_MAX_ENTRIES", 256, minimum=1,
)


@dataclass(slots=True)
class _JoinGraphEntry:
    tables_by_id: dict
    joins: list
    columns_by_id: dict
    uda_by_id: dict
    expires_at: float


# Key: (model_id_str, deployed_version_id_str, deploy_epoch_int)
_join_graph_cache: dict[tuple[str, str, int], _JoinGraphEntry] = {}


def _normalise_key(key: Any) -> tuple[str, str, int]:
    """Coerce a cache key to the ``(model_id, version_id, epoch)`` tuple form.

    A bare model id is legacy compatibility for any remaining caller.
    """
    if not isinstance(key, tuple):
        return (str(key), "None", 0)
    return key


def invalidate_join_graph_cache(model_id: UUID | str | None = None) -> None:
    """Evict cached join graph data. Called on model deploy/undeploy.

    With no argument, clears everything (test hook). With a model_id, evicts
    ALL entries for that model (across versions/epochs).
    """
    if model_id is None:
        _join_graph_cache.clear()
    else:
        mid = str(model_id)
        for key in [k for k in _join_graph_cache if k[0] == mid]:
            _join_graph_cache.pop(key, None)


def _sweep_expired(now: float) -> None:
    """Retention rule 1 — drop every entry past its TTL, not just the one being
    looked up. A superseded key is never looked up again, so lookup-time expiry
    alone leaks it forever (Bug-8503)."""
    for key in [
        k for k, entry in _join_graph_cache.items() if entry.expires_at <= now
    ]:
        _join_graph_cache.pop(key, None)


def _drop_superseded(key: tuple[str, str, int]) -> None:
    """Retention rule 2 — drop this model's entries from earlier deployments.

    ``deploy_epoch`` is monotonically increasing per model, so an entry with a
    strictly lower epoch belongs to a deployment this replica has now observed
    to be superseded. This is the per-replica self-healing that closes the
    Bug-8251 operational gap without a cross-replica invalidation bus. The
    comparison is strict so a late insert from an in-flight pre-commit request
    cannot evict the newer entry (which would cause refill/evict thrash).
    """
    model_id, _version_id, epoch = key
    for other in [
        k for k in _join_graph_cache
        if k[0] == model_id and k != key and k[2] < epoch
    ]:
        _join_graph_cache.pop(other, None)


def _enforce_size_bound() -> None:
    """Retention rule 3 — hard cap on resident entries.

    Evicts nearest-to-expiry first. With a uniform TTL that is also the oldest
    insert, so this degrades to FIFO under normal operation while still doing
    the right thing if the TTL is ever made per-entry.
    """
    while len(_join_graph_cache) > _JOIN_GRAPH_MAX_ENTRIES:
        oldest = min(
            _join_graph_cache, key=lambda k: _join_graph_cache[k].expires_at,
        )
        _join_graph_cache.pop(oldest, None)


def _get_join_graph(key: Any) -> _JoinGraphEntry | None:
    """Return cached join graph if present and not expired.

    ``key`` is ``(model_id_str, version_id_str, epoch)`` or a bare model_id
    (legacy compatibility for any remaining callers — converted to a tuple).
    """
    key = _normalise_key(key)
    entry = _join_graph_cache.get(key)
    if entry is None:
        return None
    if time.monotonic() > entry.expires_at:
        _join_graph_cache.pop(key, None)
        return None
    return entry


def _put_join_graph(
    key: Any, tables_by_id: dict, joins: list,
    columns_by_id: dict, uda_by_id: dict,
) -> None:
    """Store join graph data in the cache.

    ``key`` is ``(model_id_str, version_id_str, epoch)`` or a bare model_id.
    Applies the three retention rules documented in the module docstring.
    """
    key = _normalise_key(key)
    now = time.monotonic()
    _sweep_expired(now)
    _drop_superseded(key)
    _join_graph_cache[key] = _JoinGraphEntry(
        tables_by_id=tables_by_id,
        joins=joins,
        columns_by_id=dict(columns_by_id),
        uda_by_id=dict(uda_by_id),
        expires_at=now + _JOIN_GRAPH_TTL,
    )
    _enforce_size_bound()
