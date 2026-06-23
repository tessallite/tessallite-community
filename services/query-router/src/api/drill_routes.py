"""Drill-through REST endpoints — semantic gateway path.

``POST /v1/measures/{measure_id}/drill-through``
``POST /v1/measures/{measure_id}/drill-options``

All drill queries route through the query-router's parse → bind →
route → rewrite → execute pipeline via ``_handle_execute``.  This
ensures UDA expansion, row security, persona gating, and aggregate
routing apply automatically.

Hierarchy-aware: ``/drill-options`` returns drillable hierarchies for a
cell; ``/drill-through`` accepts ``hierarchy_id`` and steps down one
level at a time.
"""
from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.middleware import CurrentEmbedUser, CurrentUser, enforce_model_scope, require_capability
from shared.db.models import Measure
from shared.db.session import get_tenant_db
from sqlalchemy import select as sa_select
from src.api._simulate import resolve_principal
from src.api.routes import ExecuteRequest, _handle_execute, _validate_force_route
from src.security.persona_gate import resolve_execution_persona
from src.drill.semantic_builder import (
    DrillDimension,
    DrillSemanticError,
    DrillableHierarchy,
    HierarchyPathEntry,
    build_drill_sql,
    encode_cursor,
    resolve_drill_options,
)

router = APIRouter(tags=["drill-through"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

DRILL_SUPPORTED_OPS = (
    "eq", "neq", "gt", "gte", "lt", "lte",
    "like", "ilike", "in", "between", "is_null", "is_not_null",
)


class DrillThroughFilter(BaseModel):
    column: str
    op: str = Field(
        default="eq",
        description="One of: " + " | ".join(DRILL_SUPPORTED_OPS),
    )
    value: Any | None = None


class DrillThroughRequest(BaseModel):
    filters: list[DrillThroughFilter] = Field(default_factory=list)
    grouping_levels: list[DrillThroughFilter] = Field(
        default_factory=list,
        description="Cell-position constraints — e.g. country=US, quarter=2024-Q2.",
    )
    cursor: str | None = None
    limit: int | None = Field(
        default=None,
        description="Page size. Clamped server-side; default 1000, max 10000.",
    )
    persona_id: Optional[str] = Field(
        default=None,
        description="Persona-as-catalog scope. Persona gating is applied by the /execute pipeline.",
    )
    hierarchy_id: Optional[str] = Field(
        default=None,
        description="Which hierarchy to drill down. When omitted and exactly "
        "one hierarchy is drillable, it is chosen automatically.",
    )
    force_route: Optional[str] = Field(
        default=None,
        description=(
            'Optional route override; "source" keeps force-live drill '
            "investigations on source data so they are not silently served "
            "from an aggregate/pocket."
        ),
    )


class DrillThroughPageInfo(BaseModel):
    cursor: str
    next_cursor: str | None = None
    has_more: bool


class DrillDimensionOut(BaseModel):
    id: str
    name: str
    display_name: str


class HierarchyPathEntryOut(BaseModel):
    level_name: str
    dimension_name: str
    value: Any | None = None


class DrillableHierarchyOut(BaseModel):
    hierarchy_id: str
    hierarchy_name: str
    current_level_name: str
    next_level_name: str


class DrillThroughResponse(BaseModel):
    columns: list[str]
    rows: list[dict[str, Any]]
    page: DrillThroughPageInfo
    drill_mode: str = Field(description='"hierarchy" or "leaf"')
    drill_dimension: DrillDimensionOut | None = None
    hierarchy_path: list[HierarchyPathEntryOut] = Field(default_factory=list)
    drillable_hierarchies: list[DrillableHierarchyOut] = Field(default_factory=list)
    fact_table: str | None = Field(
        default=None,
        description="Physical name of the source fact table the detail rows "
        "come from (transparency; leaf detail mode).",
    )
    route_type: str = ""
    execution_ms: int = 0
    bytes_processed: int = 0
    rows_returned: int = 0


class DrillOptionsResponse(BaseModel):
    hierarchies: list[DrillableHierarchyOut]


# ---------------------------------------------------------------------------
# /drill-options
# ---------------------------------------------------------------------------

@router.post(
    "/measures/{measure_id}/drill-options",
    response_model=DrillOptionsResponse,
)
async def drill_options(
    body: DrillThroughRequest,
    measure_id: UUID = Path(...),
    current_user: CurrentUser = Depends(require_capability("query")),
) -> DrillOptionsResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _enforce_measure_model_scope(db, current_user, measure_id)

        mid_row = await db.execute(
            sa_select(Measure.model_id).where(Measure.id == measure_id)
        )
        measure_model_id = mid_row.scalar_one_or_none()
        if measure_model_id is None:
            raise HTTPException(status_code=404, detail="Measure not found")

        persona = await resolve_execution_persona(
            db,
            current_user=current_user,
            model_id=str(measure_model_id),
            requested_persona_id=body.persona_id,
        )

        if persona:
            measure_allow = [str(x) for x in (persona.included_measure_ids or [])]
            if measure_allow and str(measure_id) not in measure_allow:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Measure not included in effective persona",
                )

        try:
            drillable = await resolve_drill_options(
                measure_id=measure_id,
                grouping_levels=[g.model_dump() for g in body.grouping_levels],
                db=db,
            )
        except DrillSemanticError as exc:
            raise HTTPException(status_code=400, detail={"error_code": exc.error_code, "detail": str(exc)})

        options = [_hierarchy_out(h) for h in drillable]
        if persona:
            hier_allow = [str(x) for x in (persona.included_hierarchy_ids or [])]
            if hier_allow:
                options = [o for o in options if o.hierarchy_id in hier_allow]

        return DrillOptionsResponse(hierarchies=options)


# ---------------------------------------------------------------------------
# /drill-through
# ---------------------------------------------------------------------------

@router.post(
    "/measures/{measure_id}/drill-through",
    response_model=DrillThroughResponse,
)
async def drill_through(
    body: DrillThroughRequest,
    measure_id: UUID = Path(...),
    current_user: CurrentUser = Depends(require_capability("query")),
    x_simulate_principal: str | None = Header(default=None, alias="X-Tessallite-Simulate-Principal"),
    x_simulate_roles: str | None = Header(default=None, alias="X-Tessallite-Simulate-Roles"),
    x_simulate_groups: str | None = Header(default=None, alias="X-Tessallite-Simulate-Groups"),
    x_simulate_claims: str | None = Header(default=None, alias="X-Tessallite-Simulate-Claims"),
) -> DrillThroughResponse:
    principal = resolve_principal(
        current_user, x_simulate_principal, x_simulate_roles,
        x_simulate_groups, x_simulate_claims,
    )
    async for db in get_tenant_db(current_user.tenant_id):
        await _enforce_measure_model_scope(db, current_user, measure_id)

        mid_row = await db.execute(
            sa_select(Measure.model_id).where(Measure.id == measure_id)
        )
        measure_model_id = mid_row.scalar_one_or_none()
        if measure_model_id is not None:
            persona = await resolve_execution_persona(
                db,
                current_user=current_user,
                model_id=str(measure_model_id),
                requested_persona_id=body.persona_id,
            )
            body.persona_id = str(persona.id) if persona else None

        return await _handle_drill_through(
            measure_id, body, db,
            principal=principal,
            user_identity=current_user.email or "",
            tenant_id=current_user.tenant_id,
        )


# ---------------------------------------------------------------------------
# Embed scope helper
# ---------------------------------------------------------------------------

async def _enforce_measure_model_scope(
    db: AsyncSession, current_user: CurrentUser, measure_id: UUID,
) -> None:
    """Resolve the measure's model_id and enforce embed model scope."""
    if not isinstance(current_user, CurrentEmbedUser) or not current_user.model_ids:
        return
    row = await db.execute(
        sa_select(Measure.model_id).where(Measure.id == measure_id)
    )
    mid = row.scalar_one_or_none()
    if mid is not None:
        enforce_model_scope(current_user, str(mid))


# ---------------------------------------------------------------------------
# Internal handler
# ---------------------------------------------------------------------------

async def _handle_drill_through(
    measure_id: UUID,
    body: DrillThroughRequest,
    db: AsyncSession,
    *,
    principal=None,
    user_identity: str = "",
    tenant_id: str = "",
) -> DrillThroughResponse:
    _validate_force_route(body.force_route)
    hierarchy_uuid: UUID | None = None
    if body.hierarchy_id:
        try:
            hierarchy_uuid = UUID(body.hierarchy_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid hierarchy_id UUID")

    try:
        (
            sql,
            model_id_str,
            offset,
            effective_limit,
            drill_dim,
            drill_mode,
            hierarchy_path,
            drillable,
            fact_table,
            source_join_path,
        ) = await build_drill_sql(
            measure_id=measure_id,
            hierarchy_id=hierarchy_uuid,
            grouping_levels=[g.model_dump() for g in body.grouping_levels],
            filters=[f.model_dump() for f in body.filters],
            cursor=body.cursor,
            limit=body.limit,
            db=db,
        )
    except DrillSemanticError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error_code": exc.error_code, "detail": str(exc)},
        )

    exec_request = ExecuteRequest(
        model_id=model_id_str,
        raw_query=sql,
        protocol="jdbc",
        persona_id=body.persona_id,
        force_route=body.force_route,
    )
    try:
        exec_response = await _handle_execute(
            exec_request,
            db,
            user_identity=user_identity,
            principal=principal,
            persona_id=body.persona_id,
            tenant_id=tenant_id,
            drill_join_path_ids=source_join_path,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    # F-019-17 — pagination composes correctly with row security.
    # The drill SQL embeds ``LIMIT effective_limit+1 OFFSET m`` (semantic_builder
    # `fetch_limit = effective_limit + 1`). Row security is applied by the
    # execute pipeline via per-scan WHERE injection (`router._inject_security_where`),
    # which ANDs the security predicate into the SAME SELECT that scans the
    # physical table — *before* GROUP BY / LIMIT / OFFSET (Bug-915). So
    # `exec_response.rows` are already the persona-allowed rows, capped at
    # effective_limit+1: `has_more` keys on the post-security count, the page is
    # a full page of *allowed* rows, and no allowed row beyond the window is
    # unreachable. (The pre-F-007-01 outer-wrap shape, where LIMIT ran before
    # the filter, is gone — see router._inject_security_where.)
    all_rows = exec_response.rows
    has_more = len(all_rows) > effective_limit
    if has_more:
        all_rows = all_rows[:effective_limit]

    next_cursor = encode_cursor(offset + effective_limit) if has_more else None

    return DrillThroughResponse(
        columns=exec_response.columns,
        rows=all_rows,
        page=DrillThroughPageInfo(
            cursor=body.cursor or encode_cursor(0),
            next_cursor=next_cursor,
            has_more=has_more,
        ),
        drill_mode=drill_mode,
        drill_dimension=_dim_out(drill_dim) if drill_dim else None,
        hierarchy_path=[_path_out(p) for p in hierarchy_path],
        drillable_hierarchies=[_hierarchy_out(h) for h in drillable],
        fact_table=fact_table,
        route_type=exec_response.route_type,
        execution_ms=exec_response.execution_ms,
        bytes_processed=exec_response.bytes_processed,
        rows_returned=len(all_rows),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hierarchy_out(h: DrillableHierarchy) -> DrillableHierarchyOut:
    return DrillableHierarchyOut(
        hierarchy_id=str(h.hierarchy_id),
        hierarchy_name=h.hierarchy_name,
        current_level_name=h.current_level_name,
        next_level_name=h.next_level_name,
    )


def _dim_out(d: DrillDimension) -> DrillDimensionOut:
    return DrillDimensionOut(
        id=str(d.id),
        name=d.name,
        display_name=d.display_name,
    )


def _path_out(p: HierarchyPathEntry) -> HierarchyPathEntryOut:
    return HierarchyPathEntryOut(
        level_name=p.level_name,
        dimension_name=p.dimension_name,
        value=p.value,
    )
