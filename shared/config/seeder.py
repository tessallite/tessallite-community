"""Seed registry defaults into the settings tables.

Two entry points:

    seed_system_defaults(system_session)
        Run once at system bootstrap. Idempotent UPSERT for every system-
        level key in the registry. Skips keys that already have an
        explicit value.

    seed_tenant_defaults(tenant_session)
        Run once per tenant create. Idempotent UPSERT for every tenant-
        level key. Project/model defaults are NOT seeded here — those
        rows are created lazily on the first per-scope write so the
        resolver can fall back to tenant/system without a row existing.

Idempotency
-----------
INSERT ... ON CONFLICT DO NOTHING is used so re-runs after manual edits
do not stomp operator changes. To force a reset, the operator must
delete the row(s) first.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config.registry import all_for_level
from shared.db.models import SystemSetting, TenantSetting

logger = logging.getLogger(__name__)


async def seed_system_defaults(
    system_session: AsyncSession, *, actor: str = "seeder"
) -> int:
    """UPSERT-but-skip every registry-defined system-level default.

    Returns the count of rows inserted (existing rows are left untouched).
    """
    inserted = 0
    for definition in all_for_level("system"):
        if definition.default is None or definition.key == "meta.bootstrap_env_view":
            # bootstrap_env_view is computed at request time, never stored.
            continue
        stmt = (
            pg_insert(SystemSetting)
            .values(
                key=definition.key,
                value_json=_jsonable(definition.default),
                updated_by=actor,
            )
            .on_conflict_do_nothing(index_elements=["key"])
        )
        result = await system_session.execute(stmt)
        if result.rowcount:
            inserted += int(result.rowcount)
    await system_session.commit()
    logger.info("seed_system_defaults: inserted %d new rows", inserted)
    return inserted


async def seed_tenant_defaults(
    tenant_session: AsyncSession, *, actor: str = "seeder"
) -> int:
    """UPSERT-but-skip every registry-defined tenant-level default."""
    inserted = 0
    for definition in all_for_level("tenant"):
        if definition.default is None:
            continue
        stmt = (
            pg_insert(TenantSetting)
            .values(
                key=definition.key,
                value_json=_jsonable(definition.default),
                updated_by=actor,
            )
            .on_conflict_do_nothing(index_elements=["key"])
        )
        result = await tenant_session.execute(stmt)
        if result.rowcount:
            inserted += int(result.rowcount)
    await tenant_session.commit()
    logger.info("seed_tenant_defaults: inserted %d new rows", inserted)
    return inserted


def _jsonable(value: Any) -> Any:
    """Defensive coercion — JSONB accepts native dict/list/scalars; strip
    any tuple sneak-ins from the registry by converting to list."""
    if isinstance(value, tuple):
        return list(value)
    return value
