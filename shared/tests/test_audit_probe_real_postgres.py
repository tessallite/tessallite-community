"""Bug-9552 / Bug-9821 — the audit-store probe against a REAL PostgreSQL.

The unit guards in ``test_pre_migration_login_bootstrap.py`` run against a
mocked session, so they pin WHICH TABLE the probe names and WHICH POSTURE each
state produces, but not that the statements actually behave as the fix assumes.
These properties carry the fix and none is observable through a mock:

1. ``to_regclass`` with a BOUND PARAMETER executes. The pre-fix probe inlined
   the name as a literal; the fix binds it. Had PostgreSQL been unable to infer
   the parameter type, the probe would raise, the resolver would fail closed to
   "present", and the insert would run — reintroducing the exact login deadlock
   on a pre-migration schema that Bug-9552 was filed for.
2. ``to_regclass`` returns NULL for a missing table rather than raising. The
   whole "skip the event during bootstrap" behaviour depends on that, and it is
   also what made the original defect silent instead of loud.
3. Bug-9821: on a real schema the three states resolve as designed — audit
   table present, absent-during-bootstrap, and absent-after-migration — with
   the last one refusing the write instead of dropping it.

Opt-in: set ``TESSALLITE_AUDIT_PROBE_TEST_DB_URL`` to a THROWAWAY PostgreSQL,
e.g.::

    docker run -d --name pg-probe -e POSTGRES_PASSWORD=probe -p 55432:5432 \
        postgres:16-alpine
    TESSALLITE_AUDIT_PROBE_TEST_DB_URL=postgresql+psycopg://postgres:probe@127.0.0.1:55432/postgres \
        pytest shared/tests/test_audit_probe_real_postgres.py

The tests create and drop their own scratch schema and touch nothing else. The
schema NAME is redirected onto that scratch schema; the statements, the
migration-graph lookup, and the resolver logic under test are the real ones.
Never point this at a real system database.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text

from shared.audit import system as audit_system
from shared.audit.logger import AuditWriteError
from shared.audit.system import (
    _AUDIT_TABLE_QUALIFIED,
    _AUDIT_TABLE_REVISION,
    _PRESENCE_PROBE,
    _VERSION_TABLE_QUALIFIED,
)

_DB_URL = os.environ.get("TESSALLITE_AUDIT_PROBE_TEST_DB_URL", "")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason=(
        "set TESSALLITE_AUDIT_PROBE_TEST_DB_URL to a throwaway PostgreSQL to "
        "run the real-database audit probe checks"
    ),
)


@pytest.mark.asyncio
async def test_bug9552_probe_resolves_present_and_absent_tables_on_postgres():
    from sqlalchemy.ext.asyncio import create_async_engine

    schema = f"probe_{uuid.uuid4().hex[:12]}"
    present = f"{schema}.system_audit_events"
    absent = f"{schema}.audit_events"  # the name the pre-fix probe drifted to

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.execute(
                text(f'CREATE TABLE "{schema}".system_audit_events (id int)')
            )

        async with engine.connect() as conn:
            # Property 1: the bound-parameter probe executes and resolves.
            found = await conn.execute(
                text(_PRESENCE_PROBE),
                {"audit_table": present, "version_table": absent},
            )
            row = found.first()
            assert row is not None and row[0] is not None, (
                "the parameterised to_regclass probe did not resolve an "
                "existing table; the resolver would fail closed and the "
                "pre-migration bootstrap protection would be lost"
            )

            # Property 2: a missing table yields NULL, not an exception.
            missing = await conn.execute(
                text(_PRESENCE_PROBE),
                {"audit_table": absent, "version_table": absent},
            )
            missing_row = missing.first()
            assert missing_row is not None
            assert missing_row[0] is None
            assert missing_row[1] is None
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


def test_bug9552_probe_constant_is_schema_qualified():
    """A probe missing its schema silently resolves against the search_path."""
    assert "." in _AUDIT_TABLE_QUALIFIED
    assert not _AUDIT_TABLE_QUALIFIED.startswith("None.")
    assert "." in _VERSION_TABLE_QUALIFIED
    assert not _VERSION_TABLE_QUALIFIED.startswith("None.")


def _redirect_to_schema(monkeypatch, schema: str) -> None:
    """Point the module's real statements at a throwaway schema."""
    monkeypatch.setattr(
        audit_system, "_AUDIT_TABLE_QUALIFIED", f"{schema}.system_audit_events"
    )
    monkeypatch.setattr(
        audit_system, "_VERSION_TABLE_QUALIFIED", f"{schema}.alembic_version"
    )
    monkeypatch.setattr(
        audit_system,
        "_STAMP_QUERY",
        f'SELECT version_num FROM "{schema}"."alembic_version"',
    )


@pytest.mark.asyncio
async def test_bug9821_resolver_states_on_a_real_schema(monkeypatch):
    """The three audit-store states resolve correctly against real PostgreSQL."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    schema = f"probe_{uuid.uuid4().hex[:12]}"
    engine = create_async_engine(_DB_URL)
    _redirect_to_schema(monkeypatch, schema)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))

        # No audit table and no stamp table: first-deploy bootstrap.
        async with AsyncSession(engine) as session:
            assert await audit_system._resolve_audit_store(session) == "bootstrap"

        # Stamp table present, stamped before the creating revision: the
        # login-gated migration gate on an existing deployment.
        async with engine.begin() as conn:
            await conn.execute(
                text(f'CREATE TABLE "{schema}".alembic_version (version_num varchar(32))')
            )
            await conn.execute(
                text(f"INSERT INTO \"{schema}\".alembic_version VALUES ('0128')")
            )
        async with AsyncSession(engine) as session:
            assert await audit_system._resolve_audit_store(session) == "bootstrap"

        # Stamped AT the creating revision with the table still absent: broken.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f"UPDATE \"{schema}\".alembic_version SET version_num = "
                    f"'{_AUDIT_TABLE_REVISION}'"
                )
            )
        async with AsyncSession(engine) as session:
            assert (
                await audit_system._resolve_audit_store(session)
                == "missing_after_migration"
            )

        # The audit table exists: normal write path, whatever the stamp says.
        async with engine.begin() as conn:
            await conn.execute(
                text(f'CREATE TABLE "{schema}".system_audit_events (id int)')
            )
        async with AsyncSession(engine) as session:
            assert await audit_system._resolve_audit_store(session) == "present"
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


@pytest.mark.asyncio
async def test_bug9821_system_audit_refuses_after_migration_on_a_real_schema(
    monkeypatch,
):
    """The refusal reaches the caller, so its mutation cannot commit."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    schema = f"probe_{uuid.uuid4().hex[:12]}"
    engine = create_async_engine(_DB_URL)
    _redirect_to_schema(monkeypatch, schema)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.execute(
                text(f'CREATE TABLE "{schema}".alembic_version (version_num varchar(32))')
            )
            await conn.execute(
                text(
                    f"INSERT INTO \"{schema}\".alembic_version VALUES "
                    f"('{_AUDIT_TABLE_REVISION}')"
                )
            )
        async with AsyncSession(engine) as session:
            with pytest.raises(AuditWriteError):
                await audit_system.system_audit(
                    session, action="tenant.delete", severity="critical"
                )
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
