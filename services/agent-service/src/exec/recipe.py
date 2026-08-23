"""Cross-model recipe execution (Phase Agent-B3.5).

Iterate the recipe's `steps`, run each via the existing single-model
query executor, and feed the first row of each step into the combine
expression evaluator. Recipes name their steps; the combine expression
references those names (e.g. `sales.revenue / units.qty`).

Parameters with `resolves_to_glossary_entity=True` get a token-overlap
lookup against the glossary so a user-supplied label can be resolved to
the canonical synonym (B3.7). Non-glossary parameters pass through
unchanged.
"""
from __future__ import annotations

import logging
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    GlossaryEntry,
    GlossarySynonym,
    ProjectCrossModelRecipe,
)
from src.exec.query import (
    PersonaFieldScope,
    QueryExecution,
    QueryExecutionError,
    execute_query,
)
from src.recipes.eval import CombineEvalError, evaluate_combine
from src.tools.spec import QueryToolCall, RunRecipeToolCall

logger = logging.getLogger(__name__)


@dataclass
class StepResult:
    name: str
    execution: QueryExecution
    first_row: dict[str, Any]


@dataclass
class RecipeExecution:
    recipe_id: UUID
    recipe_name: str
    steps: list[StepResult] = field(default_factory=list)
    combine_expression: Any = None  # semantic ExprNode tree (Bug-5346) or None
    combine_value: Any = None
    parameters_resolved: dict[str, Any] = field(default_factory=dict)


class RecipeExecutionError(RuntimeError):
    """Recipe is missing, mis-configured, or a step / combine failed."""


def _substitute_params(
    value: Any, parameters: dict[str, Any]
) -> Any:
    """Replace `{param_name}` tokens inside string values with parameters."""
    if isinstance(value, str):
        out = value
        for k, v in parameters.items():
            out = out.replace("{" + k + "}", str(v))
        return out
    if isinstance(value, list):
        return [_substitute_params(v, parameters) for v in value]
    if isinstance(value, dict):
        return {k: _substitute_params(v, parameters) for k, v in value.items()}
    return value


async def _resolve_parameters(
    db: AsyncSession,
    recipe: ProjectCrossModelRecipe,
    raw_params: dict[str, Any],
) -> dict[str, Any]:
    declared = recipe.parameters or []
    step_model_ids: list[UUID] = []
    for s in recipe.steps or []:
        if isinstance(s, dict) and s.get("model_id"):
            try:
                step_model_ids.append(UUID(str(s["model_id"])))
            except ValueError:
                continue

    resolved: dict[str, Any] = {}
    for spec in declared:
        name = spec.get("name") if isinstance(spec, dict) else None
        if not name:
            continue
        supplied = raw_params.get(name)
        if supplied is None:
            resolved[name] = None
            continue
        if not (isinstance(spec, dict) and spec.get("resolves_to_glossary_entity")):
            resolved[name] = supplied
            continue
        if not isinstance(supplied, str) or not supplied.strip() or not step_model_ids:
            resolved[name] = supplied
            continue
        target = supplied.strip().lower()
        entries_q = await db.execute(
            select(GlossaryEntry).where(
                GlossaryEntry.model_id.in_(step_model_ids),
                GlossaryEntry.status == "approved",
            )
        )
        entries = list(entries_q.scalars().all())
        match: GlossaryEntry | None = None
        for e in entries:
            if (e.term or "").strip().lower() == target:
                match = e
                break
        if match is None and entries:
            syn_q = await db.execute(
                select(GlossarySynonym).where(
                    GlossarySynonym.entry_id.in_([e.id for e in entries])
                )
            )
            by_id = {e.id: e for e in entries}
            for syn in syn_q.scalars().all():
                if (syn.synonym or "").strip().lower() == target:
                    match = by_id.get(syn.entry_id)
                    if match is not None:
                        break
        resolved[name] = match.term if match is not None else supplied
    for k, v in raw_params.items():
        if k not in resolved:
            resolved[k] = v
    return resolved


async def execute_recipe(
    db: AsyncSession,
    project_id: UUID,
    call: RunRecipeToolCall,
    jwt_token: str,
    publisher: Any = None,
    *,
    allowed_model_ids: Collection[UUID],
    persona_scopes: Mapping[UUID, PersonaFieldScope] | None,
) -> RecipeExecution:
    try:
        recipe_uuid = UUID(call.recipe_id)
    except ValueError as exc:
        raise RecipeExecutionError(
            f"Invalid recipe_id from LLM: {call.recipe_id!r}"
        ) from exc

    recipe = await db.get(ProjectCrossModelRecipe, recipe_uuid)
    if recipe is None or recipe.project_id != project_id:
        raise RecipeExecutionError(
            f"Recipe {call.recipe_id} not found in this project."
        )

    parameters = await _resolve_parameters(db, recipe, call.parameters)

    result = RecipeExecution(
        recipe_id=recipe.id,
        recipe_name=recipe.name,
        combine_expression=recipe.combine,
        parameters_resolved=parameters,
    )

    combine_ctx: dict[str, Any] = {}
    for step_spec in recipe.steps or []:
        if not isinstance(step_spec, dict):
            raise RecipeExecutionError("Recipe step is not an object.")
        name = step_spec.get("name")
        model_id = step_spec.get("model_id")
        if not name or not model_id:
            raise RecipeExecutionError("Recipe step missing name or model_id.")

        # F-023-26(b) — parameter substitution applies to every value-bearing
        # clause, not just `where`. Previously a `{param}` placed in `having`
        # or `sort` passed through literally (no substitution, no warning).
        substituted_where = _substitute_params(
            step_spec.get("where") or step_spec.get("filters") or [],
            parameters,
        )
        substituted_having = _substitute_params(
            list(step_spec.get("having") or []), parameters,
        )
        substituted_sort = _substitute_params(
            list(step_spec.get("sort") or []), parameters,
        )

        step_call = QueryToolCall(
            model_id=str(model_id),
            measures=list(step_spec.get("measures") or []),
            dimensions=list(step_spec.get("dimensions") or []),
            where=substituted_where,
            having=substituted_having,
            sort=substituted_sort,
            limit=int(step_spec.get("limit") or 100),
        )

        try:
            # F-023-07 — recipe steps run through the same execution
            # chokepoint as the direct query path; the allow-list and
            # persona scope are enforced inside execute_query.
            execution = await execute_query(
                db, step_call, jwt_token,
                allowed_model_ids=allowed_model_ids,
                persona_scopes=persona_scopes,
            )
        except QueryExecutionError as exc:
            raise RecipeExecutionError(
                f"Recipe step {name!r} failed: {exc}"
            ) from exc

        first_row = execution.rows[0] if execution.rows else {}
        result.steps.append(
            StepResult(name=name, execution=execution, first_row=first_row)
        )
        combine_ctx[name] = first_row
        if publisher is not None:
            await publisher.emit(
                "recipe.step",
                step_name=name,
                model_id=str(model_id),
                rows_returned=execution.rows_returned,
                first_row=first_row,
            )

    if result.combine_expression is not None:
        try:
            result.combine_value = evaluate_combine(
                result.combine_expression, combine_ctx
            )
        except CombineEvalError as exc:
            raise RecipeExecutionError(
                f"Combine expression failed: {exc}"
            ) from exc

    return result
