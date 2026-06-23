"""Join-graph cache for the query rewriter.

Model metadata (tables, joins, columns) is static per deployed model version.
Cache for a short TTL to avoid 3-5 DB round-trips per query. Invalidated on
model deploy/undeploy via ``invalidate_join_graph_cache``, which is wired into
the ``DELETE /cache/models/{model_id}`` endpoint (``api/routes.py``) that
model-service calls after a deploy. The cache is an in-process dict, so the
invalidation clears the calling replica only; on a multi-replica deployment
(Cloud Run) the ``_JOIN_GRAPH_TTL`` window is the cross-replica staleness bound
for sibling replicas that did not receive the eviction call.

Extracted from query_rewriter.py (Phase 3 decomposition); behaviour-identical.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import UUID


_JOIN_GRAPH_TTL = 60  # seconds


@dataclass(slots=True)
class _JoinGraphEntry:
    tables_by_id: dict
    joins: list
    columns_by_id: dict
    uda_by_id: dict
    expires_at: float


_join_graph_cache: dict[str, _JoinGraphEntry] = {}


def invalidate_join_graph_cache(model_id: UUID | str | None = None) -> None:
    """Evict cached join graph data. Called on model deploy/undeploy."""
    if model_id is None:
        _join_graph_cache.clear()
    else:
        _join_graph_cache.pop(str(model_id), None)


def _get_join_graph(model_id: UUID) -> _JoinGraphEntry | None:
    """Return cached join graph if present and not expired."""
    entry = _join_graph_cache.get(str(model_id))
    if entry is None:
        return None
    if time.monotonic() > entry.expires_at:
        _join_graph_cache.pop(str(model_id), None)
        return None
    return entry


def _put_join_graph(
    model_id: UUID, tables_by_id: dict, joins: list,
    columns_by_id: dict, uda_by_id: dict,
) -> None:
    """Store join graph data in the cache."""
    _join_graph_cache[str(model_id)] = _JoinGraphEntry(
        tables_by_id=tables_by_id,
        joins=joins,
        columns_by_id=dict(columns_by_id),
        uda_by_id=dict(uda_by_id),
        expires_at=time.monotonic() + _JOIN_GRAPH_TTL,
    )
