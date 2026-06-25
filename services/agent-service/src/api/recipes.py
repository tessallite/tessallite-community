"""Cross-model recipe CRUD (Phase Agent-B3.2).

Endpoints:
  GET    /projects/{project_id}/agent/recipes
  POST   /projects/{project_id}/agent/recipes
  GET    /projects/{project_id}/agent/recipes/{recipe_id}
  PUT    /projects/{project_id}/agent/recipes/{recipe_id}
  DELETE /projects/{project_id}/agent/recipes/{recipe_id}

ROLLBACK CANDIDATE: this table can fold back into
project_agent_configs.cross_model_calculations: jsonb if maintenance
proves heavy — see D2 of the answered questions doc.
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from shared.db.models import Model, ProjectAgentConfig, ProjectCrossModelRecipe
from shared.db.session import get_tenant_db
from src.api.agent_config import (
    _require_project_modeller,
    _require_project_viewer,
)
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.recipes.eval import validate_expression

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/projects/{project_id}/agent/recipes",
    tags=["agent-recipes"],
)


class RecipeStep(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    model_id: UUID
    measures: list[str] = []
    dimensions: list[str] = []
    filters: list[dict] = []
    limit: int = 100


class RecipeParameter(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: Optional[str] = None
    resolves_to_glossary_entity: bool = False


class RecipeBody(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: Optional[str] = None
    parameters: list[RecipeParameter] = []
    steps: list[RecipeStep] = []
    # Semantic expression tree (Bug-5346) or null when the recipe has no
    # combine step. Never a formula string — see src/recipes/eval.py.
    combine: Optional[dict] = None
    notes: Optional[str] = None


class RecipeResponse(RecipeBody):
    model_config = ConfigDict(from_attributes=True)

    id: UUID


def _serialise(record: ProjectCrossModelRecipe) -> RecipeResponse:
    return RecipeResponse(
        id=record.id,
        name=record.name,
        description=record.description,
        parameters=[RecipeParameter(**p) for p in (record.parameters or [])],
        steps=[RecipeStep(**s) for s in (record.steps or [])],
        combine=record.combine,
        notes=record.notes,
    )


async def _require_agent(db, project_id: UUID) -> None:
    cfg = await db.execute(
        select(ProjectAgentConfig).where(
            ProjectAgentConfig.project_id == project_id
        )
    )
    if cfg.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=404, detail="Project agent not configured"
        )


async def _validate_step_models(db, project_id: UUID, steps: list[RecipeStep]) -> None:
    """F-023-07 — every step model must belong to this project (mirrors
    the allow-list replace endpoint). The agent allow-list itself is
    enforced at execution time by the execution chokepoint."""
    for step in steps:
        m = await db.get(Model, step.model_id)
        if m is None or m.project_id != project_id:
            raise HTTPException(
                status_code=400,
                detail=f"Model {step.model_id} not in project {project_id}",
            )


def _validate_combine(combine: Optional[dict], steps: list[RecipeStep]) -> None:
    """Statically validate the combine expression tree against the steps'
    declared measures (Bug-5346).

    ``combine`` is a semantic ExprNode tree (or ``None`` for a recipe with no
    combine step). ``validate_expression`` walks the tree: every ``ref`` node
    must name an existing step and a measure declared on that step, and the
    structure must be a well-formed node tree — without executing anything. At
    run time the executed step rows alias measures by name
    (``SUM("revenue") AS "revenue"``), so a statically valid tree evaluates
    against real rows."""
    if combine is None:
        return
    errors = validate_expression(combine, steps)
    if errors:
        raise HTTPException(
            status_code=422,
            detail="combine expression rejected: " + " ".join(errors),
        )


@router.get("", response_model=list[RecipeResponse])
async def list_recipes(
    project_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[RecipeResponse]:
    await _require_project_viewer(project_id, current_user)
    async for db in get_tenant_db(current_user.tenant_id):
        await _require_agent(db, project_id)
        rows = await db.execute(
            select(ProjectCrossModelRecipe)
            .where(ProjectCrossModelRecipe.project_id == project_id)
            .order_by(ProjectCrossModelRecipe.name)
        )
        return [_serialise(r) for r in rows.scalars().all()]
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.post("", response_model=RecipeResponse, status_code=201)
async def create_recipe(
    project_id: UUID,
    body: RecipeBody,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> RecipeResponse:
    # F-023-07 — recipe writes are modeller/admin surface, consistent
    # with the sibling agent config and allow-list write routes.
    await _require_project_modeller(project_id, current_user)
    _validate_combine(body.combine, body.steps)
    async for db in get_tenant_db(current_user.tenant_id):
        await _require_agent(db, project_id)
        await _validate_step_models(db, project_id, body.steps)
        record = ProjectCrossModelRecipe(
            project_id=project_id,
            name=body.name,
            description=body.description,
            parameters=[p.model_dump() for p in body.parameters],
            steps=[s.model_dump(mode="json") for s in body.steps],
            combine=body.combine,
            notes=body.notes,
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)
        return _serialise(record)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.get("/{recipe_id}", response_model=RecipeResponse)
async def get_recipe(
    project_id: UUID,
    recipe_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> RecipeResponse:
    await _require_project_viewer(project_id, current_user)
    async for db in get_tenant_db(current_user.tenant_id):
        await _require_agent(db, project_id)
        record = await db.get(ProjectCrossModelRecipe, recipe_id)
        if record is None or record.project_id != project_id:
            raise HTTPException(status_code=404, detail="Recipe not found")
        return _serialise(record)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.put("/{recipe_id}", response_model=RecipeResponse)
async def update_recipe(
    project_id: UUID,
    recipe_id: UUID,
    body: RecipeBody,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> RecipeResponse:
    # F-023-07 — recipe writes are modeller/admin surface.
    await _require_project_modeller(project_id, current_user)
    _validate_combine(body.combine, body.steps)
    async for db in get_tenant_db(current_user.tenant_id):
        await _require_agent(db, project_id)
        await _validate_step_models(db, project_id, body.steps)
        record = await db.get(ProjectCrossModelRecipe, recipe_id)
        if record is None or record.project_id != project_id:
            raise HTTPException(status_code=404, detail="Recipe not found")
        record.name = body.name
        record.description = body.description
        record.parameters = [p.model_dump() for p in body.parameters]
        record.steps = [s.model_dump(mode="json") for s in body.steps]
        record.combine = body.combine
        record.notes = body.notes
        await db.commit()
        await db.refresh(record)
        return _serialise(record)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.delete("/{recipe_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_recipe(
    project_id: UUID,
    recipe_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    # F-023-07 — recipe writes are modeller/admin surface.
    await _require_project_modeller(project_id, current_user)
    async for db in get_tenant_db(current_user.tenant_id):
        await _require_agent(db, project_id)
        record = await db.get(ProjectCrossModelRecipe, recipe_id)
        if record is None or record.project_id != project_id:
            raise HTTPException(status_code=404, detail="Recipe not found")
        await db.delete(record)
        await db.commit()
