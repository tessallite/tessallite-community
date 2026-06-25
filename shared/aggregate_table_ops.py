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


async def drop_aggregate_physical_table(
    agg: AggregateDefinition,
    db: AsyncSession,
    *,
    reason: str = "retired",
) -> bool:
    """Drop the materialised target table behind ``agg`` (idempotent).

    No-op (returns False) when the aggregate has no physical table name or its
    table was already purged. On success: issues ``DROP TABLE IF EXISTS`` via
    ``execute_source_ddl``, stamps ``physical_table_purged_at`` on ``agg``, adds
    a ``purged`` ``AggregateLifecycleEvent`` to the session, and returns True.

    The DROP is best-effort: a failure (target unreachable, permissions) is
    logged and swallowed — the caller's retirement must not be blocked by an
    unreachable target, and the retirement sweep will retry the drop later
    because ``physical_table_purged_at`` stays NULL.

    ``db`` is the tenant session (used both as ORM session and as the
    ``tenant_session`` for target resolution + DDL execution). Commit is the
    caller's responsibility.
    """
    if not agg.physical_table_name or agg.physical_table_purged_at is not None:
        return False

    target = await db.get(DataTarget, agg.target_id)
    if target is None:
        logger.warning(
            "Cannot drop table for aggregate %s — DataTarget %s missing",
            agg.id, agg.target_id,
        )
        return False

    # Bug-5500 fail-closed: never issue DROP TABLE against a target connection
    # in a different project than the aggregate's owning model. A legacy/imported
    # cross-project DataTarget row would otherwise drop a table on another
    # project's database during retire/cap/purge. Resolve through the shared
    # connection-scope guard and refuse (no-op) on mismatch; a missing
    # connection/model is treated like the existing best-effort no-op cleanup.
    try:
        target_conn = await resolve_endpoint_connection_for_model(
            db, target, model_id=agg.model_id
        )
    except CrossProjectConnectionError:
        logger.error(
            "Refusing to drop physical table for aggregate %s — its target "
            "connection belongs to a different project than model %s "
            "(cross-project row rejected fail-closed)",
            agg.id, agg.model_id,
        )
        return False
    except ValueError:
        logger.warning(
            "Cannot drop table for aggregate %s — target connection or owning "
            "model could not be resolved",
            agg.id,
        )
        return False

    try:
        connector = await resolve_connector_type(target_conn)
        defaults = await resolve_aggregate_target_defaults(
            tenant_session=db,
            project_id=target_conn.project_id,
        )
        tgt_ref = resolve_target_schema(
            connector,
            target.config,
            defaults,
            schema_override=agg.target_schema,
        )
        dotted = tgt_ref.qualified_table(agg.physical_table_name)
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
