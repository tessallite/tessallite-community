"""Real-Postgres proofs for Named Query failure recovery and lock ordering."""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shared.db.models import TenantBase
from shared.db.model_lock import acquire_model_definition_lock
from shared.model_snapshot.cascade_delete import delete_model_cascade
from shared.model_snapshot.snapshot_owned_tables import derive, reset_cache
from shared.named_query.refresh import _recover_failed_refresh_state

from tests.integration.test_cascade_delete_model_lock_db import (
    _DB_URL,
    _seed_model_with_named_query,
    _seed_project,
)

pytestmark = [pytest.mark.integration]


@asynccontextmanager
async def _persistent_isolated_schema() -> AsyncIterator[tuple[async_sessionmaker, str]]:
    """Use a server-level search path so rollback-first recovery keeps the schema.

    The shared consistency fixture sets ``search_path`` inside its connection
    event transaction. PostgreSQL correctly rolls that setting back with an
    aborted transaction, which is exactly the state Bug-9193 exercises. The
    production service uses a server/session-level tenant search path, so this
    proof uses asyncpg's ``server_settings`` to model that boundary faithfully.
    """
    schema = f"named_query_recovery_{uuid.uuid4().hex}"
    boot = create_async_engine(_DB_URL, future=True)
    async with boot.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.execute(text(f'SET search_path TO "{schema}"'))
        await conn.run_sync(TenantBase.metadata.create_all)
    await boot.dispose()

    engine = create_async_engine(
        _DB_URL,
        future=True,
        connect_args={"server_settings": {"search_path": schema}},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory, schema
    finally:
        drop = create_async_engine(_DB_URL, future=True)
        async with drop.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await drop.dispose()
        await engine.dispose()


async def _abort_transaction(session) -> None:
    """Leave a real asyncpg transaction aborted, as a failed metadata write does."""
    with pytest.raises(DBAPIError):
        await session.execute(text("SELECT * FROM l10_missing_metadata_table"))


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_bug9193_aborted_refresh_session_recovers_failure_without_recreation():
    """Rollback-first recovery stamps existing rows and never recreates deleted ones."""
    async with _persistent_isolated_schema() as (factory, _schema):
        async with factory() as seed:
            project_id, connection_id = await _seed_project(seed)
            model_id, _target_id, artifact_id, named_query_id, run_id = (
                await _seed_model_with_named_query(
                    seed, project_id, connection_id, "aborted"
                )
            )
            await seed.execute(
                text(
                    "UPDATE named_query_refresh_runs SET status = 'running' "
                    "WHERE id = :rid"
                ),
                {"rid": run_id},
            )
            await seed.execute(
                text(
                    "UPDATE named_query_artifacts SET status = 'invalidating' "
                    "WHERE id = :aid"
                ),
                {"aid": artifact_id},
            )
            await seed.commit()

        async with factory() as refresh_db:
            await _abort_transaction(refresh_db)
            recovered = await _recover_failed_refresh_state(
                refresh_db,
                named_query_id=named_query_id,
                model_id=model_id,
                run_id=run_id,
                artifact_id=artifact_id,
                reason="metadata connection aborted",
            )
            assert recovered is not None
            assert recovered.id == run_id
            assert recovered.status == "failed"

        async with factory() as check:
            run_status = await check.execute(
                text("SELECT status FROM named_query_refresh_runs WHERE id = :rid"),
                {"rid": run_id},
            )
            artifact_status = await check.execute(
                text("SELECT status FROM named_query_artifacts WHERE id = :aid"),
                {"aid": artifact_id},
            )
            assert run_status.scalar_one() == "failed"
            assert artifact_status.scalar_one() == "failed"

        # A concurrent model delete wins by identity; recovery must not recreate
        # either row after the owner has disappeared.
        async with factory() as deleter:
            errors = await delete_model_cascade(deleter, model_id)
            assert errors == [], errors
            await deleter.commit()
        async with factory() as refresh_db:
            await _abort_transaction(refresh_db)
            assert (
                await _recover_failed_refresh_state(
                    refresh_db,
                    named_query_id=named_query_id,
                    model_id=model_id,
                    run_id=run_id,
                    artifact_id=artifact_id,
                    reason="deleted concurrently",
                )
                is None
            )
        reset_cache()
        _tables, unresolved = derive()
        assert unresolved == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "holder_kind", ["deploy", "revert", "model_delete"],
)
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_bug9193_refresh_recovery_waits_for_definition_holders(holder_kind):
    """Refresh failure writes wait behind deploy/revert/delete's real lock."""
    async with _persistent_isolated_schema() as (factory, _schema):
        async with factory() as seed:
            project_id, connection_id = await _seed_project(seed)
            model_id, _target_id, artifact_id, named_query_id, run_id = (
                await _seed_model_with_named_query(
                    seed, project_id, connection_id, holder_kind
                )
            )
            await seed.execute(
                text(
                    "UPDATE named_query_refresh_runs SET status = 'running' "
                    "WHERE id = :rid"
                ),
                {"rid": run_id},
            )
            await seed.execute(
                text(
                    "UPDATE named_query_artifacts SET status = 'invalidating' "
                    "WHERE id = :aid"
                ),
                {"aid": artifact_id},
            )
            await seed.commit()

        holder = factory()
        refresh_db = factory()
        recovery = None
        try:
            # These are the same shared transaction advisory-lock holders used
            # by deploy, revert, and the canonical model-delete primitive.
            await acquire_model_definition_lock(holder, model_id)
            await _abort_transaction(refresh_db)
            recovery = asyncio.create_task(
                _recover_failed_refresh_state(
                    refresh_db,
                    named_query_id=named_query_id,
                    model_id=model_id,
                    run_id=run_id,
                    artifact_id=artifact_id,
                    reason=f"holder={holder_kind}",
                )
            )
            await asyncio.sleep(0.25)
            assert not recovery.done(), (
                f"refresh failure metadata write crossed the {holder_kind} "
                "model-definition lock before the holder released it"
            )
            run_status = await holder.execute(
                text("SELECT status FROM named_query_refresh_runs WHERE id = :rid"),
                {"rid": run_id},
            )
            artifact_status = await holder.execute(
                text("SELECT status FROM named_query_artifacts WHERE id = :aid"),
                {"aid": artifact_id},
            )
            assert run_status.scalar_one() == "running"
            assert artifact_status.scalar_one() == "invalidating"

            await holder.rollback()
            recovered = await asyncio.wait_for(recovery, timeout=30)
            assert recovered is not None
            assert recovered.status == "failed"
        finally:
            if recovery is not None and not recovery.done():
                recovery.cancel()
                await asyncio.gather(recovery, return_exceptions=True)
            await holder.rollback()
            await refresh_db.rollback()
            await holder.close()
            await refresh_db.close()
