"""Agent conversation retention — shared cleanup + hard-purge logic.

F-023-27: the retention cleanup used to live only in the agent-service
maintenance endpoint, so it ran only when an admin called it by hand —
``maintenance.py`` claimed it was "safe to wire into a cron job" but no
scheduler job did. Soft-deleted conversations were also never hard-purged
automatically, so the data sat forever after the soft-delete window.

This module holds the single source of truth for both operations so the
endpoint and the scheduler sweep share one implementation (no divergent
copies — the F-012-23 lesson):

  - :func:`soft_delete_inactive_conversations` — marks conversations whose
    ``last_active_at`` is older than the project's retention window with a
    ``deleted_at`` timestamp (recoverable).
  - :func:`hard_purge_soft_deleted` — permanently deletes conversations
    (and their turns) that have been soft-deleted longer than a grace
    window, reclaiming storage.
  - :func:`sweep_tenant_retention` — applies both to every project that has
    a :class:`ProjectAgentConfig` in a single tenant DB. Called by the
    scheduler's daily sweep.

All operations are idempotent and scoped to one tenant DB session.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import delete as sa_delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import AgentConversation, AgentTurn, ProjectAgentConfig

logger = logging.getLogger(__name__)

# Mirrors the maintenance endpoint default — used only when a project has no
# ProjectAgentConfig row (the scheduler sweep iterates configs, so this is the
# endpoint's fallback path).
DEFAULT_RETENTION_DAYS = 30


async def soft_delete_inactive_conversations(
    db: AsyncSession,
    project_id: UUID,
    retention_days: int,
) -> int:
    """Soft-delete conversations inactive beyond the retention window.

    Marks ``deleted_at`` on conversations whose ``last_active_at`` predates
    the cutoff and that are not already soft-deleted. Returns the count
    soft-deleted. Does not commit — the caller owns the transaction.
    """
    if retention_days <= 0:
        # Guarded at config-write time; reaching here means an out-of-band
        # value. Skip rather than soft-delete everything.
        logger.warning(
            "Skipping soft-delete for project %s: retention_days=%s (must be > 0)",
            project_id, retention_days,
        )
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    result = await db.execute(
        select(AgentConversation.id).where(
            AgentConversation.deleted_at.is_(None),
            AgentConversation.project_id == project_id,
            AgentConversation.last_active_at < cutoff,
        )
    )
    ids = [row[0] for row in result.all()]
    if not ids:
        return 0

    await db.execute(
        update(AgentConversation)
        .where(AgentConversation.id.in_(ids))
        .values(deleted_at=datetime.now(timezone.utc))
    )
    return len(ids)


async def hard_purge_soft_deleted(
    db: AsyncSession,
    project_id: UUID,
    grace_days: int,
) -> int:
    """Permanently delete conversations soft-deleted beyond the grace window.

    Deletes conversations (and their turns) whose ``deleted_at`` predates the
    grace cutoff. Returns the count hard-deleted. Does not commit.

    ``grace_days <= 0`` disables automatic hard-purge (soft-deleted rows are
    retained indefinitely for manual recovery / manual purge).
    """
    if grace_days <= 0:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=grace_days)
    result = await db.execute(
        select(AgentConversation.id).where(
            AgentConversation.project_id == project_id,
            AgentConversation.deleted_at.is_not(None),
            AgentConversation.deleted_at < cutoff,
        )
    )
    ids = [row[0] for row in result.all()]
    if not ids:
        return 0

    await db.execute(
        sa_delete(AgentTurn).where(AgentTurn.conversation_id.in_(ids))
    )
    await db.execute(
        sa_delete(AgentConversation).where(AgentConversation.id.in_(ids))
    )
    return len(ids)


async def sweep_tenant_retention(
    db: AsyncSession,
    purge_grace_days: int,
    tenant_slug: str = "",
) -> tuple[int, int]:
    """Apply retention to every agent-configured project in one tenant DB.

    For each :class:`ProjectAgentConfig` row: soft-delete inactive
    conversations using that project's ``conversation_retention_days``, then
    hard-purge conversations soft-deleted beyond ``purge_grace_days``.

    Commits once at the end. Returns ``(total_soft_deleted, total_purged)``.
    """
    configs = (
        await db.execute(
            select(
                ProjectAgentConfig.project_id,
                ProjectAgentConfig.conversation_retention_days,
            )
        )
    ).all()

    total_soft = 0
    total_purged = 0
    for project_id, retention_days in configs:
        days = retention_days if retention_days is not None else DEFAULT_RETENTION_DAYS
        try:
            # SAVEPOINT per project (Bug-2861): mirror the aggregate refresh
            # sweep discipline (sweep.py begin_nested usage). Without a
            # savepoint, a real DB error in one project's UPDATE/DELETE turns
            # the async session rollback-only — every later project's
            # db.execute(select(...)) then raises PendingRollbackError and the
            # closing commit() fails, silently dropping the rest of the
            # tenant's sweep. Wrapping each project in begin_nested() and
            # rolling the savepoint back on error isolates the failure to that
            # one project and keeps the session usable for the rest.
            async with db.begin_nested():
                soft = await soft_delete_inactive_conversations(db, project_id, days)
                purged = await hard_purge_soft_deleted(db, project_id, purge_grace_days)
            total_soft += soft
            total_purged += purged
            if soft or purged:
                logger.info(
                    "Agent retention tenant=%s project=%s retention_days=%s "
                    "soft_deleted=%s hard_purged=%s grace_days=%s",
                    tenant_slug, project_id, days, soft, purged, purge_grace_days,
                )
        except Exception as exc:  # noqa: BLE001 — never starve other projects
            # The savepoint auto-rolls back on the way out of begin_nested(),
            # so the outer transaction stays usable for the next project.
            logger.error(
                "Agent retention failed for tenant=%s project=%s: %s",
                tenant_slug, project_id, exc,
            )

    if total_soft or total_purged:
        await db.commit()

    return total_soft, total_purged
