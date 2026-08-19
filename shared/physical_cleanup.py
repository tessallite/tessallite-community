"""Durable post-commit cleanup for deleted aggregate and pocket tables.

Bug-8140 requires an outbox because target DDL cannot participate in the tenant
metadata transaction.  Deletion schedules detached rows while model/target/
connection metadata still exists; API callers attempt them only after commit,
and the scheduler retries any pending/failed rows.  ``DROP TABLE IF EXISTS`` is
idempotent, and every target statement stays behind ``execute_source_ddl`` plus
``quote_table_ref``.
"""
from __future__ import annotations

import logging
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Iterable
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.connector_qualify import quote_table_ref
from shared.db.models import PhysicalCleanupTask
from shared.schemas.domains.tenants_projects import _validate_non_sensitive_config
from shared.source_executor import execute_source_ddl
from shared.webhooks.redact import scrub_url_from_text

logger = logging.getLogger(__name__)

_SESSION_TASK_IDS = "physical_cleanup_task_ids"
_RETRYABLE_STATUSES = ("pending", "failed")


class PhysicalCleanupIdentityError(RuntimeError):
    """A physical artifact exists but its detached target cannot be proven."""


def _remember_task(db: AsyncSession, task_id: UUID) -> None:
    info = getattr(db, "info", None)
    if isinstance(info, dict):
        info.setdefault(_SESSION_TASK_IDS, []).append(task_id)


def pop_scheduled_cleanup_task_ids(db: AsyncSession) -> list[UUID]:
    """Return and clear cleanup IDs scheduled in the caller's transaction."""
    info = getattr(db, "info", None)
    if not isinstance(info, dict):
        return []
    return list(info.pop(_SESSION_TASK_IDS, []))


def _task_from_resolved_identity(
    *,
    artifact_kind: str,
    artifact_id: UUID,
    model_id: UUID,
    requested_by: str,
    resolved: tuple[object, str, str, str],
) -> PhysicalCleanupTask:
    connection, connector, target_schema, qualified_table = resolved
    config = deepcopy(getattr(connection, "config", None) or {})
    # ProjectConnection.config is a plaintext, API-visible NON-secret bag.  Do
    # not extend the lifetime of a malformed legacy secret: fail the metadata
    # delete transaction rather than copying it outside the credential envelope.
    config = _validate_non_sensitive_config(config) or {}
    return PhysicalCleanupTask(
        id=uuid4(),
        artifact_kind=artifact_kind,
        artifact_id=UUID(str(artifact_id)),
        model_id=UUID(str(model_id)),
        project_id=UUID(str(getattr(connection, "project_id"))),
        connection_id=UUID(str(getattr(connection, "id"))),
        connection_type=str(connector),
        connection_display_name=str(
            getattr(connection, "display_name", None) or "deleted connection"
        ),
        encrypted_credentials=bytes(
            getattr(connection, "encrypted_credentials", None) or b""
        ),
        connection_config=config,
        target_schema=str(target_schema or ""),
        qualified_table_name=str(qualified_table),
        requested_by=requested_by,
        status="pending",
        attempts=0,
        next_attempt_at=datetime.now(timezone.utc),
    )


async def schedule_model_physical_cleanup(
    db: AsyncSession,
    *,
    model_id: UUID,
    aggregate_definitions: Iterable[object],
    pocket_definitions: Iterable[object],
    named_query_artifacts: Iterable[object] = (),
    requested_by: str,
) -> list[UUID]:
    """Persist complete detached cleanup identity inside a delete transaction.

    A definition with physical storage may not be deleted if its target identity
    cannot be resolved safely.  Returning success in that state would discard
    the only retry ownership record, the exact Bug-8140 failure mode.

    F-013-07: Named Query artifacts are the third materialised family (after
    aggregates and pockets). Their physical table must be scheduled for drop on
    model/project delete too, or a deleted model leaves an orphan NQ table on the
    target. Their resolver needs the owning ``model_id`` because a
    ``NamedQueryArtifact`` carries no model_id of its own.
    """
    from shared.aggregate_table_ops import resolve_aggregate_physical_table
    from shared.pocket.refresh import resolve_pocket_physical_table

    task_ids: list[UUID] = []
    for artifact_kind, definitions, resolver in (
        ("aggregate", aggregate_definitions, resolve_aggregate_physical_table),
        ("pocket", pocket_definitions, resolve_pocket_physical_table),
    ):
        for definition in definitions:
            resolved = await resolver(definition, db)
            if resolved is None:
                # A purged aggregate has no outstanding physical identity.
                if (
                    artifact_kind == "aggregate"
                    and getattr(definition, "physical_table_purged_at", None)
                    is not None
                ):
                    continue
                raise PhysicalCleanupIdentityError(
                    f"{artifact_kind} {getattr(definition, 'id', '?')} has no "
                    "safe detached target identity"
                )
            task = _task_from_resolved_identity(
                artifact_kind=artifact_kind,
                artifact_id=getattr(definition, "id"),
                model_id=model_id,
                requested_by=requested_by,
                resolved=resolved,
            )
            db.add(task)
            _remember_task(db, task.id)
            task_ids.append(task.id)

    # Named Query artifacts (F-013-07). A never-refreshed artifact has no
    # physical_table_name -> the resolver returns None and it is skipped (no
    # orphan to drop). A cross-project/missing target also resolves to None and
    # is skipped fail-closed (never DROP another project's table).
    named_query_artifacts = list(named_query_artifacts)
    if named_query_artifacts:
        from shared.named_query.refresh import resolve_named_query_physical_table

        for artifact in named_query_artifacts:
            resolved = await resolve_named_query_physical_table(
                artifact, model_id, db
            )
            if resolved is None:
                continue
            task = _task_from_resolved_identity(
                artifact_kind="named_query",
                artifact_id=getattr(artifact, "id"),
                model_id=model_id,
                requested_by=requested_by,
                resolved=resolved,
            )
            db.add(task)
            _remember_task(db, task.id)
            task_ids.append(task.id)
    return task_ids


def _detached_connection(task: PhysicalCleanupTask) -> SimpleNamespace:
    """Connection-shaped view consumed by the sanctioned source executor."""
    return SimpleNamespace(
        id=task.connection_id,
        project_id=task.project_id,
        connection_type=task.connection_type,
        display_name=task.connection_display_name,
        encrypted_credentials=task.encrypted_credentials,
        config=deepcopy(task.connection_config or {}),
    )


async def _execute_task(task: PhysicalCleanupTask, db: AsyncSession) -> None:
    table_ref = quote_table_ref(
        task.connection_type, task.qualified_table_name
    )
    await execute_source_ddl(
        _detached_connection(task),
        f"DROP TABLE IF EXISTS {table_ref}",
        tenant_session=db,
        purpose=f"physical_cleanup:{task.requested_by}",
    )


async def drain_physical_cleanup_tasks(
    db: AsyncSession,
    *,
    task_ids: Iterable[UUID] | None = None,
    batch_size: int = 100,
    raise_on_error: bool = False,
) -> int:
    """Attempt due cleanup rows once each, retaining failures for retry.

    Each row is re-claimed with ``FOR UPDATE SKIP LOCKED`` immediately before
    execution.  A process crash rolls back the claim and leaves the row due; a
    target failure commits ``failed`` plus complete identity/error/attempt state;
    success commits terminal evidence.  A failed status update after successful
    DDL is also safe: the pending row retries an idempotent ``IF EXISTS`` drop.
    """
    explicit_ids = [UUID(str(value)) for value in task_ids or ()]
    now = datetime.now(timezone.utc)
    stmt = select(PhysicalCleanupTask.id).where(
        PhysicalCleanupTask.status.in_(_RETRYABLE_STATUSES)
    )
    if explicit_ids:
        stmt = stmt.where(PhysicalCleanupTask.id.in_(explicit_ids))
    else:
        stmt = stmt.where(
            or_(
                PhysicalCleanupTask.next_attempt_at.is_(None),
                PhysicalCleanupTask.next_attempt_at <= now,
            )
        )
    due_ids = list((await db.execute(
        stmt.order_by(PhysicalCleanupTask.requested_at).limit(batch_size)
    )).scalars().all())

    attempted = 0
    for task_id in due_ids:
        claim = await db.execute(
            select(PhysicalCleanupTask)
            .where(PhysicalCleanupTask.id == task_id)
            .where(PhysicalCleanupTask.status.in_(_RETRYABLE_STATUSES))
            .with_for_update(skip_locked=True)
        )
        task = claim.scalar_one_or_none()
        if task is None:
            continue

        attempted += 1
        attempted_at = datetime.now(timezone.utc)
        task.attempts += 1
        task.last_attempt_at = attempted_at
        try:
            await _execute_task(task, db)
        except Exception as exc:
            safe_detail = scrub_url_from_text(str(exc)) or type(exc).__name__
            task.status = "failed"
            task.next_attempt_at = attempted_at
            task.completed_at = None
            task.error_message = f"{type(exc).__name__}: {safe_detail}"[:4000]
            logger.warning(
                "Bug-8140 cleanup task %s failed on attempt %s: %s",
                task.id, task.attempts, task.error_message,
            )
        else:
            task.status = "succeeded"
            task.next_attempt_at = None
            task.completed_at = attempted_at
            task.error_message = None
            logger.info(
                "Bug-8140 cleanup task %s succeeded for %s %s",
                task.id, task.artifact_kind, task.artifact_id,
            )

        try:
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception(
                "Bug-8140 cleanup task %s outcome commit failed; idempotent "
                "retry remains authoritative",
                task_id,
            )
            if raise_on_error:
                raise

    return attempted


async def attempt_scheduled_physical_cleanup(db: AsyncSession) -> int:
    """Best-effort immediate drain after the caller's metadata commit.

    The durable rows, not this request-local attempt, own correctness.  A tenant
    DB outage while claiming them is logged and left for the scheduler; it must
    not turn an already-committed model/project delete into a false HTTP failure.
    """
    task_ids = pop_scheduled_cleanup_task_ids(db)
    if not task_ids:
        return 0
    try:
        return await drain_physical_cleanup_tasks(db, task_ids=task_ids)
    except Exception:
        await db.rollback()
        logger.exception(
            "Bug-8140 immediate cleanup drain failed after metadata commit; "
            "%d durable task(s) remain retryable",
            len(task_ids),
        )
        return 0
