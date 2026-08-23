"""G4/B01 producer proof against a real tenant schema.

The unit seam for source introspection can prove that the helper is called, but
it cannot prove that the join producer's commit leaves the provenance and
participation values visible to a later request.  This probe drives the real
model-service handlers with a real ``AsyncSession`` and closes/reopens the
session after each write.  It deliberately exercises both the positive proof
and every shape that must remain default-owned or manually owned.

Test escape: the earlier guard returned the same ORM objects from an
``AsyncMock`` and therefore could not catch a missing commit, an omitted
producer field, or a new-session read seeing a stale row.  Guard: this file,
including the known-value assertions after a new-session reread.  Tier: T2.
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shared.db.models import (
    DataSource,
    Join,
    Model,
    ModelColumn,
    ModelTable,
    Project,
    ProjectConnection,
    TenantBase,
)
from shared.schemas.pydantic_models import JoinCreate

from src.api.joins import create_join
from src.api.table_attributes import ColumnSyncItem, sync_columns

pytestmark = pytest.mark.integration

_DB_URL = os.environ.get("TESSALLITE_VERSIONING_DB_URL") or os.environ.get(
    "TESSALLITE_IMPORTER_REHYDRATION_DB_URL"
)


@asynccontextmanager
async def _isolated_schema():
    """Yield a real tenant session factory in a disposable PostgreSQL schema."""
    if not _DB_URL:
        pytest.skip("no versioning/importer Postgres URL configured")
    schema = f"g4b01_{uuid.uuid4().hex}"
    boot = create_async_engine(_DB_URL, future=True)
    async with boot.begin() as conn:
        await conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        await conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        await conn.run_sync(TenantBase.metadata.create_all)
    await boot.dispose()

    engine = create_async_engine(_DB_URL, future=True)

    @event.listens_for(engine.sync_engine, "connect")
    def _set_search_path(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute(f'SET search_path TO "{schema}"')
        cursor.close()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        drop = create_async_engine(_DB_URL, future=True)
        async with drop.begin() as conn:
            await conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await drop.dispose()
        await engine.dispose()


def _user() -> SimpleNamespace:
    return SimpleNamespace(
        tenant_id="g4-b01-tenant",
        user_id="g4-b01-user",
        email="g4-b01@example.test",
        raw_token="g4-b01-token",
    )


async def _tenant_db(factory):
    async with factory() as session:
        yield session


async def _seed(factory):
    ids = SimpleNamespace(
        project=uuid.uuid4(), model=uuid.uuid4(), connection=uuid.uuid4(),
        source=uuid.uuid4(), fact=uuid.uuid4(), dim=uuid.uuid4(),
        ambiguous_dim=uuid.uuid4(), fact_fk=uuid.uuid4(), dim_pk=uuid.uuid4(),
        dim_code=uuid.uuid4(), ambiguous_pk=uuid.uuid4(), ambiguous_pk2=uuid.uuid4(),
    )
    async with factory() as db:
        db.add(Project(
            id=ids.project, slug=f"g4-b01-{ids.project.hex[:8]}",
            display_name="G4 B01",
        ))
        db.add(ProjectConnection(
            id=ids.connection, project_id=ids.project,
            display_name="source", connection_type="postgresql",
            encrypted_credentials=b"test", config={"host": "source"},
        ))
        db.add(Model(
            id=ids.model, project_id=ids.project,
            slug=f"model-{ids.model.hex[:8]}", display_name="G4 model",
            seed=ids.model.hex[:16],
        ))
        db.add(DataSource(
            id=ids.source, model_id=ids.model,
            project_connection_id=ids.connection, source_type="postgresql",
            display_name="source", default_schema="public", config={},
        ))
        db.add_all([
            ModelTable(
                id=ids.fact, model_id=ids.model, source_id=ids.source,
                table_type="fact", physical_name="fact_sales", alias="fact",
                display_name="Fact",
            ),
            ModelTable(
                id=ids.dim, model_id=ids.model, source_id=ids.source,
                table_type="dim_detail", physical_name="dim_customer", alias="dim",
                display_name="Dimension",
            ),
            ModelTable(
                id=ids.ambiguous_dim, model_id=ids.model, source_id=ids.source,
                table_type="dim_detail", physical_name="dim_ambiguous", alias="amb",
                display_name="Ambiguous dimension",
            ),
        ])
        db.add_all([
            ModelColumn(
                id=ids.fact_fk, model_table_id=ids.fact,
                column_name="customer_id", data_type="bigint", is_nullable=False,
            ),
            ModelColumn(
                id=ids.dim_pk, model_table_id=ids.dim,
                column_name="id", data_type="bigint", is_nullable=False,
                is_primary_key=True,
            ),
            ModelColumn(
                id=ids.dim_code, model_table_id=ids.dim,
                column_name="code", data_type="text", is_nullable=False,
            ),
            ModelColumn(
                id=ids.ambiguous_pk, model_table_id=ids.ambiguous_dim,
                column_name="id", data_type="bigint", is_nullable=False,
                is_primary_key=True,
            ),
            ModelColumn(
                id=ids.ambiguous_pk2, model_table_id=ids.ambiguous_dim,
                column_name="tenant_id", data_type="bigint", is_nullable=False,
                is_primary_key=True,
            ),
        ])
        await db.commit()
    return ids


async def _create(factory, ids, *, left_table_id, right_table_id,
                  left_column_name, right_column_name,
                  population_participation=None):
    body_values = {
        "left_table_id": left_table_id,
        "right_table_id": right_table_id,
        "join_type": "left",
        "left_column_name": left_column_name,
        "right_column_name": right_column_name,
    }
    if population_participation is not None:
        body_values["population_participation"] = population_participation
    body = JoinCreate.model_validate(body_values)
    async def _db(_tenant):
        async with factory() as db:
            yield db
    # This is the production handler, not a helper or a test double.  Only the
    # tenant-session dependency is supplied so the handler uses the real
    # ownership check, column resolver, lock, commit, and response builder.
    from unittest.mock import patch
    with patch("src.api.joins.get_tenant_db", _db):
        return await create_join(
            ids.project, ids.model, body, current_user=_user()
        )


async def _sync_dimension(factory, ids, table_id):
    async def _db(_tenant):
        async with factory() as db:
            yield db
    from unittest.mock import patch
    with patch("src.api.table_attributes.get_tenant_db", _db):
        await sync_columns(
            ids.project,
            ids.model,
            table_id,
            [ColumnSyncItem(
                column_name="id", data_type="bigint", is_nullable=False,
                is_primary_key=True,
            )],
            current_user=_user(),
        )


@pytest.mark.asyncio
async def test_g4_r2_b01_join_producer_commits_safe_auto_flag_and_preserves_ownership():
    """B01: one real producer commit + one new-session reread for all guards."""
    async with _isolated_schema() as factory:
        ids = await _seed(factory)
        safe = await _create(
            factory, ids, left_table_id=ids.fact, right_table_id=ids.dim,
            left_column_name="customer_id", right_column_name="id",
        )
        manual = await _create(
            factory, ids, left_table_id=ids.fact, right_table_id=ids.dim,
            left_column_name="customer_id", right_column_name="id",
            population_participation="population_defining",
        )
        reversed_join = await _create(
            factory, ids, left_table_id=ids.dim, right_table_id=ids.fact,
            left_column_name="id", right_column_name="customer_id",
        )
        wrong_endpoint = await _create(
            factory, ids, left_table_id=ids.fact, right_table_id=ids.dim,
            left_column_name="customer_id", right_column_name="code",
        )
        ambiguous = await _create(
            factory, ids, left_table_id=ids.fact,
            right_table_id=ids.ambiguous_dim,
            left_column_name="customer_id", right_column_name="id",
        )

        # Source introspection is the real table-attributes producer boundary.
        await _sync_dimension(factory, ids, ids.dim)
        await _sync_dimension(factory, ids, ids.ambiguous_dim)

        async with factory() as db:
            rows = {
                row.id: row
                for row in (
                    await db.execute(select(Join).where(Join.model_id == ids.model))
                ).scalars().all()
            }
            # Omitted/default ownership is the only row eligible for automatic
            # classification.  The manual value, orientation, endpoint, and
            # ambiguous key all survive exactly as persisted.
            assert (
                rows[safe.id].population_participation,
                rows[safe.id].population_participation_source,
            ) == ("preserve_base_rows", "auto")
            assert (
                rows[manual.id].population_participation,
                rows[manual.id].population_participation_source,
            ) == ("population_defining", "manual")
            for row in (rows[reversed_join.id], rows[wrong_endpoint.id], rows[ambiguous.id]):
                assert (
                    row.population_participation,
                    row.population_participation_source,
                ) == ("preserve_base_rows", "default")
