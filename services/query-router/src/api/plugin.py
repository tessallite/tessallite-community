"""
Plugin execution endpoint for the Excel add-in.

POST /api/v1/plugin/execute — accepts the plugin's semantic query JSON,
resolves persona, delegates to the standard bind → route → execute pipeline.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.middleware import (
    CurrentUser,
    enforce_model_scope,
    require_capability,
)
from shared.auth.project_access import load_authorized_model
from shared.db.models import ModelColumn
from shared.db.session import get_tenant_db
from src.api.filter_contract import (
    SemanticFilter,
    build_logical_filters,
    canonical_raw_query,
    normalize_order_by,
    semantic_fingerprint,
)
# F-027-07: the plugin endpoint shares the per-tenant rate limiter with the
# headless API (previously it had none, despite being internet-facing
# through nginx). Both draw from the same per-tenant buckets.
from src.api.rate_limit import check_rate_limit
from src.api.routes import execute_with_observation
from src.ir.logical_query import (
    BoundQuery,
    LogicalQuery,
    ModelNotDeployedError,
    SemanticBindingError,
)
from src.routing.router import route_query
from src.security import Principal, RowSecurityCompileError
from src.security.persona_gate import enforce_persona_gate, merge_default_filters, resolve_execution_persona
from src.semantic.binder import bind_query_to_model

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/plugin", tags=["plugin"])

_MAX_LIMIT = 50_000


# ---------------------------------------------------------------------------
# Request / Response
# ---------------------------------------------------------------------------

# Canonical filter contract (F-027-03 / F-025-01): vocabulary, aliases
# and validation live in src.api.filter_contract — shared verbatim with
# the headless API so the two surfaces can never drift again.
PluginFilter = SemanticFilter


class PluginOrderBy(BaseModel):
    field: str
    direction: str = "asc"


class PluginExecuteRequest(BaseModel):
    project_id: str
    model_id: str
    measures: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[PluginFilter] = Field(default_factory=list)
    limit: Optional[int] = Field(default=None, ge=1)
    offset: Optional[int] = Field(default=None, ge=0)
    order_by: list[PluginOrderBy] = Field(default_factory=list)
    persona_id: Optional[str] = None


class PluginAnnotationField(BaseModel):
    title: str
    type: str
    format: Optional[str] = None


class PluginRouteTrace(BaseModel):
    """F-025-20: the route decision for a plugin query, surfaced so the Excel
    Query Trace modal can show whether the report hit an aggregate / pocket /
    source and the rewritten SQL — the same route-visibility the web pivot has.
    The rewritten SQL is the server's own (already persona-rewritten) query
    text; no raw client SQL is echoed."""

    route_type: str
    reason: str
    aggregate_id: Optional[str] = None
    pocket_id: Optional[str] = None
    rewritten_query: Optional[str] = None


class PluginExecuteResponse(BaseModel):
    query: dict[str, Any]
    data: list[dict[str, Any]]
    annotation: Optional[dict[str, Any]] = None
    route: Optional[PluginRouteTrace] = None


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

# Back-compat alias — the canonical implementation is
# filter_contract.build_logical_filters (F-027-03 / F-025-01).
_build_filters = build_logical_filters


def _compute_fingerprint(body: PluginExecuteRequest) -> str:
    # F-027-17: shared semantic fingerprint (page-independent — same payload
    # the headless surface hashes, so the two cannot drift).
    return semantic_fingerprint(
        model_id=body.model_id,
        measures=body.measures,
        dimensions=body.dimensions,
        filters=body.filters,
    )


async def _resolve_dimension_data_types(
    db: AsyncSession,
    dimensions: list[Any],
) -> dict[Any, Optional[str]]:
    """Map each resolved dimension's id to its source-column ``data_type``.

    Bug-1057: the ``Dimension`` ORM has no ``data_type`` of its own — the
    physical type lives on the dimension's source ``ModelColumn`` (joined
    via ``Dimension.source_column_id``), exactly as the headless
    ``/dimensions`` endpoint resolves it. Calculated dimensions without a
    source column report ``None``. A single batch query avoids N+1.
    """
    source_ids = [
        d.source_column_id
        for d in dimensions
        if getattr(d, "source_column_id", None) is not None
    ]
    if not source_ids:
        return {}
    rows = await db.execute(
        select(ModelColumn.id, ModelColumn.data_type).where(
            ModelColumn.id.in_(source_ids)
        )
    )
    type_by_column = {col_id: data_type for col_id, data_type in rows.all()}
    return {
        d.id: type_by_column.get(getattr(d, "source_column_id", None))
        for d in dimensions
    }


async def _build_annotation(
    db: AsyncSession,
    model_id: str,
    bound: BoundQuery,
) -> dict[str, Any]:
    measures_ann: dict[str, dict[str, Any]] = {}
    for m in bound.resolved_measures:
        measures_ann[m.name] = {
            "title": getattr(m, "display_name", None) or m.name,
            "type": getattr(m, "default_agg", "sum"),
            "format": getattr(m, "format", None),
        }
    # Bug-1057: join the real physical type from each dimension's source
    # column instead of the always-default ``getattr(d, "data_type", ...)``
    # (the Dimension ORM carries no data_type). The Report Builder consumes
    # these types for cell formatting and operator choice.
    data_type_by_dim = await _resolve_dimension_data_types(
        db, bound.resolved_dimensions
    )
    dims_ann: dict[str, dict[str, Any]] = {}
    for d in bound.resolved_dimensions:
        data_type = data_type_by_dim.get(getattr(d, "id", None))
        dims_ann[d.name] = {
            "title": getattr(d, "display_name", None) or d.name,
            # Calculated dimensions (no source column) report "string"
            # as a safe default; physical dimensions report the real type.
            "type": data_type or "string",
        }
    return {
        "measures": measures_ann,
        "dimensions": dims_ann,
        "timeDimensions": {},
    }


@router.post("/execute", response_model=PluginExecuteResponse)
async def plugin_execute(
    body: PluginExecuteRequest,
    response: Response,
    current_user: CurrentUser = Depends(require_capability("query")),
) -> PluginExecuteResponse:
    enforce_model_scope(current_user, body.model_id)
    # F-027-07: throttle the plugin endpoint with the shared per-tenant
    # limiter (emits 429 + Retry-After when the bucket is empty).
    remaining = await check_rate_limit(current_user.tenant_id)
    response.headers["X-RateLimit-Remaining"] = str(remaining)

    if not body.measures:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="At least one measure is required.",
        )

    filters = _build_filters(body.filters)
    # F-027-17: shared order-by direction validation (invalid -> 422).
    order_by = normalize_order_by(body.order_by)
    fingerprint = _compute_fingerprint(body)

    effective_limit = min(body.limit, _MAX_LIMIT) if body.limit else _MAX_LIMIT
    effective_offset = body.offset or 0

    logical_query = LogicalQuery(
        model_id=body.model_id,
        protocol="plugin",
        # B10 round-1 finding 3: QueryLog.raw_query was empty for this
        # seam — query history showed blank previews. Log the canonical
        # semantic payload instead (compact JSON, size-capped).
        raw_query=canonical_raw_query(
            measures=body.measures,
            dimensions=body.dimensions,
            filters=body.filters,
            order_by=order_by,
            limit=body.limit,
            offset=body.offset,
        ),
        requested_measures=body.measures,
        requested_dimensions=body.dimensions,
        filters=filters,
        grain=list(body.dimensions),
        order_by=order_by,
        limit=effective_limit,
        offset=effective_offset,
        query_fingerprint=fingerprint,
    )

    async for db in get_tenant_db(current_user.tenant_id):
        await load_authorized_model(
            db,
            current_user,
            model_id=body.model_id,
            project_id=body.project_id,
            min_role="viewer",
        )
        persona = await resolve_execution_persona(
            db,
            current_user=current_user,
            model_id=body.model_id,
            requested_persona_id=body.persona_id,
        )

        try:
            bound = await bind_query_to_model(logical_query, db)
        except ModelNotDeployedError as e:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
        except SemanticBindingError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(e),
            )

        if str(getattr(bound.model, "project_id", "")) != body.project_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Model does not belong to the specified project",
            )

        if persona is not None:
            await enforce_persona_gate(
                db, persona=persona, model_id=body.model_id, bound=bound,
            )
            merge_default_filters(persona, bound)

        principal = Principal.from_current_user(current_user)
        try:
            decision = await route_query(
                bound, db, principal=principal, persona=persona,
            )
        except RowSecurityCompileError as e:
            # F-007-04: fail closed with a typed error on a misconfigured
            # row-security rule rather than a generic 500.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "message": (
                        "A row-level security rule on this model is "
                        f"misconfigured and could not be compiled: {e}."
                    ),
                    "error_type": "row_security_misconfigured",
                },
            )
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(e),
            )

        # F-030-01: shared observed pipeline — filter/result security
        # audits, execution with source fallback, QueryLog + miss log +
        # metrics + audit record. Identical machinery to /execute.
        rows, _bytes_processed, _columns, _source, _elapsed_ms, decision = (
            await execute_with_observation(
                bound=bound,
                decision=decision,
                db=db,
                user_identity=current_user.email,
                tenant_id=current_user.tenant_id,
                persona=persona,
                # F-025-12: tag every plugin-execute QueryLog/QueryMissLog row
                # with client_kind="plugin" so Excel workload is attributable
                # in telemetry, audit and ROI scoring (the shared observed
                # pipeline already writes the rows; without this they were
                # attributed to a null client).
                client_kind="plugin",
            )
        )

        annotation = await _build_annotation(db, body.model_id, bound)
        query_echo = {
            "measures": body.measures,
            "dimensions": body.dimensions,
            "limit": effective_limit,
            "offset": effective_offset,
        }

        return PluginExecuteResponse(
            query=query_echo,
            data=rows,
            annotation=annotation,
            # F-025-20: route trace from the (possibly fallback-re-routed)
            # decision returned by execute_with_observation. Coerce to str so a
            # non-string field can never 500 the response (the route trace is
            # informational, never load-bearing).
            route=PluginRouteTrace(
                route_type=str(decision.route_type),
                reason=str(decision.reason),
                aggregate_id=str(decision.aggregate_id) if decision.aggregate_id else None,
                pocket_id=str(decision.pocket_id) if decision.pocket_id else None,
                rewritten_query=str(decision.rewritten_query) if decision.rewritten_query is not None else None,
            ),
        )
