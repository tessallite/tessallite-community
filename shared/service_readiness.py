"""Bounded readiness probes shared by services that use the system database.

Readiness is deliberately separate from process liveness.  A service may keep
its process alive while PostgreSQL is unavailable so the orchestrator does not
turn a dependency outage into a restart cascade.
"""
from __future__ import annotations

import asyncio

from shared.config.settings import get_settings


async def probe_metadata_database() -> tuple[bool, str]:
    """Run one bounded metadata-database probe and return ``(ready, detail)``.

    Imports for the session and SQL expression stay inside the probe so tests
    and service startup can replace the configured session factory without
    rebinding this shared helper.  The probe performs no migrations, writes, or
    dependency calls.
    """

    from sqlalchemy import text

    from shared.db.session import SystemSessionLocal

    timeout = get_settings().READINESS_PROBE_TIMEOUT_SECONDS
    try:
        async with SystemSessionLocal() as session:
            try:
                await asyncio.wait_for(
                    session.execute(text("SELECT 1")), timeout=timeout
                )
            except asyncio.TimeoutError:
                # A cancelled SQLAlchemy/asyncpg operation can leave its
                # connection in a pending-rollback state. Discard it so a
                # health check cannot poison the system pool used by requests.
                await session.invalidate()
                return False, "metadata database probe timed out"
            except asyncio.CancelledError:
                # Preserve caller cancellation, but finish invalidation before
                # the context returns the connection to the shared pool.
                await asyncio.shield(session.invalidate())
                raise
            except Exception as exc:  # noqa: BLE001 — readiness must never raise
                await session.invalidate()
                return False, str(exc)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — readiness must never raise
        return False, str(exc)
    return True, "ok"
