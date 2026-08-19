"""
Plugin execution endpoint for the Excel add-in.

POST /api/v1/plugin/execute — accepts the plugin's semantic query JSON,
resolves persona, delegates to the standard bind → route → execute pipeline.
"""
from __future__ import annotations

import logging
import time
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
from src.api._sql_disclosure import (
    redact_physical_sql,
    row_security_misconfigured_detail,
)
from src.api.filter_contract import (
    SemanticFilter,
    StrictModel,
    build_logical_filters,
    canonical_raw_query,
    normalize_order_by,
    project_ids_match,
    semantic_fingerprint,
    validate_uuid_id,
)
# F-027-07: the plugin endpoint shares the per-tenant rate limiter with the
# headless API (previously it had none, despite being internet-facing
# through nginx). Both draw from the same per-tenant buckets.
from src.api.rate_limit import check_rate_limit
from src.api.routes import (
    _log_preexec_failure,
    _security_rule_ids,
    execute_with_observation,
)
from src.ir.logical_query import (
    BoundQuery,
    DeployedSnapshotUnavailableError,
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


class PluginOrderBy(StrictModel):
    field: str
    direction: str = "asc"


class PluginExecuteRequest(StrictModel):
    # F-027-01: strict — an unknown/misspelled field is a 422 naming the
    # field, never silently dropped (a dropped ``filters``/``dimensions``
    # typo would return an unfiltered grand total as the requested answer).
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
    text; no raw client SQL is echoed.

    Bug-6389 [SECURITY]: ``rewritten_query`` carries the PHYSICAL schema/table/
    column names and the COMPILED row-security predicate, and was returned to
    any ``query``-capability caller — the same content the product already gates
    behind ``require_tenant_admin`` on ``/diagnostics/query-rewrites``. It is now
    withheld below the ``modeler`` tier (see ``_sql_disclosure``), with
    ``rewritten_query_redacted`` distinguishing a policy withhold from a route
    that simply produced no SQL."""

    route_type: str
    reason: str
    aggregate_id: Optional[str] = None
    pocket_id: Optional[str] = None
    rewritten_query: Optional[str] = None
    # True only when SQL existed and the caller's role was not entitled to it.
    # Still LIVE: set by the modeller-tier ``redact_physical_sql`` gate, which
    # reads the caller's PROJECT BINDING. Only the token-type embed withhold
    # was removed (2026-08-11); this one is entitlement-based and stays.
    rewritten_query_redacted: bool = False


class PluginExecuteResponse(BaseModel):
    query: dict[str, Any]
    data: list[dict[str, Any]]
    annotation: Optional[dict[str, Any]] = None
    route: Optional[PluginRouteTrace] = None
    # Bug-7998 / F-027-02 [CRITICAL]: explicit completeness so the Report
    # Builder never inserts a capped extract as if it were the whole result.
    # ``row_limit`` is the effective cap applied to THIS request; ``has_more``
    # is True when more rows matched beyond the cap (fetch cap+1 + trim);
    # ``complete`` is its inverse.
    #
    # SCOPE (R6 finding 4): ``complete`` answers ONE question -- "did the row
    # cap withhold anything?" -- and answers it identically here and on
    # ``/api/v1/headless/query``. It is NOT a statement that the caller was
    # permitted to see everything: a row-security deny-all returns zero rows
    # with ``complete=True`` because the cap withheld nothing. That question is
    # answered by ``security_rules_applied`` below.
    row_limit: int = 0
    has_more: bool = False
    complete: bool = True
    # Bug-8453 / R3 finding S-1: the row-security rule ids the router applied to
    # THIS execution (empty when none fired), carrying the ``__deny_all__``
    # sentinel when row security denied every row. Without it the Excel add-in
    # -- the product's headline BI surface -- could not tell "your policy grants
    # you no rows" from "this slice is empty", which is the whole point of
    # Bug-8453 and was still missing on this route. Rule IDS only, never
    # predicate SQL, so it discloses no data and no policy logic.
    security_rules_applied: list[str] = []


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
    deployed_shape: Any = None,
) -> dict[Any, Optional[str]]:
    """Map each resolved dimension's id to its source-column ``data_type``.

    Bug-1057: the ``Dimension`` ORM has no ``data_type`` of its own — the
    physical type lives on the dimension's source ``ModelColumn`` (joined
    via ``Dimension.source_column_id``), exactly as the headless
    ``/dimensions`` endpoint resolves it. Calculated dimensions without a
    source column report ``None``. A single batch query avoids N+1.

    F-013-11 (Bug-8521): for a DEPLOYED model, the type is pinned from the
    deployed snapshot's ``columns_by_id`` — never from a live ``ModelColumn``
    read — so a draft column-type edit does not change the plugin's advertised
    type chips before the next Deploy. This mirrors the headless ``/dimensions``
    snapshot path. Live reads remain the authority only when there is no pinned
    shape (undeployed authoring).
    """
    source_ids = [
        getattr(d, "source_column_id", None)
        for d in dimensions
        if getattr(d, "source_column_id", None) is not None
    ]
    if not source_ids:
        return {}

    if deployed_shape is not None:
        # Deployed: resolve from the pinned snapshot columns (str-keyed).
        cols_by_id = getattr(deployed_shape, "columns_by_id", {}) or {}
        out: dict[Any, Optional[str]] = {}
        for d in dimensions:
            src_col_id = getattr(d, "source_column_id", None)
            col = cols_by_id.get(str(src_col_id)) if src_col_id is not None else None
            out[d.id] = col.get("data_type") if col else None
        return out

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
        db, bound.resolved_dimensions, getattr(bound, "deployed_shape", None)
    )
    dims_ann: dict[str, dict[str, Any]] = {}
    # Bug-8158: the annotation previously shipped ``timeDimensions: {}`` — a
    # literal empty map — so the Report Builder had no way to tell a time
    # dimension (date/period axis) from an ordinary categorical one. Populate
    # it from the resolved dimensions flagged ``is_time_dim``, mirroring the
    # dimensions/measures maps above. A time dimension appears in BOTH
    # ``dimensions`` (its axis role) and ``timeDimensions`` (its temporal role),
    # matching the Cube.js-style annotation shape the add-in consumes.
    time_dims_ann: dict[str, dict[str, Any]] = {}
    for d in bound.resolved_dimensions:
        data_type = data_type_by_dim.get(getattr(d, "id", None))
        dims_ann[d.name] = {
            "title": getattr(d, "display_name", None) or d.name,
            # Calculated dimensions (no source column) report "string"
            # as a safe default; physical dimensions report the real type.
            "type": data_type or "string",
        }
        if getattr(d, "is_time_dim", False):
            time_dims_ann[d.name] = {
                "title": getattr(d, "display_name", None) or d.name,
                # Physical time dimensions report the real source type
                # (e.g. ``date``/``timestamp``); when the source type is
                # unavailable fall back to the generic ``time`` marker.
                "type": data_type or "time",
            }
    return {
        "measures": measures_ann,
        "dimensions": dims_ann,
        "timeDimensions": time_dims_ann,
    }


@router.post("/execute", response_model=PluginExecuteResponse)
async def plugin_execute(
    body: PluginExecuteRequest,
    response: Response,
    current_user: CurrentUser = Depends(require_capability("query")),
) -> PluginExecuteResponse:
    # Bug-6381: validate ids at the boundary so a malformed (non-UUID)
    # project_id/model_id returns a clean 400 rather than a 500 from the
    # downstream UUID parse in shared.auth.project_access.
    validate_uuid_id(body.model_id, field_name="model_id")
    validate_uuid_id(body.project_id, field_name="project_id")
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
    # Bug-7998 / F-027-02: fetch one extra row to detect truncation by the
    # cap; the probe row is trimmed before returning (see below).
    probe_limit = effective_limit + 1

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
        limit=probe_limit,
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

        # Bug-7674: persist a QueryLog error row for pre-execution failures
        # (bind / project-scope / persona-gate / route rejection) that raise
        # before execution. Execution-boundary failures self-mark (already
        # logged by execute_with_observation) and are skipped to avoid a
        # double row.
        _preexec_start = time.monotonic()
        try:
            try:
                bound = await bind_query_to_model(logical_query, db)
            except ModelNotDeployedError as e:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
            except DeployedSnapshotUnavailableError as e:
                # Bug-8302: a deployed model whose snapshot is corrupt/missing/empty
                # is temporarily unusable, NOT a bad query. 503 mirrors routes.py so
                # every serving surface signals the same condition to the BI client.
                # This catch MUST precede SemanticBindingError (its base class).
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=str(e),
                )
            except SemanticBindingError as e:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=str(e),
                )

            # Bug-6382: compare project ids as UUIDs (case-insensitive) so a
            # case-variant id of the CORRECT project is not falsely rejected.
            if not project_ids_match(getattr(bound.model, "project_id", None), body.project_id):
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
                # Bug-8809: identifier-free body; specifics go to the log.
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=row_security_misconfigured_detail(
                        e, surface="/api/v1/plugin/execute",
                    ),
                )
            except DeployedSnapshotUnavailableError as e:
                # Bug-7981: the same condition can now surface at the REWRITE
                # stage (the join-graph loader fails closed when the pinned
                # deployed snapshot carries no physical graph). It is still a
                # temporarily unusable deployment, not a bad query, so it must
                # map to 503 exactly like the bind-stage catch above. This
                # catch MUST precede ValueError (its ancestor class).
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=str(e),
                )
            except ValueError as e:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=str(e),
                )

            # F-030-01: shared observed pipeline — filter/result security
            # audits, execution with source fallback, QueryLog + miss log +
            # metrics + audit record. Identical machinery to /execute.
            try:
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
            except HTTPException as _exec_exc:
                _exec_exc._tessallite_failure_logged = True  # type: ignore[attr-defined]
                raise
        except HTTPException as _exc:
            if not getattr(_exc, "_tessallite_failure_logged", False):
                await _log_preexec_failure(
                    db,
                    current_user.email,
                    current_user.tenant_id,
                    logical_query.raw_query,
                    "plugin",
                    _exc,
                    _preexec_start,
                    persona_id=persona.id if persona is not None else None,
                    client_kind="plugin",
                )
            raise

        # Bug-7998 / F-027-02: trim the probe row and report completeness.
        has_more = len(rows) > effective_limit
        if has_more:
            rows = rows[:effective_limit]

        _trace_sql, _trace_sql_redacted = await redact_physical_sql(
            decision.rewritten_query,
            db,
            current_user,
            project_id=body.project_id,
            model_id=body.model_id,
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
            row_limit=effective_limit,
            has_more=has_more,
            complete=not has_more,
            security_rules_applied=_security_rule_ids(decision),
            # F-025-20: route trace from the (possibly fallback-re-routed)
            # decision returned by execute_with_observation. Coerce to str so a
            # non-string field can never 500 the response (the route trace is
            # informational, never load-bearing).
            route=PluginRouteTrace(
                route_type=str(decision.route_type),
                reason=str(decision.reason),
                aggregate_id=str(decision.aggregate_id) if decision.aggregate_id else None,
                pocket_id=str(decision.pocket_id) if decision.pocket_id else None,
                # Bug-6389 [SECURITY]: physical SQL (physical identifiers +
                # compiled row-security predicate) only for modeller and above.
                rewritten_query=_trace_sql,
                rewritten_query_redacted=_trace_sql_redacted,
            ),
        )
