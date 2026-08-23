"""Derived-grain relationship health read API (Model Health surface).

Spec: strategy_derived-grain-operational-serving.md §E. Extends the Model
Health report to list each declared attribute relationship with its CURRENT health
state (the state the router trust predicate keys off), when it was last checked, and
the detail column it accelerates.

  GET /projects/{project_id}/models/{model_id}/relationship-health

The state per relationship is the status of its NEWEST verification evidence row
(artifact-local AGGREGATE evidence first, falling back to the deploy-time
DEPLOY_CHECK row when no artifact has been built yet). This is read-only health, not
a serving authority: the router still evaluates the full trust predicate at query
time. A relationship with no evidence yet is ``pending``.
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    Dimension,
    DimensionAttributeRelationship,
    DimensionAttributeVerification,
    Model,
    ModelColumn,
)
from shared.db.session import get_tenant_db
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/relationship-health",
    tags=["relationship-health"],
)


# Map raw evidence status -> a stable, user-facing health state. VERIFIED (serving)
# reads as "healthy"; BROKEN/ERROR/STALE/PENDING each map to their own state so the
# modeller sees WHY a relationship is not currently serving.
_STATE_BY_STATUS = {
    "VERIFIED": "healthy",
    "BROKEN": "broken",
    "STALE": "stale",
    "ERROR": "error",
    "PENDING": "pending",
}


class RelationshipHealthItem(BaseModel):
    relationship_id: str
    dimension_id: str
    dimension_name: Optional[str] = None
    # The detail column the relabel accelerates (grouping by it is served from the
    # key-grained aggregate) + the internal key column the bijection maps from.
    detail_column_name: Optional[str] = None
    key_column_name: Optional[str] = None
    cardinality: str
    enabled: bool
    # healthy | broken | stale | error | pending
    state: str
    last_checked_at: Optional[str] = None
    error_code: Optional[str] = None


class RelationshipHealthResponse(BaseModel):
    model_id: str
    items: list[RelationshipHealthItem]


@router.get(
    "",
    response_model=RelationshipHealthResponse,
    dependencies=[require_role("viewer")],
)
async def list_relationship_health(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> RelationshipHealthResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        model = await db.get(Model, model_id)
        if model is None or model.project_id != project_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Model {model_id} not found in project {project_id}",
            )

        rels = (
            await db.execute(
                select(DimensionAttributeRelationship).where(
                    DimensionAttributeRelationship.model_id == model_id,
                )
            )
        ).scalars().all()
        if not rels:
            return RelationshipHealthResponse(model_id=str(model_id), items=[])

        # Resolve dimension + column names in one batch each (no per-row queries).
        dim_ids = {r.dimension_id for r in rels}
        col_ids = {
            cid
            for r in rels
            for cid in (r.detail_column_id, r.key_column_id)
            if cid is not None
        }
        dim_names = {
            d.id: d.name
            for d in (
                await db.execute(
                    select(Dimension).where(Dimension.id.in_(dim_ids))
                )
            ).scalars().all()
        }
        col_names = {
            c.id: c.column_name
            for c in (
                await db.execute(
                    select(ModelColumn).where(ModelColumn.id.in_(col_ids))
                )
            ).scalars().all()
        } if col_ids else {}

        items: list[RelationshipHealthItem] = []
        for rel in rels:
            ev = await _newest_evidence(db, rel.id)
            if ev is None:
                state = "pending"
                last_checked = None
                error_code = None
            else:
                state = _STATE_BY_STATUS.get(str(ev.status), "pending")
                last_checked = (
                    ev.checked_at.isoformat() if ev.checked_at else None
                )
                error_code = ev.error_code
            items.append(
                RelationshipHealthItem(
                    relationship_id=str(rel.id),
                    dimension_id=str(rel.dimension_id),
                    dimension_name=dim_names.get(rel.dimension_id),
                    detail_column_name=(
                        col_names.get(rel.detail_column_id)
                        if rel.detail_column_id else None
                    ),
                    key_column_name=(
                        col_names.get(rel.key_column_id)
                        if rel.key_column_id else None
                    ),
                    cardinality=rel.cardinality,
                    enabled=bool(rel.enabled),
                    state=state,
                    last_checked_at=last_checked,
                    error_code=error_code,
                )
            )
        return RelationshipHealthResponse(model_id=str(model_id), items=items)

    raise HTTPException(status_code=500, detail="DB session exhausted")


async def _newest_evidence(
    db: AsyncSession, relationship_id: UUID,
) -> Optional[DimensionAttributeVerification]:
    """Newest verification evidence row for one relationship.

    Prefers artifact-local AGGREGATE evidence (what the sweep writes and what the
    trust predicate consumes); falls back to the deploy-time DEPLOY_CHECK row when
    no artifact has been built. Ordered by ``checked_at`` then id so the newest row
    wins deterministically.
    """
    # Artifact-local (serving) evidence first.
    ev = (
        await db.execute(
            select(DimensionAttributeVerification)
            .where(
                DimensionAttributeVerification.relationship_id == relationship_id,
                DimensionAttributeVerification.artifact_kind != "DEPLOY_CHECK",
            )
            .order_by(
                DimensionAttributeVerification.checked_at.desc(),
                DimensionAttributeVerification.id.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if ev is not None:
        return ev
    # Fall back to the deploy-time health check.
    return (
        await db.execute(
            select(DimensionAttributeVerification)
            .where(
                DimensionAttributeVerification.relationship_id == relationship_id,
            )
            .order_by(
                DimensionAttributeVerification.checked_at.desc(),
                DimensionAttributeVerification.id.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
