"""DB-backed importer rehydration round-trip (F-020-E2).

Each ecosystem importer snapshot is rehydrated through the real
``prepare_snapshot_for_import`` + ``rehydrate_into_live`` path against a
throwaway Postgres schema, proving the importers emit ORM-aligned snapshots
that land as live rows. Skipped unless TESSALLITE_IMPORTER_REHYDRATION_DB_URL
points at a reachable Postgres.

Run:
    cd tessallite/services/model-service
    TESSALLITE_IMPORTER_REHYDRATION_DB_URL=postgresql+asyncpg://user:pw@localhost:5432/db \
      pytest tests/integration/test_importer_rehydration_harness.py -v
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from shared.db.models import (
    DataSource,
    Dimension,
    Measure,
    Model,
    ModelColumn,
    ModelTable,
    Project,
    ProjectConnection,
    TenantBase,
)
from shared.model_snapshot.importer import prepare_snapshot_for_import
from shared.model_snapshot.rehydrator import rehydrate_into_live
from tests.importer_rehydration_harness import (
    ImporterRehydrationCase,
    importer_rehydration_cases,
)

pytestmark = [pytest.mark.integration]

DB_URL_ENV = "TESSALLITE_IMPORTER_REHYDRATION_DB_URL"


@asynccontextmanager
async def _isolated_schema_session(db_url: str) -> AsyncIterator[AsyncSession]:
    schema = f"importer_rehydration_{uuid.uuid4().hex}"
    engine = create_async_engine(db_url, future=True)

    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.execute(text(f'SET search_path TO "{schema}"'))
        await conn.run_sync(TenantBase.metadata.create_all)

    @event.listens_for(engine.sync_engine, "connect")
    def _set_search_path(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f'SET search_path TO "{schema}"')
        cursor.close()

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            yield session
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def _seed_project(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    project_id = uuid.uuid4()
    connection_id = uuid.uuid4()
    session.add(
        Project(
            id=project_id,
            slug=f"importer-harness-{project_id.hex[:8]}",
            display_name="Importer Harness",
        )
    )
    session.add(
        ProjectConnection(
            id=connection_id,
            project_id=project_id,
            display_name="Harness Source",
            connection_type="postgresql",
            encrypted_credentials=b"test",
            config={},
        )
    )
    await session.flush()
    return project_id, connection_id


async def _rehydrate_case(
    session: AsyncSession,
    case: ImporterRehydrationCase,
    project_id: uuid.UUID,
    connection_id: uuid.UUID,
) -> uuid.UUID:
    snapshot = case.build_snapshot()
    if case.inject_project_connection:
        for source in snapshot.get("data_sources", []) or []:
            source.setdefault("project_connection_id", str(connection_id))

    new_model_id = uuid.uuid4()
    rewritten, _missing = prepare_snapshot_for_import(
        snapshot,
        new_model_id=new_model_id,
    )
    slug = rewritten.get("model", {}).get("slug") or case.name
    session.add(
        Model(
            id=new_model_id,
            project_id=project_id,
            slug=slug,
            display_name=rewritten.get("model", {}).get("display_name") or slug,
            seed=str(uuid.uuid4()),
        )
    )
    await session.flush()

    await rehydrate_into_live(
        new_model_id,
        rewritten,
        session,
        drop_orphan_aggregates=False,
        actor="importer-rehydration-harness",
        force_aggregate_pending=True,
        force_pocket_stale=True,
    )
    await session.flush()
    return new_model_id


async def _count(session: AsyncSession, model_cls, *criteria) -> int:
    result = await session.execute(
        select(func.count()).select_from(model_cls).where(*criteria)
    )
    return int(result.scalar_one())


@pytest.mark.parametrize(
    "case",
    importer_rehydration_cases(),
    ids=lambda case: case.name,
)
async def test_importer_snapshot_rehydrates_against_postgres(
    case: ImporterRehydrationCase,
):
    db_url = os.environ.get(DB_URL_ENV)
    if not db_url:
        pytest.skip(f"Set {DB_URL_ENV} to run DB-backed importer rehydration tests")

    async with _isolated_schema_session(db_url) as session:
        project_id, connection_id = await _seed_project(session)

        model_id = await _rehydrate_case(
            session,
            case,
            project_id,
            connection_id,
        )

        assert await _count(session, DataSource, DataSource.model_id == model_id) >= 1
        assert await _count(session, ModelTable, ModelTable.model_id == model_id) >= 1

        table_ids = select(ModelTable.id).where(ModelTable.model_id == model_id)
        assert await _count(
            session,
            ModelColumn,
            ModelColumn.model_table_id.in_(table_ids),
        ) >= 1
        assert (
            await _count(session, Dimension, Dimension.model_id == model_id)
            + await _count(session, Measure, Measure.model_id == model_id)
        ) >= 1
