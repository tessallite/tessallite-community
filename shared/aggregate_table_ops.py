"""Shared aggregate physical-table operations.

One canonical place to DROP the materialised target table behind an aggregate
definition, used by every retire/purge path so the SQL is identical everywhere:

- resolve the aggregate's target connection + schema,
- quote the table reference connector-correctly via ``quote_table_ref``
  (never hand-rolled — BigQuery needs backticks, not double quotes),
- issue the DROP through ``execute_source_ddl`` (the sanctioned target-execution
  gateway — no direct connector access),
- stamp ``physical_table_purged_at`` so the drop is never re-attempted,
- record a ``purged`` lifecycle event for the audit trail.

"Retired means the table is actually dropped": retire paths call this at retire
time; the retirement sweep calls it as a safety net for tables that slipped
through (e.g. a revert that marked an aggregate retired without target access).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.config.source_db import (
    resolve_aggregate_target_defaults,
    resolve_target_schema,
)
from shared.connection_scope import (
    CrossProjectConnectionError,
    resolve_endpoint_connection_for_model,
)
from shared.connector_qualify import quote_table_ref
from shared.db.models import (
    AggregateDefinition,
    AggregateLifecycleEvent,
    DataTarget,
)
from shared.source_executor import execute_source_ddl, resolve_connector_type

logger = logging.getLogger(__name__)


async def resolve_aggregate_physical_table(
    agg: AggregateDefinition,
    db: AsyncSession,
) -> tuple[Any, str, str, str] | None:
    """Resolve a validated aggregate target to detached cleanup identity.

    Returns ``(connection, connector, schema, qualified_table_name)``.  Both
    ordinary retirement drops and Bug-8140's durable delete outbox use this
    exact resolver, so cross-project refusal and connector qualification cannot
    drift between immediate and post-commit cleanup paths.
    """
    if not agg.physical_table_name or agg.physical_table_purged_at is not None:
        return None

    target = await db.get(DataTarget, agg.target_id)
    if target is None:
        logger.warning(
            "Cannot resolve physical table for aggregate %s — DataTarget %s missing",
            agg.id, agg.target_id,
        )
        return None

    try:
        target_conn = await resolve_endpoint_connection_for_model(
            db, target, model_id=agg.model_id
        )
    except CrossProjectConnectionError:
        logger.error(
            "Refusing to resolve physical table for aggregate %s — its target "
            "connection belongs to a different project than model %s",
            agg.id, agg.model_id,
        )
        return None
    except ValueError:
        logger.warning(
            "Cannot resolve physical table for aggregate %s — target connection "
            "or owning model could not be resolved",
            agg.id,
        )
        return None

    connector = await resolve_connector_type(target_conn)
    defaults = await resolve_aggregate_target_defaults(
        tenant_session=db,
        project_id=target_conn.project_id,
    )
    from shared.config.source_db import resolve_connection_bq_project

    tgt_ref = resolve_target_schema(
        connector,
        target.config,
        defaults,
        schema_override=agg.target_schema,
        connection_bq_project=resolve_connection_bq_project(target_conn),
    )
    return (
        target_conn,
        connector,
        tgt_ref.schema,
        tgt_ref.qualified_table(agg.physical_table_name),
    )


async def drop_aggregate_physical_table(
    agg: AggregateDefinition,
    db: AsyncSession,
    *,
    reason: str = "retired",
    record_bookkeeping: bool = True,
) -> bool:
    """Drop the materialised target table behind ``agg`` (idempotent).

    No-op (returns False) when the aggregate has no physical table name or its
    table was already purged. On success: issues ``DROP TABLE IF EXISTS`` via
    ``execute_source_ddl``, stamps ``physical_table_purged_at`` on ``agg``, adds
    a ``purged`` ``AggregateLifecycleEvent`` to the session, and returns True.

    ``record_bookkeeping=False`` (TMP-20260811210534687) performs the DROP
    through the exact same hardened path but writes NEITHER the
    ``physical_table_purged_at`` stamp NOR the ``AggregateLifecycleEvent``. It
    remains as a compatibility behavior protected by its Bug-9006 regression,
    but has no production caller. In particular, model/project deletion and
    project replacement MUST NOT use it: those paths schedule
    ``PhysicalCleanupTask`` rows in their metadata transaction and execute DDL
    only after commit. The suppressed writes would be unsafe in the historical
    owner-delete shape because they target rows that no longer exist when the
    session next flushes:

      * the stamp is an UPDATE of a row the cascade deletes — it either matches
        zero rows (``StaleDataError``) or is silently swallowed by asyncpg's
        ``supports_sane_multi_rowcount = False`` on a batched update;
      * the event is an INSERT carrying ``model_id`` of the model being deleted,
        so it violates ``aggregate_lifecycle_events_model_id_fkey``.

    Neither is a loss of audit: the cascade deletes this model's
    ``aggregate_lifecycle_events`` rows in the same transaction, so a purge event
    written here could never outlive the model it describes. The durable record
    of a model deletion is the ``audit_events`` row its endpoint writes.
    The stamp's purpose — "never re-attempt this drop" — is likewise moot for a
    definition row that is being deleted.

    The DROP is best-effort: a failure (target unreachable, permissions) is
    logged and swallowed — the caller's retirement must not be blocked by an
    unreachable target, and the retirement sweep will retry the drop later
    because ``physical_table_purged_at`` stays NULL.

    ``db`` is the tenant session (used both as ORM session and as the
    ``tenant_session`` for target resolution + DDL execution). Commit is the
    caller's responsibility.
    """
    try:
        resolved = await resolve_aggregate_physical_table(agg, db)
        if resolved is None:
            return False
        target_conn, connector, _schema, dotted = resolved
        table_ref = quote_table_ref(connector, dotted)
        await execute_source_ddl(
            target_conn,
            f"DROP TABLE IF EXISTS {table_ref}",
            tenant_session=db,
        )
    except Exception as exc:
        logger.error(
            "Failed to drop physical table for aggregate %s (%s): %s",
            agg.id, agg.physical_table_name, exc,
        )
        return False

    if record_bookkeeping:
        agg.physical_table_purged_at = datetime.now(timezone.utc)
        db.add(
            AggregateLifecycleEvent(
                model_id=agg.model_id,
                aggregate_id=agg.id,
                event_type="purged",
                reason=reason,
                payload={"table": agg.physical_table_name},
            )
        )
    logger.info(
        "Dropped physical table %s for aggregate %s (reason=%s)",
        agg.physical_table_name, agg.id, reason,
    )
    return True
