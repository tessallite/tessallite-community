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
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from shared.auth.middleware import CurrentEmbedUser, CurrentUser, enforce_model_scope, require_capability
from shared.auth.project_access import ensure_project_model_access, load_authorized_model
from shared.db.models import Dimension, Measure, Model, ModelColumn
from shared.db.session import get_tenant_db
from shared.security import Principal, RowSecurityCompileError
from src.api._sql_disclosure import row_security_misconfigured_detail
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
# F-027-07: the per-tenant rate limiter is now a shared module so the plugin
# endpoint draws from the same buckets. ``_buckets`` / ``_check_rate_limit``
# remain importable here for back-compat with existing tests.
from src.api.rate_limit import _buckets, check_rate_limit as _check_rate_limit  # noqa: F401
from src.api.routes import (
    _log_preexec_failure,
    _security_rule_ids,
    execute_with_observation,
)

from src.ir.logical_query import (
    DeployedSnapshotUnavailableError,
    LogicalQuery,
    ModelNotDeployedError,
    SemanticBindingError,
)
from src.routing.router import route_query
from src.security.cls_metadata import cls_blocked_measure_and_dimension_ids
from src.security.persona_gate import (
    enforce_persona_gate,
    merge_default_filters,
    resolve_execution_persona,
)
from src.semantic.binder import bind_query_to_model
from src.semantic.snapshot_resolver import resolve_deployed_shape

router = APIRouter(prefix="/api/v1/headless", tags=["headless"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

# Canonical filter contract (F-027-03): shared with /plugin/execute.
HeadlessFilter = SemanticFilter

_HEADLESS_MAX_LIMIT = 100_000


class HeadlessOrderBy(StrictModel):
    field: str
    direction: str = "asc"


class HeadlessQueryRequest(StrictModel):
    # F-027-01: strict — an unknown/misspelled field (``dimentions``,
    # ``filterz``, ``persona``, ``limt`` ...) is a 422 naming the field,
    # never silently dropped into a default (which would turn a filtered /
    # grouped request into an unfiltered grand total).
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


class HeadlessRouteTrace(BaseModel):
    """Bug-7409: a SAFE route summary for headless queries — whether the
    query hit an aggregate / pocket / source, plus the reason code.

    Bug-8061 / F-027-03 [HIGH]: this route trace deliberately does NOT carry
    the rewritten physical SQL. The cross-cutting contract requires a safe
    route summary with "no physical SQL for ordinary viewers"
    (``architecture_cross-cutting-contracts.md``); an earlier copy of the
    plugin diagnostic shape leaked target schema/table names and rewritten
    security/routing predicates to ordinary application credentials. Physical
    SQL stays behind the privileged ``/explain`` diagnostics surface."""

    route_type: str
    reason: str
    aggregate_id: Optional[str] = None
    pocket_id: Optional[str] = None
    # ``reason_redacted`` removed 2026-08-11 with the embed withhold that was
    # its only writer (decision option C — see _sql_disclosure).


class HeadlessColumnDescriptor(BaseModel):
    """Bug-8159: a typed descriptor for each column in the result page.

    ``HeadlessQueryResponse.columns`` is a bare ``list[str]`` — the ordered
    output column names only — which forces a headless client to guess whether
    a given column is a measure or a dimension, and gives it no display label
    or time-axis flag. ``column_descriptors`` carries that typing alongside
    ``columns`` (which is retained verbatim for wire back-compat), derived from
    the bound query's resolved measures/dimensions. A result column with no
    matching semantic field (e.g. an injected caption column) is reported with
    ``kind="unknown"`` rather than dropped, so the descriptor list always lines
    up one-to-one with ``columns``.
    """

    name: str
    # "measure" | "dimension" | "unknown"
    kind: str
    display_name: Optional[str] = None
    # dimension-only: True when the column is a time/date axis (matches the
    # ``is_time_dim`` flag exposed by ``/models/{id}/dimensions``).
    is_time_dim: bool = False
    # measure-only: the default aggregation (sum/avg/...); None for dimensions.
    aggregation: Optional[str] = None


class HeadlessQueryResponse(BaseModel):
    columns: list[str]
    # Bug-8159: typed, order-aligned descriptors for the columns above.
    column_descriptors: list[HeadlessColumnDescriptor] = []
    rows: list[dict[str, Any]]
    # F-027-15: this is the row count of the RETURNED PAGE, not the total
    # matching-row count (the API does not run a separate COUNT(*)). Named
    # ``page_row_count`` and documented as such; ``total_rows`` is retained
    # as a deprecated alias for wire back-compat and carries the same value.
    page_row_count: int
    total_rows: int
    # Bug-7998 / F-027-02 [CRITICAL]: explicit completeness contract. The
    # server enforces a hard row cap; without a completeness marker a capped
    # extract could be published as the whole dataset (silent business-data
    # loss). ``row_limit`` is the effective cap applied to THIS request;
    # ``has_more`` is True when at least one more row matched beyond the cap
    # (detected by fetching cap+1 and trimming); ``complete`` is its inverse.
    #
    # SCOPE (R6 finding 4): ``complete`` answers ONE question -- "did the row
    # cap withhold anything?" -- and answers it identically on every surface
    # that publishes it (here and ``/api/v1/plugin/execute``). It is NOT a
    # statement that the caller was permitted to see everything: a row-security
    # deny-all returns zero rows with ``complete=True`` because the cap
    # withheld nothing. Whether row security withheld anything is a SEPARATE
    # question answered by ``security_rules_applied`` below. Do not overload
    # this field with that signal -- one field, one meaning, same on both
    # surfaces, so a consumer never has to branch per surface.
    row_limit: int
    has_more: bool
    complete: bool
    # Bug-8453 / R5 finding F1: the row-security rule ids the router applied to
    # THIS execution, carrying the ``__deny_all__`` sentinel when row security
    # denied every row. This route is a customer-facing API that runs
    # route_query with the caller's principal, so RLS applies -- and without
    # this field a denial came back as ``rows: [], complete: true``, i.e. the
    # completeness contract two fields above ACTIVELY ASSERTING that an empty
    # set is the whole dataset. That is the Bug-8453 defect with an
    # authoritative "this is all of it" attached to it. Rule IDS only, never
    # predicate SQL. Consumers: classify with the shared execute contract.
    security_rules_applied: list[str] = []
    # F-027-15: page-aware identifier. The id now folds in limit / offset /
    # order_by so two different result pages of the same semantic query get
    # DIFFERENT ids — callers using it as a result-cache key no longer
    # mis-key pages. (The QueryLog/miss-log fingerprint stays semantic-only
    # so miss dedup still groups pages of one query.)
    query_id: str
    # Bug-7409: route trace so headless callers can see how the query was
    # served (aggregate/pocket/source), matching the plugin surface.
    route: Optional[HeadlessRouteTrace] = None


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
    # Bug-5973 / F-027-04: deployment status so clients know which models
    # are queryable. Only deployed models are returned by default; this
    # field is always True in the current contract (undeployed models are
    # filtered out) but is retained for forward-compatibility and client
    # introspection.
    deployed: bool = True


# ---------------------------------------------------------------------------
# Query endpoint
# ---------------------------------------------------------------------------

@router.post("/query", response_model=HeadlessQueryResponse)
async def headless_query(
    body: HeadlessQueryRequest,
    response: Response,
    current_user: CurrentUser = Depends(require_capability("query")),
) -> HeadlessQueryResponse:
    # Bug-6381: validate ids at the boundary so a malformed (non-UUID)
    # project_id/model_id returns a clean 400 rather than a 500 from the
    # downstream UUID parse in shared.auth.project_access.
    validate_uuid_id(body.model_id, field_name="model_id")
    validate_uuid_id(body.project_id, field_name="project_id")
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

    # Bug-7998 / F-027-02: fetch ONE extra row so we can tell whether the
    # result was truncated by the cap. The engine simply executes whatever
    # LIMIT it is handed; we trim the probe row before returning and report
    # completeness explicitly. probe_limit is the SQL LIMIT actually run.
    probe_limit = effective_limit + 1

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
        limit=probe_limit,
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

            # F-027-09: project scoping is part of the documented contract —
            # validate it exactly as /plugin/execute does. Bug-6382: compare
            # project ids as UUIDs (case-insensitive) so a case-variant id of the
            # CORRECT project is not falsely rejected.
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
                decision = await route_query(bound, db, principal=principal, persona=persona)
            except RowSecurityCompileError as e:
                # F-007-04: fail closed with a typed error on a misconfigured
                # row-security rule rather than a generic 500.
                # Bug-8809: the compiler message names the dimension path /
                # rule id / mapping table. Logged, never published — this
                # surface accepts embed credentials.
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=row_security_misconfigured_detail(
                        e, surface="/api/v1/headless/query",
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

            # F-027-01 / F-030-01: execute through the shared observed
            # pipeline (security audits, execution with source fallback,
            # QueryLog/QueryMissLog, metrics, audit record) — the same
            # machinery /execute uses, not a parallel shortcut.
            # Bug-5813: tag client_kind="headless" so telemetry/observability
            # can distinguish headless queries from other sources (previously
            # omitted, leaving a null client attribution).
            try:
                rows, _bytes_processed, columns, _source, _elapsed_ms, decision = (
                    await execute_with_observation(
                        bound=bound,
                        decision=decision,
                        db=db,
                        user_identity=current_user.email,
                        tenant_id=current_user.tenant_id,
                        persona=persona,
                        client_kind="headless",
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
                    "headless",
                    _exc,
                    _preexec_start,
                    persona_id=persona.id if persona is not None else None,
                    client_kind="headless",
                )
            raise

        # Bug-7998 / F-027-02: we fetched effective_limit + 1 rows. If the
        # extra row came back the result was truncated; trim it off and
        # report has_more/complete honestly so a capped page is never
        # presented as the whole dataset.
        has_more = len(rows) > effective_limit
        if has_more:
            rows = rows[:effective_limit]

        return HeadlessQueryResponse(
            columns=columns,
            # Bug-8159: typed, order-aligned column descriptors derived from
            # the bound query's resolved measures/dimensions.
            column_descriptors=_build_column_descriptors(columns, bound),
            rows=rows,
            page_row_count=len(rows),
            total_rows=len(rows),
            row_limit=effective_limit,
            has_more=has_more,
            # R6 finding 4: ``complete`` is ROW-CAP-ONLY on every surface.
            # R5 briefly overloaded it here to also mean "not denied", which
            # made the same field mean different things on headless vs
            # /plugin/execute and would have had a consumer branch differently
            # per surface for one property. The denial signal is
            # ``security_rules_applied`` -- which every classified consumer
            # already reads -- so one field carries one meaning everywhere.
            complete=not has_more,
            security_rules_applied=_security_rule_ids(decision),
            query_id=page_id[:16],
            # Bug-7409 / F-027-03: SAFE route summary only — route type,
            # reason and materialisation id. The rewritten physical SQL is
            # deliberately NOT surfaced here (see HeadlessRouteTrace).
            route=HeadlessRouteTrace(
                route_type=str(decision.route_type),
                reason=str(decision.reason),
                aggregate_id=str(decision.aggregate_id) if decision.aggregate_id else None,
                pocket_id=str(decision.pocket_id) if decision.pocket_id else None,
            ),
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
            # Bug-5973 / F-027-04: only include deployed models —
            # undeployed models cannot be queried (409) so listing them
            # in a discovery endpoint is misleading for headless clients.
            if not m.deployed_version_id:
                continue
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
                    deployed=True,
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

        # Bug-7418: resolve measures from the deployed snapshot when the
        # model is deployed, so discovery agrees with what execution binds.
        model = await db.get(Model, model_id)
        if model is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Model {model_id} not found.",
            )
        shape = await resolve_deployed_shape(model, db)
        is_deployed = getattr(model, "deployed_version_id", None) is not None
        if shape is not None:
            measures = list(shape.measures)
        elif is_deployed:
            # Bug-7418 (GAP 2): deployed model with unresolvable snapshot
            # is an error state — fail closed rather than silently serving
            # draft metadata that may disagree with what execution binds.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Model is deployed but the deployed snapshot could not "
                    "be resolved. Redeploy the model to regenerate its snapshot."
                ),
            )
        else:
            result = await db.execute(
                select(Measure).where(Measure.model_id == model_id)
            )
            measures = list(result.scalars().all())

        # Bug-7412 / Bug-6141: the persona allow-list is NOT sufficient — a persona
        # may INCLUDE a measure whose column closure reaches a data-tag (CLS)
        # restricted column. The base-table gate blocks it at query time, so the
        # metadata surface must ALSO withhold its name/description (fail closed).
        # Bug-7418 (GAP 1): pass snapshot-resolved measures/dimensions to the
        # CLS closure so it matches execution's snapshot-bound CLS decision.
        # Pass BOTH snapshot kwargs so neither set falls back to live drafts
        # on the deployed path (the function computes both blocked sets
        # internally even though this endpoint only uses blocked_measures).
        cls_blocked_measures, _ = await cls_blocked_measure_and_dimension_ids(
            db, model_id, persona,
            measures=measures,
            dimensions=list(shape.dimensions) if shape is not None else None,
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
            if (allowed is None or str(m.id) in allowed)
            and str(m.id) not in cls_blocked_measures
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

        # Bug-7418: resolve dimensions from the deployed snapshot when the
        # model is deployed, so discovery agrees with what execution binds.
        model = await db.get(Model, model_id)
        if model is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Model {model_id} not found.",
            )
        shape = await resolve_deployed_shape(model, db)
        is_deployed = getattr(model, "deployed_version_id", None) is not None
        if shape is not None:
            dimensions_for_cls = list(shape.dimensions)
            # Resolve data_type from snapshot columns; the Dimension ORM
            # has no data_type of its own.
            dim_rows: list[tuple[Any, str | None]] = []
            for d in shape.dimensions:
                src_col_id = str(getattr(d, "source_column_id", "") or "")
                col_dict = shape.columns_by_id.get(src_col_id) if src_col_id else None
                data_type = col_dict.get("data_type") if col_dict else None
                dim_rows.append((d, data_type))
        elif is_deployed:
            # Bug-7418 (GAP 2): deployed model with unresolvable snapshot
            # is an error state — fail closed rather than silently serving
            # draft metadata that may disagree with what execution binds.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Model is deployed but the deployed snapshot could not "
                    "be resolved. Redeploy the model to regenerate its snapshot."
                ),
            )
        else:
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
            dim_rows = list(result.all())
            dimensions_for_cls = [d for d, _ in dim_rows]

        # Bug-7412 / Bug-6141: withhold dimensions whose column closure (incl. a
        # separate restricted display column) reaches a CLS-restricted column,
        # even when the persona allow-list includes them (fail closed).
        # Bug-7418 (GAP 1): pass snapshot-resolved dimensions to the CLS closure
        # so it matches execution's snapshot-bound CLS decision.
        # Pass BOTH snapshot kwargs so neither set falls back to live drafts
        # on the deployed path (the function computes both blocked sets
        # internally even though this endpoint only uses blocked_dimensions).
        _, cls_blocked_dimensions = await cls_blocked_measure_and_dimension_ids(
            db, model_id, persona,
            measures=list(shape.measures) if shape is not None else None,
            dimensions=dimensions_for_cls,
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
            for d, data_type in dim_rows
            if (allowed is None or str(d.id) in allowed)
            and str(d.id) not in cls_blocked_dimensions
        ]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _build_column_descriptors(
    columns: list[str], bound: Any
) -> list[HeadlessColumnDescriptor]:
    """Bug-8159: map each output column name to a typed descriptor.

    Uses only the already-resolved measures/dimensions on ``bound`` — no extra
    source or metadata query — so the descriptors are derived from the same
    semantic objects execution bound. A column that matches neither is reported
    as ``kind="unknown"`` so the list stays one-to-one with ``columns``.
    """
    measures_by_name = {
        m.name: m for m in getattr(bound, "resolved_measures", []) or []
    }
    dims_by_name = {
        d.name: d for d in getattr(bound, "resolved_dimensions", []) or []
    }
    descriptors: list[HeadlessColumnDescriptor] = []
    for col in columns:
        if col in measures_by_name:
            m = measures_by_name[col]
            descriptors.append(
                HeadlessColumnDescriptor(
                    name=col,
                    kind="measure",
                    display_name=getattr(m, "display_name", None),
                    aggregation=getattr(m, "default_agg", None),
                )
            )
        elif col in dims_by_name:
            d = dims_by_name[col]
            descriptors.append(
                HeadlessColumnDescriptor(
                    name=col,
                    kind="dimension",
                    display_name=getattr(d, "display_name", None),
                    is_time_dim=bool(getattr(d, "is_time_dim", False)),
                )
            )
        else:
            descriptors.append(
                HeadlessColumnDescriptor(name=col, kind="unknown")
            )
    return descriptors


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
