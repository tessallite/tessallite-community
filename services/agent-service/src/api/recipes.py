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
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from shared.db.model_lock import acquire_model_definition_lock
from shared.db.models import Measure, Model, ProjectAgentConfig, ProjectCrossModelRecipe
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
    # Bug-7367 -- enforce the same 1..1000 row-limit contract as the LLM tool
    # spec (src/tools/spec.py). Without bounds, a modeller could save a recipe
    # with zero, negative, or extremely large limits that bypass the query-router
    # row-limit guardrail.
    limit: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode="after")
    def _validate_filter_shapes(self) -> "RecipeStep":
        """Bug-7360 -- validate between/in filter value shapes at save time
        so a malformed filter is caught on the recipe API, not silently
        dropped at execution time (wrong numbers)."""
        for f in self.filters:
            op = f.get("op")
            val = f.get("value")
            if op == "between":
                if not isinstance(val, (list, tuple)) or len(val) != 2:
                    raise ValueError(
                        f"Filter on {f.get('name')!r} with op='between' "
                        f"requires a 2-element list as 'value'."
                    )
            if op == "in" and val is not None:
                if not isinstance(val, list):
                    raise ValueError(
                        f"Filter on {f.get('name')!r} with op='in' "
                        f"requires a list as 'value'."
                    )
        return self


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


def _clamp_step_limit(step_dict: dict) -> dict:
    """Bug-7367 -- clamp pre-existing out-of-range limits on read so
    recipes saved before the validation tightening remain readable."""
    d = dict(step_dict)
    raw = d.get("limit", 100)
    d["limit"] = max(1, min(1000, raw if isinstance(raw, int) else 100))
    return d


def _serialise(record: ProjectCrossModelRecipe) -> RecipeResponse:
    # Bug-7367 / Bug-7360 -- use model_construct to skip validators on
    # read so pre-existing recipes with out-of-range limits or malformed
    # filters remain readable.  Validation runs on write (POST/PUT).
    return RecipeResponse(
        id=record.id,
        name=record.name,
        description=record.description,
        parameters=[RecipeParameter(**p) for p in (record.parameters or [])],
        steps=[RecipeStep.model_construct(**_clamp_step_limit(s)) for s in (record.steps or [])],
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

        declared = set(step.measures)
        if not declared:
            continue
        result = await db.execute(
            select(Measure.name).where(
                Measure.model_id == step.model_id,
                Measure.name.in_(declared),
            )
        )
        missing = sorted(declared - set(result.scalars().all()))
        if missing:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Recipe step {step.name!r} references measures that do not "
                    f"exist in model {step.model_id}: {', '.join(missing)}"
                ),
            )


def _persisted_step_model_ids(steps: object) -> set[UUID]:
    model_ids: set[UUID] = set()
    if not isinstance(steps, list):
        return model_ids
    for step in steps:
        if not isinstance(step, dict):
            continue
        try:
            model_ids.add(UUID(str(step.get("model_id"))))
        except (TypeError, ValueError, AttributeError):
            continue
    return model_ids


async def _lock_recipe_models(
    db,
    steps: list[RecipeStep],
    persisted_steps: object = None,
) -> None:
    """Serialize recipe writes with every referenced model-definition edit."""
    model_ids = {step.model_id for step in steps}
    model_ids.update(_persisted_step_model_ids(persisted_steps))
    for model_id in sorted(model_ids, key=lambda value: value.int):
        await acquire_model_definition_lock(db, model_id)


def _validate_step_names_unique(steps: list[RecipeStep]) -> None:
    """Bug-8500 — reject recipes with duplicate step names (case-insensitive).

    During execution, step results are stored by name and combine references
    resolve by name. A duplicate step name causes one result to silently
    overwrite another, producing wrong numbers."""
    seen: dict[str, int] = {}
    for idx, step in enumerate(steps):
        lower = step.name.lower()
        if lower in seen:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Duplicate step name {step.name!r} at indices "
                    f"{seen[lower]} and {idx}."
                ),
            )
        seen[lower] = idx


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
    _validate_step_names_unique(body.steps)
    _validate_combine(body.combine, body.steps)
    async for db in get_tenant_db(current_user.tenant_id):
        await _require_agent(db, project_id)
        await _lock_recipe_models(db, body.steps)
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
    _validate_step_names_unique(body.steps)
    _validate_combine(body.combine, body.steps)
    async for db in get_tenant_db(current_user.tenant_id):
        await _require_agent(db, project_id)
        record = await db.get(ProjectCrossModelRecipe, recipe_id)
        if record is None or record.project_id != project_id:
            raise HTTPException(status_code=404, detail="Recipe not found")
        # Lock both the existing and replacement model sets. Locking only the
        # replacement set lets a concurrent rename of a removed model rewrite
        # this row from a stale pre-update JSON image and resurrect the old step.
        await _lock_recipe_models(db, body.steps, record.steps)
        await db.refresh(record)
        if record.project_id != project_id:
            raise HTTPException(status_code=404, detail="Recipe not found")
        await _validate_step_models(db, project_id, body.steps)
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
