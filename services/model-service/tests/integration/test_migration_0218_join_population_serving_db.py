"""Executable Bug-8621/U01 proof for migration 0218.

The unit checks for revision 0218 prove that the migration names the shared
predicate.  This test exercises the actual state transition through Alembic's
``Operations`` context: only deployed models advance, every incompatible
active/fresh artifact family is made non-servable with the same G3 reason, and
disabled/retired/undeployed rows are not promoted.  ``downgrade`` is also
executed and must not restore trust.

The test uses the existing isolated-tenant migration harness and is skipped
when no versioning/importer Postgres URL is configured.
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
    / "0218_join_population_serving_contract_v1_reset.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("m0218_join_population_probe", _MIGRATION)
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
    schema = f"m0218_{uuid.uuid4().hex}"
    boot = create_async_engine(_DB_URL, future=True)
    async with boot.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.execute(text(f'SET search_path TO "{schema}"'))
        await conn.run_sync(TenantBase.metadata.create_all)
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


async def _seed(session) -> dict[str, object]:
    """Seed deployed and undeployed models plus all three artifact families."""
    tables = TenantBase.metadata.tables
    project_id = uuid.uuid4()
    connection_id = uuid.uuid4()
    deployed_model_id = uuid.uuid4()
    undeployed_model_id = uuid.uuid4()
    deployed_version_id = uuid.uuid4()
    undeployed_version_id = uuid.uuid4()
    deployed_target_id = uuid.uuid4()
    undeployed_target_id = uuid.uuid4()
    named_query_ids = {
        "deployed": uuid.uuid4(),
        "undeployed": uuid.uuid4(),
    }

    await session.execute(tables["projects"].insert().values(
        id=project_id, slug=f"p-{project_id.hex[:8]}", display_name="U01",
    ))
    await session.execute(tables["project_connections"].insert().values(
        id=connection_id, project_id=project_id, display_name="C",
        connection_type="postgresql", encrypted_credentials=b"x", config={},
    ))
    for model_id, slug, version_id, epoch in (
        (deployed_model_id, "deployed", deployed_version_id, 10),
        (undeployed_model_id, "undeployed", None, 20),
    ):
        await session.execute(tables["models"].insert().values(
            id=model_id, project_id=project_id, slug=slug,
            display_name=slug.title(), seed=uuid.uuid4().hex,
            deployed_version_id=version_id, deploy_epoch=epoch,
        ))
    for model_id, target_id, label in (
        (deployed_model_id, deployed_target_id, "D"),
        (undeployed_model_id, undeployed_target_id, "U"),
    ):
        source_id = uuid.uuid4()
        await session.execute(tables["data_sources"].insert().values(
            id=source_id, model_id=model_id, project_connection_id=connection_id,
            source_type="postgresql", display_name=f"{label} source", config={},
        ))
        await session.execute(tables["data_targets"].insert().values(
            id=target_id, model_id=model_id, project_connection_id=connection_id,
            target_type="postgresql", display_name=f"{label} target", config={},
        ))
    await session.execute(tables["model_versions"].insert().values(
        id=deployed_version_id, model_id=deployed_model_id, version_number=1,
        snapshot_json={"joins": []}, created_by="u01",
    ))
    await session.execute(tables["model_versions"].insert().values(
        id=undeployed_version_id, model_id=undeployed_model_id, version_number=1,
        snapshot_json={"joins": []}, created_by="u01",
    ))

    aggregate_ids: dict[str, uuid.UUID] = {}
    for key, status, stale, retired_at, version_id, epoch in (
        ("active-compatible", "active", False, None, deployed_version_id, 10),
        ("active-null", "active", False, None, None, None),
        ("retired", "active", False, True, deployed_version_id, 10),
        ("disabled", "disabled", False, None, deployed_version_id, 10),
        ("already-stale", "active", True, None, deployed_version_id, 9),
        ("undeployed", "active", False, None, undeployed_version_id, 20),
    ):
        aggregate_id = uuid.uuid4()
        aggregate_ids[key] = aggregate_id
        retired_value = text("now()") if retired_at else None
        values = {
            "id": aggregate_id,
            "model_id": undeployed_model_id if key == "undeployed" else deployed_model_id,
            "target_id": undeployed_target_id if key == "undeployed" else deployed_target_id,
            "physical_table_name": f"agg_{key.replace('-', '_')}",
            "status": status, "is_stale": stale, "grain": [],
            "built_for_version_id": version_id, "built_for_epoch": epoch,
        }
        if retired_value is not None:
            values["retired_at"] = retired_value
        await session.execute(tables["aggregate_definitions"].insert().values(**values))

    pocket_ids: dict[str, uuid.UUID] = {}
    for key, status, retired_at, version_id, epoch in (
        ("active-compatible", "fresh", None, deployed_version_id, 10),
        ("active-null", "fresh", None, None, None),
        ("retired", "fresh", text("now()"), deployed_version_id, 10),
        ("undeployed", "fresh", None, undeployed_version_id, 20),
    ):
        pocket_id = uuid.uuid4()
        pocket_ids[key] = pocket_id
        values = {
            "id": pocket_id,
            "model_id": undeployed_model_id if key == "undeployed" else deployed_model_id,
            "target_id": undeployed_target_id if key == "undeployed" else deployed_target_id,
            "physical_table_name": f"pocket_{key.replace('-', '_')}",
            "defining_sql": "SELECT 1", "query_fingerprint": uuid.uuid4().hex,
            "predicate_set_hash": uuid.uuid4().hex, "status": status,
            "built_for_version_id": version_id, "built_for_epoch": epoch,
        }
        if retired_at is not None:
            values["retired_at"] = retired_at
        await session.execute(tables["pocket_definitions"].insert().values(**values))

    for key, model_id, target_id, version_id, epoch, retired_at in (
        ("active-compatible", deployed_model_id, deployed_target_id, deployed_version_id, 10, None),
        ("active-null", deployed_model_id, deployed_target_id, None, None, None),
        ("retired", deployed_model_id, deployed_target_id, deployed_version_id, 10, text("now()")),
        ("undeployed", undeployed_model_id, undeployed_target_id, undeployed_version_id, 20, None),
    ):
        nq_id = named_query_ids["undeployed" if key == "undeployed" else "deployed"]
        if key != "undeployed" and key != "active-compatible":
            nq_id = uuid.uuid4()
        await session.execute(tables["named_queries"].insert().values(
            id=nq_id, model_id=model_id, name=f"nq_{key.replace('-', '_')}",
            definition_sql="SELECT 1", shape="projection",
        ))
        values = {
            "id": uuid.uuid4(), "named_query_id": nq_id, "target_id": target_id,
            "physical_table_name": f"nq_{key.replace('-', '_')}",
            "status": "fresh", "built_for_version_id": version_id,
            "built_for_epoch": epoch,
        }
        if retired_at is not None:
            values["retired_at"] = retired_at
        await session.execute(tables["named_query_artifacts"].insert().values(**values))

    await session.commit()
    return {
        "deployed_model_id": deployed_model_id,
        "undeployed_model_id": undeployed_model_id,
        "deployed_version_id": deployed_version_id,
        "aggregate_ids": aggregate_ids,
        "pocket_ids": pocket_ids,
    }


async def _state(session, ids: dict[str, object]) -> dict[str, list[dict]]:
    deployed_model_id = ids["deployed_model_id"]
    undeployed_model_id = ids["undeployed_model_id"]
    rows = {}
    rows["models"] = [dict(row) for row in (
        await session.execute(text(
            "SELECT id, deployed_version_id, deploy_epoch FROM models "
            "WHERE id IN (:deployed, :undeployed) ORDER BY id"
        ), {"deployed": deployed_model_id, "undeployed": undeployed_model_id})
    ).mappings().all()]
    for name, table, model_column in (
        ("aggregates", "aggregate_definitions", "model_id"),
        ("pockets", "pocket_definitions", "model_id"),
    ):
        stale_column = ", is_stale" if name == "aggregates" else ""
        reason_column = "invalid_reason" if name == "aggregates" else "failure_reason"
        rows[name] = [dict(row) for row in (
            await session.execute(text(
                f"SELECT model_id, physical_table_name, status{stale_column}, "
                f"retired_at, built_for_version_id, built_for_epoch, "
                f"{reason_column} AS reason FROM {table} "
                f"WHERE model_id IN (:deployed, :undeployed) "
                f"ORDER BY model_id, physical_table_name"
            ), {"deployed": deployed_model_id, "undeployed": undeployed_model_id})
        ).mappings().all()]
    rows["named_queries"] = [dict(row) for row in (
        await session.execute(text(
            "SELECT n.model_id, a.physical_table_name, a.status, a.retired_at, "
            "a.built_for_version_id, a.built_for_epoch, a.failure_reason AS reason "
            "FROM named_query_artifacts a JOIN named_queries n "
            "ON n.id = a.named_query_id WHERE n.model_id IN (:deployed, :undeployed) "
            "ORDER BY n.model_id, a.physical_table_name"
        ), {"deployed": deployed_model_id, "undeployed": undeployed_model_id})
    ).mappings().all()]
    return rows


@pytest.mark.asyncio
async def test_0218_upgrade_invalidates_deployed_families_and_downgrade_never_retrusts():
    if not _DB_URL:
        pytest.skip("TESSALLITE_VERSIONING_DB_URL/importer URL is not configured")

    async with _isolated_schema() as (factory, engine):
        async with factory() as session:
            ids = await _seed(session)
            before = await _state(session, ids)
        async with engine.begin() as connection:
            await connection.run_sync(_upgrade)
        async with factory() as session:
            after_upgrade = await _state(session, ids)
            deployed_model = next(
                row for row in after_upgrade["models"]
                if str(row["id"]) == str(ids["deployed_model_id"])
            )
            undeployed_model = next(
                row for row in after_upgrade["models"]
                if str(row["id"]) == str(ids["undeployed_model_id"])
            )
            assert deployed_model["deploy_epoch"] == 11
            assert undeployed_model["deploy_epoch"] == 20

            for family in ("aggregates", "pockets", "named_queries"):
                deployed_rows = [
                    row for row in after_upgrade[family]
                    if str(row["model_id"]) == str(ids["deployed_model_id"])
                ]
                assert deployed_rows
                prefix = {
                    "aggregates": "agg",
                    "pockets": "pocket",
                    "named_queries": "nq",
                }[family]
                for row in deployed_rows:
                    table_name = row["physical_table_name"]
                    should_stale = table_name in {
                        f"{prefix}_active_compatible",
                        f"{prefix}_active_null",
                    }
                    if should_stale:
                        assert row["reason"] and "G3 join-population" in row["reason"]
                        if family == "aggregates":
                            assert row["is_stale"] is True
                        else:
                            assert row["status"] == "stale"
                    else:
                        # Disabled/retired/already-stale artifacts are not
                        # promoted or made fresh by the migration.
                        assert row["status"] not in {"fresh"} or row["retired_at"] is not None

                undeployed_rows = [
                    row for row in after_upgrade[family]
                    if str(row["model_id"]) == str(ids["undeployed_model_id"])
                ]
                assert undeployed_rows
                assert undeployed_rows == [
                    row for row in before[family]
                    if str(row["model_id"]) == str(ids["undeployed_model_id"])
                ]

        async with engine.begin() as connection:
            await connection.run_sync(_downgrade)
        async with factory() as session:
            after_downgrade = await _state(session, ids)
            assert after_downgrade == after_upgrade
