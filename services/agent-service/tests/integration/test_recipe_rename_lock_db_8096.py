"""DB-backed serialization guard for recipe writes versus measure rename.

Test escape: mocked rename tests could prove atomic JSON planning, but not that a
recipe request carrying the old canonical name waits for an in-flight rename and
then validates against the committed new definition. Guard: the real shared
PostgreSQL advisory lock plus the real recipe write helpers reject that stale
request, leaving the renamed recipe executable. Tier: T1 db-integration.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shared.db.model_lock import acquire_model_definition_lock
from shared.db.models import (
    Dimension,
    Measure,
    Model,
    Project,
    ProjectCrossModelRecipe,
    TenantBase,
)
from shared.model_snapshot.project_rehydrator import import_project
from shared.model_snapshot.project_serialiser import export_project
from src.api.recipes import RecipeStep, _lock_recipe_models, _validate_step_models
from src.exec.query import QueryExecution
from src.exec.recipe import execute_recipe
from src.tools.spec import RunRecipeToolCall

pytestmark = pytest.mark.integration

_DB_URL = os.environ.get("TESSALLITE_VERSIONING_DB_URL") or os.environ.get(
    "TESSALLITE_IMPORTER_REHYDRATION_DB_URL"
)


@asynccontextmanager
async def _isolated_schema() -> AsyncIterator[async_sessionmaker]:
    schema = f"recipe_rename_8096_{uuid.uuid4().hex}"
    boot = create_async_engine(_DB_URL, future=True)
    async with boot.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        await connection.execute(text(f'SET search_path TO "{schema}"'))
        await connection.run_sync(TenantBase.metadata.create_all)
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
        async with drop.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await drop.dispose()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_stale_recipe_writer_waits_for_rename_then_rejects_old_measure():
    async with _isolated_schema() as factory:
        project_id = uuid.uuid4()
        model_id = uuid.uuid4()
        recipe_id = uuid.uuid4()
        old_steps = [
            {
                "name": "sales",
                "model_id": str(model_id),
                "measures": ["Revenue"],
                "dimensions": [],
            }
        ]
        async with factory() as setup:
            setup.add(
                Project(
                    id=project_id,
                    slug=f"p-{project_id.hex[:8]}",
                    display_name="Project",
                )
            )
            setup.add(
                Model(
                    id=model_id,
                    project_id=project_id,
                    slug=f"m-{model_id.hex[:8]}",
                    display_name="Model",
                    seed=uuid.uuid4().hex,
                )
            )
            setup.add(
                Measure(
                    model_id=model_id,
                    name="Revenue",
                    display_name="Revenue",
                    measure_type="standard",
                    data_type="numeric",
                    default_agg="sum",
                )
            )
            setup.add(
                ProjectCrossModelRecipe(
                    id=recipe_id,
                    project_id=project_id,
                    name="Revenue recipe",
                    parameters=[],
                    steps=old_steps,
                    combine={"ref": {"step": "sales", "measure": "Revenue"}},
                )
            )
            await setup.commit()

        writer = factory()
        stale_recipe = await writer.get(ProjectCrossModelRecipe, recipe_id)
        stale_body = [
            RecipeStep(name="sales", model_id=model_id, measures=["Revenue"])
        ]

        holder = factory()
        await acquire_model_definition_lock(holder, model_id)
        measure = (
            await holder.execute(
                select(Measure).where(
                    Measure.model_id == model_id, Measure.name == "Revenue"
                )
            )
        ).scalar_one()
        recipe = await holder.get(ProjectCrossModelRecipe, recipe_id)
        measure.name = "Net Revenue"
        recipe.steps = [
            {**old_steps[0], "measures": ["Net Revenue"]}
        ]
        recipe.combine = {
            "ref": {"step": "sales", "measure": "Net Revenue"}
        }
        await holder.flush()

        entered = asyncio.Event()

        async def _stale_writer_attempt() -> HTTPException | None:
            entered.set()
            try:
                await _lock_recipe_models(writer, stale_body, stale_recipe.steps)
                await writer.refresh(stale_recipe)
                await _validate_step_models(writer, project_id, stale_body)
            except HTTPException as exc:
                await writer.rollback()
                return exc
            stale_recipe.steps = old_steps
            await writer.commit()
            return None

        attempt = asyncio.create_task(_stale_writer_attempt())
        await entered.wait()
        await asyncio.sleep(0.2)
        assert not attempt.done(), "recipe writer did not wait for the rename lock"

        await holder.commit()
        rejection = await asyncio.wait_for(attempt, timeout=5)
        assert rejection is not None
        assert rejection.status_code == 422
        assert "Revenue" in str(rejection.detail)

        async with factory() as verify:
            stored = await verify.get(ProjectCrossModelRecipe, recipe_id)
            assert stored.steps[0]["measures"] == ["Net Revenue"]
            assert stored.combine["ref"]["measure"] == "Net Revenue"

        await writer.close()
        await holder.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_two_model_bundle_remaps_recipe_and_imported_recipe_executes():
    async with _isolated_schema() as factory:
        source_project_id = uuid.uuid4()
        sales_model_id = uuid.uuid4()
        units_model_id = uuid.uuid4()
        source_recipe_id = uuid.uuid4()
        async with factory() as source:
            source.add(
                Project(
                    id=source_project_id,
                    slug="source-project",
                    display_name="Source project",
                )
            )
            source.add_all(
                [
                    Model(
                        id=sales_model_id,
                        project_id=source_project_id,
                        slug="sales",
                        display_name="Sales",
                        seed=uuid.uuid4().hex,
                    ),
                    Model(
                        id=units_model_id,
                        project_id=source_project_id,
                        slug="units",
                        display_name="Units",
                        seed=uuid.uuid4().hex,
                    ),
                ]
            )
            source.add(
                Dimension(
                    model_id=sales_model_id,
                    name="Region",
                    display_name="Region",
                    description="Sales region",
                )
            )
            source.add_all(
                [
                    Measure(
                        model_id=sales_model_id,
                        name="Revenue",
                        display_name="Revenue",
                        measure_type="standard",
                        data_type="numeric",
                        default_agg="sum",
                    ),
                    Measure(
                        model_id=units_model_id,
                        name="Units",
                        display_name="Units",
                        measure_type="standard",
                        data_type="numeric",
                        default_agg="sum",
                    ),
                ]
            )
            source.add(
                ProjectCrossModelRecipe(
                    id=source_recipe_id,
                    project_id=source_project_id,
                    name="Revenue per unit",
                    parameters=[],
                    steps=[
                        {
                            "name": "sales",
                            "model_id": str(sales_model_id),
                            "measures": ["Revenue"],
                            "dimensions": [],
                        },
                        {
                            "name": "units",
                            "model_id": str(units_model_id),
                            "measures": ["Units"],
                            "dimensions": [],
                        },
                    ],
                    combine={
                        "op": "div",
                        "args": [
                            {"ref": {"step": "sales", "measure": "Revenue"}},
                            {"ref": {"step": "units", "measure": "Units"}},
                        ],
                    },
                )
            )
            await source.commit()
            bundle = await export_project(
                source_project_id,
                source,
                tenant_slug="test-tenant",
                sections={"cross_model_recipes"},
            )

        async with factory() as target:
            result = await import_project(
                bundle,
                target,
                mode="create",
                project_slug_override="imported-project",
            )
            await target.commit()
            imported_project_id = uuid.UUID(result["project_id"])
            imported_sales_id = uuid.UUID(result["id_map"]["models"][str(sales_model_id)])
            imported_units_id = uuid.UUID(result["id_map"]["models"][str(units_model_id)])
            assert imported_sales_id not in {sales_model_id, units_model_id}
            assert imported_units_id not in {sales_model_id, units_model_id}
            assert imported_sales_id != imported_units_id

            imported_recipe = (
                await target.execute(
                    select(ProjectCrossModelRecipe).where(
                        ProjectCrossModelRecipe.project_id == imported_project_id
                    )
                )
            ).scalar_one()
            assert [step["model_id"] for step in imported_recipe.steps] == [
                str(imported_sales_id),
                str(imported_units_id),
            ]

            def _execution(measure: str, value: int) -> QueryExecution:
                return QueryExecution(
                    sql=f"select {value}",
                    columns=[measure],
                    rows=[{measure: value}],
                    rows_returned=1,
                    route_type="source",
                    routed_sql=None,
                    aggregate_id=None,
                    pocket_id=None,
                    execution_ms=1,
                )

            with patch(
                "src.exec.recipe.execute_query",
                new=AsyncMock(
                    side_effect=[
                        _execution("Revenue", 100),
                        _execution("Units", 4),
                    ]
                ),
            ):
                execution = await execute_recipe(
                    target,
                    imported_project_id,
                    RunRecipeToolCall(
                        recipe_id=str(imported_recipe.id), parameters={}
                    ),
                    "jwt",
                    allowed_model_ids={imported_sales_id, imported_units_id},
                )
            assert execution.combine_value == 25
