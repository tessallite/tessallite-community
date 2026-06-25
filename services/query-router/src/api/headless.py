"""
Headless REST API — JSON-in, JSON-out query endpoint for non-SQL integrations.

POST /query         — resolve measure/dimension names, build SQL, execute, return rows
GET  /models        — list models accessible to the caller
GET  /models/{id}/measures   — list measures in a model (persona-scoped)
GET  /models/{id}/dimensions — list dimensions in a model (persona-scoped)

Filters follow the canonical operator contract in
``src.api.filter_contract`` (shared verbatim with /plugin/execute).
Execution goes through the same observed pipeline as /execute
(``routes.execute_with_observation``): persona/CLS gate, row security
via Principal, security audits, QueryLog/QueryMissLog, metrics, audit.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from shared.auth.middleware import CurrentEmbedUser, CurrentUser, enforce_model_scope, require_capability
from shared.auth.project_access import ensure_project_model_access, load_authorized_model
from shared.db.models import Dimension, Measure, Model, ModelColumn
from shared.db.session import get_tenant_db
from shared.security import Principal, RowSecurityCompileError
from src.api.filter_contract import (
    SemanticFilter,
    build_logical_filters,
    canonical_raw_query,
    normalize_order_by,
    semantic_fingerprint,
)
# F-027-07: the per-tenant rate limiter is now a shared module so the plugin
# endpoint draws from the same buckets. ``_buckets`` / ``_check_rate_limit``
# remain importable here for back-compat with existing tests.
from src.api.rate_limit import _buckets, check_rate_limit as _check_rate_limit  # noqa: F401
from src.api.routes import execute_with_observation
from src.ir.logical_query import (
    LogicalQuery,
    ModelNotDeployedError,
    SemanticBindingError,
)
from src.routing.router import route_query
from src.security.persona_gate import (
    enforce_persona_gate,
    merge_default_filters,
    resolve_execution_persona,
)
from src.semantic.binder import bind_query_to_model

router = APIRouter(prefix="/api/v1/headless", tags=["headless"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

# Canonical filter contract (F-027-03): shared with /plugin/execute.
HeadlessFilter = SemanticFilter

_HEADLESS_MAX_LIMIT = 100_000


class HeadlessOrderBy(BaseModel):
    field: str
    direction: str = "asc"


class HeadlessQueryRequest(BaseModel):
    project_id: str
    model_id: str
    measures: list[str]
    dimensions: list[str] = Field(default_factory=list)
    filters: list[HeadlessFilter] = Field(default_factory=list)
    limit: Optional[int] = Field(default=None, ge=1)
    offset: Optional[int] = Field(default=None, ge=0)
    order_by: list[HeadlessOrderBy] = Field(default_factory=list)
    # F-027-05: voluntary persona pick. Resolution follows the platform
    # audience matrix (embed-locked personas always win; single-assigned
    # users are auto-locked; multi-assigned users must pick).
    persona_id: Optional[str] = None


class HeadlessQueryResponse(BaseModel):
    columns: list[str]
    rows: list[dict[str, Any]]
    # F-027-15: this is the row count of the RETURNED PAGE, not the total
    # matching-row count (the API does not run a separate COUNT(*)). Named
    # ``page_row_count`` and documented as such; ``total_rows`` is retained
    # as a deprecated alias for wire back-compat and carries the same value.
    page_row_count: int
    total_rows: int
    # F-027-15: page-aware identifier. The id now folds in limit / offset /
    # order_by so two different result pages of the same semantic query get
    # DIFFERENT ids — callers using it as a result-cache key no longer
    # mis-key pages. (The QueryLog/miss-log fingerprint stays semantic-only
    # so miss dedup still groups pages of one query.)
    query_id: str


class HeadlessMeasureInfo(BaseModel):
    id: str
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    format: Optional[str] = None
    aggregation_type: str
    variant_kind: Optional[str] = None


class HeadlessDimensionInfo(BaseModel):
    id: str
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    is_time_dim: bool = False
    data_type: Optional[str] = None


class HeadlessModelInfo(BaseModel):
    id: str
    project_id: str
    slug: str
    display_name: str
    description: Optional[str] = None


# ---------------------------------------------------------------------------
# Query endpoint
# ---------------------------------------------------------------------------

@router.post("/query", response_model=HeadlessQueryResponse)
async def headless_query(
    body: HeadlessQueryRequest,
    response: Response,
    current_user: CurrentUser = Depends(require_capability("query")),
) -> HeadlessQueryResponse:
    enforce_model_scope(current_user, body.model_id)
    remaining = await _check_rate_limit(current_user.tenant_id)
    response.headers["X-RateLimit-Remaining"] = str(remaining)

    if not body.measures:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="At least one measure is required.",
        )

    filters = build_logical_filters(body.filters)
    # F-027-17: order-by direction validation is the shared contract helper
    # (same outcomes as before: invalid direction -> 422).
    order_by = normalize_order_by(body.order_by)
    # Semantic fingerprint (page-independent) — feeds QueryLog / miss-log
    # dedup so pages of one query group together.
    fingerprint = _compute_fingerprint(body)
    # Page-aware id (F-027-15) — distinguishes result pages for caching.
    page_id = _compute_page_id(body, order_by)

    effective_limit = min(body.limit, _HEADLESS_MAX_LIMIT) if body.limit else _HEADLESS_MAX_LIMIT

    logical_query = LogicalQuery(
        model_id=body.model_id,
        protocol="headless",
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
        offset=body.offset or 0,
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
        # F-027-05: persona resolution + allow-list gate + default-filter
        # merge — same audience matrix as /execute and /plugin/execute.
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

        # F-027-09: project scoping is part of the documented contract —
        # validate it exactly as /plugin/execute does.
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
            decision = await route_query(bound, db, principal=principal, persona=persona)
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

        # F-027-01 / F-030-01: execute through the shared observed
        # pipeline (security audits, execution with source fallback,
        # QueryLog/QueryMissLog, metrics, audit record) — the same
        # machinery /execute uses, not a parallel shortcut.
        rows, _bytes_processed, columns, _source, _elapsed_ms, decision = (
            await execute_with_observation(
                bound=bound,
                decision=decision,
                db=db,
                user_identity=current_user.email,
                tenant_id=current_user.tenant_id,
                persona=persona,
            )
        )

        return HeadlessQueryResponse(
            columns=columns,
            rows=rows,
            page_row_count=len(rows),
            total_rows=len(rows),
            query_id=page_id[:16],
        )


# ---------------------------------------------------------------------------
# Metadata endpoints
# ---------------------------------------------------------------------------

def _allowed_id_set(raw_ids: Any) -> set[str] | None:
    """Persona allow-list as a string set; ``None`` = unrestricted."""
    if not raw_ids:
        return None
    return {str(v) for v in raw_ids}


@router.get("/models", response_model=list[HeadlessModelInfo])
async def list_models(
    response: Response,
    current_user: CurrentUser = Depends(require_capability("query")),
) -> list[HeadlessModelInfo]:
    remaining = await _check_rate_limit(current_user.tenant_id)
    response.headers["X-RateLimit-Remaining"] = str(remaining)

    # F-027-14: fail-closed model scoping. An embed credential minted with an
    # explicit ``model_ids: []`` is scoped to ZERO models and must enumerate
    # nothing — matching ``enforce_model_scope`` (``is not None`` semantics).
    # Only an unset/None scope (no ``model_ids`` claim) is unrestricted.
    # Truthiness here would treat the empty list as "unrestricted" and leak
    # every model's id/slug/description to a deliberately zero-scoped token.
    embed_ids = None
    if isinstance(current_user, CurrentEmbedUser) and current_user.model_ids is not None:
        # Claims are stored lowercased (middleware ``_build_user_from_payload``);
        # compare lowercased model ids to stay consistent with the comparison.
        embed_ids = {str(mid).lower() for mid in current_user.model_ids}

    async for db in get_tenant_db(current_user.tenant_id):
        result = await db.execute(select(Model))
        models = result.scalars().all()
        visible: list[HeadlessModelInfo] = []
        for m in models:
            if embed_ids is not None and str(m.id).lower() not in embed_ids:
                continue
            try:
                await ensure_project_model_access(
                    db,
                    current_user,
                    project_id=m.project_id,
                    model_id=m.id,
                    min_role="viewer",
                )
            except HTTPException:
                continue
            visible.append(
                HeadlessModelInfo(
                    id=str(m.id),
                    project_id=str(m.project_id),
                    slug=m.slug,
                    display_name=m.display_name,
                    description=m.description,
                )
            )
        return visible


@router.get("/models/{model_id}/measures", response_model=list[HeadlessMeasureInfo])
async def list_measures(
    model_id: str,
    response: Response,
    current_user: CurrentUser = Depends(require_capability("query")),
    persona_id: Optional[str] = Query(default=None),
) -> list[HeadlessMeasureInfo]:
    enforce_model_scope(current_user, model_id)
    remaining = await _check_rate_limit(current_user.tenant_id)
    response.headers["X-RateLimit-Remaining"] = str(remaining)

    async for db in get_tenant_db(current_user.tenant_id):
        await load_authorized_model(
            db, current_user, model_id=model_id, min_role="viewer"
        )
        # F-027-05: persona-scope the metadata — restricted measures must
        # not be disclosed to persona-locked or embed callers.
        persona = await resolve_execution_persona(
            db, current_user=current_user, model_id=model_id,
            requested_persona_id=persona_id,
        )
        allowed = _allowed_id_set(persona.included_measure_ids) if persona else None

        result = await db.execute(
            select(Measure).where(Measure.model_id == model_id)
        )
        measures = result.scalars().all()
        if not measures:
            model = await db.get(Model, model_id)
            if model is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Model {model_id} not found.",
                )
        return [
            HeadlessMeasureInfo(
                id=str(m.id),
                name=m.name,
                display_name=m.display_name,
                description=m.description,
                format=m.format,
                aggregation_type=m.default_agg,
                variant_kind=m.variant_kind,
            )
            for m in measures
            if allowed is None or str(m.id) in allowed
        ]


@router.get("/models/{model_id}/dimensions", response_model=list[HeadlessDimensionInfo])
async def list_dimensions(
    model_id: str,
    response: Response,
    current_user: CurrentUser = Depends(require_capability("query")),
    persona_id: Optional[str] = Query(default=None),
) -> list[HeadlessDimensionInfo]:
    enforce_model_scope(current_user, model_id)
    remaining = await _check_rate_limit(current_user.tenant_id)
    response.headers["X-RateLimit-Remaining"] = str(remaining)

    async for db in get_tenant_db(current_user.tenant_id):
        await load_authorized_model(
            db, current_user, model_id=model_id, min_role="viewer"
        )
        # F-027-05: persona-scope the metadata (see list_measures).
        persona = await resolve_execution_persona(
            db, current_user=current_user, model_id=model_id,
            requested_persona_id=persona_id,
        )
        allowed = _allowed_id_set(persona.included_dimension_ids) if persona else None

        # data_type lives on the dimension's source column (the Dimension
        # ORM has no data_type of its own); calculated dimensions without
        # a source column report null.
        result = await db.execute(
            select(Dimension, ModelColumn.data_type)
            .join(
                ModelColumn,
                Dimension.source_column_id == ModelColumn.id,
                isouter=True,
            )
            .where(Dimension.model_id == model_id)
        )
        dimensions = result.all()
        if not dimensions:
            model = await db.get(Model, model_id)
            if model is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Model {model_id} not found.",
                )
        return [
            HeadlessDimensionInfo(
                id=str(d.id),
                name=d.name,
                display_name=d.display_name,
                description=d.description,
                is_time_dim=d.is_time_dim,
                data_type=data_type,
            )
            for d, data_type in dimensions
            if allowed is None or str(d.id) in allowed
        ]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _compute_fingerprint(body: HeadlessQueryRequest) -> str:
    # F-027-17: page-INDEPENDENT semantic fingerprint via the shared helper
    # (paging args deliberately omitted so pages of one query group for
    # QueryLog / miss-log dedup; _compute_page_id layers paging on top).
    return semantic_fingerprint(
        model_id=body.model_id,
        measures=body.measures,
        dimensions=body.dimensions,
        filters=body.filters,
    )


def _compute_page_id(
    body: HeadlessQueryRequest, order_by: list[tuple[str, str]]
) -> str:
    """Page-aware identifier (F-027-15): semantic fingerprint + pagination.

    Folds in ``limit`` / ``offset`` / ``order_by`` so two different pages of
    the same semantic query produce distinct ids — safe to use as a
    result-cache key.
    """
    payload = json.dumps({
        "fp": _compute_fingerprint(body),
        "limit": body.limit,
        "offset": body.offset,
        "order_by": order_by,
    }, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()
