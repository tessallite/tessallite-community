"""System-plane audit writer (G-022-01 / F-022-03).

Tenant ``audit_events`` cannot survive tenant-schema drop and cannot record
system-admin actions that have no tenant session. Callers pass the system
DB session and emit **before** ``commit`` so a flush failure rolls back the
mutation.

Pre-migration bootstrap: ``tess_system.system_audit_events`` is created by
migration 0212, so a system whose schema is behind that migration has no audit
store. The login-gated migration path must still authenticate and apply
migrations, so ``system_audit`` skips the event when the table is absent (probe
only — it never touches the caller's transaction). Post-migration behaviour is
unchanged: the event is added and flushed before the caller's commit.

Bug-9552: the probe target is derived from the ``SystemAuditEvent`` ORM table
and is never restated as a literal here. A restated name had drifted to the
tenant table (``audit_events``) while the migration creates
``system_audit_events``. Because ``to_regclass`` returns NULL instead of
raising, the probe reported "absent" on every schema — including fully
migrated ones — so every system audit event was silently discarded.

Bug-9821: the tolerance above is a BOOTSTRAP concession, not a posture. The
tenant plane (``shared.audit.logger.audit_required``) is fail-closed: a
persistence failure raises and the mutation rolls back. This module now matches
that posture everywhere except the one window the concession was granted for.
"Absent" is only accepted while the schema itself says migrations have not yet
reached the revision that creates the table:

* audit table present            -> normal write; a flush failure propagates
                                    (already fail-closed).
* absent, no ``alembic_version`` -> nothing has ever been migrated; this is the
                                    first-deploy bootstrap. Skip.
* absent, stamped before the      -> the login-gated migration gate on an
  creating revision                 existing deployment (the Bug-9552 case).
                                    Skip.
* absent, anything else          -> the schema claims to have applied the
                                    revision that creates the audit store, or
                                    carries a stamp this build cannot place.
                                    FAIL CLOSED — raise ``AuditWriteError`` so
                                    the caller's mutation rolls back instead of
                                    committing without durable evidence.

The "stamped before" set is read from the shipped migration graph through
Alembic's own ``ScriptDirectory``, not from revision-number arithmetic: the
chain has two branches (``system`` rooted at 0001, ``tenant`` at 0002) whose
numbers interleave, so 0213 is NOT "after" 0212 — it is on the other branch.
A graph that cannot be read is an unsupported shape and also fails closed.
"""
from __future__ import annotations

import logging
import pathlib
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from shared.audit.logger import AuditWriteError
from shared.db.models import SystemAuditEvent

logger = logging.getLogger(__name__)

_AUDIT_TABLE = SystemAuditEvent.__table__
_AUDIT_TABLE_QUALIFIED = f"{_AUDIT_TABLE.schema}.{_AUDIT_TABLE.name}"
_VERSION_TABLE_QUALIFIED = f"{_AUDIT_TABLE.schema}.alembic_version"

# The revision that creates ``_AUDIT_TABLE``. Bound to the migration graph by
# ``shared/tests/test_pre_migration_login_bootstrap.py`` so it cannot drift away
# from the migration that actually creates the table.
_AUDIT_TABLE_REVISION = "0212"

_MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[1] / "db" / "migrations"

# Both lookups in one round trip. ``to_regclass`` returns NULL rather than
# raising for a missing relation, so this probe can never poison the caller's
# transaction.
_PRESENCE_PROBE = (
    "SELECT to_regclass(:audit_table) AS audit_table, "
    "to_regclass(:version_table) AS version_table"
)

_PREPARER = postgresql.dialect().identifier_preparer
_STAMP_QUERY = (
    "SELECT version_num FROM "
    f"{_PREPARER.quote_schema(_AUDIT_TABLE.schema)}."
    f"{_PREPARER.quote('alembic_version')}"
)

# Audit-store states resolved from the schema.
_PRESENT = "present"
_BOOTSTRAP = "bootstrap"
_MISSING_AFTER_MIGRATION = "missing_after_migration"


@lru_cache(maxsize=1)
def _revisions_before_audit_table() -> Optional[frozenset[str]]:
    """Revisions strictly older than the one that creates the audit table.

    A schema stamped with one of these has genuinely not reached the audit
    store yet, which is the only state the bootstrap concession covers.

    Returns ``None`` when the migration graph cannot be read (Alembic absent,
    migrations not shipped, or ``_AUDIT_TABLE_REVISION`` no longer in the
    graph). The caller treats that as an unsupported shape and fails closed
    rather than silently reverting to fail-open.

    Alembic is imported here rather than at module scope: services that never
    write platform-plane audit events must not gain an import-time dependency
    on the migration tooling, and this function is only reached when the audit
    table is missing.
    """
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config()
        cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
        script = ScriptDirectory.from_config(cfg)
        chain = {
            rev.revision
            for rev in script.iterate_revisions(_AUDIT_TABLE_REVISION, "base")
        }
    except Exception:  # noqa: BLE001 — any failure to read the graph fails closed
        logger.exception(
            "Could not read the migration graph at %s to place revision %s; "
            "platform-plane audit will fail closed on a missing audit table.",
            _MIGRATIONS_DIR,
            _AUDIT_TABLE_REVISION,
        )
        return None
    if _AUDIT_TABLE_REVISION not in chain:
        logger.error(
            "Revision %s is not present in the migration graph at %s; "
            "platform-plane audit will fail closed on a missing audit table.",
            _AUDIT_TABLE_REVISION,
            _MIGRATIONS_DIR,
        )
        return None
    return frozenset(chain - {_AUDIT_TABLE_REVISION})


async def _resolve_audit_store(db: AsyncSession) -> str:
    """Classify the audit store as present, pre-migration, or missing-in-error.

    The probe must never break auditing, so any probe error reports the store
    present (fail closed) and the normal insert path raises as before.
    """
    try:
        result = await db.execute(
            text(_PRESENCE_PROBE),
            {
                "audit_table": _AUDIT_TABLE_QUALIFIED,
                "version_table": _VERSION_TABLE_QUALIFIED,
            },
        )
        row = result.first()
    except Exception:  # noqa: BLE001 — an unreadable probe must not disable auditing
        return _PRESENT
    if row is None:
        return _PRESENT
    audit_table, version_table = row[0], row[1]
    if audit_table is not None:
        return _PRESENT
    if version_table is None:
        # No stamp table at all: nothing has ever been migrated here.
        return _BOOTSTRAP

    before = _revisions_before_audit_table()
    if before is None:
        return _MISSING_AFTER_MIGRATION

    try:
        stamps = {
            str(value)
            for value in (await db.execute(text(_STAMP_QUERY))).scalars().all()
            if value is not None
        }
    except Exception:  # noqa: BLE001 — an unreadable stamp cannot prove bootstrap
        logger.exception(
            "Could not read %s while classifying a missing audit store.",
            _VERSION_TABLE_QUALIFIED,
        )
        return _MISSING_AFTER_MIGRATION

    if not stamps:
        # Stamp table present but empty: the schema is at base.
        return _BOOTSTRAP
    if stamps <= before:
        return _BOOTSTRAP
    return _MISSING_AFTER_MIGRATION


async def system_audit(
    db: AsyncSession,
    *,
    action: str,
    severity: str,
    actor_email: Optional[str] = None,
    actor_id: Optional[uuid.UUID] = None,
    target_type: Optional[str] = None,
    target_id: Optional[uuid.UUID] = None,
    target_name: Optional[str] = None,
    tenant_slug: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
    ip_address: Optional[str] = None,
) -> Optional[SystemAuditEvent]:
    state = await _resolve_audit_store(db)
    if state is _BOOTSTRAP:
        return None  # pre-migration bootstrap: no audit store yet
    if state is _MISSING_AFTER_MIGRATION:
        # Bug-9821: past the bootstrap window the platform plane is fail-closed
        # like the tenant plane. The caller emits before ``commit``, so raising
        # here rolls the mutation back rather than committing it with no
        # durable evidence.
        logger.error(
            "Platform-plane audit store %s is missing on a schema that is not "
            "in the pre-migration bootstrap window; failing the mutation "
            "closed: action=%s",
            _AUDIT_TABLE_QUALIFIED,
            action,
        )
        raise AuditWriteError(
            f"Required system audit event {action!r} could not be persisted: "
            f"{_AUDIT_TABLE_QUALIFIED} is missing outside the pre-migration "
            "bootstrap window"
        )
    event = SystemAuditEvent(
        timestamp=datetime.now(timezone.utc),
        actor_id=actor_id,
        actor_email=actor_email,
        action=action,
        target_type=target_type,
        target_id=target_id,
        target_name=target_name,
        tenant_slug=tenant_slug,
        severity=severity,
        detail=detail,
        ip_address=ip_address,
    )
    db.add(event)
    await db.flush()
    return event
