"""Real-Postgres upgrade/downgrade guard for tenant migration 0222.

The static migration test catches revision wiring and named operations.  This
test executes the transition against an isolated schema as well: the legacy
live-join FK is removed, selected evidence can retain an absent live join, and
the downgrade deletes only rows the pre-0222 schema cannot represent before
restoring the named FK.
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

from shared.db.models import JoinPopulationCheck, Model, Project, TenantBase

pytestmark = [pytest.mark.integration]

_DB_URL = os.environ.get("TESSALLITE_VERSIONING_DB_URL") or os.environ.get(
    "TESSALLITE_IMPORTER_REHYDRATION_DB_URL"
)
_MIGRATION = (
    Path(__file__).resolve().parents[4]
    / "shared" / "db" / "migrations" / "versions"
    / "0222_join_population_snapshot_evidence.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "m0222_join_population_snapshot_evidence_db_probe", _MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade(connection) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    ctx = MigrationContext.configure(connection)
    with Operations.context(ctx):
        _load_migration().upgrade()


def _downgrade(connection) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    ctx = MigrationContext.configure(connection)
    with Operations.context(ctx):
        _load_migration().downgrade()


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
    schema = f"m0222_{uuid.uuid4().hex}"
    boot = create_async_engine(_DB_URL, future=True)
    async with boot.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.execute(text(f'SET search_path TO "{schema}"'))
        await conn.run_sync(TenantBase.metadata.create_all)
        # Rewind the current ORM shape to the state immediately before 0222.
        await conn.execute(text(
            'ALTER TABLE "join_population_checks" '
            'DROP COLUMN IF EXISTS "join_label", '
            'DROP COLUMN IF EXISTS "left_table_name", '
            'DROP COLUMN IF EXISTS "right_table_name", '
            'DROP COLUMN IF EXISTS "left_column_name", '
            'DROP COLUMN IF EXISTS "right_column_name"'
        ))
        await conn.execute(text(
            'ALTER TABLE "join_population_checks" '
            'ADD CONSTRAINT "legacy_join_population_checks_join_id_fk" '
            'FOREIGN KEY ("join_id") REFERENCES "joins" ("id") '
            'ON DELETE CASCADE'
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


async def _columns(session) -> set[str]:
    rows = await session.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        "AND table_name = 'join_population_checks'"
    ))
    return {str(row[0]) for row in rows}


async def _join_fks(session) -> list[dict[str, object]]:
    rows = await session.execute(text(
        "SELECT tc.constraint_name, kcu.column_name "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "ON kcu.constraint_schema = tc.constraint_schema "
        "AND kcu.constraint_name = tc.constraint_name "
        "WHERE tc.table_schema = current_schema() "
        "AND tc.table_name = 'join_population_checks' "
        "AND tc.constraint_type = 'FOREIGN KEY' "
        "AND kcu.column_name = 'join_id'"
    ))
    return [dict(row) for row in rows.mappings().all()]


@pytest.mark.asyncio
async def test_0222_upgrade_downgrade_preserves_snapshot_identity_contract():
    if not _DB_URL:
        pytest.skip(
            "TESSALLITE_VERSIONING_DB_URL/importer URL is not configured"
        )

    project_id, model_id = uuid.uuid4(), uuid.uuid4()
    historical_join_id = uuid.uuid4()
    historical_check_id = uuid.uuid4()
    async with _isolated_schema() as (factory, engine):
        async with factory() as session:
            assert "join_label" not in await _columns(session)
            legacy_fks = await _join_fks(session)
            assert legacy_fks == [{
                "constraint_name": "legacy_join_population_checks_join_id_fk",
                "column_name": "join_id",
            }]
            session.add(Project(
                id=project_id, slug=f"m0222-{project_id.hex[:8]}",
                display_name="migration 0222",
            ))
            await session.flush()
            session.add(Model(
                id=model_id, project_id=project_id,
                slug=f"model-{model_id.hex[:8]}",
                display_name="historical evidence",
                seed=model_id.hex,
            ))
            await session.commit()

        async with engine.begin() as connection:
            await connection.run_sync(_upgrade)
        async with engine.begin() as connection:
            # A deployment can resume a partially applied migration safely.
            await connection.run_sync(_upgrade)

        async with factory() as session:
            columns = await _columns(session)
            assert {
                "join_label", "left_table_name", "right_table_name",
                "left_column_name", "right_column_name",
            } <= columns
            assert await _join_fks(session) == []

            # This selected join exists only in the immutable version snapshot;
            # no live ``joins`` row is inserted.  The post-0222 schema must
            # accept and retain its evidence and labels.
            session.add(JoinPopulationCheck(
                id=historical_check_id, join_id=historical_join_id,
                model_id=model_id, deploy_epoch=4,
                classification="neutral",
                population_participation="preserve_base_rows",
                status="OK", measured=True,
                row_loss_ratio=0.0, row_mult_ratio=0.0,
                row_effect_ratio=0.0, reason="measured",
                inputs_fingerprint="historical-fingerprint",
                join_label="Fact.customer_id ↔ Customer.id",
                left_table_name="Fact", right_table_name="Customer",
                left_column_name="customer_id", right_column_name="id",
            ))
            await session.commit()

            retained = await session.get(JoinPopulationCheck, historical_check_id)
            assert retained is not None
            assert retained.join_id == historical_join_id
            assert retained.join_label == "Fact.customer_id ↔ Customer.id"
            assert retained.inputs_fingerprint == "historical-fingerprint"

        async with engine.begin() as connection:
            await connection.run_sync(_downgrade)

        async with factory() as session:
            assert "join_label" not in await _columns(session)
            assert "left_table_name" not in await _columns(session)
            assert await _join_fks(session) == [{
                "constraint_name": "join_population_checks_join_id_fkey",
                "column_name": "join_id",
            }]
            # Downgrade must clean the orphan before restoring the old FK; the
            # old schema has no columns with which to preserve it safely.
            remaining = await session.execute(text(
                "SELECT id FROM join_population_checks WHERE id = :id"
            ), {"id": historical_check_id})
            assert remaining.scalar_one_or_none() is None
