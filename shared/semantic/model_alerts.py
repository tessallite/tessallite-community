"""Model alerts recorder + lifecycle.

The ``model_alerts`` table is an event stream with database-level
dedup (see migration 0013). This module is the single place every
service goes through to record or resolve an alert so the dedup
contract stays consistent.

Two writers matter:

- **Structural revalidation** (``model_validator.revalidate_model``)
  calls :func:`record_alert` for every currently-invalid object and
  :func:`resolve_alert` for every object that transitioned back to
  valid. The partial-unique index on ``(model_id, category,
  related_object_type, related_object_id) WHERE open`` means repeat
  record_alert calls on the same condition just bump
  ``occurrence_count`` + ``last_seen_at`` rather than creating a
  flood of rows.
- **Runtime events** (refresh failures, optimiser failures, query
  router fallbacks) call :func:`record_alert` with their own
  category. Auto-resolve comes from whatever condition fires later
  (e.g. a successful refresh resolves the previous refresh-failure
  alert by category+object).

Readers are the Model Health tab via the model-service's
``/alerts`` endpoint (Phase 2 API) and any future notification
consumers. All readers go through :func:`list_open_alerts`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import ModelAlert


logger = logging.getLogger(__name__)


# Recognised severities. Kept here so every call-site uses the same
# vocabulary — add new levels only at the top to keep comparisons
# consistent.
SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_ERROR = "error"
SEVERITY_CRITICAL = "critical"
_ALL_SEVERITIES = (SEVERITY_INFO, SEVERITY_WARNING, SEVERITY_ERROR, SEVERITY_CRITICAL)

# Recognised categories. Kept as string constants rather than an Enum
# so new categories can be introduced via code change only (no
# migration).
CATEGORY_INVALID_DIMENSION = "invalid_dimension"
CATEGORY_INVALID_MEASURE = "invalid_measure"
CATEGORY_INVALID_AGGREGATE = "invalid_aggregate"
CATEGORY_REFRESH_FAILURE = "refresh_failure"
CATEGORY_OPTIMISER_FAILURE = "optimiser_failure"
CATEGORY_QUERY_FALLBACK = "query_fallback"
CATEGORY_SCHEMA_DRIFT = "schema_drift"

OBJECT_DIMENSION = "dimension"
OBJECT_MEASURE = "measure"
OBJECT_AGGREGATE = "aggregate"
OBJECT_MODEL = "model"


async def record_alert(
    db: AsyncSession,
    *,
    model_id: UUID,
    severity: str,
    category: str,
    title: str,
    detail: Optional[str] = None,
    related_object_type: Optional[str] = None,
    related_object_id: Optional[UUID] = None,
) -> ModelAlert:
    """Insert or bump an open alert for the given dedup key.

    If an open alert exists for the same ``(model_id, category,
    related_object_type, related_object_id)`` tuple, increment its
    ``occurrence_count`` + refresh ``last_seen_at`` and return it.
    Otherwise create a new row.

    The dedup key uses IS NOT DISTINCT FROM semantics so a NULL
    ``related_object_id`` matches another NULL — model-wide alerts
    (e.g. "model has no tables") dedup correctly.

    Caller must ``db.commit()``; this function only flushes.
    """
    if severity not in _ALL_SEVERITIES:
        raise ValueError(f"Unknown alert severity: {severity!r}")

    now = datetime.now(timezone.utc)

    stmt = (
        select(ModelAlert)
        .where(ModelAlert.model_id == model_id)
        .where(ModelAlert.category == category)
        .where(ModelAlert.resolved_at.is_(None))
        .where(ModelAlert.dismissed_at.is_(None))
    )
    if related_object_type is None:
        stmt = stmt.where(ModelAlert.related_object_type.is_(None))
    else:
        stmt = stmt.where(ModelAlert.related_object_type == related_object_type)
    if related_object_id is None:
        stmt = stmt.where(ModelAlert.related_object_id.is_(None))
    else:
        stmt = stmt.where(ModelAlert.related_object_id == related_object_id)

    existing = (await db.execute(stmt.limit(1))).scalar_one_or_none()

    if existing is not None:
        _apply_update(existing, severity, title, detail, now)
        await db.flush()
        return existing

    # Insert in a nested transaction (SAVEPOINT) so a concurrent
    # writer racing the same dedup key can't kill the caller's
    # outer transaction. If the partial-unique index rejects our
    # INSERT, we catch the IntegrityError, roll back just the
    # savepoint, re-SELECT to find the winner's row, and apply the
    # same bump-count update as the hit path.
    alert = ModelAlert(
        model_id=model_id,
        severity=severity,
        category=category,
        title=title,
        detail=detail,
        related_object_type=related_object_type,
        related_object_id=related_object_id,
        first_seen_at=now,
        last_seen_at=now,
        occurrence_count=1,
    )
    try:
        async with db.begin_nested():
            db.add(alert)
            await db.flush()
        return alert
    except IntegrityError:
        # Clean the failed row out of the session's identity map so
        # the re-SELECT below doesn't hit a stale pending-insert.
        try:
            db.expunge(alert)
        except Exception:
            pass
        logger.info(
            "record_alert raced on (%s, %s, %s) — retrying as update",
            model_id,
            category,
            related_object_id,
        )
        existing = (await db.execute(stmt.limit(1))).scalar_one_or_none()
        if existing is None:
            # Nothing to update after rollback — the concurrent writer
            # may have already resolved the alert between our retries.
            # Re-raise so the caller sees the unexpected state.
            raise
        _apply_update(existing, severity, title, detail, now)
        await db.flush()
        return existing


def _apply_update(
    existing: ModelAlert,
    severity: str,
    title: str,
    detail: Optional[str],
    now: datetime,
) -> None:
    """Shared bump path for both the hit and the race-retry flows."""
    existing.occurrence_count = (existing.occurrence_count or 1) + 1
    existing.last_seen_at = now
    if severity != existing.severity and _severity_rank(severity) > _severity_rank(existing.severity):
        existing.severity = severity
    if title != existing.title:
        existing.title = title
    if detail and detail != existing.detail:
        existing.detail = detail


async def resolve_alert(
    db: AsyncSession,
    *,
    model_id: UUID,
    category: str,
    related_object_type: Optional[str] = None,
    related_object_id: Optional[UUID] = None,
) -> int:
    """Mark any open alert matching the dedup key as resolved.

    Returns the number of alerts closed (0 or 1 under normal
    conditions given the dedup constraint). Caller must ``commit``.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        update(ModelAlert)
        .where(ModelAlert.model_id == model_id)
        .where(ModelAlert.category == category)
        .where(ModelAlert.resolved_at.is_(None))
        .where(ModelAlert.dismissed_at.is_(None))
        .values(resolved_at=now)
    )
    if related_object_type is None:
        stmt = stmt.where(ModelAlert.related_object_type.is_(None))
    else:
        stmt = stmt.where(ModelAlert.related_object_type == related_object_type)
    if related_object_id is None:
        stmt = stmt.where(ModelAlert.related_object_id.is_(None))
    else:
        stmt = stmt.where(ModelAlert.related_object_id == related_object_id)

    result = await db.execute(stmt)
    await db.flush()
    return result.rowcount or 0


async def dismiss_alert(
    db: AsyncSession,
    *,
    alert_id: UUID,
    dismissed_by: Optional[UUID] = None,
) -> Optional[ModelAlert]:
    """Manually dismiss an alert by id. Idempotent: a second dismiss
    call is a no-op. Returns the alert row or None if not found."""
    alert = await db.get(ModelAlert, alert_id)
    if alert is None:
        return None
    if alert.dismissed_at is not None:
        return alert
    alert.dismissed_at = datetime.now(timezone.utc)
    alert.dismissed_by = dismissed_by
    await db.flush()
    return alert


async def list_open_alerts(
    db: AsyncSession,
    *,
    model_id: UUID,
    severity: Optional[str] = None,
    category: Optional[str] = None,
    include_resolved: bool = False,
    include_dismissed: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[ModelAlert]:
    """Return alerts for a model, newest first, paginated.

    Defaults to open alerts only; pass ``include_resolved`` or
    ``include_dismissed`` to widen the result set (used by the Model
    Health tab's history view).
    """
    stmt = select(ModelAlert).where(ModelAlert.model_id == model_id)
    if not include_resolved:
        stmt = stmt.where(ModelAlert.resolved_at.is_(None))
    if not include_dismissed:
        stmt = stmt.where(ModelAlert.dismissed_at.is_(None))
    if severity is not None:
        stmt = stmt.where(ModelAlert.severity == severity)
    if category is not None:
        stmt = stmt.where(ModelAlert.category == category)
    stmt = stmt.order_by(ModelAlert.last_seen_at.desc()).limit(limit).offset(offset)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def count_open_alerts(
    db: AsyncSession,
    *,
    model_id: UUID,
) -> int:
    stmt = (
        select(func.count())
        .select_from(ModelAlert)
        .where(ModelAlert.model_id == model_id)
        .where(ModelAlert.resolved_at.is_(None))
        .where(ModelAlert.dismissed_at.is_(None))
    )
    return int((await db.execute(stmt)).scalar_one() or 0)


async def count_filtered_alerts(
    db: AsyncSession,
    *,
    model_id: UUID,
    severity: Optional[str] = None,
    category: Optional[str] = None,
    include_resolved: bool = False,
    include_dismissed: bool = False,
) -> int:
    """Count alerts under the SAME filter predicate as ``list_open_alerts``.

    Used to give the Model Health alerts table a real total so pagination
    cannot run past the end (F-030-23).
    """
    stmt = (
        select(func.count())
        .select_from(ModelAlert)
        .where(ModelAlert.model_id == model_id)
    )
    if not include_resolved:
        stmt = stmt.where(ModelAlert.resolved_at.is_(None))
    if not include_dismissed:
        stmt = stmt.where(ModelAlert.dismissed_at.is_(None))
    if severity is not None:
        stmt = stmt.where(ModelAlert.severity == severity)
    if category is not None:
        stmt = stmt.where(ModelAlert.category == category)
    return int((await db.execute(stmt)).scalar_one() or 0)


def _severity_rank(severity: str) -> int:
    try:
        return _ALL_SEVERITIES.index(severity)
    except ValueError:
        return 0
