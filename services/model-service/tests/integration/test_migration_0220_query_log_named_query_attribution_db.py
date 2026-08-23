"""Executable upgrade/downgrade proof for migration 0220 (Bug-9172).

The unit migration guard checks the revision shape, but it cannot prove that
Postgres accepts the UUID/varchar columns, preserves historical rows, creates
the intended index, or can downgrade cleanly. This test creates an isolated
tenant schema, simulates the 0219 state by removing the new ORM columns, and
executes the real Alembic ``upgrade`` twice and ``downgrade`` once.

Set ``TESSALLITE_VERSIONING_DB_URL`` (or
``TESSALLITE_IMPORTER_REHYDRATION_DB_URL``) to run it. It is skipped when no
Postgres URL is configured so unit-only environments remain usable.
"""
from __future__ import annotations

import importlib.util
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shared.db.models import TenantBase

pytestmark = [pytest.mark.integration]

_DB_URL = os.environ.get("TESSALLITE_VERSIONING_DB_URL") or os.environ.get(
    "TESSALLITE_IMPORTER_REHYDRATION_DB_URL"
)
_MIGRATION = (
    Path(__file__).resolve().parents[4]
    / "shared" / "db" / "migrations" / "versions"
    / "0220_query_log_named_query_attribution.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "m0220_query_log_named_query_attribution_probe", _MIGRATION
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade(connection) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module = _load_migration()
    ctx = MigrationContext.configure(connection)
    with Operations.context(ctx):
        module.upgrade()


def _downgrade(connection) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module = _load_migration()
    ctx = MigrationContext.configure(connection)
    with Operations.context(ctx):
        module.downgrade()


def _engine_for_schema(schema: str):
    engine = create_async_engine(_DB_URL, future=True)

    @event.listens_for(engine.sync_engine, "connect")
    def _set_search_path(dbapi_connection, _record):  # noqa: ANN001
        cur = dbapi_connection.cursor()
        cur.execute(f'SET search_path TO "{schema}"')
        cur.close()

    return engine


@asynccontextmanager
async def _isolated_schema() -> AsyncIterator[tuple[async_sessionmaker, object]]:
    schema = f"m0220_{uuid.uuid4().hex}"
    boot = create_async_engine(_DB_URL, future=True)
    async with boot.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.execute(text(f'SET search_path TO "{schema}"'))
        await conn.run_sync(TenantBase.metadata.create_all)
        # The current ORM describes the post-0220 state. Rewind only these
        # fields so the real 0220 upgrade, rather than create_all, owns them.
        await conn.execute(text('DROP INDEX IF EXISTS "ix_query_logs_named_query_id"'))
        await conn.execute(text(
            'ALTER TABLE "query_logs" '
            'DROP COLUMN IF EXISTS "named_query_fallback_reason", '
            'DROP COLUMN IF EXISTS "named_query_id"'
        ))
    await boot.dispose()

    engine = _engine_for_schema(schema)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory, engine
    finally:
        drop = create_async_engine(_DB_URL, future=True)
        async with drop.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await drop.dispose()
        await engine.dispose()


async def _query_log_columns(session) -> dict[str, dict[str, object]]:
    rows = (
        await session.execute(text(
            "SELECT column_name, is_nullable, data_type, character_maximum_length "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'query_logs'"
        ))
    ).mappings().all()
    return {str(row["column_name"]): dict(row) for row in rows}


async def _query_log_indexes(session) -> list[dict[str, object]]:
    return [
        dict(row)
        for row in (
            await session.execute(text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = current_schema() AND tablename = 'query_logs'"
            ))
        ).mappings().all()
    ]


@pytest.mark.asyncio
async def test_0220_upgrade_and_downgrade_preserve_querylog_rows_and_contract():
    """B9172-SOL-R1-F6: execute the real Postgres migration transition."""
    if not _DB_URL:
        pytest.skip(
            "TESSALLITE_VERSIONING_DB_URL/importer URL is not configured"
        )

    historical_id = uuid.uuid4()
    async with _isolated_schema() as (factory, engine):
        async with factory() as session:
            before = await _query_log_columns(session)
            assert "named_query_id" not in before
            assert "named_query_fallback_reason" not in before
            await session.execute(text(
                "INSERT INTO query_logs "
                "(id, protocol, raw_query, query_fingerprint, route_type, "
                "status, execution_ms, bytes_processed) "
                "VALUES (:id, 'jdbc', 'SELECT 1', 'historical-fp', 'source', "
                "'success', 7, 11)"
            ), {"id": historical_id})
            await session.commit()

        async with engine.begin() as connection:
            await connection.run_sync(_upgrade)
        async with engine.begin() as connection:
            # Idempotency is part of the production migration contract: a
            # partially applied deployment must be safe to resume.
            await connection.run_sync(_upgrade)

        async with factory() as session:
            columns = await _query_log_columns(session)
            named_id = columns["named_query_id"]
            fallback_reason = columns["named_query_fallback_reason"]
            assert named_id["data_type"] == "uuid"
            assert named_id["is_nullable"] == "YES"
            assert fallback_reason["data_type"] == "character varying"
            assert fallback_reason["character_maximum_length"] == 128
            assert fallback_reason["is_nullable"] == "YES"

            indexes = await _query_log_indexes(session)
            matching = [
                row for row in indexes
                if row["indexname"] == "ix_query_logs_named_query_id"
            ]
            assert len(matching) == 1
            assert "named_query_id" in str(matching[0]["indexdef"])

            foreign_key_count = (
                await session.execute(text(
                    "SELECT count(*) FROM information_schema.table_constraints tc "
                    "JOIN information_schema.key_column_usage kcu "
                    "ON kcu.constraint_schema = tc.constraint_schema "
                    "AND kcu.constraint_name = tc.constraint_name "
                    "WHERE tc.table_schema = current_schema() "
                    "AND tc.table_name = 'query_logs' "
                    "AND tc.constraint_type = 'FOREIGN KEY' "
                    "AND kcu.column_name IN ('named_query_id', "
                    "'named_query_fallback_reason')"
                ))
            ).scalar_one()
            assert foreign_key_count == 0

            historical = (
                await session.execute(text(
                    "SELECT named_query_id, named_query_fallback_reason "
                    "FROM query_logs WHERE id = :id"
                ), {"id": historical_id})
            ).mappings().one()
            assert historical["named_query_id"] is None
            assert historical["named_query_fallback_reason"] is None

        async with engine.begin() as connection:
            await connection.run_sync(_downgrade)

        async with factory() as session:
            after = await _query_log_columns(session)
            assert "named_query_id" not in after
            assert "named_query_fallback_reason" not in after
            assert not any(
                row["indexname"] == "ix_query_logs_named_query_id"
                for row in await _query_log_indexes(session)
            )
            retained = (
                await session.execute(text(
                    "SELECT id FROM query_logs WHERE id = :id"
                ), {"id": historical_id})
            ).scalar_one_or_none()
            assert retained == historical_id
