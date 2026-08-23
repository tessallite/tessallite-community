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

G5 enforcement is active at the deploy boundary: a measured, policy-relevant
``BLOCKED`` row refuses deployment before publish state changes. This read API
continues to report the full rollup, including unmeasured rows, so the
modeller can see what must be corrected or re-measured.

``evaluated`` is the field that keeps this honest: it is False whenever the
model has joins with no measured verdict (validation switched off, source
unreachable, probe timed out, never deployed since the feature shipped).
Enforcement is per row rather than gated by the aggregate ``evaluated`` flag:
an unmeasured row never blocks, but a measured blocker still blocks a mixed
model so it cannot be hidden by another timeout or disabled measurement.
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from shared.db.models import (
    Join,
    JoinPopulationCheck,
    Model,
    ModelColumn,
    ModelTable,
    ModelVersion,
)
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    DEFAULT_POPULATION_PARTICIPATION,
    coerce_population_participation,
)
from shared.semantic.join_population_validator import (
    _join_labels,
    _snapshot_graph,
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
    # The selected deployed snapshot's declaration. Live draft edits are not
    # serving or health authority while a model is deployed.
    population_participation: str
    # The declaration the stored verdict was actually computed against. None
    # when this selected join has no verdict yet.
    checked_population_participation: Optional[str] = None
    # True when the persisted evidence disagrees with the selected snapshot.
    declaration_changed_since_check: bool = False
    # True when ANY classifier input moved since the verdict was measured —
    # the join's type or either of its join columns, not only its declaration.
    # This compares the selected snapshot fingerprint, never mutable joins.
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
    # True when the verdict no longer describes the selected deployed join:
    # either the model moved (a newer deploy epoch) or selected inputs differ.
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
    # Retained for API compatibility; false is the active G5 posture.
    warn_only: bool = False
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

        joins, tables, columns = await _load_definition_graph(db, model)
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
        selected_ids = {join.id for join in joins}
        checks = {
            join_id: check
            for join_id, check in checks.items()
            if join_id in selected_ids
        }

        current_epoch = int(getattr(model, "deploy_epoch", 0) or 0)
        item_payloads = _project_items(
            joins, tables, columns, checks, current_epoch=current_epoch,
            prefer_alias=getattr(model, "deployed_version_id", None) is None,
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
            items=[JoinPopulationHealthItem(**item) for item in item_payloads],
        )

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="tenant database session unavailable",
    )


async def _load_definition_graph(
    db, model: Model,
) -> tuple[list[Any], dict[Any, Any], dict[Any, Any]]:
    """Load the deployed snapshot graph, falling back to draft only if absent.

    A deployed model's snapshot is the sole definition authority.  A missing
    or malformed pointed-to version fails closed to an empty graph; it must
    never silently substitute today's mutable draft.  Undeployed models retain
    the pre-G5 draft health view because they have no serving snapshot.
    """
    deployed_version_id = getattr(model, "deployed_version_id", None)
    if deployed_version_id is not None:
        version = await db.get(ModelVersion, deployed_version_id)
        snapshot = getattr(version, "snapshot_json", None) if version else None
        if isinstance(snapshot, dict):
            return _snapshot_graph(snapshot)
        logger.error(
            "deployed model %s has no usable snapshot for join health",
            getattr(model, "id", None),
        )
        return [], {}, {}

    joins = list(
        (
            await db.execute(
                select(Join).where(Join.model_id == model.id).order_by(Join.id)
            )
        ).scalars().all()
    )
    table_rows = list(
        (
            await db.execute(
                select(ModelTable).where(ModelTable.model_id == model.id)
            )
        ).scalars().all()
    )
    tables = {table.id: table for table in table_rows}
    column_ids = {
        cid
        for join in joins
        for cid in (join.left_column_id, join.right_column_id)
        if cid is not None
    }
    columns = {
        column.id: column
        for column in (
            await db.execute(
                select(ModelColumn).where(ModelColumn.id.in_(column_ids))
            )
        ).scalars().all()
    } if column_ids else {}
    return joins, tables, columns


def _project_items(
    joins: list[Any],
    tables: dict[Any, Any],
    columns: dict[Any, Any],
    checks: dict[Any, Any],
    *,
    current_epoch: int,
    prefer_alias: bool = False,
) -> list[dict[str, Any]]:
    """Project selected definition rows with their version-bound evidence."""
    labels = _join_labels(joins, tables, columns, prefer_alias=prefer_alias)
    items: list[dict[str, Any]] = []
    for join in joins:
        check = checks.get(join.id)
        selected_participation = coerce_population_participation(
            getattr(join, "population_participation", None)
            or DEFAULT_POPULATION_PARTICIPATION
        )
        checked_participation = (
            coerce_population_participation(check.population_participation)
            if check is not None else None
        )
        selected_fingerprint = join_definition_fingerprint(join)
        # Missing evidence fingerprints are old rows, not proof of equality.
        inputs_changed = bool(
            check is not None
            and (
                not getattr(check, "inputs_fingerprint", None)
                or check.inputs_fingerprint != selected_fingerprint
            )
        )
        label = labels.get(str(join.id), {})
        # New rows persist labels, while selected snapshots remain the primary
        # source. The stored labels cover old snapshots whose graph names were
        # unavailable at the time evidence was written.
        def label_value(name: str) -> Any:
            return label.get(name) or getattr(check, name, None)

        items.append({
            "join_id": str(join.id),
            "left_table_name": label_value("left_table_name"),
            "right_table_name": label_value("right_table_name"),
            "left_column_name": label_value("left_column_name"),
            "right_column_name": label_value("right_column_name"),
            "join_type": str(getattr(join, "join_type", None) or ""),
            "population_participation": selected_participation,
            "checked_population_participation": checked_participation,
            "declaration_changed_since_check": (
                checked_participation is not None
                and checked_participation != selected_participation
            ),
            "inputs_changed_since_check": inputs_changed,
            "classification": getattr(check, "classification", None),
            "status": getattr(check, "status", None),
            "measured": bool(getattr(check, "measured", False)) if check else False,
            "row_loss_ratio": getattr(check, "row_loss_ratio", None),
            "row_mult_ratio": getattr(check, "row_mult_ratio", None),
            "row_effect_ratio": getattr(check, "row_effect_ratio", None),
            "reason": getattr(check, "reason", None),
            "checked_at": (
                check.checked_at.isoformat()
                if check is not None and check.checked_at else None
            ),
            "stale": bool(
                check is not None
                and (
                    int(getattr(check, "deploy_epoch", 0) or 0) != current_epoch
                    or inputs_changed
                )
            ),
        })
    return items


async def summarise_join_population(
    db, model_id: UUID, *, snapshot: Optional[dict] = None,
) -> dict:
    """Compact rollup for the deploy response (invariant 6, deploy surface).

    Read straight off the rows the deploy transaction just staged and the
    selected deployed snapshot graph, so the modeller learns the verdict at
    the moment they act. NEVER raises: this decorates a deploy response and
    must not be able to fail one.
    """
    try:
        if snapshot is not None:
            joins, tables, columns = _snapshot_graph(snapshot)
        else:
            model = await db.get(Model, model_id)
            joins, tables, columns = await _load_definition_graph(db, model)
        rows = list(
            (
                await db.execute(
                    select(JoinPopulationCheck).where(
                        JoinPopulationCheck.model_id == model_id
                    )
                )
            ).scalars().all()
        )
        selected_ids = {join.id for join in joins}
        rows = [row for row in rows if row.join_id in selected_ids]
        checks = {row.join_id: row for row in rows}
        rollup = roll_up_model_status(rows, join_count=len(joins))
        return {
            "status": rollup.status,
            "evaluated": rollup.evaluated,
            "join_count": rollup.join_count,
            "evaluated_count": rollup.evaluated_count,
            "warning_count": rollup.warning_count,
            "blocked_count": rollup.blocked_count,
            # Retained for compatibility; measured blockers are enforced by
            # the deploy route before any publish state is committed.
            "warn_only": False,
            "items": _project_items(
                joins, tables, columns, checks,
                current_epoch=(
                    int(getattr(model, "deploy_epoch", 0) or 0)
                    if snapshot is None else int(
                        max(
                            (
                                int(getattr(row, "deploy_epoch", 0) or 0)
                                for row in rows
                            ),
                            default=0,
                        )
                    )
                ),
                prefer_alias=(
                    snapshot is None
                    and getattr(model, "deployed_version_id", None) is None
                ),
            ),
        }
    except Exception:
        logger.warning(
            "join population deploy summary unavailable for model %s",
            model_id, exc_info=True,
        )
        return {}
