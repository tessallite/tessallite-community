"""Bug-8602 — the SERVE-TIME source binding, against real PostgreSQL.

Every other test of this hop is either a source-inspection assertion (the
producer and the guard both name ``resolve_source_binding_dict``) or a unit
test that monkeypatches ``current_source_build_binding`` away. Both stop short
of the thing that actually has to hold in production: that a binding WRITTEN by
a build and PERSISTED as ``aggregate_definitions.built_for_source_binding``
JSONB re-derives to the same fingerprint when the query-router reads it back
out of a real database, through real Fernet-encrypted credentials and the real
endpoint-resolution chain.

Both directions are load-bearing and each fails differently:

* MATCH — nothing moved, so the recorded binding must re-prove and the route
  must be ADMITTED. A false refusal here is silent: every aggregate carrying a
  recorded binding would be refused and aggregate acceleration would collapse
  to source with no error anywhere.
* MISMATCH — the source ``ProjectConnection``'s endpoint is edited in place
  (same primary key, aggregate row untouched, ``status`` still active,
  ``is_stale`` still false), so the route must be REFUSED before any scan. This
  is the Bug-8602 defect itself: without the refusal the aggregate keeps
  serving rows materialised from the previous database while the source-route
  fallback for the same query reads the new one.

Test escape: the serve-time leg was covered only by fakes and by static
source-inspection, so a cross-process fingerprint divergence — the exact
failure mode those inspections exist to approximate — would have passed every
gate. Guard: this file. Tier: T1.
"""
from __future__ import annotations

import os
import types
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shared.artifact_target_binding import capture_source_build_binding
from shared.db.models import (
    AggregateDefinition,
    DataSource,
    DataTarget,
    Model,
    ModelTable,
    Project,
    ProjectConnection,
    TenantBase,
)
from shared.security.credential_crypto import encrypt_json

from src.routing.aggregate_generation_guard import (
    AggregateGenerationChangedError,
    assert_aggregate_route_admissible,
    read_aggregate_generation,
)

pytestmark = pytest.mark.integration

_DB_URL = os.environ.get("TESSALLITE_VERSIONING_DB_URL")

_OLD_SOURCE = {"host": "src-old", "port": 5432, "database": "warehouse"}
_NEW_SOURCE = {"host": "src-new", "port": 5432, "database": "warehouse"}


@asynccontextmanager
async def _isolated_schema():
    schema = f"probe8602_{uuid.uuid4().hex[:8]}"
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


class _Ids:
    def __init__(self, suffix: str):
        self.project = uuid.uuid4()
        self.model = uuid.uuid4()
        self.source_conn = uuid.uuid4()
        self.target_conn = uuid.uuid4()
        self.source = uuid.uuid4()
        self.table = uuid.uuid4()
        self.target = uuid.uuid4()
        self.aggregate = uuid.uuid4()
        self.suffix = suffix


async def _seed(session, ids: _Ids):
    session.add(Project(id=ids.project, slug=f"p-{ids.suffix}", display_name="P"))
    await session.flush()
    session.add(ProjectConnection(
        id=ids.source_conn, project_id=ids.project,
        display_name=f"source-{ids.suffix}", connection_type="postgresql",
        encrypted_credentials=encrypt_json({"user": "svc", "password": "p"}),
        config=dict(_OLD_SOURCE),
    ))
    session.add(ProjectConnection(
        id=ids.target_conn, project_id=ids.project,
        display_name=f"target-{ids.suffix}", connection_type="postgresql",
        encrypted_credentials=encrypt_json({"user": "svc", "password": "p"}),
        config={"host": "tgt", "port": 5432, "database": "aggregates"},
    ))
    session.add(Model(
        id=ids.model, project_id=ids.project, slug=f"m-{ids.suffix}",
        display_name="M", seed="seed",
    ))
    session.add(DataSource(
        id=ids.source, model_id=ids.model,
        project_connection_id=ids.source_conn,
        display_name=f"src-{ids.suffix}", source_type="postgresql",
        default_schema="public",
    ))
    session.add(ModelTable(
        id=ids.table, model_id=ids.model, source_id=ids.source,
        physical_name="fact_sales", alias="base", display_name="Sales",
        table_type="fact",
    ))
    session.add(DataTarget(
        id=ids.target, model_id=ids.model,
        project_connection_id=ids.target_conn, target_type="postgresql",
        display_name="Target", config={"schema": "aggregates"},
    ))
    session.add(AggregateDefinition(
        id=ids.aggregate, model_id=ids.model, target_id=ids.target,
        physical_table_name=f"agg_{ids.suffix}", target_schema="aggregates",
        grain=["region"], status="active", is_stale=False,
    ))


def _bound():
    return types.SimpleNamespace(
        model=types.SimpleNamespace(
            id=None, project_id=None, deployed_version_id=None, deploy_epoch=0,
        )
    )


def _decision(ids: _Ids, admitted):
    return types.SimpleNamespace(
        route_type="aggregate",
        aggregate_id=ids.aggregate,
        pocket_id=None,
        rewritten_query=(
            f'SELECT region, SUM(amount) FROM "aggregates"."agg_{ids.suffix}" '
            "GROUP BY region"
        ),
        target_dialect="postgres",
        security_compiled=None,
        admitted_generation=admitted,
    )


@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
@pytest.mark.asyncio
async def test_serve_time_admits_when_the_source_has_not_moved():
    async with _isolated_schema() as factory:
        ids = _Ids("m")
        async with factory() as seed:
            await _seed(seed, ids)
            await seed.commit()

        async with factory() as db:
            conn = (await db.execute(
                select(ProjectConnection)
                .where(ProjectConnection.id == ids.source_conn)
            )).scalar_one()
            binding = await capture_source_build_binding(
                ids.model, conn, tenant_session=db,
            )
            await db.execute(
                update(AggregateDefinition)
                .where(AggregateDefinition.id == ids.aggregate)
                .values(built_for_source_binding=binding.to_dict())
            )
            await db.commit()

        async with factory() as db:
            admitted = await read_aggregate_generation(db, ids.aggregate)
            gen = await assert_aggregate_route_admissible(
                db, bound=_bound(), decision=_decision(ids, admitted),
            )
            assert gen == admitted


@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
@pytest.mark.asyncio
async def test_serve_time_refuses_after_the_source_endpoint_moves():
    async with _isolated_schema() as factory:
        ids = _Ids("x")
        async with factory() as seed:
            await _seed(seed, ids)
            await seed.commit()

        async with factory() as db:
            conn = (await db.execute(
                select(ProjectConnection)
                .where(ProjectConnection.id == ids.source_conn)
            )).scalar_one()
            binding = await capture_source_build_binding(
                ids.model, conn, tenant_session=db,
            )
            await db.execute(
                update(AggregateDefinition)
                .where(AggregateDefinition.id == ids.aggregate)
                .values(built_for_source_binding=binding.to_dict())
            )
            await db.commit()

        async with factory() as db:
            admitted = await read_aggregate_generation(db, ids.aggregate)

        # The admin edits the SOURCE connection in place. Nothing else moves:
        # the aggregate row is untouched, status stays active, is_stale stays
        # false, and the connection keeps its primary key.
        async with factory() as repoint:
            await repoint.execute(
                update(ProjectConnection)
                .where(ProjectConnection.id == ids.source_conn)
                .values(config=dict(_NEW_SOURCE))
            )
            await repoint.commit()

        async with factory() as db:
            with pytest.raises(
                AggregateGenerationChangedError, match="different source database"
            ):
                await assert_aggregate_route_admissible(
                    db, bound=_bound(), decision=_decision(ids, admitted),
                )
