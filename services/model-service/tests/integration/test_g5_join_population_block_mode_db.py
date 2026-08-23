"""G5 deployment refusal against real tenant and system database sessions.

This is the permanent T3 guard for the policy flip.  It drives the production
deploy handler with real ORM rows, a real system-level threshold override, and
the real deploy-time validator (only the source probe is deterministic).  The
refusal is asserted together with the rollback boundary: the old pointer,
epoch, evidence, artifact state, audit ledger, and KPI outbox all remain
unchanged, and no publish side-effect hook is reached.

Test escape: the prior route guards used an ``AsyncMock`` session and asserted
only a status code, so they could not catch a staged evidence delete or a
pointer/artifact mutation that survived a 409.  Guard: this file's new-session
rereads after the real route transaction rolls back.  Tier: T3.
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shared.config.resolver import clear_cache
from shared.db.models import (
    AggregateDefinition,
    AuditEvent,
    DataSource,
    DataTarget,
    Join,
    JoinPopulationCheck,
    Model,
    ModelColumn,
    ModelTable,
    ModelVersion,
    PendingKpiReeval,
    Project,
    ProjectConnection,
    SystemBase,
    SystemSetting,
    TenantBase,
)
from shared.semantic.join_population_validator import (
    EdgeMeasurement,
    REASON_MEASURED,
    SETTING_PROBE_BUDGET_SECONDS,
    SETTING_ROW_EFFECT_THRESHOLD,
    SideProbe,
    _snapshot_graph,
    join_definition_fingerprint,
)
import shared.semantic.join_population_validator as join_population_validator
from src.api._model_lock import acquire_model_definition_lock
from src.api.versions import DeployBody, deploy_model
from src.auth.middleware import CurrentUser

pytestmark = [pytest.mark.integration]

_DB_URL = os.environ.get("TESSALLITE_VERSIONING_DB_URL") or os.environ.get(
    "TESSALLITE_IMPORTER_REHYDRATION_DB_URL"
)


def _tenant_engine(schema: str):
    # Match production's tenant session factory.  A connect-event ``SET`` is
    # transaction-scoped with asyncpg and is reverted when SQLAlchemy resets a
    # pooled connection, so a second request can otherwise fall back to the
    # database default schema after the first refusal.
    return create_async_engine(
        _DB_URL,
        future=True,
        connect_args={
            "server_settings": {"search_path": f'"{schema}",public'}
        },
    )


@asynccontextmanager
async def _isolated_databases():
    """Create a disposable tenant schema and use the real system schema."""
    if not _DB_URL:
        pytest.skip("no versioning/importer Postgres URL configured")

    tenant_schema = f"g5_block_{uuid.uuid4().hex}"
    boot = create_async_engine(_DB_URL, future=True)
    async with boot.begin() as conn:
        await conn.exec_driver_sql(f'CREATE SCHEMA "{tenant_schema}"')
        await conn.exec_driver_sql(f'SET search_path TO "{tenant_schema}"')
        await conn.run_sync(TenantBase.metadata.create_all)
        await conn.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS tess_system")
        await conn.run_sync(SystemBase.metadata.create_all)
    await boot.dispose()

    tenant_engine = _tenant_engine(tenant_schema)
    tenant_factory = async_sessionmaker(tenant_engine, expire_on_commit=False)
    system_engine = create_async_engine(_DB_URL, future=True)
    system_factory = async_sessionmaker(system_engine, expire_on_commit=False)
    clear_cache()
    try:
        yield tenant_factory, system_factory
    finally:
        clear_cache()
        drop = create_async_engine(_DB_URL, future=True)
        async with drop.begin() as conn:
            await conn.exec_driver_sql(
                f'DROP SCHEMA IF EXISTS "{tenant_schema}" CASCADE'
            )
        await drop.dispose()
        await tenant_engine.dispose()
        await system_engine.dispose()


def _user(tenant: str) -> CurrentUser:
    return CurrentUser(
        user_id="g5-modeler",
        tenant_id=tenant,
        email="g5-modeler@example.test",
        role="tenant_admin",
    )


def _snapshot(
    model_id: uuid.UUID,
    ids,
    *,
    participation: str = "preserve_base_rows",
    omit_participation: bool = False,
    join_type: str = "inner",
    composite_dimension_key: bool = False,
) -> dict:
    columns = [
        {
            "id": str(ids.fact_column), "model_table_id": str(ids.fact),
            "column_name": "customer_id", "data_type": "bigint",
            "is_primary_key": False,
        },
        {
            "id": str(ids.dim_column), "model_table_id": str(ids.dim),
            "column_name": "id", "data_type": "bigint",
            "is_primary_key": True,
        },
    ]
    if composite_dimension_key:
        columns.append({
            "id": str(ids.dim_key2), "model_table_id": str(ids.dim),
            "column_name": "tenant_id", "data_type": "bigint",
            "is_primary_key": True,
        })
    join = {
        "id": str(ids.join), "model_id": str(model_id),
        "left_table_id": str(ids.fact), "right_table_id": str(ids.dim),
        "left_column_id": str(ids.fact_column),
        "right_column_id": str(ids.dim_column), "join_type": join_type,
    }
    if not omit_participation:
        join.update({
            "population_participation": participation,
            "population_participation_source": "manual",
        })
    return {
        "schema_version": 6,
        "model": {"id": str(model_id)},
        "tables": [
            {
                "id": str(ids.fact), "model_id": str(model_id),
                "source_id": str(ids.source), "table_type": "fact",
                "physical_name": "fact_sales", "alias": "fact",
                "display_name": "Fact",
            },
            {
                "id": str(ids.dim), "model_id": str(model_id),
                "source_id": str(ids.source), "table_type": "dim_detail",
                "physical_name": "dim_customer", "alias": "dim",
                "display_name": "Customer",
            },
        ],
        "columns": columns,
        "joins": [join],
        "measures": [{"id": str(uuid.uuid4()), "name": "revenue"}],
        "dimensions": [{"id": str(uuid.uuid4()), "name": "customer"}],
        "hierarchies": [],
    }


async def _seed(tenant_factory):
    ids = SimpleNamespace(
        project=uuid.uuid4(), model=uuid.uuid4(), connection=uuid.uuid4(),
        source=uuid.uuid4(), target=uuid.uuid4(), fact=uuid.uuid4(),
        dim=uuid.uuid4(), fact_column=uuid.uuid4(), dim_column=uuid.uuid4(),
        dim_key2=uuid.uuid4(),
        join=uuid.uuid4(), old_version=uuid.uuid4(), new_version=uuid.uuid4(),
        aggregate=uuid.uuid4(), old_check=uuid.uuid4(),
    )
    old_epoch = 7
    async with tenant_factory() as db:
        model = Model(
            id=ids.model, project_id=ids.project,
            slug=f"model-{ids.model.hex[:8]}", display_name="G5 model",
            seed=ids.model.hex[:16], deploy_epoch=old_epoch,
        )
        db.add_all([
            Project(
                id=ids.project, slug=f"g5-{ids.project.hex[:8]}",
                display_name="G5 project",
            ),
            ProjectConnection(
                id=ids.connection, project_id=ids.project,
                display_name="source", connection_type="postgresql",
                encrypted_credentials=b"test", config={"host": "source"},
            ),
            model,
        ])
        await db.flush()
        # Seed the model-owned graph under the same advisory lock required by
        # production definition writers.  The model row itself is created
        # first; the lock then covers every guarded child write and keeps the
        # runtime Bug-7982 guard quiet without disabling it.
        await acquire_model_definition_lock(db, ids.model)
        db.add_all([
            DataSource(
                id=ids.source, model_id=ids.model,
                project_connection_id=ids.connection, source_type="postgresql",
                display_name="source", default_schema="public", config={},
            ),
            DataTarget(
                id=ids.target, model_id=ids.model,
                project_connection_id=ids.connection, target_type="postgresql",
                display_name="target", config={},
            ),
        ])
        await db.flush()
        db.add_all([
            ModelTable(
                id=ids.fact, model_id=ids.model, source_id=ids.source,
                table_type="fact", physical_name="fact_sales", alias="fact",
                display_name="Fact",
            ),
            ModelTable(
                id=ids.dim, model_id=ids.model, source_id=ids.source,
                table_type="dim_detail", physical_name="dim_customer", alias="dim",
                display_name="Customer",
            ),
        ])
        await db.flush()
        db.add_all([
            ModelColumn(
                id=ids.fact_column, model_table_id=ids.fact,
                column_name="customer_id", data_type="bigint", is_nullable=False,
            ),
            ModelColumn(
                id=ids.dim_column, model_table_id=ids.dim,
                column_name="id", data_type="bigint", is_nullable=False,
                is_primary_key=True,
            ),
        ])
        await db.flush()
        db.add(
            Join(
                id=ids.join, model_id=ids.model,
                left_table_id=ids.fact, right_table_id=ids.dim,
                left_column_id=ids.fact_column, right_column_id=ids.dim_column,
                join_type="inner", population_participation="population_defining",
            ),
        )
        await db.flush()
        # The production schema has a model-version FK in both directions:
        # seed the model without its deployed pointer, then add versions and
        # set the pointer once both sides exist.
        await db.flush()
        db.add_all([
            ModelVersion(
                id=ids.old_version, model_id=ids.model, version_number=1,
                snapshot_json=_snapshot(
                    ids.model, ids, participation="undeclared",
                ), created_by="seed",
            ),
            ModelVersion(
                id=ids.new_version, model_id=ids.model, version_number=2,
                snapshot_json=_snapshot(
                    ids.model, ids, participation="population_defining",
                ), created_by="seed",
            ),
        ])
        await db.flush()
        model.deployed_version_id = ids.old_version
        await db.flush()
        # JoinPopulationCheck has a real FK to Join but no ORM relationship;
        # flush the producer rows first so SQLAlchemy cannot order the evidence
        # insert ahead of its referenced join.
        await db.flush()
        db.add_all([
            JoinPopulationCheck(
                id=ids.old_check, join_id=ids.join, model_id=ids.model,
                deployed_version_id=ids.old_version, deploy_epoch=old_epoch,
                classification="filtering", population_participation="undeclared",
                status="WARNING", measured=True, row_loss_ratio=0.01,
                row_effect_ratio=0.01, reason=REASON_MEASURED,
            ),
            AggregateDefinition(
                id=ids.aggregate, model_id=ids.model, target_id=ids.target,
                physical_table_name="g5_aggregate", status="active", grain=[],
                built_for_version_id=ids.old_version, built_for_epoch=old_epoch,
            ),
        ])
        await db.commit()
    return ids, old_epoch


async def _set_system_policy(system_factory, *, threshold: float, budget: float):
    """Set and later restore the two system policy rows used by G5."""
    async with system_factory() as db:
        previous = {}
        for key, value in (
            (SETTING_ROW_EFFECT_THRESHOLD, threshold),
            (SETTING_PROBE_BUDGET_SECONDS, budget),
        ):
            previous[key] = (
                await db.execute(
                    select(SystemSetting).where(SystemSetting.key == key)
                )
            ).scalar_one_or_none()
            await db.execute(
                delete(SystemSetting).where(SystemSetting.key == key)
            )
            db.add(SystemSetting(key=key, value_json=value, updated_by="g5-test"))
        await db.commit()
    return previous


async def _restore_system_policy(system_factory, previous):
    async with system_factory() as db:
        for key, row in previous.items():
            await db.execute(delete(SystemSetting).where(SystemSetting.key == key))
            if row is not None:
                db.add(row)
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_g5_real_route_refuses_atomically_with_system_threshold():
    """G5 B01: explicit system threshold blocks and leaves no partial publish."""
    async with _isolated_databases() as (tenant_factory, system_factory):
        ids, old_epoch = await _seed(tenant_factory)
        previous = await _set_system_policy(
            system_factory, threshold=0.15, budget=5.0,
        )
        try:
            current_measurement = {"value": EdgeMeasurement(
                left=SideProbe(rows=100, key_non_null=100, key_distinct=100, matched=80),
                right=SideProbe(rows=10, key_non_null=10, key_distinct=10, matched=10),
            )}
            seen_probe = {}

            async def _probe(**kwargs):
                seen_probe.update(kwargs)
                return current_measurement["value"], REASON_MEASURED

            captured_sessions = []
            validator_calls = []
            real_validate_model_joins_on_deploy = (
                join_population_validator.validate_model_joins_on_deploy
            )

            async def _capture_validator_call(**kwargs):
                validator_calls.append(kwargs.copy())
                return await real_validate_model_joins_on_deploy(**kwargs)

            async def _tenant_db(_tenant):
                async with tenant_factory() as db:
                    captured_sessions.append(db)
                    # Match production get_tenant_db: AsyncSession context
                    # close rolls back the uncommitted refusal transaction.
                    yield db

            async def _successful_deploy(version_id):
                """Run one successful deploy with publish hooks isolated."""
                async with system_factory() as system_db:
                    effects = {
                        "audit": AsyncMock(),
                        "stale": AsyncMock(),
                        "outbox": AsyncMock(),
                        "evict": AsyncMock(),
                        "webhook": AsyncMock(),
                        "verify": AsyncMock(),
                    }
                    with (
                        patch("src.api.versions.get_tenant_db", _tenant_db),
                        patch("shared.aggregate_connection.resolve_source_connection", AsyncMock(return_value=object())),
                        patch("shared.source_executor.resolve_connector_type", AsyncMock(return_value="postgresql")),
                        patch("shared.semantic.join_population_validator.probe_edge", _probe),
                        patch("src.api.versions.audit", effects["audit"]),
                        patch("src.api.versions._stale_incompatible_artifacts", effects["stale"]),
                        patch("src.api.versions._enqueue_pending_kpi_reeval", effects["outbox"]),
                        patch("src.api.versions._evict_query_router_cache", effects["evict"]),
                        patch("src.api.versions.emit_webhook", effects["webhook"]),
                        patch("src.api.versions._verify_attribute_relationships_on_deploy", effects["verify"]),
                    ):
                        result = await deploy_model(
                            ids.project, ids.model,
                            DeployBody(version_id=version_id),
                            current_user=_user(f"g5-{ids.project.hex[:8]}"),
                            system_db=system_db,
                        )
                    for effect in effects.values():
                        effect.assert_awaited()
                    return result

            async with system_factory() as system_db:
                side_effects = {
                    "audit": AsyncMock(),
                    "stale": AsyncMock(),
                    "outbox": AsyncMock(),
                    "evict": AsyncMock(),
                    "webhook": AsyncMock(),
                    "verify": AsyncMock(),
                }
                with (
                    patch("src.api.versions.get_tenant_db", _tenant_db),
                    patch("shared.aggregate_connection.resolve_source_connection", AsyncMock(return_value=object())),
                    patch("shared.source_executor.resolve_connector_type", AsyncMock(return_value="postgresql")),
                    patch("shared.semantic.join_population_validator.probe_edge", _probe),
                    patch(
                        "shared.semantic.join_population_validator.validate_model_joins_on_deploy",
                        _capture_validator_call,
                    ),
                    patch("src.api.versions.audit", side_effects["audit"]),
                    patch("src.api.versions._stale_incompatible_artifacts", side_effects["stale"]),
                    patch("src.api.versions._enqueue_pending_kpi_reeval", side_effects["outbox"]),
                    patch("src.api.versions._evict_query_router_cache", side_effects["evict"]),
                    patch("src.api.versions.emit_webhook", side_effects["webhook"]),
                    patch("src.api.versions._verify_attribute_relationships_on_deploy", side_effects["verify"]),
                ):
                    with pytest.raises(HTTPException) as raised:
                        await deploy_model(
                            ids.project, ids.model,
                            DeployBody(version_id=ids.old_version),
                            current_user=_user(f"g5-{ids.project.hex[:8]}"),
                            system_db=system_db,
                        )

            assert getattr(raised.value, "status_code", None) == 409
            detail = raised.value.detail
            assert detail["code"] == "JOIN_POPULATION_BLOCKED"
            assert detail["threshold"] == 0.15
            assert detail["joins"][0]["join_id"] == str(ids.join)
            assert detail["joins"][0]["join_label"] == "Fact.customer_id ↔ Customer.id"
            assert detail["joins"][0]["left_table_name"] == "Fact"
            assert detail["joins"][0]["right_table_name"] == "Customer"
            assert detail["joins"][0]["row_effect_ratio"] == 0.2
            assert "Declare" in detail["message"]
            assert seen_probe["tenant_session"] is captured_sessions[0]
            assert len(validator_calls) == 1
            assert validator_calls[0]["threshold"] == 0.15
            assert validator_calls[0]["budget_seconds"] == 5.0

            async with tenant_factory() as db:
                model = await db.get(Model, ids.model)
                assert model.deployed_version_id == ids.old_version
                assert model.deploy_epoch == old_epoch
                assert model.last_deployed_at is None

                check = await db.get(JoinPopulationCheck, ids.old_check)
                assert check is not None
                assert check.status == "WARNING"
                assert check.deployed_version_id == ids.old_version
                assert check.deploy_epoch == old_epoch

                aggregate = await db.get(AggregateDefinition, ids.aggregate)
                assert aggregate is not None
                assert aggregate.status == "active"
                assert aggregate.built_for_version_id == ids.old_version
                assert aggregate.built_for_epoch == old_epoch

                assert (
                    await db.execute(
                        select(AuditEvent).where(AuditEvent.target_id == ids.model)
                    )
                ).scalars().all() == []
                assert (
                    await db.execute(
                        select(PendingKpiReeval).where(
                            PendingKpiReeval.model_id == ids.model
                        )
                    )
                ).scalars().all() == []
                # The selected v1 snapshot is unsafe, but validation must not
                # rehydrate or mutate the live draft graph.
                live_join = await db.get(Join, ids.join)
                assert live_join.population_participation == "population_defining"

            for hook in side_effects.values():
                hook.assert_not_awaited()

            # B01 two-direction matrix: change only the mutable draft after the
            # selected v1 refusal.  The selected v2 snapshot remains
            # population-defining and must deploy even though live state is
            # now an undeclared blocker.
            async with tenant_factory() as db:
                await acquire_model_definition_lock(db, ids.model)
                live_join = await db.get(Join, ids.join)
                live_join.population_participation = "undeclared"
                await db.commit()

            async with system_factory() as system_db:
                success_effects = {
                    "audit": AsyncMock(),
                    "stale": AsyncMock(),
                    "outbox": AsyncMock(),
                    "evict": AsyncMock(),
                    "webhook": AsyncMock(),
                    "verify": AsyncMock(),
                }
                with (
                    patch("src.api.versions.get_tenant_db", _tenant_db),
                    patch("shared.aggregate_connection.resolve_source_connection", AsyncMock(return_value=object())),
                    patch("shared.source_executor.resolve_connector_type", AsyncMock(return_value="postgresql")),
                    patch("shared.semantic.join_population_validator.probe_edge", _probe),
                    patch("src.api.versions.audit", success_effects["audit"]),
                    patch("src.api.versions._stale_incompatible_artifacts", success_effects["stale"]),
                    patch("src.api.versions._enqueue_pending_kpi_reeval", success_effects["outbox"]),
                    patch("src.api.versions._evict_query_router_cache", success_effects["evict"]),
                    patch("src.api.versions.emit_webhook", success_effects["webhook"]),
                    patch("src.api.versions._verify_attribute_relationships_on_deploy", success_effects["verify"]),
                ):
                    deployed = await deploy_model(
                        ids.project, ids.model,
                        DeployBody(version_id=ids.new_version),
                        current_user=_user(f"g5-{ids.project.hex[:8]}"),
                        system_db=system_db,
                    )
            assert deployed["status"] == "ok"

            async with tenant_factory() as db:
                model = await db.get(Model, ids.model)
                assert model.deployed_version_id == ids.new_version
                assert model.deploy_epoch == old_epoch + 1
                check = (
                    await db.execute(
                        select(JoinPopulationCheck).where(
                            JoinPopulationCheck.join_id == ids.join
                        )
                    )
                ).scalar_one()
                assert check.population_participation == "population_defining"
                assert check.join_label == "Fact.customer_id ↔ Customer.id"
                assert check.left_table_name == "Fact"
                assert check.right_table_name == "Customer"
                assert check.left_column_name == "customer_id"
                assert check.right_column_name == "id"
                selected = await db.get(ModelVersion, ids.new_version)
                selected_joins, _tables, _columns = _snapshot_graph(
                    selected.snapshot_json
                )
                assert check.inputs_fingerprint == join_definition_fingerprint(
                    selected_joins[0]
                )

            # Legacy snapshot compatibility: the missing key is the only
            # condition that receives the compatibility default.  The selected
            # graph, not the mutable draft, feeds both evidence and summary.
            legacy_version = uuid.uuid4()
            orientation_version = uuid.uuid4()
            composite_version = uuid.uuid4()
            historical_version = uuid.uuid4()
            legacy_snapshot = _snapshot(
                ids.model, ids, omit_participation=True,
            )
            orientation_snapshot = _snapshot(
                ids.model, ids, participation="population_defining",
                join_type="left",
            )
            composite_snapshot = _snapshot(
                ids.model, ids, participation="preserve_base_rows",
                join_type="left", composite_dimension_key=True,
            )
            historical_snapshot = _snapshot(
                ids.model, ids, participation="population_defining",
                join_type="left",
            )
            async with tenant_factory() as db:
                await acquire_model_definition_lock(db, ids.model)
                db.add_all([
                    ModelVersion(
                        id=legacy_version, model_id=ids.model, version_number=3,
                        snapshot_json=legacy_snapshot, created_by="g5-test",
                    ),
                    ModelVersion(
                        id=orientation_version, model_id=ids.model, version_number=4,
                        snapshot_json=orientation_snapshot, created_by="g5-test",
                    ),
                    ModelVersion(
                        id=composite_version, model_id=ids.model, version_number=5,
                        snapshot_json=composite_snapshot, created_by="g5-test",
                    ),
                    ModelVersion(
                        id=historical_version, model_id=ids.model, version_number=6,
                        snapshot_json=historical_snapshot, created_by="g5-test",
                    ),
                ])
                await db.commit()

            legacy_deployed = await _successful_deploy(legacy_version)
            assert legacy_deployed["join_population"]["status"] == "WARNING"
            legacy_item = legacy_deployed["join_population"]["items"][0]
            assert legacy_item["population_participation"] == "preserve_base_rows"
            assert legacy_item["left_table_name"] == "Fact"
            assert legacy_item["right_table_name"] == "Customer"
            async with tenant_factory() as db:
                legacy_check = (
                    await db.execute(
                        select(JoinPopulationCheck).where(
                            JoinPopulationCheck.join_id == ids.join
                        )
                    )
                ).scalar_one()
                assert legacy_check.population_participation == "preserve_base_rows"
                assert legacy_check.inputs_fingerprint == join_definition_fingerprint(
                    _snapshot_graph(legacy_snapshot)[0][0]
                )
                assert legacy_check.join_label == "Fact.customer_id ↔ Customer.id"

            # Mutation-sensitive fact orientation: the fact table is the LEFT
            # retained side.  The unmatched dimension rows must not turn this
            # left-preserving join into filtering evidence.
            current_measurement["value"] = EdgeMeasurement(
                left=SideProbe(rows=100, key_non_null=100, key_distinct=100, matched=100),
                right=SideProbe(rows=20, key_non_null=20, key_distinct=20, matched=10),
            )
            orientation_deployed = await _successful_deploy(orientation_version)
            orientation_item = orientation_deployed["join_population"]["items"][0]
            assert orientation_item["classification"] == "neutral"
            assert orientation_item["status"] == "OK"
            assert orientation_item["row_effect_ratio"] == 0.0

            # Whole-composite-key transfer: both PK columns are present in the
            # selected version.  A join on only the first half is not declared
            # unique, so a measured zero-effect edge remains an honest warning
            # instead of a false neutral.
            current_measurement["value"] = EdgeMeasurement(
                left=SideProbe(rows=100, key_non_null=100, key_distinct=100, matched=100),
                right=SideProbe(rows=20, key_non_null=20, key_distinct=20, matched=10),
            )
            composite_deployed = await _successful_deploy(composite_version)
            composite_item = composite_deployed["join_population"]["items"][0]
            assert composite_item["classification"] == "multiplying"
            assert composite_item["reason"] == "uniqueness_not_declared"
            assert composite_item["status"] == "WARNING"
            async with tenant_factory() as db:
                selected = await db.get(ModelVersion, composite_version)
                selected_joins, _tables, selected_columns = _snapshot_graph(
                    selected.snapshot_json
                )
                assert {
                    column.id for column in selected_columns.values()
                    if column.model_table_id == ids.dim
                    and column.is_primary_key
                } == {ids.dim_column, ids.dim_key2}
                composite_check = (
                    await db.execute(
                        select(JoinPopulationCheck).where(
                            JoinPopulationCheck.join_id == ids.join
                        )
                    )
                ).scalar_one()
                assert composite_check.inputs_fingerprint == join_definition_fingerprint(
                    selected_joins[0]
                )

            # Delete the mutable draft join under the production model lock,
            # then deploy the selected historical snapshot.  Evidence must
            # survive because its identity is version-bound, not a live FK.
            async with tenant_factory() as db:
                await acquire_model_definition_lock(db, ids.model)
                await db.delete(await db.get(Join, ids.join))
                await db.commit()
            historical_deployed = await _successful_deploy(historical_version)
            historical_item = historical_deployed["join_population"]["items"][0]
            assert historical_item["join_id"] == str(ids.join)
            assert historical_item["left_table_name"] == "Fact"
            assert historical_item["right_table_name"] == "Customer"
            assert historical_item["population_participation"] == "population_defining"
            async with tenant_factory() as db:
                assert await db.get(Join, ids.join) is None
                historical_check = (
                    await db.execute(
                        select(JoinPopulationCheck).where(
                            JoinPopulationCheck.join_id == ids.join
                        )
                    )
                ).scalar_one()
                assert historical_check.deployed_version_id == historical_version
                assert historical_check.join_label == "Fact.customer_id ↔ Customer.id"
                assert historical_check.left_table_name == "Fact"
                assert historical_check.right_table_name == "Customer"
                assert historical_check.inputs_fingerprint == join_definition_fingerprint(
                    _snapshot_graph(historical_snapshot)[0][0]
                )

            from src.api.join_population_health import get_join_population_health

            with patch(
                "src.api.join_population_health.get_tenant_db", _tenant_db,
            ):
                health = await get_join_population_health(
                    ids.project, ids.model,
                    current_user=_user(f"g5-{ids.project.hex[:8]}"),
                )
            assert health.status == "OK"
            assert [item.join_id for item in health.items] == [str(ids.join)]
            assert health.items[0].left_table_name == "Fact"
            assert health.items[0].right_table_name == "Customer"
            assert health.items[0].population_participation == "population_defining"
        finally:
            await _restore_system_policy(system_factory, previous)
