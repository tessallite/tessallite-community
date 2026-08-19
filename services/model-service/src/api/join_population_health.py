"""Join population health read API (Model Health surface).

Contract: ``docs/architecture/architecture_join-population-governance.md``
invariant 6 — a model's ``OK``/``WARNING``/``BLOCKED`` rollup is a first-class
surfaced fact, not a log line.

  GET /projects/{project_id}/models/{model_id}/join-population-health

Deliberately the same shape as ``relationship_health.py``, the codebase's
existing deploy-time-evidence surface: a deploy-time verifier stages typed
evidence rows bound to the deployed version + deploy epoch, and a read-only
health endpoint projects the current state of those rows. This is health, not a
serving authority — no route reads it, and nothing here changes what a query
returns.

WARN-ONLY in this phase (governance plan G1). A ``BLOCKED`` model status is
reported here and in the deploy response, and the deploy still succeeds. Block
mode is phase G5.

``evaluated`` is the field that keeps this honest: it is False whenever the
model has joins with no measured verdict (validation switched off, source
unreachable, probe timed out, never deployed since the feature shipped).
Phase G5 MUST require ``evaluated`` before it refuses a deploy, so a
temporarily unreachable source can never become an outage.
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select

from shared.db.models import Join, JoinPopulationCheck, Model, ModelColumn, ModelTable
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    DEFAULT_POPULATION_PARTICIPATION,
    coerce_population_participation,
)
from shared.semantic.join_population_validator import (
    join_definition_fingerprint,
    roll_up_model_status,
)
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/join-population-health",
    tags=["join-population-health"],
)


class JoinPopulationHealthItem(BaseModel):
    join_id: str
    left_table_name: Optional[str] = None
    right_table_name: Optional[str] = None
    left_column_name: Optional[str] = None
    right_column_name: Optional[str] = None
    join_type: str
    # The join's declaration as it stands RIGHT NOW.
    population_participation: str
    # Bug-8667: the declaration the stored verdict was actually computed
    # against. ``PATCH /joins/{id}`` does not bump ``deploy_epoch``, so a
    # modeller who re-declares a BLOCKED join would otherwise see their new
    # declaration sitting next to the old verdict with ``stale=False`` and no
    # hint that the two disagree. None when this join has no verdict yet.
    checked_population_participation: Optional[str] = None
    # True when the verdict was computed against a DIFFERENT declaration than
    # the live one — i.e. re-deploy to refresh it.
    declaration_changed_since_check: bool = False
    # True when ANY classifier input moved since the verdict was measured —
    # the join's type or either of its join columns, not only its declaration.
    # ``PATCH /joins/{id}`` changes those without bumping the deploy epoch.
    inputs_changed_since_check: bool = False
    # neutral | filtering | multiplying — None when this join has no verdict
    # (never deployed since the feature shipped, or added after the last deploy).
    classification: Optional[str] = None
    # OK | WARNING | BLOCKED — None for the same reason.
    status: Optional[str] = None
    measured: bool = False
    row_loss_ratio: Optional[float] = None
    row_mult_ratio: Optional[float] = None
    row_effect_ratio: Optional[float] = None
    reason: Optional[str] = None
    checked_at: Optional[str] = None
    # True when the verdict no longer describes the join as it stands: either
    # the model moved (a newer deploy epoch) or a classifier input changed
    # under it. Both must count — keying staleness on the epoch alone let a
    # join re-typed from LEFT to INNER keep serving its old ``neutral``/``OK``
    # verdict, because PATCH does not bump the epoch.
    stale: bool = False


class JoinPopulationHealthResponse(BaseModel):
    model_id: str
    # OK | WARNING | BLOCKED
    status: str
    # False when any join lacks a measured verdict; see the module docstring.
    evaluated: bool
    join_count: int
    evaluated_count: int
    warning_count: int
    blocked_count: int
    # Always True in this phase — restated in the payload so a consumer never
    # has to infer the enforcement posture from the absence of an error.
    warn_only: bool = True
    items: list[JoinPopulationHealthItem]


@router.get(
    "",
    response_model=JoinPopulationHealthResponse,
    dependencies=[require_role("viewer")],
)
async def get_join_population_health(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> JoinPopulationHealthResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        model = await db.get(Model, model_id)
        if model is None or model.project_id != project_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Model {model_id} not found in project {project_id}",
            )

        joins = list(
            (
                await db.execute(
                    select(Join).where(Join.model_id == model_id).order_by(Join.id)
                )
            ).scalars().all()
        )
        checks = {
            c.join_id: c
            for c in (
                await db.execute(
                    select(JoinPopulationCheck).where(
                        JoinPopulationCheck.model_id == model_id
                    )
                )
            ).scalars().all()
        }

        table_names, column_names = await _resolve_names(db, model_id, joins)
        current_epoch = int(getattr(model, "deploy_epoch", 0) or 0)

        items: list[JoinPopulationHealthItem] = []
        for j in joins:
            check = checks.get(j.id)
            live_participation = coerce_population_participation(
                getattr(j, "population_participation", None)
                or DEFAULT_POPULATION_PARTICIPATION
            )
            checked_participation = (
                coerce_population_participation(check.population_participation)
                if check is not None else None
            )
            # A NULL fingerprint means the row predates the column, not that
            # nothing changed — do not manufacture a "still current" claim
            # from its absence.
            inputs_changed = bool(
                check is not None
                and check.inputs_fingerprint
                and check.inputs_fingerprint != join_definition_fingerprint(j)
            )
            items.append(
                JoinPopulationHealthItem(
                    join_id=str(j.id),
                    left_table_name=table_names.get(j.left_table_id),
                    right_table_name=table_names.get(j.right_table_id),
                    left_column_name=column_names.get(j.left_column_id),
                    right_column_name=column_names.get(j.right_column_id),
                    join_type=str(j.join_type or ""),
                    population_participation=live_participation,
                    checked_population_participation=checked_participation,
                    declaration_changed_since_check=(
                        checked_participation is not None
                        and checked_participation != live_participation
                    ),
                    classification=check.classification if check else None,
                    status=check.status if check else None,
                    measured=bool(check.measured) if check else False,
                    row_loss_ratio=check.row_loss_ratio if check else None,
                    row_mult_ratio=check.row_mult_ratio if check else None,
                    row_effect_ratio=check.row_effect_ratio if check else None,
                    reason=check.reason if check else None,
                    checked_at=(
                        check.checked_at.isoformat()
                        if check is not None and check.checked_at
                        else None
                    ),
                    inputs_changed_since_check=inputs_changed,
                    stale=(
                        check is not None
                        and (
                            int(check.deploy_epoch or 0) != current_epoch
                            or inputs_changed
                        )
                    ),
                )
            )

        # One rollup implementation, shared with the deploy path, fed the
        # persisted rows rather than a second parallel computation.
        rollup = roll_up_model_status(
            [checks[j.id] for j in joins if j.id in checks],
            join_count=len(joins),
        )
        return JoinPopulationHealthResponse(
            model_id=str(model_id),
            status=rollup.status,
            evaluated=rollup.evaluated,
            join_count=rollup.join_count,
            evaluated_count=rollup.evaluated_count,
            warning_count=rollup.warning_count,
            blocked_count=rollup.blocked_count,
            items=items,
        )

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="tenant database session unavailable",
    )


async def _resolve_names(
    db, model_id: UUID, joins: list[Join],
) -> tuple[dict[UUID, str], dict[UUID, str]]:
    """Batch-resolve table + column display names (no per-row queries)."""
    if not joins:
        return {}, {}
    tables = {
        t.id: (t.alias or t.display_name or t.physical_name)
        for t in (
            await db.execute(
                select(ModelTable).where(ModelTable.model_id == model_id)
            )
        ).scalars().all()
    }
    column_ids = {
        cid
        for j in joins
        for cid in (j.left_column_id, j.right_column_id)
        if cid is not None
    }
    columns = {
        c.id: c.column_name
        for c in (
            await db.execute(
                select(ModelColumn).where(ModelColumn.id.in_(column_ids))
            )
        ).scalars().all()
    } if column_ids else {}
    return tables, columns


async def summarise_join_population(db, model_id: UUID) -> dict:
    """Compact rollup for the deploy response (invariant 6, deploy surface).

    Read straight off the rows the deploy transaction just staged, so the
    modeller learns the verdict at the moment they act. Returns the same
    vocabulary the health endpoint returns. NEVER raises: this decorates a
    deploy response and must not be able to fail one.
    """
    try:
        join_count = int(
            (
                await db.execute(
                    select(func.count(Join.id)).where(Join.model_id == model_id)
                )
            ).scalar_one_or_none()
            or 0
        )
        rows = list(
            (
                await db.execute(
                    select(JoinPopulationCheck).where(
                        JoinPopulationCheck.model_id == model_id
                    )
                )
            ).scalars().all()
        )
        rollup = roll_up_model_status(rows, join_count=join_count)
        return {
            "status": rollup.status,
            "evaluated": rollup.evaluated,
            "join_count": rollup.join_count,
            "evaluated_count": rollup.evaluated_count,
            "warning_count": rollup.warning_count,
            "blocked_count": rollup.blocked_count,
            # Restated per response: BLOCKED is reported, not enforced (G1).
            "warn_only": True,
        }
    except Exception:
        logger.warning(
            "join population deploy summary unavailable for model %s",
            model_id, exc_info=True,
        )
        return {}
