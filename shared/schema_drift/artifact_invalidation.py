"""Drift-driven materialisation invalidation (F-012-01).

When schema-drift remediation invalidates a dimension or measure because its
backing source column was removed or its type changed incompatibly, every
physical materialisation that DEPENDS on that dimension/measure must also be made
non-routable in the SAME transaction. Otherwise the matcher keeps serving old
aggregate/pocket rows as if they were current — silent wrong-number exposure.

The matcher already fails closed on ``AggregateDefinition.is_stale`` /
``invalid_reason`` (Bug-6984) and on ``PocketDefinition.status`` (only ``fresh``
pockets are candidates). This module is the missing PRODUCER: it trips those
canonical non-routable flags. It never physically drops anything — a stale
aggregate is re-materialised by the refresh sweep; a stale pocket is re-filled by
the pocket refresh sweep. Fail-closed and self-correcting.

Dependency resolution (no SQL re-parse; uses the durable metadata links):
  * Aggregate depends on an invalidated MEASURE when an ``AggregateColumn`` of the
    aggregate references that measure (``AggregateColumn.measure_id``).
  * Aggregate depends on an invalidated DIMENSION when the dimension's logical
    ``name`` appears in the aggregate's ``grain`` list (the grain stores logical
    dimension names — the same identity the router binds against).
  * Pocket depends on ANY breaking source-schema change to its model: a pocket
    caches arbitrary source SQL whose rows or refresh can be invalidated by a
    removed/retyped column, and pockets carry no per-column FK to resolve
    precisely. Marking every currently-servable (``fresh``) pocket of the model
    stale is the conservative fail-closed action — the refresh sweep re-fills the
    ones whose SQL still resolves, and drops the rest via its own guards.

Status: active. Last update 2026-07-21 (F-012-01).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    AggregateColumn,
    AggregateDefinition,
    PocketDefinition,
)

logger = logging.getLogger(__name__)

# PocketDefinition statuses that represent a currently-servable / in-pool pocket.
# Only these are flipped to ``stale`` — a pocket already ``stale``/``failed``/
# ``invalidating`` is already queued or being handled, and a ``retired`` pocket
# must not be resurrected into the refresh queue.
_SERVABLE_POCKET_STATUSES = ("fresh",)


async def invalidate_dependent_materialisations(
    model_id: object,
    db: AsyncSession,
    *,
    invalidated_measure_ids: set,
    invalidated_dimension_names: set[str],
    has_breaking_event: bool,
    reason: str,
) -> tuple[int, int]:
    """Mark every aggregate/pocket that depends on an invalidated dimension or
    measure non-routable, in the caller's (uncommitted) transaction.

    Args:
        model_id: the model whose materialisations to resolve.
        db: tenant-scoped session (the drift transaction — NOT committed here).
        invalidated_measure_ids: measure ids whose backing column was removed /
            retyped incompatibly this run.
        invalidated_dimension_names: logical dimension names likewise invalidated.
        has_breaking_event: True when at least one breaking (column_removed or
            incompatible type_changed) event was produced. Gates the pocket
            fail-closed sweep so a purely non-breaking (column_added) run does
            not stale every pocket.
        reason: human-readable reason stamped onto ``invalid_reason``.

    Returns:
        ``(aggregates_marked, pockets_marked)``.
    """
    aggregates_marked = await _invalidate_dependent_aggregates(
        model_id, db,
        invalidated_measure_ids=invalidated_measure_ids,
        invalidated_dimension_names=invalidated_dimension_names,
        reason=reason,
    )

    pockets_marked = 0
    if has_breaking_event:
        pockets_marked = await _invalidate_model_pockets(model_id, db, reason=reason)

    return aggregates_marked, pockets_marked


async def _invalidate_dependent_aggregates(
    model_id: object,
    db: AsyncSession,
    *,
    invalidated_measure_ids: set,
    invalidated_dimension_names: set[str],
    reason: str,
) -> int:
    """Set ``is_stale``/``invalid_reason`` on active aggregates that reference an
    invalidated measure (via AggregateColumn) or dimension (via grain)."""
    if not invalidated_measure_ids and not invalidated_dimension_names:
        return 0

    # Only aggregates that could still SERVE need flipping. A retired/disabled
    # aggregate is already out of the pool; a stale one is already refused.
    result = await db.execute(
        select(AggregateDefinition)
        .where(
            AggregateDefinition.model_id == model_id,
            AggregateDefinition.status == "active",
            AggregateDefinition.is_stale.is_(False),
            AggregateDefinition.retired_at.is_(None),
        )
    )
    aggregates = list(result.scalars().all())
    if not aggregates:
        return 0

    # Which aggregate ids reference an invalidated measure?
    measure_dep_agg_ids: set = set()
    if invalidated_measure_ids:
        col_result = await db.execute(
            select(AggregateColumn.aggregate_definition_id)
            .where(
                AggregateColumn.aggregate_definition_id.in_([a.id for a in aggregates]),
                AggregateColumn.measure_id.in_(list(invalidated_measure_ids)),
            )
        )
        measure_dep_agg_ids = {row[0] for row in col_result.fetchall()}

    marked = 0
    for agg in aggregates:
        depends_on_measure = agg.id in measure_dep_agg_ids
        depends_on_dim = bool(
            invalidated_dimension_names
            and set(agg.grain or []) & invalidated_dimension_names
        )
        if not depends_on_measure and not depends_on_dim:
            continue
        agg.is_stale = True
        agg.invalid_reason = reason
        marked += 1
        logger.warning(
            "Schema drift: aggregate %s marked stale (model %s) — %s",
            agg.id, model_id, reason,
        )
    return marked


async def _invalidate_model_pockets(
    model_id: object,
    db: AsyncSession,
    *,
    reason: str,
) -> int:
    """Flip every currently-servable pocket of the model to ``stale`` so the
    matcher stops serving cached rows that a breaking source change may have
    invalidated. The pocket refresh sweep re-materialises the survivors."""
    result = await db.execute(
        update(PocketDefinition)
        .where(
            PocketDefinition.model_id == model_id,
            PocketDefinition.status.in_(_SERVABLE_POCKET_STATUSES),
            PocketDefinition.retired_at.is_(None),
        )
        .values(status="stale", failure_reason=reason, updated_at=datetime.now(timezone.utc))
    )
    count = int(result.rowcount or 0)
    if count:
        logger.warning(
            "Schema drift: marked %d pocket(s) stale for model %s after "
            "breaking source change — %s",
            count, model_id, reason,
        )
    return count
