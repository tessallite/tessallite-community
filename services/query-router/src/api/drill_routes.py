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

import logging
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.middleware import CurrentEmbedUser, CurrentUser, enforce_model_scope, require_capability
from shared.auth.project_access import load_authorized_model
from shared.db.models import Measure
from shared.db.session import get_tenant_db
from sqlalchemy import select as sa_select
from src.api._simulate import (
    persona_current_user_for_principal,
    resolve_principal,
    simulate_headers_present,
)
from src.api.routes import ExecuteRequest, _handle_execute, _validate_force_route
from src.security.persona_gate import resolve_execution_persona
from src.drill.cursor import CursorValidationError
from src.drill.semantic_builder import (
    DrillDimension,
    DrillSemanticError,
    DrillableHierarchy,
    HierarchyPathEntry,
    build_drill_sql,
    resolve_drill_options,
)

logger = logging.getLogger(__name__)

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
    override_agg: Optional[str] = Field(
        default=None,
        description=(
            "Bug-6273: aggregate to apply to the measure on a hierarchy "
            "step-down, overriding the measure's default_agg. The pivot column "
            "the user drilled from may have been aggregated with a non-default "
            "function (e.g. a SUM measure shown as AVG); passing that column's "
            "chosen aggregate keeps the drilled hierarchy total consistent with "
            "the clicked cell. Validated against the supported aggregate set; "
            "an unsupported value is a 400. Ignored in leaf detail mode."
        ),
    )
    force_route: Optional[str] = Field(
        default=None,
        description=(
            'Optional route override; "source" keeps force-live drill '
            "investigations on source data so they are not silently served "
            "from an aggregate/pocket."
        ),
    )
    # Wave C #11 / B1: XMLA <Parameters> forwarded by the gateway as
    # ``app.<name>`` → value. A DRILLTHROUGH is a result-bearing query of the
    # same Execute, so the detail query must resolve the same declared model
    # parameters (row-security / default filters) as the main /execute path.
    # Producer: gateway router_client.execute_drill_through. Consumer: this
    # handler threads it into the ExecuteRequest below so _handle_execute's
    # apply_parameters() scopes the drill SQL identically. Absent this field,
    # Pydantic would silently drop the gateway's session_vars and the drill
    # would run UNSCOPED. Field name MUST match ExecuteRequest.session_vars.
    session_vars: Optional[dict[str, str]] = None


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
    # Bug-8453 / R4 finding 3: the drill grid is RLS-filtered like any other
    # routed read, so it needs the same denial channel -- an empty detail grid
    # must be distinguishable from "you may not see any of these rows".
    # Sourced from the /execute response this handler already consumes.
    security_rules_applied: list[str] = []


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
        # Bug-8613: enforce project/model RBAC and refuse unverified service
        # principals. require_capability("query") does not scope-check service
        # tokens, so the shared primitive's default (refuse) applies here.
        await load_authorized_model(
            db, current_user, model_id=str(measure_model_id), min_role="viewer",
        )

        persona = await resolve_execution_persona(
            db,
            current_user=current_user,
            model_id=str(measure_model_id),
            requested_persona_id=body.persona_id,
        )

        if persona:
            # Bug-6832: normalize UUIDs to lowercase (no braces) before
            # comparison. Case-variant UUID representations (upper vs lower
            # hex) must not produce a false 403 on ALLOWED objects.
            measure_allow = {str(x).lower().strip("{}") for x in (persona.included_measure_ids or [])}
            mid_norm = str(measure_id).lower().strip("{}")
            if measure_allow and mid_norm not in measure_allow:
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
            hier_allow = {str(x).lower().strip("{}") for x in (persona.included_hierarchy_ids or [])}
            if hier_allow:
                options = [o for o in options if str(o.hierarchy_id).lower().strip("{}") in hier_allow]

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
    # Bug-8363 (Bug-8301 class): drill-through already applies the SIMULATED
    # principal for row security, but resolved the persona against the REAL
    # (privileged admin) caller — so an admin previewing "what does this
    # restricted user see" got the user's ROW filter with the ADMIN's persona
    # entitlement: no persona-CLS, no measure/hierarchy allow-list, no persona
    # default filters. The preview showed drill rows the simulated user could
    # never reach. Resolve the persona against the simulated identity, exactly
    # as /execute and /explain do. persona_current_user_for_principal carries
    # ONLY the simulated roles, so this can never ESCALATE: a simulated viewer
    # gets viewer persona entitlement, not the admin's.
    _drill_simulated = simulate_headers_present(
        x_simulate_principal, x_simulate_roles, x_simulate_groups,
        x_simulate_claims,
    )
    _persona_current_user = persona_current_user_for_principal(
        current_user, principal, simulated=_drill_simulated,
    )
    async for db in get_tenant_db(current_user.tenant_id):
        await _enforce_measure_model_scope(db, current_user, measure_id)

        mid_row = await db.execute(
            sa_select(Measure.model_id).where(Measure.id == measure_id)
        )
        measure_model_id = mid_row.scalar_one_or_none()
        if measure_model_id is None:
            raise HTTPException(status_code=404, detail="Measure not found")
        # Bug-8613: enforce project/model RBAC and refuse unverified service
        # principals. require_capability("query") does not scope-check service
        # tokens, so the shared primitive's default (refuse) applies here.
        await load_authorized_model(
            db, current_user, model_id=str(measure_model_id), min_role="viewer",
        )
        allowed_hierarchy_ids: set[str] | None = None
        if measure_model_id is not None:
            persona = await resolve_execution_persona(
                db,
                current_user=_persona_current_user,
                model_id=str(measure_model_id),
                requested_persona_id=body.persona_id,
            )
            body.persona_id = str(persona.id) if persona else None

            # SECURITY: enforce the persona MEASURE allow-list that
            # /drill-options enforces (drill_options lines above). Without this,
            # a persona scoped to a subset of measures could call
            # /measures/{forbidden}/drill-through and read the forbidden
            # measure's detail rows — a persona-scope bypass of the same class
            # as Bug-6274. Empty list imposes no restriction.
            # Bug-6832: normalize UUIDs to lowercase (no braces) before
            # comparison so case-variant representations do not false-403.
            if persona:
                measure_allow = {str(x).lower().strip("{}") for x in (persona.included_measure_ids or [])}
                mid_norm = str(measure_id).lower().strip("{}")
                if measure_allow and mid_norm not in measure_allow:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Measure not included in effective persona",
                    )

            # Bug-6274 [SECURITY]: enforce the persona hierarchy allow-list that
            # /drill-options already enforces. A persona with a non-empty
            # included_hierarchy_ids may only drill those hierarchies; an empty
            # list imposes no restriction. Reject an explicitly-requested
            # non-allowed hierarchy loudly (403), and pass the allow-list down
            # so the single-hierarchy auto-select cannot pick a non-allowed one.
            # Bug-6832: normalize UUIDs to lowercase (no braces).
            if persona:
                hier_allow = {str(x).lower().strip("{}") for x in (persona.included_hierarchy_ids or [])}
                if hier_allow:
                    allowed_hierarchy_ids = hier_allow
                    if body.hierarchy_id and str(body.hierarchy_id).lower().strip("{}") not in hier_allow:
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail="Hierarchy not included in effective persona",
                        )

        # ``fact_table`` is the PHYSICAL source table name. R5 finding F5 used
        # to null it for an embed token; that withhold was removed 2026-08-11
        # with the rest of the token-type disclosure gating (decision option C
        # — see _sql_disclosure's module docstring). Every authenticated caller
        # now receives the same physical detail here as on /execute.
        return await _handle_drill_through(
            measure_id, body, db,
            principal=principal,
            user_identity=current_user.email or "",
            tenant_id=current_user.tenant_id,
            allowed_hierarchy_ids=allowed_hierarchy_ids,
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
    allowed_hierarchy_ids: set[str] | None = None,
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
            cursor_spec,
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
            allowed_hierarchy_ids=allowed_hierarchy_ids,
            override_agg=body.override_agg,
            tenant_id=tenant_id,
            security_context={
                "user_identity": getattr(principal, "user_identity", user_identity),
                "roles": sorted(getattr(principal, "roles", ()) or ()),
                "groups": sorted(getattr(principal, "groups", ()) or ()),
                "claims": dict(getattr(principal, "claims", {}) or {}),
                "persona_id": body.persona_id,
            },
            request_context={"force_route": body.force_route},
        )
    except DrillSemanticError as exc:
        raise HTTPException(
            status_code=(
                status.HTTP_409_CONFLICT
                if exc.error_code == "STALE_CURSOR"
                else status.HTTP_400_BAD_REQUEST
            ),
            detail={"error_code": exc.error_code, "detail": str(exc)},
        )

    exec_request = ExecuteRequest(
        model_id=model_id_str,
        raw_query=sql,
        # protocol stays "jdbc" so the generated GROUP BY SQL is parsed with
        # strict JDBC syntax enforcement (see routes.ExecuteRequest.protocol).
        protocol="jdbc",
        # Bug-6430: label drill-through REST executions distinctly so telemetry
        # can tell this internal traffic apart from real BI JDBC traffic, which
        # previously logged as protocol="jdbc" with no client_kind.
        client_kind="drill",
        persona_id=body.persona_id,
        force_route=body.force_route,
        # Wave C #11 / B1: scope the drill detail query by the Execute's declared
        # XMLA <Parameters>, exactly as the main /execute path does. _handle_execute
        # resolves these through apply_parameters() into parameterised
        # row-security / default filters.
        session_vars=body.session_vars,
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
        # Bug-8809 (same defect class as the 403 leak, lower severity): this
        # catches EVERY untyped failure of the execute pipeline — a driver
        # error naming the physical table, a generation-guard message carrying
        # ``(schema=... table=...)``, a rewriter assertion quoting the rewritten
        # SQL — and used to return ``str(exc)`` verbatim to a caller that may be
        # an embed session. ``sanitize_error_for_client`` is not a control here
        # either: per ``shared/error_sanitizer.py`` it scrubs connection
        # strings, file paths and SQLAlchemy class names only, NOT table or
        # column names. The caller gets the fact; the log gets the cause.
        logger.exception("drill-through execution failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail={
                "error_code": "drill_execution_failed",
                "detail": (
                    "The drill-through query could not be executed. The "
                    "failure has been logged; ask an administrator to check "
                    "the query-router service log."
                ),
            },
        )

    # F-019-17 — pagination composes correctly with row security.
    # The drill SQL embeds ``LIMIT effective_limit+1`` after its scoped keyset
    # continuation predicate. Row security is applied by the
    # execute pipeline via per-scan WHERE injection (`router._inject_security_where`),
    # which ANDs the security predicate into the SAME SELECT that scans the
    # physical table — *before* GROUP BY / keyset / LIMIT (Bug-915). So
    # `exec_response.rows` are already the persona-allowed rows, capped at
    # effective_limit+1: `has_more` keys on the post-security count, the page is
    # a full page of *allowed* rows, and no allowed row beyond the window is
    # unreachable. (The pre-F-007-01 outer-wrap shape, where LIMIT ran before
    # the filter, is gone — see router._inject_security_where.)
    all_rows = exec_response.rows
    has_more = len(all_rows) > effective_limit
    if has_more:
        all_rows = all_rows[:effective_limit]

    # Bug-8048: a continuation token is minted from the complete unique order
    # key of the final visible row. If leaf metadata exposes no projectable PK
    # tail, do not return a page that claims it can be continued safely: an
    # unstable scan position would skip/repeat under ingestion, and a
    # non-unique keyset has the same
    # ambiguity. The coded 409 tells the client to restart after the modeller
    # exposes a source PK dimension.
    if has_more and not cursor_spec.stable:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "STABLE_CURSOR_UNAVAILABLE",
                "detail": (
                    "This drill result has no projectable unique order key. "
                    "Add a dimension over the source table primary key before "
                    "paging this result."
                ),
            },
        )
    try:
        current_cursor = body.cursor or cursor_spec.encode()
        next_cursor = (
            cursor_spec.encode(all_rows[-1]) if has_more and all_rows else None
        )
    except CursorValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error_code": exc.code, "detail": str(exc)},
        ) from exc

    return DrillThroughResponse(
        columns=exec_response.columns,
        rows=all_rows,
        page=DrillThroughPageInfo(
            cursor=current_cursor,
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
        security_rules_applied=list(
            getattr(exec_response, "security_rules_applied", None) or []
        ),
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
