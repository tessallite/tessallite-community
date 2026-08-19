"""Real-Postgres upgrade guard for Bug-8140's detached cleanup outbox.

Skipped unless the versioning/importer harness DB URL is configured.  The test
starts from an ORM-created tenant schema with the new table removed, applies
0209 twice, and proves the required identity/retry columns exist with no foreign
key that model/project deletion could cascade away.
"""
from __future__ import annotations

import importlib.util
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine

from shared.db.models import TenantBase

pytestmark = [pytest.mark.integration]

_DB_URL = os.environ.get("TESSALLITE_VERSIONING_DB_URL") or os.environ.get(
    "TESSALLITE_IMPORTER_REHYDRATION_DB_URL"
)
pytestmark.append(
    pytest.mark.skipif(not _DB_URL, reason="no Postgres URL configured")
)

_MIGRATION = (
    Path(__file__).resolve().parents[4]
    / "shared" / "db" / "migrations" / "versions"
    / "0209_physical_cleanup_tasks.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("m0209_probe", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_upgrade(connection) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    ctx = MigrationContext.configure(connection)
    with Operations.context(ctx):
        _load_migration().upgrade()


@asynccontextmanager
async def _isolated_schema():
    schema = f"m0209_{uuid.uuid4().hex}"
    boot = create_async_engine(_DB_URL, future=True)
    async with boot.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.execute(text(f'SET search_path TO "{schema}"'))
        await conn.run_sync(TenantBase.metadata.create_all)
        await conn.execute(text("DROP TABLE physical_cleanup_tasks"))
    await boot.dispose()

    engine = create_async_engine(_DB_URL, future=True)

    @event.listens_for(engine.sync_engine, "connect")
    def _set_search_path(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute(f'SET search_path TO "{schema}"')
        cursor.close()

    try:
        yield engine, schema
    finally:
        drop = create_async_engine(_DB_URL, future=True)
        async with drop.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await drop.dispose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_bug_8140_upgrade_creates_detached_retryable_cleanup_identity():
    async with _isolated_schema() as (engine, schema):
        async with engine.begin() as conn:
            await conn.run_sync(_run_upgrade)
        async with engine.begin() as conn:
            await conn.run_sync(_run_upgrade)  # idempotent rerun

        async with engine.connect() as conn:
            columns = set((await conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = :schema AND table_name = "
                "'physical_cleanup_tasks'"
            ), {"schema": schema})).scalars().all())
            required = {
                "artifact_kind", "artifact_id", "model_id", "project_id",
                "connection_id", "connection_type", "encrypted_credentials",
                "connection_config", "target_schema", "qualified_table_name",
                "status", "attempts", "next_attempt_at", "error_message",
            }
            assert required <= columns

            foreign_keys = (await conn.execute(text(
                "SELECT count(*) FROM information_schema.table_constraints "
                "WHERE table_schema = :schema AND table_name = "
                "'physical_cleanup_tasks' AND constraint_type = 'FOREIGN KEY'"
            ), {"schema": schema})).scalar_one()
            assert foreign_keys == 0
