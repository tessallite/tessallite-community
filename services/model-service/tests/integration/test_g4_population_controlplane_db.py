"""G4/B02 persistence proof through model-service control-plane writers.

The matcher/refresh integration probe proves the serving side, but a stale
physical row can still come back if one of the durable writers forgets to
clear its build or population fields.  This probe calls the production pocket
definition PATCH, target PATCH, and version-revert handlers with a real
PostgreSQL session, then closes/reopens before asserting the exact state.

Test escape: mocked route tests and same-session ORM assertions cannot catch a
missing commit, a target invalidator that misses a pocket, or revert state
that is only changed in memory.  Guard: the new-session known-value checks in
this file.  Tier: T3.
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shared.auth.middleware import CurrentUser
from shared.db.models import (
    DataSource,
    DataTarget,
    Model,
    ModelVersion,
    PocketDefinition,
    PocketRefreshRun,
    Project,
    ProjectConnection,
    TenantBase,
)
from shared.schemas.pydantic_models import (
    DataTargetUpdate,
    PocketDefinitionUpdate,
)

from src.api.pockets import update_pocket
from src.api.targets import update_target
from src.api.versions import RevertBody, revert_to_version

pytestmark = pytest.mark.integration

_DB_URL = os.environ.get("TESSALLITE_VERSIONING_DB_URL") or os.environ.get(
    "TESSALLITE_IMPORTER_REHYDRATION_DB_URL"
)


@asynccontextmanager
async def _isolated_schema():
    if not _DB_URL:
        pytest.skip("no versioning/importer Postgres URL configured")
    schema = f"g4b02ctl_{uuid.uuid4().hex}"
    boot = create_async_engine(_DB_URL, future=True)
    async with boot.begin() as conn:
        await conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        await conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        await conn.run_sync(TenantBase.metadata.create_all)
    await boot.dispose()

    engine = create_async_engine(_DB_URL, future=True)

    # ``AsyncSession`` returns pooled connections between the three production
    # handlers below.  Set the disposable search path at checkout as well as
    # first connect so a pool reset cannot silently fall back to ``public``.
    @event.listens_for(engine.sync_engine, "checkout")
    def _set_search_path(dbapi_connection, _record, _proxy):  # noqa: ANN001
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


def _user() -> CurrentUser:
    return CurrentUser(
        user_id="g4-b02-admin",
        tenant_id="g4-b02-tenant",
        email="g4-b02-admin@example.test",
        role="tenant_admin",
        roles=["tenant_admin"],
        raw_token="g4-b02-token",
    )


async def _tenant_db(factory, tenant_id):
    async with factory() as session:
        yield session


async def _seed(factory):
    ids = SimpleNamespace(
        project=uuid.uuid4(), model=uuid.uuid4(), connection=uuid.uuid4(),
        source=uuid.uuid4(), target=uuid.uuid4(), pocket=uuid.uuid4(),
        version_a=uuid.uuid4(), version_b=uuid.uuid4(),
        refresh_run=uuid.uuid4(),
    )
    async with factory() as db:
        db.add(Project(
            id=ids.project, slug=f"g4b02ctl-{ids.project.hex[:8]}",
            display_name="G4 B02 control plane",
        ))
        db.add(ProjectConnection(
            id=ids.connection, project_id=ids.project,
            display_name="target connection", connection_type="postgresql",
            encrypted_credentials=b"test", config={"host": "control-plane"},
        ))
        model = Model(
            id=ids.model, project_id=ids.project,
            slug=f"g4b02ctlmodel-{ids.model.hex[:8]}",
            display_name="G4 B02 control model", seed=ids.model.hex[:16],
            deploy_epoch=4,
        )
        db.add(model)
        db.add(DataSource(
            id=ids.source, model_id=ids.model,
            project_connection_id=ids.connection, source_type="postgresql",
            display_name="source", default_schema="public", config={},
        ))
        db.add(DataTarget(
            id=ids.target, model_id=ids.model,
            project_connection_id=ids.connection, target_type="postgresql",
            display_name="target", config={"schema": "pockets_a"},
        ))
        await db.flush()
        model.target_id = ids.target
        version_a = ModelVersion(
            id=ids.version_a, model_id=ids.model, version_number=1,
            snapshot_json={}, created_by="g4-b02-admin",
        )
        version_b = ModelVersion(
            id=ids.version_b, model_id=ids.model, version_number=2,
            snapshot_json={}, created_by="g4-b02-admin",
        )
        db.add_all([version_a, version_b])
        await db.flush()
        model.deployed_version_id = ids.version_a
        pocket = PocketDefinition(
            id=ids.pocket, model_id=ids.model, target_id=ids.target,
            physical_table_name="slice_a", target_schema="pockets_a",
            defining_sql="SELECT * FROM g4b02ctlmodel /* slice A */",
            query_fingerprint="slice-a-fingerprint",
            predicate_set_hash="slice-a-predicates", status="fresh",
            population_eligibility="eligible",
            population_proof_fingerprint="slice-a-fingerprint",
            row_manifest={
                "columns": ["id"],
                "build_refresh_run_id": str(ids.refresh_run),
            }, row_count=3, storage_bytes=300,
            built_for_version_id=ids.version_a, built_for_epoch=4,
            last_refresh_at=datetime.now(timezone.utc),
        )
        db.add(pocket)
        await db.flush()
        db.add(PocketRefreshRun(
            id=ids.refresh_run, pocket_definition_id=ids.pocket,
            refresh_mode="full", status="completed", triggered_by="g4-b02",
            completed_at=datetime.now(timezone.utc), rows_written=3,
            bytes_processed=300,
        ))
        await db.flush()
        pocket.active_refresh_run_id = ids.refresh_run
        await db.commit()
    return ids


def _router_validation(model_slug: str, fingerprint: str) -> dict:
    return {
        "ok": True,
        "errors": [],
        "select_star": True,
        "from_tables": [model_slug],
        "has_complex_sql": False,
        "has_unresolvable_where": False,
        "grain": [],
        "query_fingerprint": fingerprint,
        "filters": [],
    }


@pytest.mark.asyncio
async def test_g4_r2_b02_production_writers_with_new_session_state():
    """B02: definition edit, target move, and revert all withdraw old rows."""
    async with _isolated_schema() as factory:
        ids = await _seed(factory)
        user = _user()

        # The production definition writer must make slice B a new stale
        # generation, while retaining the old physical cardinality only as a
        # non-serving historical value until a real rebuild replaces it.
        async def _db(_tenant):
            async with factory() as db:
                yield db

        async with factory() as db:
            model = await db.get(Model, ids.model)
            model_slug = model.slug

        with (
            patch("src.api.pockets.get_tenant_db", _db),
            patch(
                "src.api.pockets._validate_via_router",
                AsyncMock(return_value=_router_validation(model_slug, "slice-b-fingerprint")),
            ),
        ):
            await update_pocket(
                ids.project, ids.model, ids.pocket,
                PocketDefinitionUpdate(
                    defining_sql=f"SELECT * FROM {model_slug} /* slice B */",
                ),
                current_user=user,
            )

        async with factory() as db:
            pocket = await db.get(PocketDefinition, ids.pocket)
            assert (
                pocket.status, pocket.population_eligibility,
                pocket.population_proof_fingerprint, pocket.row_manifest,
                pocket.built_for_version_id, pocket.built_for_epoch,
                pocket.row_count,
            ) == (
                "stale", "unknown", None, None, None, None, 3,
            )
            assert pocket.active_refresh_run_id is None

        # Restore an explicitly built A generation so the production target
        # invalidator, rather than the prior definition edit, is the writer
        # under test in the next step.
        async with factory() as db:
            pocket = await db.get(PocketDefinition, ids.pocket)
            pocket.status = "fresh"
            pocket.population_eligibility = "eligible"
            pocket.population_proof_fingerprint = "slice-a-fingerprint"
            pocket.row_manifest = {"columns": ["id"]}
            pocket.active_refresh_run_id = ids.refresh_run
            pocket.built_for_version_id = ids.version_a
            pocket.built_for_epoch = 4
            pocket.defining_sql = f"SELECT * FROM {model_slug} /* slice A */"
            pocket.query_fingerprint = "slice-a-fingerprint"
            pocket.predicate_set_hash = "slice-a-predicates"
            await db.commit()

        async with factory() as db:
            pocket = await db.get(PocketDefinition, ids.pocket)
            assert pocket.active_refresh_run_id == ids.refresh_run

        with patch("src.api.targets.get_tenant_db", _db):
            await update_target(
                ids.project, ids.model, ids.target,
                DataTargetUpdate(config={"schema": "pockets_b"}),
                current_user=user,
            )

        async with factory() as db:
            pocket = await db.get(PocketDefinition, ids.pocket)
            target = await db.get(DataTarget, ids.target)
            assert (
                target.config,
                pocket.status, pocket.population_eligibility,
                pocket.failure_reason, pocket.row_manifest,
                pocket.built_for_version_id, pocket.built_for_epoch,
            ) == (
                {"schema": "pockets_b"}, "stale", "unknown",
                "The target's storage location changed (connection, connector type or config), "
                "so this cache was built on a different database and must be rebuilt before it can serve again.",
                None, None, None,
            )
            assert pocket.active_refresh_run_id is None

        # Re-establish the exact A binding, then call the production revert
        # route.  The heavy snapshot/git/network side effects are isolated, but
        # the route's authorization, version append, deploy pointer/epoch move,
        # and _stale_incompatible_artifacts SQL all run against this schema.
        async with factory() as db:
            model = await db.get(Model, ids.model)
            model.deploy_epoch = 4
            model.deployed_version_id = ids.version_a
            pocket = await db.get(PocketDefinition, ids.pocket)
            pocket.status = "fresh"
            pocket.failure_reason = None
            pocket.population_eligibility = "eligible"
            pocket.population_eligibility_reason = None
            pocket.population_proof_fingerprint = "slice-a-fingerprint"
            pocket.row_manifest = {"columns": ["id"]}
            pocket.active_refresh_run_id = ids.refresh_run
            pocket.built_for_version_id = ids.version_a
            pocket.built_for_epoch = 4
            await db.commit()

        async with factory() as db:
            pocket = await db.get(PocketDefinition, ids.pocket)
            assert pocket.active_refresh_run_id == ids.refresh_run

        import src.api.versions as versions

        async def _noop(*_args, **_kwargs):
            return None

        with (
            patch("src.api.versions.get_tenant_db", _db),
            patch.object(versions, "rehydrate_into_live", new=AsyncMock()),
            patch.object(versions, "audit", new=AsyncMock()),
            patch.object(versions, "_evict_query_router_cache", new=AsyncMock()),
            patch.object(versions, "emit_webhook", new=AsyncMock()),
            patch.object(versions, "trigger_predictive_cold_start", new=_noop),
            patch.object(versions.asyncio, "to_thread", new=AsyncMock()),
        ):
            result = await revert_to_version(
                ids.project, ids.model, ids.version_b,
                RevertBody(confirm=str(ids.version_b)),
                current_user=user,
            )
        assert result["status"] == "ok"

        async with factory() as db:
            model = await db.get(Model, ids.model)
            pocket = await db.get(PocketDefinition, ids.pocket)
            assert model.deployed_version_id not in {ids.version_a, ids.version_b}
            assert model.deploy_epoch == 5
            assert (
                pocket.status, pocket.failure_reason,
                pocket.population_eligibility,
                pocket.population_eligibility_reason,
                pocket.population_proof_fingerprint,
                pocket.row_manifest, pocket.active_refresh_run_id,
                pocket.built_for_version_id, pocket.built_for_epoch,
            ) == (
                "stale", "Model definition changed; pocket rebuild required.",
                "unknown", None, None, None, None, None, None,
            )
