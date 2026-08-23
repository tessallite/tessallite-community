"""G4/B03 real query API proof for read-only explain and refusal durability.

The route-level unit tests can prove that a flag is passed to a mocked router,
but not that ``/explain`` leaves a persisted pocket untouched or that a real
``/execute`` refusal commits the population observation and failure log.  This
probe drives both API handlers with a real tenant session and a real matcher,
RLS compiler, persistence boundary, and close/reopen read.

Test escape: the previous guard mocked the matcher and asserted calls, so it
could not catch an accidental explain commit or an execute refusal that rolled
back its durable observation.  Guard: the RLS rule id, exact pocket state, and
QueryLog assertions after new-session rereads.  Tier: T3.
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
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shared.auth.middleware import CurrentUser
from shared.db.models import (
    DataSource,
    DataTarget,
    Join,
    Model,
    ModelColumn,
    ModelTable,
    PocketDefinition,
    PocketRefreshRun,
    Project,
    ProjectConnection,
    QueryLog,
    RowSecurityRule,
    TenantBase,
)
from shared.semantic.artifact_manifest import MANIFEST_VERSION

from src.ir.logical_query import BoundQuery, LogicalQuery
from src.routing import pocket_matcher

pytestmark = pytest.mark.integration

_DB_URL = os.environ.get("TESSALLITE_VERSIONING_DB_URL") or os.environ.get(
    "TESSALLITE_IMPORTER_REHYDRATION_DB_URL"
)


@asynccontextmanager
async def _isolated_schema():
    if not _DB_URL:
        pytest.skip("no versioning/importer Postgres URL configured")
    schema = f"g4b03api_{uuid.uuid4().hex}"
    boot = create_async_engine(_DB_URL, future=True)
    async with boot.begin() as conn:
        await conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        await conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        await conn.run_sync(TenantBase.metadata.create_all)
    await boot.dispose()

    engine = create_async_engine(_DB_URL, future=True)

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
        user_id="g4-b03-user", tenant_id="g4-b03-tenant",
        email="g4-b03-user@example.test", role="tenant_admin",
        roles=["tenant_admin"], raw_token="g4-b03-token",
    )


async def _seed(factory):
    ids = SimpleNamespace(
        project=uuid.uuid4(), model=uuid.uuid4(), connection=uuid.uuid4(),
        source=uuid.uuid4(), target=uuid.uuid4(), fact=uuid.uuid4(),
        dim=uuid.uuid4(), amount=uuid.uuid4(), region=uuid.uuid4(),
        join=uuid.uuid4(), pocket=uuid.uuid4(), refresh_run=uuid.uuid4(),
        rls_rule=uuid.uuid4(),
    )
    async with factory() as db:
        db.add(Project(
            id=ids.project, slug="g4b03-project", display_name="G4 B03",
        ))
        db.add(ProjectConnection(
            id=ids.connection, project_id=ids.project,
            display_name="same-db", connection_type="postgresql",
            encrypted_credentials=b"test", config={"host": "same-db"},
        ))
        model = Model(
            id=ids.model, project_id=ids.project, slug="g4b03model",
            display_name="G4 B03 model", seed="g4b03seed", deploy_epoch=4,
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
            display_name="target", config={"schema": "pockets"},
        ))
        await db.flush()
        model.target_id = ids.target
        db.add_all([
            ModelTable(
                id=ids.fact, model_id=ids.model, source_id=ids.source,
                table_type="fact", physical_name="fact_sales", alias="fact",
                display_name="Fact",
            ),
            ModelTable(
                id=ids.dim, model_id=ids.model, source_id=ids.source,
                table_type="dim_detail", physical_name="dim_region", alias="region",
                display_name="Region",
            ),
        ])
        await db.flush()
        db.add_all([
            ModelColumn(
                id=ids.amount, model_table_id=ids.fact,
                column_name="amount", data_type="numeric", is_nullable=False,
            ),
            ModelColumn(
                id=ids.region, model_table_id=ids.dim,
                column_name="region", data_type="text", is_nullable=False,
                is_primary_key=True,
            ),
        ])
        await db.flush()
        db.add_all([
            Join(
                id=ids.join, model_id=ids.model,
                left_table_id=ids.fact, right_table_id=ids.dim,
                join_type="left", cardinality="many_to_one",
                left_column_id=ids.amount, right_column_id=ids.region,
                population_participation="preserve_base_rows",
                population_participation_source="manual",
            ),
            RowSecurityRule(
                id=ids.rls_rule, model_id=ids.model, name="g4-b03-region",
                dimension_path="region.region", rule_type="role_predicate",
                predicate_expression="dimension_equals('region.region', 'EMEA')",
                applies_to_roles=["tenant_admin"], is_enabled=True,
            ),
        ])
        await db.flush()
        db.add(PocketDefinition(
            id=ids.pocket, model_id=ids.model, target_id=ids.target,
            physical_table_name="slice_a", target_schema="pockets",
            defining_sql="SELECT * FROM g4b03model",
            query_fingerprint="g4b03-fingerprint",
            predicate_set_hash="g4b03-predicates", status="fresh",
            population_eligibility="unknown", row_count=3, storage_bytes=300,
            last_refresh_at=datetime.now(timezone.utc),
            row_manifest={
                "manifest_version": MANIFEST_VERSION,
                "build_refresh_run_id": str(ids.refresh_run),
                "columns": [{"physical_column": "region"}],
            },
        ))
        await db.flush()
        db.add(PocketRefreshRun(
            id=ids.refresh_run, pocket_definition_id=ids.pocket,
            refresh_mode="full", status="completed", triggered_by="g4-b03",
            completed_at=datetime.now(timezone.utc), rows_written=3,
        ))
        await db.flush()
        pocket = await db.get(PocketDefinition, ids.pocket)
        pocket.active_refresh_run_id = ids.refresh_run
        await db.commit()
    return ids


def _bound(model, column_id) -> BoundQuery:
    measure = SimpleNamespace(
        id="measure-amount", name="amount", source_column_id=column_id,
        default_agg="sum", is_additive=True, measure_type="standard",
        expression=None, calc_agg_mode=None, semi_additive_behavior=None,
        variant_kind=None,
    )
    logical = LogicalQuery(
        model_id=str(model.id), protocol="jdbc",
        raw_query="SELECT SUM(amount) FROM g4b03model",
        requested_measures=["amount"], requested_dimensions=[], filters=[],
        grain=[], order_by=[], limit=None, offset=None,
        query_fingerprint="g4b03-fingerprint",
    )
    return BoundQuery(
        logical_query=logical, model=model, resolved_measures=[measure],
        resolved_dimensions=[], resolved_filters=[],
        resolved_dimensions_by_name={},
    )


async def _true_setting(*_args, **_kwargs):
    return True


@pytest.mark.asyncio
async def test_g4_r2_b03_real_explain_rls_is_read_only_and_execute_refusal_is_durable(
    monkeypatch,
):
    """B03: explain has RLS evidence without mutation; execute refusal commits."""
    monkeypatch.setattr(
        pocket_matcher, "system_snapshot_get",
        lambda key: {
            "pocket.enabled": True,
            "pocket.require_tenant_filter": False,
        }.get(key),
    )
    monkeypatch.setattr(pocket_matcher, "get_setting", _true_setting)

    async with _isolated_schema() as factory:
        ids = await _seed(factory)
        user = _user()

        async def _db(_tenant):
            async with factory() as db:
                yield db

        async with factory() as db:
            model = await db.get(Model, ids.model)
            bound = _bound(model, ids.amount)

        import src.api.routes as routes

        explain_body = routes.ExecuteRequest(
            model_id=str(ids.model), raw_query="SELECT SUM(amount) FROM g4b03model",
            protocol="jdbc",
        )
        # Call the production /explain function and only replace expensive
        # auth/persona/bind dependencies.  The parser, route_query, RLS
        # compiler, pocket matcher, and trace builder remain real.
        with (
            patch.object(routes, "get_tenant_db", _db),
            patch.object(routes, "load_authorized_model", new=AsyncMock()),
            patch.object(routes, "resolve_execution_persona", new=AsyncMock(return_value=None)),
            patch.object(routes, "bind_query_to_model", new=AsyncMock(return_value=bound)),
        ):
            explained = await routes.explain_query(
                explain_body,
                current_user=user,
                x_simulate_principal=None,
                x_simulate_roles=None,
                x_simulate_groups=None,
                x_simulate_claims=None,
            )

        assert explained.route_type == "pocket"
        assert explained.pocket_id == str(ids.pocket)
        assert explained.security_rules_applied == [str(ids.rls_rule)]

        # Explain's read-only route flag must not persist the proof it evaluated.
        async with factory() as db:
            pocket = await db.get(PocketDefinition, ids.pocket)
            assert (
                pocket.status, pocket.population_eligibility,
                pocket.population_eligibility_reason,
                pocket.population_proof_fingerprint,
            ) == ("fresh", "unknown", None, None)

        # Change the real persisted join graph to a filtering edge.  The next
        # execute/refusal must record the mismatch, while force_route=pocket
        # prevents a silent source fallback.
        async with factory() as db:
            join = await db.get(Join, ids.join)
            join.join_type = "inner"
            await db.commit()
        # A production definition writer evicts the join-graph proof cache in
        # its mutation fan-out.  Reproduce that durable caller boundary before
        # the next request so execute cannot reuse the pre-edit LEFT graph.
        pocket_matcher.invalidate_model_join_graph_cache(ids.model)

        execute_body = routes.ExecuteRequest(
            model_id=str(ids.model), raw_query="SELECT SUM(amount) FROM g4b03model",
            protocol="jdbc", force_route="pocket",
        )
        request = SimpleNamespace(headers={})
        with (
            patch.object(routes, "get_tenant_db", _db),
            patch.object(routes, "load_authorized_model", new=AsyncMock()),
            patch.object(routes, "resolve_execution_persona", new=AsyncMock(return_value=None)),
            patch.object(routes, "bind_query_to_model", new=AsyncMock(return_value=bound)),
        ):
            with pytest.raises(HTTPException) as refusal:
                await routes.execute_query(
                    execute_body, request=request, current_user=user,
                    x_simulate_principal=None,
                    x_simulate_roles=None,
                    x_simulate_groups=None,
                    x_simulate_claims=None,
                )

        assert refusal.value.status_code == 422
        assert refusal.value.detail["error_type"] == "no_aggregate_match"

        async with factory() as db:
            pocket = await db.get(PocketDefinition, ids.pocket)
            failure = (
                await db.execute(
                    select(QueryLog)
                    .where(QueryLog.raw_query == execute_body.raw_query)
                    .order_by(QueryLog.created_at.desc())
                )
            ).scalars().first()
            assert (
                pocket.status, pocket.population_eligibility,
                pocket.population_eligibility_reason,
                pocket.row_count,
            ) == ("fresh", "ineligible", "Ineligible: population mismatch", 3)
            assert failure is not None
            assert failure.status == "error"
            assert failure.error_type == "no_aggregate_match"
