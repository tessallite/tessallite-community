"""
Query Router API.

POST /execute  — parse, route, execute and return results + trace.
POST /explain  — parse, route, return decision and full pipeline trace.
POST /validate — parse + bind only, returns errors gracefully (200 OK).
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
import uuid
from collections import defaultdict, deque
from types import SimpleNamespace
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.middleware import CurrentUser, enforce_model_scope, require_capability, require_tenant_admin
from shared.auth.project_access import load_authorized_model
from shared.connection_scope import (
    CrossProjectConnectionError,
    resolve_endpoint_connection,
)
from shared.db.models import (
    AggregateColumn,
    AggregateDefinition,
    DataSource,
    DataTarget,
    Dimension,
    HierarchyDefinition,
    HierarchyLevel,
    Join,
    Measure,
    Model,
    ModelVersion,
    ModelColumn,
    ModelTable,
    PersonaTagRestriction,
    QueryLog,
    UserDefinedAttribute,
    data_tag_columns,
)
from shared.db.session import get_tenant_db
from shared.semantic.field_compatibility import (
    HIDDEN_FIELD_UNAVAILABLE,
    PERSONA_FIELD_UNAVAILABLE,
    FieldAccessPolicy,
    evaluate_field_compatibility,
)
from shared.semantic.kpi_expression import extract_measure_names
from src.api._simulate import resolve_principal
from src.execution.dispatcher import execute_on_connection
from src.ir.logical_query import (
    BoundQuery,
    CrossModelNotResolvedError,
    LogicalQuery,
    ModelNotDeployedError,
    NoAggregateMatchError,
    ResultTooLargeError,
    RouteDecision,
    SelectExpression,
    SemanticBindingError,
    UnsupportedSQL,
)
from src.logging.query_logger import log_query, log_query_failure, log_query_miss
from src.params.resolver import ParameterError, apply_parameters
from src.parsing.dax_normalizer import parse_dax_to_ir
from src.parsing.sql_parser import parse_sql_to_ir
from src.rewrite.query_rewriter import invalidate_join_graph_cache, rewrite_for_source
from src.routing.aggregate_matcher import record_aggregate_hit
from src.routing.router import route_query
from src.security import Principal, RowSecurityCompileError
from src.security.persona_gate import apply_persona_gate, enforce_persona_gate, load_persona, merge_default_filters, resolve_execution_persona
from src.security.query_audit import (
    SecurityAuditError,
    audit_filters_present,
    audit_result_columns,
    resolve_filter_anchors,
)
from src.semantic.binder import bind_query_to_model

from shared.audit.logger import audit
from shared.cache.result_cache import ResultCache
from shared.config.settings import get_settings as _get_settings
from shared.error_sanitizer import sanitize_error_for_client
from shared.metrics import (
    MODEL_BYTES_PROCESSED,
    MODEL_QUERY_COUNT,
    MODEL_QUERY_DURATION,
    MODEL_QUERY_ERRORS,
    MODEL_ROWS_RETURNED,
    QUERY_ROUTED_COUNT,
)
from shared.source_executor import QueryTimeoutError

logger = logging.getLogger(__name__)
_query_audit_logger = logging.getLogger("tessallite.query_audit")

router = APIRouter(tags=["query"])

_cache = ResultCache(ttl_seconds=_get_settings().QUERY_CACHE_TTL_SECONDS)
SEMANTIC_COMPATIBILITY_NOT_ANALYZED = "SEMANTIC_COMPATIBILITY_NOT_ANALYZED"

# F-030-14/B01: the failure-spike tracker is keyed by tenant, preserving the
# existing tenant-wide threshold/dedup behavior. Each in-window event also
# carries optional project context so project-scoped notification routes created
# by modelers are selected by the real producer dispatch.
#
# Still process-local: in multi-replica Cloud Run each replica tracks its own
# window. The replica-safe alternative, deriving the spike from QueryLog rows,
# remains the durable follow-up.
_failure_timestamps: dict[str, deque[tuple[float, uuid.UUID | str | None]]] = defaultdict(deque)
_failure_lock = asyncio.Lock()
_FAILURE_SPIKE_WINDOW = 300
_FAILURE_SPIKE_THRESHOLD = 10
_last_spike_alert: dict[str, float] = defaultdict(lambda: float("-inf"))


async def _log_query_failure(
    db: AsyncSession,
    user_identity: str,
    tenant_id: str,
    bound,
    decision,
    start_ms: float,
    error_type: str,
    error_detail: str,
    persona_id: uuid.UUID | None = None,
    client_kind: str | None = None,
) -> None:
    elapsed_ms = int((time.monotonic() - start_ms) * 1000)
    if bound and bound.model:
        _model = bound.model.display_name
        project = getattr(bound.model, "project", None)
        _project = getattr(project, "display_name", "") if project else ""
        _tenant = tenant_id
        MODEL_QUERY_ERRORS.labels(tenant=_tenant, project=_project, model_name=_model, error_type=error_type).inc()
        MODEL_QUERY_DURATION.labels(tenant=_tenant, project=_project, model_name=_model).observe(elapsed_ms / 1000)
    _query_audit_logger.warning(
        "query_failed",
        extra={
            "user_email": user_identity,
            "tenant_id": tenant_id,
            "model_id": str(bound.model.id) if bound and bound.model else "",
            "model_name": getattr(bound.model, "display_name", "") if bound and bound.model else "",
            "route_type": decision.route_type if decision else "",
            "duration_ms": elapsed_ms,
            "row_count": 0,
            "error_type": error_type,
            "error_detail": error_detail[:500],
            "query_fingerprint": getattr(bound.logical_query, "query_fingerprint", "")[:16] if bound else "",
            "protocol": getattr(bound.logical_query, "protocol", "") if bound else "",
        },
    )
    try:
        await log_query_failure(
            db=db,
            bound_query=bound,
            decision=decision,
            execution_ms=elapsed_ms,
            user_identity=user_identity,
            persona_id=persona_id,
            client_kind=client_kind,
            error_type=error_type,
            error_detail=error_detail,
        )
    except Exception:
        logger.warning("Failed to persist query failure log", exc_info=True)

    project_id = None
    if bound and bound.model:
        project_id = getattr(bound.model, "project_id", None)

    await _check_failure_spike(db, tenant_id, project_id=project_id)


async def _security_audit_block(
    db: AsyncSession,
    user_identity: str,
    tenant_id: str,
    bound,
    decision,
    start_ms: float,
    exc: SecurityAuditError,
    *,
    audit_layer: str,
    persona_id: uuid.UUID | None = None,
    client_kind: str | None = None,
) -> HTTPException:
    """Record one security-audit block and build the client error.

    B10 round-1 finding 4: audit blocks used to surface as bare 500s with
    no QueryLog failure row and no platform audit event — every Bug-1045
    hit was unobservable outside container stdout.  This helper:

    1. persists the failure QueryLog row (status=error,
       error_type=security_audit) + metrics via ``_log_query_failure``,
    2. writes a ``query.security_audit_block`` platform audit event,
    3. returns an HTTP 403 with a structured, precise detail (the gateway
       extracts ``detail["message"]`` for JDBC errors / SOAP faults) —
       not a bare 500.

    The caller raises the returned exception; the query is still blocked
    fail-closed.
    """
    await _log_query_failure(
        db, user_identity, tenant_id, bound, decision, start_ms,
        "security_audit", str(exc),
        persona_id=persona_id, client_kind=client_kind,
    )
    try:
        await audit(
            db,
            action="query.security_audit_block",
            # NB: the audit vocabulary is critical|warn|info ("warning"
            # would rank 0 and be gated out at the default info level).
            severity="warn",
            actor_email=user_identity,
            target_type="model",
            target_id=bound.model.id if bound and bound.model else None,
            target_name=getattr(bound.model, "display_name", "") if bound and bound.model else "",
            detail={
                "audit_layer": audit_layer,
                "route_type": decision.route_type if decision else "",
                "reason": str(exc)[:500],
                "protocol": getattr(bound.logical_query, "protocol", "") if bound else "",
            },
        )
        # audit() only flushes; the request is about to error out, so the
        # session would roll back the event without an explicit commit.
        await db.commit()
    except Exception:
        logger.warning("Failed to persist security-audit-block audit event", exc_info=True)
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error_type": "security_audit_failure",
            "audit_layer": audit_layer,
            "message": (
                f"Query blocked by the security audit ({audit_layer}): {exc} "
                "No data was returned. This is a fail-closed server-side "
                "integrity safeguard — the rewritten query could not be "
                "proven to preserve every required filter/column. "
                "Contact your administrator with this message."
            ),
        },
    )


async def _check_failure_spike(
    db: AsyncSession,
    tenant_id: str,
    *,
    project_id: uuid.UUID | str | None = None,
) -> None:
    # F-18: Protect shared _failure_timestamps with asyncio.Lock
    # F-030-14/B01: threshold + dedup stay tenant-scoped, while the alert
    # dispatch below fans out to project-scoped routes represented in the
    # current tenant window.
    async with _failure_lock:
        now = time.monotonic()
        cutoff = now - _FAILURE_SPIKE_WINDOW
        window = _failure_timestamps[tenant_id]
        window.append((now, project_id))
        while window and window[0][0] < cutoff:
            window.popleft()
        if len(window) < _FAILURE_SPIKE_THRESHOLD:
            return
        if now - _last_spike_alert[tenant_id] < _FAILURE_SPIKE_WINDOW:
            return
        _last_spike_alert[tenant_id] = now
        count = len(window)
        project_ids_by_key: dict[str, uuid.UUID | str] = {}
        project_counts_by_key: dict[str, int] = defaultdict(int)
        for _, event_project_id in window:
            if event_project_id is not None:
                project_key = str(event_project_id)
                project_ids_by_key.setdefault(project_key, event_project_id)
                project_counts_by_key[project_key] += 1
        project_scopes = [
            (project_id, project_counts_by_key[project_key])
            for project_key, project_id in project_ids_by_key.items()
        ]
    try:
        from shared.alerting.dispatcher import dispatch_alert
        dispatch_scopes: list[tuple[uuid.UUID | str | None, str, str]] = [
            (
                None,
                (
                    f"<strong>{count}</strong> query failures detected in the last "
                    f"{_FAILURE_SPIKE_WINDOW // 60} minutes for tenant "
                    f"<strong>{tenant_id}</strong>."
                ),
                (
                    f"{count} failures in {_FAILURE_SPIKE_WINDOW // 60} min "
                    f"(tenant: {tenant_id})"
                ),
            )
        ]
        dispatch_scopes.extend(
            (
                scoped_project_id,
                (
                    f"Tenant <strong>{tenant_id}</strong> had <strong>{count}</strong> "
                    f"query failures in the last {_FAILURE_SPIKE_WINDOW // 60} minutes, "
                    f"including <strong>{project_count}</strong> for project "
                    f"<strong>{scoped_project_id}</strong>."
                ),
                (
                    f"{count} tenant failures in {_FAILURE_SPIKE_WINDOW // 60} min; "
                    f"{project_count} for project: {scoped_project_id}"
                ),
            )
            for scoped_project_id, project_count in project_scopes
        )
        for scoped_project_id, scope_html, slack_scope_text in dispatch_scopes:
            await dispatch_alert(
                db,
                event_type="query_failure_spike",
                project_id=scoped_project_id,
                subject=f"Query failure spike: {count} failures in {_FAILURE_SPIKE_WINDOW // 60} minutes",
                body_html=(
                    f"<h2>Query Failure Spike</h2>"
                    f"<p>{scope_html}</p>"
                ),
                slack_text=f"Query failure spike: {slack_scope_text}",
            )
    except Exception:
        logger.warning("Failed to dispatch failure spike alert", exc_info=True)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ExecuteRequest(BaseModel):
    model_id: str
    raw_query: str
    # jdbc | dax | mcp. "mcp" parses with identical strictness to "jdbc"
    # (SQL over HTTP) but is labelled distinctly in QueryLog / metrics so
    # MCP traffic is attributable in telemetry (B10 round-1 finding 5).
    protocol: str = "jdbc"
    # Input dialect the producer wrote the SQL in. The parser honours this
    # so identifier quoting matches the producer's flavour. Defaults to
    # postgres (the canonical internal dialect for this stack); BigQuery,
    # Spark, and any other sqlglot dialect name are also accepted. The
    # field has no effect on the DAX path.
    dialect: Optional[str] = None
    # Phase 2 of the semantic-layer plan: gateways set this to True for
    # queries against the technical view (<model>_technical). The binder
    # then skips the is_hidden cascade so power users see every column.
    include_hidden: bool = False
    # Per-query route override. "source" skips aggregate + pocket matching;
    # "aggregate" or "pocket" requires a match or raises NoAggregateMatchError.
    force_route: Optional[str] = None
    # Phase 8 persona-as-catalog: the gateway resolves persona from the
    # catalog suffix and plumbs it through here. Empty/None means "no
    # persona gate" (business base view).
    persona_id: Optional[str] = None
    # Observability label forwarded by the gateway. This is not used for
    # authorization or routing decisions.
    client_kind: Optional[Literal["looker_studio", "looker_cloud"]] = None
    # Pre-parsed DAX IR from the gateway (optional). When present, the
    # query-router reads ``time_variant_hints`` from it instead of
    # re-parsing the raw DAX string. Avoids double-parse on the DAX path.
    parsed_dax: Optional[dict] = None
    # F-027-02: hard cap on returned rows, applied after parse by
    # clamping the logical query's LIMIT (the smaller of the two wins).
    # Added for the MCP server's TESSALLITE_ROW_LIMIT feature; optional
    # and backwards-compatible for all other callers.
    row_limit: Optional[int] = Field(default=None, ge=1)
    # F-029-01: JDBC session variables captured by the gateway from
    # ``SET app.<name> = <value>`` statements, keyed ``app.<name>``. The
    # gateway forwards them on every execute (router_client.execute_query);
    # the query-router resolves them against the model's declared
    # parameters (persona default > session var > model default) and binds
    # the values into the SQL as type-safe literals before parsing. Absent
    # this field, Pydantic's ``extra="ignore"`` silently dropped them and
    # no parameterised query could ever resolve.
    session_vars: Optional[dict[str, str]] = None
    # CR-002 Finding 1: ``user_identity`` is no longer accepted on the
    # request body. It was client-controlled and defaulted to an empty
    # string from the gateway, which poisoned every audit-log line.
    # Identity is now derived from the validated JWT claim
    # (``current_user.email``) at the route handler.


class TraceStep(BaseModel):
    """One stage of the query routing pipeline."""

    stage: str       # parser | binder | router | rewriter | executor
    title: str       # Short label rendered in the diagram
    detail: str      # Longer explanation rendered in the tooltip
    status: str      # ok | warn | error
    data: dict[str, Any] = {}


class TargetSystemInfo(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None       # postgresql | bigquery | hadoop_spark
    location: Optional[str] = None   # schema / dataset / database


class AggregateUsedInfo(BaseModel):
    id: str
    physical_table_name: str
    grain: list[str]
    creation_reason: Optional[str] = None
    status: Optional[str] = None


class PocketUsedInfo(BaseModel):
    id: str
    physical_table_name: str
    status: Optional[str] = None
    refresh_policy: Optional[str] = None


class PipelineTrace(BaseModel):
    steps: list[TraceStep] = []
    target_system: Optional[TargetSystemInfo] = None
    aggregate_used: Optional[AggregateUsedInfo] = None
    pocket_used: Optional[PocketUsedInfo] = None


class FieldCompatibilityIssueResponse(BaseModel):
    code: str
    severity: Literal["error", "warning"] = "error"
    measure_name: Optional[str] = None
    dimension_name: Optional[str] = None
    message: str
    compatible_dimension_names: list[str] = []


class FieldCompatibilityFeedback(BaseModel):
    status: Literal["compatible", "incompatible", "not_analyzed"]
    issues: list[FieldCompatibilityIssueResponse] = []


class ExecuteResponse(BaseModel):
    rows: list[dict[str, Any]]
    columns: list[str]
    route_type: str                  # pocket | aggregate | source
    # Phase 5.2 — route selection reason, carried on every response so
    # the frontend badge can show why the router picked this path
    # without issuing a second /explain call.
    reason: str = ""
    aggregate_id: Optional[str]
    pocket_id: Optional[str] = None
    execution_ms: int
    bytes_processed: int
    rows_returned: int
    # Agent Phase B2 (F3): the rewritten physical SQL the executor ran,
    # surfaced on every response so the conversational agent can persist
    # `agent_turns.routed_sql` and the trace drawer can render the
    # physical query without a second /explain call. Backwards-compatible
    # default keeps existing JDBC/XMLA consumers unaffected.
    routed_sql: Optional[str] = None
    trace: PipelineTrace = PipelineTrace()
    field_compatibility: Optional[FieldCompatibilityFeedback] = None


class ExplainResponse(BaseModel):
    route_type: str
    aggregate_id: Optional[str]
    pocket_id: Optional[str] = None
    reason: str
    rewritten_query: str
    requested_measures: list[str]
    requested_dimensions: list[str]
    grain: list[str]
    query_fingerprint: str
    # F-005-06: the active row-security rule ids the router applied to this
    # rewrite (empty when none fired). Surfaced from the route decision so
    # internal callers that must NOT cache a row-filtered result — pocket
    # refresh materialisation — can fail closed structurally instead of
    # string-matching the rewrite. Backwards-compatible default for all other
    # consumers.
    security_rules_applied: list[str] = []
    trace: PipelineTrace = PipelineTrace()
    field_compatibility: Optional[FieldCompatibilityFeedback] = None


class ValidateResponse(BaseModel):
    """Validate-only response. `ok=False` means a friendly error message
    was captured and the caller should display it; HTTP status stays 200."""

    ok: bool
    errors: list[str] = []
    warnings: list[str] = []
    requested_measures: list[str] = []
    requested_dimensions: list[str] = []
    query_fingerprint: Optional[str] = None
    filters: Optional[list[dict[str, Any]]] = None
    grain: list[str] = []
    has_unresolvable_where: bool = False
    has_complex_sql: bool = False
    select_star: bool = False
    from_tables: list[str] = []
    field_compatibility: Optional[FieldCompatibilityFeedback] = None


class DiscoverMembersRequest(BaseModel):
    model_id: str
    dimension_name: str
    persona_id: Optional[str] = None


class DiscoverMembersResponse(BaseModel):
    members: list[dict[str, Any]]
    levels: list[str]


class QueryRewriteRow(BaseModel):
    """One distinct (raw, rewritten) pair seen by the router."""

    model_id: Optional[str] = None
    query_fingerprint: str
    raw_query: str
    rewritten_query: Optional[str] = None
    route_type: str
    hit_count: int
    last_seen: str
    first_seen: str


class QueryRewritesResponse(BaseModel):
    rows: list[QueryRewriteRow]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/execute", response_model=ExecuteResponse)
async def execute_query(
    body: ExecuteRequest,
    current_user: CurrentUser = Depends(require_capability("query")),
    x_simulate_principal: str | None = Header(default=None, alias="X-Tessallite-Simulate-Principal"),
    x_simulate_roles: str | None = Header(default=None, alias="X-Tessallite-Simulate-Roles"),
    x_simulate_groups: str | None = Header(default=None, alias="X-Tessallite-Simulate-Groups"),
    x_simulate_claims: str | None = Header(default=None, alias="X-Tessallite-Simulate-Claims"),
) -> ExecuteResponse:
    enforce_model_scope(current_user, body.model_id)
    _validate_force_route(body.force_route)
    principal = resolve_principal(
        current_user, x_simulate_principal, x_simulate_roles,
        x_simulate_groups, x_simulate_claims,
    )
    async for db in get_tenant_db(current_user.tenant_id):
        await load_authorized_model(
            db, current_user, model_id=body.model_id, min_role="viewer"
        )
        effective_persona = await resolve_execution_persona(
            db,
            current_user=current_user,
            model_id=body.model_id,
            requested_persona_id=body.persona_id,
        )
        return await _handle_execute(
            body,
            db,
            user_identity=principal.user_identity,
            principal=principal,
            persona_id=str(effective_persona.id) if effective_persona else None,
            persona=effective_persona,
            tenant_id=current_user.tenant_id,
        )


@router.post("/explain", response_model=ExplainResponse)
async def explain_query(
    body: ExecuteRequest,
    current_user: CurrentUser = Depends(require_capability("query")),
    x_simulate_principal: str | None = Header(default=None, alias="X-Tessallite-Simulate-Principal"),
    x_simulate_roles: str | None = Header(default=None, alias="X-Tessallite-Simulate-Roles"),
    x_simulate_groups: str | None = Header(default=None, alias="X-Tessallite-Simulate-Groups"),
    x_simulate_claims: str | None = Header(default=None, alias="X-Tessallite-Simulate-Claims"),
) -> ExplainResponse:
    enforce_model_scope(current_user, body.model_id)
    _validate_force_route(body.force_route)
    principal = resolve_principal(
        current_user, x_simulate_principal, x_simulate_roles,
        x_simulate_groups, x_simulate_claims,
    )
    async for db in get_tenant_db(current_user.tenant_id):
        await load_authorized_model(
            db, current_user, model_id=body.model_id, min_role="viewer"
        )
        effective_persona = await resolve_execution_persona(
            db,
            current_user=current_user,
            model_id=body.model_id,
            requested_persona_id=body.persona_id,
        )
        return await _handle_explain(
            body, db, principal=principal,
            persona_id=str(effective_persona.id) if effective_persona else None,
            persona=effective_persona,
        )


@router.post("/validate", response_model=ValidateResponse)
async def validate_query(
    body: ExecuteRequest,
    current_user: CurrentUser = Depends(require_capability("query")),
) -> ValidateResponse:
    """Parse + bind without routing or executing.

    Always returns 200; failures live inside the response body so the UI can
    render a friendly error message instead of toasting an HTTP error.
    """
    enforce_model_scope(current_user, body.model_id)
    async for db in get_tenant_db(current_user.tenant_id):
        await load_authorized_model(
            db, current_user, model_id=body.model_id, min_role="viewer"
        )
        effective_persona = await resolve_execution_persona(
            db,
            current_user=current_user,
            model_id=body.model_id,
            requested_persona_id=body.persona_id,
        )
        return await _handle_validate(
            body, db,
            persona_id=str(effective_persona.id) if effective_persona else None,
            persona=effective_persona,
        )


@router.post("/discover/members", response_model=DiscoverMembersResponse)
async def discover_members(
    body: DiscoverMembersRequest,
    current_user: CurrentUser = Depends(require_capability("query")),
) -> DiscoverMembersResponse:
    enforce_model_scope(current_user, body.model_id)
    async for db in get_tenant_db(current_user.tenant_id):
        await load_authorized_model(
            db, current_user, model_id=body.model_id, min_role="viewer"
        )
        effective_persona = await resolve_execution_persona(
            db, current_user=current_user, model_id=body.model_id,
            requested_persona_id=body.persona_id,
        )
        if effective_persona and effective_persona.included_dimension_ids:
            dim_result = await db.execute(
                select(Dimension).where(
                    Dimension.model_id == body.model_id,
                    Dimension.name == body.dimension_name,
                )
            )
            dim = dim_result.scalar_one_or_none()
            if dim is None or str(dim.id) not in {
                str(d) for d in effective_persona.included_dimension_ids
            }:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Dimension not available for this persona",
                )
        return await _handle_discover_members(
            body, db,
            current_user=current_user,
            persona=effective_persona,
        )


@router.get("/diagnostics/query-rewrites", response_model=QueryRewritesResponse)
async def list_query_rewrites(
    current_user: CurrentUser = Depends(require_tenant_admin),
    model_id: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> QueryRewritesResponse:
    """Distinct (raw, rewritten) query pairs from this tenant's query_logs.

    Surfaces what the rewriter actually produced so silent-drop bugs
    (e.g. Bug-102 — column-vs-column WHERE dropped during rewrite)
    can be caught by inspection. Tenant-scoped: a tenant_admin sees
    only their own tenant's logs; system_admin sees the tenant they
    authenticated against.
    """
    async for db in get_tenant_db(current_user.tenant_id):
        return await _handle_query_rewrites(db, model_id=model_id, limit=limit)


@router.delete(
    "/cache/models/{model_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_tenant_admin)],
)
async def evict_model_cache(model_id: str) -> None:
    """Evict all cached state for a model. Called by model-service after deploy.

    F-006-05: in addition to the result cache, evict the in-process join-graph
    cache (tables/joins/columns, 60s TTL). A deploy can change the model's
    tables, joins, or physical column names; without this, queries could be
    rewritten against the previous model graph for up to the TTL window after a
    deploy — contradicting the immutable-deploy contract and risking join
    errors or stale physical names. The cache is per-replica (an in-process
    dict), so this clears the calling replica; the TTL remains the cross-replica
    bound on Cloud Run, as documented on ``join_graph_cache``.
    """
    _cache.evict_model(model_id)
    invalidate_join_graph_cache(model_id)
    # F-003-14 / F-004-17: evict the versioned binder and routing caches too.
    # They are keyed by (model_id, deployed_version_id) so they self-invalidate
    # on the next query after a deploy, but evicting here drops the stale entry
    # on the calling replica immediately (matches the join-graph cache contract).
    from src.semantic.snapshot_resolver import (
        invalidate as _invalidate_snapshot_cache,
        invalidate_live_metadata as _invalidate_live_metadata_cache,
    )
    from src.routing.aggregate_matcher import invalidate_canonical_dim_cache
    _invalidate_snapshot_cache(model_id)
    _invalidate_live_metadata_cache(model_id)
    invalidate_canonical_dim_cache(model_id)


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------

_ALLOWED_FORCE_ROUTES = frozenset({"source", "aggregate", "pocket"})


def _validate_force_route(value: Optional[str]) -> None:
    """Reject unsupported ``force_route`` values at the HTTP boundary.

    Accepted values: ``source``, ``aggregate``, ``pocket``.

    Semantics (F-004-08): ``force_route`` pins one SPECIFIC route.
    ``"aggregate"`` runs the aggregate path only (the pocket matcher is
    skipped, so a matching pocket never wins); ``"pocket"`` runs the pocket
    path only (the aggregate matcher is skipped); ``"source"`` bypasses both.
    Coverage validation, the percentile exactness gate, and structural
    bypasses (row-security, disabled model/aggregations, invalid objects, DAX
    time-variant) still run after forcing — so an incompatible force produces a
    ``NoAggregateMatchError`` (HTTP error) rather than silently returning a
    different route or an unrequested source result. Security is never weakened
    by a force; a force incompatible with active row-security errors out.
    """
    if value is None:
        return
    if value not in _ALLOWED_FORCE_ROUTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"force_route must be one of {sorted(_ALLOWED_FORCE_ROUTES)!r}; "
                f"got {value!r}."
            ),
        )


async def _bind_query_parameters(
    body: ExecuteRequest,
    db: AsyncSession,
    persona_id: Optional[str],
) -> None:
    """Resolve and bind model parameters into ``body.raw_query`` in place.

    F-029-01: runs before parse, on the canonical SQL, so sqlglot
    transpilation handles the final dialect literal form. The ``@param``
    tokens sqlglot's parser would otherwise reject are replaced with
    type-safe typed literals here. Resolution order is persona default
    filter > JDBC session variable > model default value.

    Only the SQL protocols (jdbc / mcp) carry ``@param`` tokens and
    ``SET app.*`` session variables; the DAX path is left untouched.
    Short-circuits inside ``apply_parameters`` when the model declares no
    parameters, so non-parameterised queries pay only one indexed lookup.
    """
    if body.protocol == "dax":
        return

    # No ``@param`` token -> nothing to bind. Skip persona load and the
    # parameter probe entirely (matches apply_parameters' fast path).
    if "@" not in body.raw_query:
        return

    # Persona default filters take top precedence. Load the persona once
    # here to read its ``default_filters`` (keyed by ``@param`` for the
    # parameters it overrides). The later persona gate reuses the same
    # session-cached row, so this is not a redundant round trip.
    persona_filters: dict[str, Any] = {}
    if persona_id:
        persona = await load_persona(
            db, model_id=body.model_id, persona_id=persona_id
        )
        persona_filters = persona.default_filters or {}

    try:
        body.raw_query = await apply_parameters(
            model_id=body.model_id,
            sql=body.raw_query,
            session_vars=body.session_vars,
            persona_filters=persona_filters,
            db=db,
            dialect="postgres",
        )
    except ParameterError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _stable_uuid(value: Any) -> uuid.UUID:
    return _uuid_or_none(value) or uuid.uuid5(uuid.NAMESPACE_URL, str(value))


def _coerce_snapshot_value(value: Any) -> Any:
    if isinstance(value, str):
        maybe_uuid = _uuid_or_none(value)
        if maybe_uuid is not None:
            return maybe_uuid
    return value


def _snapshot_rows(snapshot: dict[str, Any], key: str) -> list[Any]:
    return [
        SimpleNamespace(**{
            name: _coerce_snapshot_value(value)
            for name, value in row.items()
        })
        for row in (snapshot.get(key, []) or [])
        if isinstance(row, dict)
    ]


def _field_name(field_obj: Any | None) -> str | None:
    if field_obj is None:
        return None
    return getattr(field_obj, "name", None) or getattr(field_obj, "display_name", None)


def _not_analyzed_field_compatibility() -> FieldCompatibilityFeedback:
    return FieldCompatibilityFeedback(
        status="not_analyzed",
        issues=[
            FieldCompatibilityIssueResponse(
                code=SEMANTIC_COMPATIBILITY_NOT_ANALYZED,
                severity="warning",
                message=(
                    "This query uses advanced SQL that cannot be verified for "
                    "field compatibility before execution."
                ),
            )
        ],
    )


async def _field_access_policy(
    *,
    db: AsyncSession,
    persona: Any | None,
    include_hidden: bool,
) -> FieldAccessPolicy:
    allowed_measure_ids = None
    allowed_dimension_ids = None
    restricted_column_ids: set[uuid.UUID] = set()
    persona_scoped = persona is not None

    if persona is not None:
        raw_measure_ids = getattr(persona, "included_measure_ids", None) or []
        raw_dimension_ids = getattr(persona, "included_dimension_ids", None) or []
        if raw_measure_ids:
            allowed_measure_ids = {
                measure_id for value in raw_measure_ids
                if (measure_id := _uuid_or_none(value)) is not None
            }
        if raw_dimension_ids:
            allowed_dimension_ids = {
                dimension_id for value in raw_dimension_ids
                if (dimension_id := _uuid_or_none(value)) is not None
            }

        restriction_rows = (
            await db.execute(
                select(PersonaTagRestriction.data_tag_id)
                .where(PersonaTagRestriction.persona_id == persona.id)
            )
        ).scalars().all()
        if restriction_rows:
            restricted_rows = (
                await db.execute(
                    select(data_tag_columns.c.model_column_id)
                    .where(data_tag_columns.c.tag_id.in_(restriction_rows))
                )
            ).scalars().all()
            restricted_column_ids = {
                column_id for value in restricted_rows
                if (column_id := _uuid_or_none(value)) is not None
            }

    return FieldAccessPolicy(
        allowed_measure_ids=allowed_measure_ids,
        allowed_dimension_ids=allowed_dimension_ids,
        restricted_column_ids=restricted_column_ids,
        include_hidden=include_hidden,
        persona_scoped=persona_scoped,
    )


async def _load_field_compatibility_metadata(
    model_id: Any,
    deployed_version_id: Any,
    db: AsyncSession,
) -> tuple[
    list[Any],
    list[Any],
    list[Any],
    list[Any],
    list[Any],
    list[Any],
    list[Any],
    list[Any],
]:
    snapshot = None
    version_id = _uuid_or_none(deployed_version_id)
    if version_id is not None:
        version = await db.get(ModelVersion, version_id)
        if version is not None and isinstance(version.snapshot_json, dict):
            snapshot = version.snapshot_json

    if snapshot and (
        snapshot.get("measures")
        or snapshot.get("dimensions")
        or snapshot.get("columns")
        or snapshot.get("tables")
    ):
        measures = _snapshot_rows(snapshot, "measures")
        dimensions = _snapshot_rows(snapshot, "dimensions")
        tables = _snapshot_rows(snapshot, "tables")
        columns = _snapshot_rows(snapshot, "columns")
        joins = _snapshot_rows(snapshot, "joins")
        user_defined_attributes = _snapshot_rows(snapshot, "user_defined_attributes")
    else:
        measures = (
            await db.execute(select(Measure).where(Measure.model_id == model_id))
        ).scalars().all()
        dimensions = (
            await db.execute(select(Dimension).where(Dimension.model_id == model_id))
        ).scalars().all()
        tables = (
            await db.execute(select(ModelTable).where(ModelTable.model_id == model_id))
        ).scalars().all()
        columns = (
            await db.execute(
                select(ModelColumn)
                .join(ModelTable, ModelColumn.model_table_id == ModelTable.id)
                .where(ModelTable.model_id == model_id)
            )
        ).scalars().all()
        joins = (
            await db.execute(select(Join).where(Join.model_id == model_id))
        ).scalars().all()
        user_defined_attributes = (
            await db.execute(
                select(UserDefinedAttribute)
                .where(UserDefinedAttribute.model_id == model_id)
            )
        ).scalars().all()
    aggregates = (
        await db.execute(
            select(AggregateDefinition)
            .where(AggregateDefinition.model_id == model_id)
        )
    ).scalars().all()
    aggregate_ids = [
        aggregate_id for aggregate in aggregates
        if (aggregate_id := _uuid_or_none(getattr(aggregate, "id", None))) is not None
    ]
    if aggregate_ids:
        aggregate_columns = (
            await db.execute(
                select(AggregateColumn)
                .where(AggregateColumn.aggregate_definition_id.in_(aggregate_ids))
            )
        ).scalars().all()
    else:
        aggregate_columns = []

    return (
        list(measures),
        list(dimensions),
        list(tables),
        list(columns),
        list(joins),
        list(user_defined_attributes),
        list(aggregates),
        list(aggregate_columns),
    )


async def _evaluate_bound_field_compatibility(
    bound: BoundQuery,
    db: AsyncSession,
    *,
    persona: Any | None,
    include_hidden: bool,
) -> FieldCompatibilityFeedback | None:
    logical_query = bound.logical_query
    if getattr(logical_query, "has_complex_sql", False):
        return _not_analyzed_field_compatibility()

    # Bug-5382: SELECT * (whole-model expansion) must never 422 on
    # field-compatibility.  Semi-additive / non-additive measures with
    # narrow compatible-dimension sets fire NO_JOIN_PATH for dimensions
    # that have no path to their table.  This is informational for
    # discovery UIs but must NOT block execution — the source path
    # handles star expansion correctly by projecting all physical columns.
    if getattr(logical_query, "select_star", False):
        return None

    selected_measures = [
        m for m in (getattr(bound, "resolved_measures", None) or [])
        if _uuid_or_none(getattr(m, "id", None)) is not None
    ]
    selected_dimensions = [
        d for d in (getattr(bound, "resolved_dimensions", None) or [])
        if _uuid_or_none(getattr(d, "id", None)) is not None
    ]
    if not selected_measures or not selected_dimensions:
        if getattr(bound, "has_passthrough_expressions", False):
            return _not_analyzed_field_compatibility()
        return None

    (
        measures,
        dimensions,
        tables,
        columns,
        joins,
        user_defined_attributes,
        aggregates,
        aggregate_columns,
    ) = await _load_field_compatibility_metadata(
        bound.model.id,
        getattr(bound.model, "deployed_version_id", None),
        db,
    )

    known_measure_ids = {
        measure_id for measure in measures
        if (measure_id := _uuid_or_none(getattr(measure, "id", None))) is not None
    }
    known_dimension_ids = {
        dimension_id for dimension in dimensions
        if (dimension_id := _uuid_or_none(getattr(dimension, "id", None))) is not None
    }
    measure_by_id = {
        measure_id: measure for measure in selected_measures
        if (measure_id := _uuid_or_none(getattr(measure, "id", None))) is not None
        and measure_id in known_measure_ids
    }
    dimension_by_id = {
        dimension_id: dimension for dimension in selected_dimensions
        if (dimension_id := _uuid_or_none(getattr(dimension, "id", None))) is not None
        and dimension_id in known_dimension_ids
    }
    has_unanalyzed_semantic_field = (
        len(measure_by_id) != len(selected_measures)
        or len(dimension_by_id) != len(selected_dimensions)
    )
    selected_measure_ids = list(measure_by_id)
    selected_dimension_ids = list(dimension_by_id)
    if not selected_measure_ids or not selected_dimension_ids:
        return _not_analyzed_field_compatibility() if has_unanalyzed_semantic_field else None

    policy = await _field_access_policy(
        db=db,
        persona=persona,
        include_hidden=include_hidden,
    )
    result = evaluate_field_compatibility(
        model_id=_stable_uuid(bound.model.id),
        version_id=_uuid_or_none(getattr(bound.model, "deployed_version_id", None)),
        measures=measures,
        dimensions=dimensions,
        tables=tables,
        columns=columns,
        joins=joins,
        user_defined_attributes=user_defined_attributes,
        aggregate_definitions=aggregates,
        aggregate_columns=aggregate_columns,
        policy=policy,
        selected_measure_ids=selected_measure_ids,
        selected_dimension_ids=selected_dimension_ids,
    )

    issues: list[FieldCompatibilityIssueResponse] = []
    for measure_id in selected_measure_ids:
        entry = result.measures.get(measure_id)
        if entry is None:
            continue
        for dimension_id in selected_dimension_ids:
            issue = entry.incompatible_dimensions.get(dimension_id)
            if issue is None:
                continue
            # Bug-5456 / Bug-3592 / Bug-5381: ``is_hidden`` is display CURATION,
            # not an access boundary. The binder already resolved this dimension
            # into ``bound.resolved_dimensions`` because it was named explicitly
            # (``SELECT channel_code`` resolves for any caller even when
            # ``include_hidden=False``; only ``SELECT *`` suppresses it). The
            # real access boundary (persona / RLS / CLS) is enforced by the
            # persona gate and surfaces as PERSONA_FIELD_UNAVAILABLE. Re-rejecting
            # an already-resolved hidden dimension here only because a measure is
            # also selected is inconsistent (``SELECT channel_code`` succeeds but
            # ``SELECT channel_code, SUM(transaction_amount)`` would 422) and
            # contradicts the documented curation-vs-access design, so a
            # HIDDEN_FIELD_UNAVAILABLE issue must not gate execution.
            if issue.code == HIDDEN_FIELD_UNAVAILABLE:
                continue
            # Only PERSONA_FIELD_UNAVAILABLE reaches here as a security-scoped
            # issue now (HIDDEN_FIELD_UNAVAILABLE is dropped above), so field
            # names are redacted for that code alone.
            hide_names = issue.code == PERSONA_FIELD_UNAVAILABLE
            issues.append(
                FieldCompatibilityIssueResponse(
                    code=issue.code,
                    severity=getattr(issue, "severity", "error"),
                    measure_name=(
                        None if hide_names else _field_name(measure_by_id.get(measure_id)) or entry.name
                    ),
                    dimension_name=(
                        None if hide_names else _field_name(dimension_by_id.get(dimension_id))
                    ),
                    message=issue.message,
                    compatible_dimension_names=issue.compatible_dimension_names,
                )
            )

    if not issues:
        return (
            _not_analyzed_field_compatibility()
            if has_unanalyzed_semantic_field
            else None
        )
    status_value = (
        "incompatible"
        if any(issue.severity == "error" for issue in issues)
        else "compatible"
    )
    return FieldCompatibilityFeedback(status=status_value, issues=issues)


def _compatibility_error_detail(
    feedback: FieldCompatibilityFeedback,
) -> dict[str, Any]:
    messages = [issue.message for issue in feedback.issues if issue.severity == "error"]
    return {
        "message": messages[0] if messages else "Field compatibility could not be verified.",
        "error_type": "field_compatibility",
        "field_compatibility": feedback.model_dump(),
    }


def _has_security_scoped_compatibility_issue(
    feedback: FieldCompatibilityFeedback | None,
) -> bool:
    if feedback is None:
        return False
    # HIDDEN_FIELD_UNAVAILABLE is no longer emitted into ``feedback.issues``
    # (it is dropped in _evaluate_bound_field_compatibility: hidden is curation,
    # not an access boundary). Only persona scoping is security-scoped here.
    return any(
        issue.code == PERSONA_FIELD_UNAVAILABLE
        for issue in feedback.issues
    )


def _compatibility_warning_messages(
    feedback: FieldCompatibilityFeedback | None,
) -> list[str]:
    if feedback is None:
        return []
    return [
        issue.message
        for issue in feedback.issues
        if issue.severity == "warning"
    ]


def _safe_requested_field_names(
    bound: BoundQuery,
    feedback: FieldCompatibilityFeedback | None,
) -> tuple[list[str], list[str]]:
    if _has_security_scoped_compatibility_issue(feedback):
        return [], []
    return (
        [m.name for m in bound.resolved_measures],
        [d.name for d in bound.resolved_dimensions],
    )


async def _handle_execute(
    body: ExecuteRequest,
    db: AsyncSession,
    user_identity: str,
    principal: Principal | None = None,
    persona_id: Optional[str] = None,
    persona: Any | None = None,
    tenant_id: str = "",
    drill_join_path_ids: Optional[list[str]] = None,
) -> ExecuteResponse:
    # 0. Bind model parameters into the SQL before parse (F-029-01).
    await _bind_query_parameters(body, db, persona_id)

    # 1. Parse to IR
    try:
        logical_query = _parse(body)
    except UnsupportedSQL as e:
        # F-003-03 / F-003-04: the DAX normalizer rejects unrepresentable
        # SUMMARIZE(COLUMNS) arguments and raw MDX with a typed error rather
        # than silently dropping grain/filters or routing non-SQL to source.
        # Surface it as a clean 422 feature_not_supported, not a generic 400.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"message": str(e), "error_type": "feature_not_supported", "sqlstate": e.sqlstate},
        )
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Parse failed: {e}")
    if drill_join_path_ids:
        logical_query.drill_join_path_ids = [
            str(join_id) for join_id in drill_join_path_ids if join_id
        ]

    # 1.2 Row-limit clamp (F-027-02): the smaller of the query's own
    # LIMIT and the caller's row_limit wins. Applied before the cache
    # key is computed so limited and unlimited shapes never collide.
    if body.row_limit is not None:
        logical_query.limit = (
            min(logical_query.limit, body.row_limit)
            if logical_query.limit is not None
            else body.row_limit
        )

    # 1.5 Intercept $KPIs virtual table queries
    kpi_table_hit = _detect_kpi_table(logical_query)
    if kpi_table_hit:
        return await _handle_kpi_table_query(
            db,
            body.model_id,
            logical_query,
            persona=persona,
            user_identity=user_identity,
            tenant_id=tenant_id,
            client_kind=body.client_kind,
        )

    # 2. Bind to semantic model
    try:
        bound = await bind_query_to_model(
            logical_query, db, include_hidden=body.include_hidden
        )
    except ModelNotDeployedError as e:
        # F-2: undeployed models are metadata-only. 409 Conflict tells the
        # gateway / BI tool the target isn't available yet, distinct from a
        # 422 which means the query itself couldn't be bound.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except CrossModelNotResolvedError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"message": str(e), "error_type": "cross_model_not_resolved"},
        )
    except SemanticBindingError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e))

    # 2.5 Persona allow-list gate (Phase 8.B.3) + default-filter merge (Phase 8.B.4)
    # Persona gate runs BEFORE cache lookup so policy is always enforced.
    #
    # F-008-23: route handlers resolve the execution persona before entering
    # this helper (resolve_execution_persona). Reuse that already-loaded row
    # for enforcement and the default-filter merge so we do not perform a
    # second identical DB load. The reused persona is a full Persona ORM row
    # and every field the gate/merge consume (included_*_ids, default_filters,
    # name, id) is a mapped_column loaded with the row — equivalent to what
    # load_persona returns, so reuse never under-enforces. Direct callers that
    # supply only persona_id still take the DB-loading apply_persona_gate path.
    try:
        if persona is not None:
            await enforce_persona_gate(
                db, persona=persona, model_id=body.model_id, bound=bound
            )
        else:
            persona = await apply_persona_gate(
                db, model_id=body.model_id, persona_id=persona_id, bound=bound
            )
    except HTTPException:
        if persona is None and persona_id:
            persona = await load_persona(
                db, model_id=body.model_id, persona_id=persona_id
            )
        field_compatibility = await _evaluate_bound_field_compatibility(
            bound,
            db,
            persona=persona,
            include_hidden=body.include_hidden,
        )
        if field_compatibility is not None and field_compatibility.status == "incompatible":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=_compatibility_error_detail(field_compatibility),
            )
        raise
    if persona is not None:
        merge_default_filters(persona, bound)

    field_compatibility = await _evaluate_bound_field_compatibility(
        bound,
        db,
        persona=persona,
        include_hidden=body.include_hidden,
    )
    if field_compatibility is not None and field_compatibility.status == "incompatible":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=_compatibility_error_detail(field_compatibility),
        )

    # 2.6 Cache lookup — keyed by model, tenant, principal, query shape,
    # include_hidden, and force_route. Persona-scoped queries bypass cache
    # entirely because persona policy is mutable and can change within TTL.
    if persona_id:
        cached = None
    else:
        # F-007-10 (closed in passing with H2): the hash must capture the
        # FULL row-security matching subject, not just the identity. Roles,
        # groups and claims all drive which RLS rules fire — two callers
        # sharing an identity but differing in any of them (re-login with
        # new roles/claims, admin simulate-as) must never collide on a
        # cached result within the TTL.
        principal_hash = ResultCache.make_principal_hash({
            "user_identity": user_identity,
            "roles": sorted(principal.roles) if principal else [],
            "groups": sorted(principal.groups) if principal else [],
            "claims": principal.claims if principal else {},
        })
        try:
            _q_dict = dataclasses.asdict(logical_query)
        except Exception:
            _q_dict = {"raw_query": body.raw_query}
        query_hash = ResultCache.make_query_hash(_q_dict)
        # B15 / F-013-01: the deployed version id is part of the cache key so a
        # Deploy invalidates cached results immediately. Without it, a query
        # cached under the previous deployment would be served stale after a
        # deploy — the served shape changed but the cached numbers did not,
        # defeating the snapshot pin.
        deployed_ver = str(getattr(bound.model, "deployed_version_id", "") or "")
        cache_key = (
            str(body.model_id), tenant_id, principal_hash, query_hash,
            body.force_route or "", str(body.include_hidden), deployed_ver,
        )
        cached = _cache.get(cache_key)

    if cached is not None:
        # Run column audit on cache hits to catch authorization drift
        try:
            audit_result_columns(bound, cached.columns, persona)
        except SecurityAuditError:
            _cache.delete(cache_key)
            cached = None
        if cached is not None:
            # F-030-03: a cache hit must still write the QueryLog row, emit the
            # Prometheus counters, the query-audit log line, and the platform
            # audit record — otherwise hot (cacheable) queries are an
            # observability blind spot: undercounted volume / top-users and no
            # "who saw this data" audit evidence. Recorded as the cached
            # route_type with execution_ms=0 (served-from-cache), no miss row.
            await record_query_cache_hit(
                db,
                bound=bound,
                cached=cached,
                user_identity=user_identity,
                tenant_id=tenant_id,
                persona=persona,
                client_kind=body.client_kind,
            )
            return cached

    # 3. Route
    _route_start_ms = time.monotonic()
    try:
        decision = await route_query(
            bound,
            db,
            principal=principal,
            force_route=body.force_route,
            persona=persona,
        )
    except NoAggregateMatchError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"message": str(e), "error_type": "no_aggregate_match"},
        )
    except UnsupportedSQL as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"message": str(e), "error_type": "feature_not_supported", "sqlstate": e.sqlstate},
        )
    except RowSecurityCompileError as e:
        # F-007-04: a malformed row-security rule must fail closed with a
        # typed, actionable error — not a generic 500. (Save-time validation
        # rejects most malformed rules; this guards rules that became
        # uncompilable via import or a direct DB edit.)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": (
                    "A row-level security rule on this model is misconfigured "
                    f"and could not be compiled: {e}. The query was blocked "
                    "(fail closed); ask a modeler to fix the rule's predicate."
                ),
                "error_type": "row_security_misconfigured",
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        # Bug-5360: a routing/rewrite-stage failure that is not one of the
        # typed errors above (e.g. the compiled-build TypeError where the
        # aggregate rewriter passed a `set` to a Cython-typed `list[str]`
        # param) used to propagate uncaught — FastAPI returned a bare 500
        # with NO QueryLog failure row, NO diagnostic warning and NO metric,
        # so the failure was invisible outside the raw container traceback.
        # Mirror the execution-stage treatment: log the failure (persisted
        # row + audit logger + metrics + failure-spike) then return a mapped
        # 502 with a sanitized message, so rewrite-path breakage is observable.
        logger.error("Routing/rewrite failed: %s", e, exc_info=True)
        await _log_query_failure(
            db, user_identity, tenant_id, bound, None, _route_start_ms,
            "routing_error", str(e),
            persona_id=persona.id if persona is not None else None,
            client_kind=body.client_kind,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=sanitize_error_for_client(e),
        )

    # 3.5 — 5. Execute through the shared observed pipeline (security
    # audits + execution + QueryLog/miss/metrics/audit). F-030-01: this
    # block is shared with /headless/query and /plugin/execute so those
    # paths can never skip logging again.
    rows, bytes_processed, columns, chosen_source, elapsed_ms, decision = (
        await execute_with_observation(
            bound=bound,
            decision=decision,
            db=db,
            user_identity=user_identity,
            tenant_id=tenant_id,
            persona=persona,
            client_kind=body.client_kind,
        )
    )

    trace = await _build_trace(
        body, logical_query, bound, decision, db,
        executed=True, chosen_source=chosen_source,
    )
    response = ExecuteResponse(
        rows=rows,
        columns=columns,
        route_type=decision.route_type,
        reason=decision.reason,
        aggregate_id=decision.aggregate_id,
        pocket_id=decision.pocket_id,
        execution_ms=elapsed_ms,
        bytes_processed=bytes_processed,
        rows_returned=len(rows),
        routed_sql=decision.rewritten_query,
        trace=trace,
        field_compatibility=field_compatibility,
    )
    if not persona_id:
        _cache.set(cache_key, response)
    return response


def _is_missing_relation_error(exc: Exception) -> bool:
    text = str(exc).lower()
    if 'relation "' in text and '" does not exist' in text:
        return True
    if "not found" in text and ("dataset" in text or "table" in text):
        return True
    return False


async def record_query_success(
    db: AsyncSession,
    *,
    bound,
    decision: RouteDecision,
    elapsed_ms: int,
    rows_returned: int,
    bytes_processed: int,
    user_identity: str,
    tenant_id: str,
    persona=None,
    client_kind: Optional[str] = None,
    log_miss: bool = True,
) -> None:
    """Persist QueryLog + miss log, emit Prometheus counters, the query
    audit log line, and the platform audit record for one successful
    query execution.

    F-030-01: extracted from ``_handle_execute`` step 5 so the query
    surfaces /execute, /headless/query and /plugin/execute run the
    identical observation block — headless/plugin used to skip it
    entirely, leaving those workloads invisible to telemetry, audit and
    the optimizer's miss analysis.

    NOTE: ``/discover`` (member discovery) is NOT routed through this
    function — ``_handle_discover_members`` writes its own QueryLog row
    and ``[SOURCE_AUDIT]`` line but intentionally emits no Prometheus
    metrics, no ``query.execute`` platform audit record and no miss log
    (member discovery is metadata-shaped traffic, not an analytical
    query; counting it would skew model-usage metrics and the
    optimizer's miss analysis).

    Identity is JWT-derived (CR-002 Finding 1), never client-supplied.
    """
    persona_uuid = persona.id if persona is not None else None
    await log_query(
        db=db,
        bound_query=bound,
        decision=decision,
        execution_ms=elapsed_ms,
        rows_returned=rows_returned,
        bytes_processed=bytes_processed,
        user_identity=user_identity,
        persona_id=persona_uuid,
        client_kind=client_kind,
    )
    QUERY_ROUTED_COUNT.labels(routed_to=decision.route_type).inc()
    _model = bound.model.display_name
    project = getattr(bound.model, "project", None)
    _project = getattr(project, "display_name", "") if project else ""
    _tenant = tenant_id
    MODEL_QUERY_COUNT.labels(
        tenant=_tenant,
        project=_project,
        model_name=_model,
        protocol=bound.logical_query.protocol,
        route_type=decision.route_type,
    ).inc()
    MODEL_QUERY_DURATION.labels(tenant=_tenant, project=_project, model_name=_model).observe(elapsed_ms / 1000)
    MODEL_BYTES_PROCESSED.labels(tenant=_tenant, project=_project, model_name=_model).inc(bytes_processed)
    MODEL_ROWS_RETURNED.labels(tenant=_tenant, project=_project, model_name=_model).inc(rows_returned)
    _query_audit_logger.info(
        "query_executed",
        extra={
            "user_email": user_identity,
            "tenant_id": tenant_id,
            "model_id": str(bound.model.id),
            "model_name": bound.model.display_name,
            "route_type": decision.route_type,
            "duration_ms": elapsed_ms,
            "row_count": rows_returned,
            "query_fingerprint": bound.logical_query.query_fingerprint[:16],
            "protocol": bound.logical_query.protocol,
        },
    )

    await audit(
        db,
        action="query.execute",
        severity="info",
        actor_email=user_identity,
        target_type="model",
        target_id=bound.model.id,
        target_name=bound.model.display_name,
        detail={
            "route_type": decision.route_type,
            "duration_ms": elapsed_ms,
            "row_count": rows_returned,
            "protocol": bound.logical_query.protocol,
        },
    )

    # A cache HIT served from a previously source-routed result is NOT a new
    # miss (the source execution already ran and logged its miss when the row
    # was first cached); ``log_miss=False`` suppresses a spurious miss row on
    # the cache-hit path (F-030-03) without re-classifying the route.
    if log_miss and decision.route_type == "source":
        miss_reason = decision.reason
        agg_skipped = getattr(decision, "aggregate_skipped_reasons", None)
        if agg_skipped:
            miss_reason = "aggregate_skip:" + ",".join(sorted(set(agg_skipped)))
        await log_query_miss(db, bound, miss_reason, persona_id=persona_uuid)


async def record_query_cache_hit(
    db: AsyncSession,
    *,
    bound,
    cached: "ExecuteResponse",
    user_identity: str,
    tenant_id: str,
    persona=None,
    client_kind: Optional[str] = None,
) -> None:
    """Persist observability for a result-cache HIT (F-030-03).

    Before this, ``_handle_execute`` returned the cached ``ExecuteResponse``
    BEFORE the logging/metrics/audit block, so a query repeated within the
    cache TTL produced no QueryLog row, no Prometheus increment, no
    ``query.execute`` audit record, and no per-user audit evidence no matter
    how many distinct users hit it. Usage analytics undercounted volume and
    top-users, and "who saw this data" was unanswerable from QueryLog for the
    hottest (cacheable, non-persona) queries.

    A cache hit is recorded as a real query of the cached route_type so it
    counts toward volume / top-users / acceleration analytics, with
    ``execution_ms=0`` as the served-from-cache signal (no source/aggregate/
    pocket execution happened). No miss row is written: the underlying route
    already ran (and was logged, including any miss) when the result was first
    cached — a cache hit is a HIT, never a miss, regardless of the route the
    cached value originally took.

    The original route is preserved by reconstructing the ``RouteDecision``
    from the cached response so a cached ``aggregate`` hit is still counted as
    an aggregate hit (not re-classified as source).
    """
    decision = RouteDecision(
        route_type=cached.route_type,
        rewritten_query=cached.routed_sql or "",
        reason=cached.reason,
        aggregate_id=cached.aggregate_id,
        pocket_id=cached.pocket_id,
    )
    # bytes_processed is reset to 0: no source bytes were scanned on a cache
    # hit. rows_returned is the served page size. execution_ms=0 distinguishes
    # the served-from-cache row from a real execution in the same route_type.
    await record_query_success(
        db,
        bound=bound,
        decision=decision,
        elapsed_ms=0,
        rows_returned=cached.rows_returned,
        bytes_processed=0,
        user_identity=user_identity,
        tenant_id=tenant_id,
        persona=persona,
        client_kind=client_kind,
        log_miss=False,
    )


async def execute_with_observation(
    *,
    bound,
    decision: RouteDecision,
    db: AsyncSession,
    user_identity: str,
    tenant_id: str,
    persona=None,
    client_kind: Optional[str] = None,
) -> tuple[list[dict], int, list[str], DataSource | DataTarget, int, RouteDecision]:
    """Run a routed query through the full observed execution pipeline.

    Steps (identical to the historical ``_handle_execute`` 3.5—5 block):
      1. ``audit_filters_present`` guardrail (structured 403 + failure
         QueryLog row + ``query.security_audit_block`` audit event on
         failure — see ``_security_audit_block``).
      2. ``execute_routed_query`` with the missing-relation source
         fallback, failure logging (``_log_query_failure``) and the
         standard 408/413/502 error mapping.
      3. ``audit_result_columns`` guardrail (same block treatment).
      4. ``record_query_success`` — QueryLog, miss log, metrics, audit.

    Returns ``(rows, bytes_processed, columns, chosen_source,
    elapsed_ms, decision)`` — ``decision`` may differ from the input
    when the missing-relation fallback re-routed to source.

    Shared by /execute, /headless/query and /plugin/execute (F-030-01):
    logging and the security guardrails cannot be skipped by any of the
    consumer API seams.
    """
    persona_uuid = persona.id if persona is not None else None
    start_ms = time.monotonic()

    # Bug-1045 round-3: anchor each filter to its dimension's physical
    # column / derivation expression so the presence audit can never be
    # satisfied by a value-colliding predicate on a different column.
    filter_anchors = await resolve_filter_anchors(bound, db)

    try:
        audit_filters_present(
            bound, decision.rewritten_query, decision.route_type,
            filter_anchors=filter_anchors,
        )
    except SecurityAuditError as e:
        raise await _security_audit_block(
            db, user_identity, tenant_id, bound, decision, start_ms, e,
            audit_layer="filter_presence",
            persona_id=persona_uuid, client_kind=client_kind,
        )

    try:
        rows, bytes_processed, columns, chosen_source = await execute_routed_query(bound, decision, db)
    except ResultTooLargeError as e:
        await _log_query_failure(db, user_identity, tenant_id, bound, decision, start_ms, "result_too_large", str(e), persona_id=persona_uuid, client_kind=client_kind)
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(e))
    except QueryTimeoutError as e:
        await _log_query_failure(db, user_identity, tenant_id, bound, decision, start_ms, "timeout", str(e), persona_id=persona_uuid, client_kind=client_kind)
        raise HTTPException(status_code=status.HTTP_408_REQUEST_TIMEOUT, detail=str(e))
    except CrossProjectConnectionError as e:
        # Bug-5325: the routed source/target connection belongs to a different
        # project than the model owning the query (legacy/imported malformed
        # row). The guard already fired BEFORE any SQL ran, so the query is
        # rejected fail-closed. Surface it as a clean 422 misconfiguration error
        # rather than letting it fall through to the generic 502 execution path.
        await _log_query_failure(
            db, user_identity, tenant_id, bound, decision, start_ms,
            "cross_project_connection", str(e),
            persona_id=persona_uuid, client_kind=client_kind,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "This model's source/target connection belongs to a different "
                "project and cannot be used. Re-point it at a connection in the "
                "correct project."
            ),
        )
    except Exception as e:
        if decision.route_type in ("aggregate", "pocket") and _is_missing_relation_error(e):
            # Bug-5346: the routed aggregate's physical table is missing (it was
            # never materialised, or was dropped, while the definition stayed
            # `active`). Best-effort flag the (still-`active`) definition `pending`
            # so the aggregate matcher stops routing to a vanished table (ending
            # the repeated source-fallback) and the optimizer sweep rebuilds it.
            # Isolated in a SAVEPOINT and fully swallowed so it can NEVER affect
            # the already-correct source-fallback response below.
            _missing_agg_id = (
                decision.aggregate_id if decision.route_type == "aggregate" else None
            )
            if _missing_agg_id:
                try:
                    async with db.begin_nested():
                        await db.execute(
                            update(AggregateDefinition)
                            .where(
                                AggregateDefinition.id == _missing_agg_id,
                                AggregateDefinition.status == "active",
                            )
                            .values(status="pending")
                        )
                except Exception:
                    logger.warning(
                        "Bug-5346: could not flag missing aggregate %s as pending",
                        _missing_agg_id,
                    )
            # F-006-01: re-rewrite for the SOURCE connection in the SOURCE
            # dialect. ``decision.target_dialect`` on an aggregate route is the
            # aggregate TARGET dialect (where the cache table lives); using it
            # would emit target-dialect SQL (e.g. PostgreSQL quoting) against a
            # cross-database source (BigQuery/Spark) and fail with a second,
            # more confusing error instead of recovering. ``source_dialect``
            # carries the source connection's dialect; fall back to
            # ``target_dialect`` for same-database routes where they coincide.
            _fallback_source_dialect = decision.source_dialect or decision.target_dialect
            decision = RouteDecision(
                route_type="source",
                rewritten_query=await rewrite_for_source(bound, db, target_dialect=_fallback_source_dialect),
                reason=(
                    f"Routed cache table missing physical table ({decision.aggregate_id or decision.pocket_id}); "
                    "fell back to source"
                ),
                aggregate_id=None,
                pocket_id=None,
                target_dialect=_fallback_source_dialect,
                source_dialect=_fallback_source_dialect,
            )
            try:
                audit_filters_present(
                    bound, decision.rewritten_query, decision.route_type,
                    filter_anchors=filter_anchors,
                )
            except SecurityAuditError as sa_err:
                raise await _security_audit_block(
                    db, user_identity, tenant_id, bound, decision, start_ms, sa_err,
                    audit_layer="filter_presence",
                    persona_id=persona_uuid, client_kind=client_kind,
                )
            try:
                rows, bytes_processed, columns, chosen_source = await execute_routed_query(bound, decision, db)
            except ResultTooLargeError as inner:
                await _log_query_failure(db, user_identity, tenant_id, bound, decision, start_ms, "result_too_large", str(inner), persona_id=persona_uuid, client_kind=client_kind)
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=str(inner),
                )
            except QueryTimeoutError as inner:
                await _log_query_failure(db, user_identity, tenant_id, bound, decision, start_ms, "timeout", str(inner), persona_id=persona_uuid, client_kind=client_kind)
                raise HTTPException(
                    status_code=status.HTTP_408_REQUEST_TIMEOUT,
                    detail=str(inner),
                )
            except Exception as inner:
                logger.error("Source query failed (fallback): %s", inner, exc_info=True)
                await _log_query_failure(db, user_identity, tenant_id, bound, decision, start_ms, "execution_error", str(inner), persona_id=persona_uuid, client_kind=client_kind)
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=sanitize_error_for_client(inner),
                )
        else:
            logger.error("Source query failed: %s", e, exc_info=True)
            await _log_query_failure(db, user_identity, tenant_id, bound, decision, start_ms, "execution_error", str(e), persona_id=persona_uuid, client_kind=client_kind)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=sanitize_error_for_client(e))
    elapsed_ms = int((time.monotonic() - start_ms) * 1000)

    # Security guardrail — verify result columns are authorised.
    # B10 round-2: audit blocks return a structured 403 (with telemetry)
    # rather than a bare 500 — the message makes clear this is a server-
    # side integrity safeguard, not a fixable request error.
    try:
        audit_result_columns(bound, columns, persona)
    except SecurityAuditError as e:
        raise await _security_audit_block(
            db, user_identity, tenant_id, bound, decision, start_ms, e,
            audit_layer="result_columns",
            persona_id=persona_uuid, client_kind=client_kind,
        )

    # Bug-5195: credit the aggregate hit ONLY after the routed query
    # executed successfully AND the security audits passed. The router
    # sets ``pending_hit_credit`` on aggregate RouteDecisions; consuming
    # it here (the single post-success point shared by /execute,
    # /plugin/execute and /headless/query) guarantees exactly-once
    # crediting — no credit on failure, no double-count.
    _pending_credit = getattr(decision, "pending_hit_credit", None)
    if _pending_credit is not None:
        await record_aggregate_hit(_pending_credit, db)

    await record_query_success(
        db,
        bound=bound,
        decision=decision,
        elapsed_ms=elapsed_ms,
        rows_returned=len(rows),
        bytes_processed=bytes_processed,
        user_identity=user_identity,
        tenant_id=tenant_id,
        persona=persona,
        client_kind=client_kind,
    )

    return rows, bytes_processed, columns, chosen_source, elapsed_ms, decision


async def _handle_explain(
    body: ExecuteRequest,
    db: AsyncSession,
    principal: Principal | None = None,
    persona_id: Optional[str] = None,
    persona: Any | None = None,
) -> ExplainResponse:
    # Bind model parameters before parse (F-029-01), same as execute.
    await _bind_query_parameters(body, db, persona_id)

    try:
        logical_query = _parse(body)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Parse failed: {e}")

    try:
        bound = await bind_query_to_model(
            logical_query, db, include_hidden=body.include_hidden
        )
    except ModelNotDeployedError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except CrossModelNotResolvedError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"message": str(e), "error_type": "cross_model_not_resolved"},
        )
    except SemanticBindingError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e))

    try:
        if persona is not None:
            await enforce_persona_gate(
                db, persona=persona, model_id=body.model_id, bound=bound
            )
        else:
            persona = await apply_persona_gate(
                db, model_id=body.model_id, persona_id=persona_id, bound=bound
            )
    except HTTPException:
        if persona is None and persona_id:
            persona = await load_persona(
                db, model_id=body.model_id, persona_id=persona_id
            )
        field_compatibility = await _evaluate_bound_field_compatibility(
            bound,
            db,
            persona=persona,
            include_hidden=body.include_hidden,
        )
        if field_compatibility is not None and field_compatibility.status == "incompatible":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=_compatibility_error_detail(field_compatibility),
            )
        raise
    if persona is not None:
        merge_default_filters(persona, bound)

    field_compatibility = await _evaluate_bound_field_compatibility(
        bound,
        db,
        persona=persona,
        include_hidden=body.include_hidden,
    )
    if field_compatibility is not None and field_compatibility.status == "incompatible":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=_compatibility_error_detail(field_compatibility),
        )

    try:
        decision = await route_query(
            bound,
            db,
            principal=principal,
            force_route=body.force_route,
            persona=persona,
        )
    except NoAggregateMatchError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"message": str(e), "error_type": "no_aggregate_match"},
        )
    except UnsupportedSQL as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"message": str(e), "error_type": "feature_not_supported", "sqlstate": e.sqlstate},
        )
    except RowSecurityCompileError as e:
        # F-007-04: explain must also fail closed with a typed error on a
        # misconfigured row-security rule (mirrors /execute).
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": (
                    "A row-level security rule on this model is misconfigured "
                    f"and could not be compiled: {e}."
                ),
                "error_type": "row_security_misconfigured",
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e))

    trace = await _build_trace(body, logical_query, bound, decision, db, executed=False)
    return ExplainResponse(
        route_type=decision.route_type,
        aggregate_id=decision.aggregate_id,
        pocket_id=decision.pocket_id,
        reason=decision.reason,
        rewritten_query=decision.rewritten_query,
        requested_measures=[m.name for m in bound.resolved_measures],
        requested_dimensions=[d.name for d in bound.resolved_dimensions],
        grain=logical_query.grain,
        query_fingerprint=logical_query.query_fingerprint,
        security_rules_applied=[
            str(r.get("rule_id"))
            for r in (decision.security_rules_applied or [])
            if isinstance(r, dict) and r.get("rule_id")
        ],
        trace=trace,
        field_compatibility=field_compatibility,
    )


async def _handle_validate(
    body: ExecuteRequest,
    db: AsyncSession,
    persona_id: Optional[str] = None,
    persona: Any | None = None,
) -> ValidateResponse:
    """Parse + bind only. Returns errors gracefully without raising."""
    # Bind model parameters before parse (F-029-01). A parameter resolution
    # failure is a validation error, surfaced gracefully (no raise).
    try:
        await _bind_query_parameters(body, db, persona_id)
    except HTTPException as e:
        return ValidateResponse(ok=False, errors=[str(e.detail)])

    try:
        logical_query = _parse(body)
    except Exception as e:
        return ValidateResponse(ok=False, errors=[f"Parse failed: {e}"])

    warnings = list(getattr(logical_query, "syntax_warnings", []) or [])
    try:
        bound = await bind_query_to_model(
            logical_query, db, include_hidden=body.include_hidden
        )
    except SemanticBindingError as e:
        return ValidateResponse(ok=False, errors=[str(e)], warnings=warnings)
    except Exception as e:
        return ValidateResponse(
            ok=False, errors=[f"Validation failed: {e}"], warnings=warnings
        )

    if persona is not None or persona_id:
        try:
            if persona is not None:
                await enforce_persona_gate(
                    db, persona=persona, model_id=body.model_id, bound=bound
                )
            else:
                persona = await apply_persona_gate(
                    db,
                    model_id=body.model_id,
                    persona_id=persona_id,
                    bound=bound,
                )
        except HTTPException as e:
            if persona is None and persona_id:
                try:
                    persona = await load_persona(
                        db, model_id=body.model_id, persona_id=persona_id
                    )
                except HTTPException:
                    persona = None
            if persona is not None:
                field_compatibility = await _evaluate_bound_field_compatibility(
                    bound,
                    db,
                    persona=persona,
                    include_hidden=body.include_hidden,
                )
                if (
                    field_compatibility is not None
                    and field_compatibility.status == "incompatible"
                ):
                    requested_measures, requested_dimensions = _safe_requested_field_names(
                        bound, field_compatibility
                    )
                    return ValidateResponse(
                        ok=False,
                        errors=[
                            issue.message for issue in field_compatibility.issues
                            if issue.severity == "error"
                        ],
                        warnings=warnings,
                        requested_measures=requested_measures,
                        requested_dimensions=requested_dimensions,
                        field_compatibility=field_compatibility,
                    )
            detail = e.detail if isinstance(e.detail, dict) else {"message": str(e.detail)}
            persona_denial = detail.get("error_code") == "PERSONA_OBJECT_NOT_INCLUDED"
            return ValidateResponse(
                ok=False,
                errors=[
                    "Selected fields are not available for the current persona."
                    if persona_denial
                    else detail.get("message", str(e.detail))
                ],
                warnings=warnings,
                requested_measures=[] if persona_denial else [m.name for m in bound.resolved_measures],
                requested_dimensions=[] if persona_denial else [d.name for d in bound.resolved_dimensions],
            )
        if persona is not None:
            merge_default_filters(persona, bound)

    field_compatibility = await _evaluate_bound_field_compatibility(
        bound,
        db,
        persona=persona,
        include_hidden=body.include_hidden,
    )
    if field_compatibility is not None and field_compatibility.status == "incompatible":
        requested_measures, requested_dimensions = _safe_requested_field_names(
            bound, field_compatibility
        )
        redact_security_fields = _has_security_scoped_compatibility_issue(
            field_compatibility
        )
        return ValidateResponse(
            ok=False,
            errors=[
                issue.message for issue in field_compatibility.issues
                if issue.severity == "error"
            ],
            warnings=warnings,
            requested_measures=requested_measures,
            requested_dimensions=requested_dimensions,
            query_fingerprint=logical_query.query_fingerprint,
            filters=[] if redact_security_fields else [
                {"dimension_name": f.dimension_name, "operator": f.operator, "value": f.value}
                for f in bound.resolved_filters
            ],
            grain=[] if redact_security_fields else logical_query.grain,
            has_unresolvable_where=logical_query.has_unresolvable_where,
            has_complex_sql=logical_query.has_complex_sql,
            select_star=logical_query.select_star,
            from_tables=logical_query.from_tables,
            field_compatibility=field_compatibility,
        )

    if field_compatibility is not None and field_compatibility.status == "not_analyzed":
        warnings = [
            *warnings,
            *[
                issue.message for issue in field_compatibility.issues
                if issue.severity == "warning"
            ],
        ]
    else:
        warnings = [*warnings, *_compatibility_warning_messages(field_compatibility)]

    return ValidateResponse(
        ok=True,
        errors=[],
        warnings=warnings,
        requested_measures=[m.name for m in bound.resolved_measures],
        requested_dimensions=[d.name for d in bound.resolved_dimensions],
        query_fingerprint=logical_query.query_fingerprint,
        filters=[
            {"dimension_name": f.dimension_name, "operator": f.operator, "value": f.value}
            for f in bound.resolved_filters
        ],
        grain=logical_query.grain,
        has_unresolvable_where=logical_query.has_unresolvable_where,
        has_complex_sql=logical_query.has_complex_sql,
        select_star=logical_query.select_star,
        from_tables=logical_query.from_tables,
        field_compatibility=field_compatibility,
    )


async def _build_trace(
    body: ExecuteRequest,
    logical_query: LogicalQuery,
    bound: BoundQuery,
    decision: RouteDecision,
    db: AsyncSession,
    executed: bool,
    chosen_source: DataSource | DataTarget | None = None,
) -> PipelineTrace:
    """Assemble the structured pipeline trace returned to the UI.

    ``chosen_source``: the DataSource or DataTarget the executor ran
    against. For aggregate/pocket routes this is a DataTarget; for
    source routes it's a DataSource.
    """
    steps: list[TraceStep] = []

    # 1. Parser
    parser_data: dict[str, Any] = {
        "protocol": body.protocol,
        "measures": logical_query.requested_measures,
        "dimensions": logical_query.requested_dimensions,
        "grain": logical_query.grain,
        "from_tables": logical_query.from_tables,
        "filter_count": len(logical_query.filters),
        "limit": logical_query.limit,
        "select_star": logical_query.select_star,
        "has_complex_sql": logical_query.has_complex_sql,
        "fingerprint": logical_query.query_fingerprint[:16],
    }
    if body.dialect:
        parser_data["input_dialect"] = body.dialect
    parser_status = "warn" if logical_query.syntax_warnings else "ok"
    parser_detail = (
        f"Parsed {body.protocol.upper()} query into the logical IR. "
        f"{len(logical_query.requested_measures)} measure(s), "
        f"{len(logical_query.requested_dimensions)} dimension(s), "
        f"{len(logical_query.filters)} filter(s)."
    )
    if logical_query.syntax_warnings:
        parser_detail += " Warnings: " + "; ".join(logical_query.syntax_warnings)
    steps.append(
        TraceStep(
            stage="parser",
            title="Parse SQL → IR",
            detail=parser_detail,
            status=parser_status,
            data=parser_data,
        )
    )

    # 2. Binder
    steps.append(
        TraceStep(
            stage="binder",
            title="Bind to semantic model",
            detail=(
                f"Resolved {len(bound.resolved_measures)} measure(s) and "
                f"{len(bound.resolved_dimensions)} dimension(s) against model "
                f"'{bound.model.display_name or bound.model.slug}'."
            ),
            status="ok",
            data={
                "model": bound.model.display_name or bound.model.slug,
                "measures": [m.name for m in bound.resolved_measures],
                "dimensions": [d.name for d in bound.resolved_dimensions],
                "passthrough": bound.has_passthrough_expressions,
            },
        )
    )

    # 3. Router
    router_status = "ok"
    if decision.route_type == "aggregate":
        router_title = "Route → aggregate"
    elif decision.route_type == "pocket":
        router_title = "Route → pocket"
    else:
        router_title = "Route → source"
    # F-004-11: surface the per-candidate skip reasons in the trace step data
    # so a modeler can see WHY a query did not hit an aggregate or pocket.
    # These were previously computed (AggregateSkipReason / PocketSkipReason)
    # and written only to the write-only RouteLog / a 64-char-truncated
    # miss_reason — never reaching the /explain drawer. ``aggregate_skipped_reasons``
    # is deduplicated for readability; the raw reasons remain on the decision.
    router_data: dict[str, Any] = {
        "route_type": decision.route_type,
        "aggregate_id": decision.aggregate_id,
        "pocket_id": decision.pocket_id,
    }
    _agg_skips = getattr(decision, "aggregate_skipped_reasons", None)
    if _agg_skips:
        router_data["aggregate_skipped_reasons"] = sorted(set(_agg_skips))
    _pocket_skip = getattr(decision, "pocket_skipped_reason", None)
    if _pocket_skip:
        router_data["pocket_skipped_reason"] = _pocket_skip
    steps.append(
        TraceStep(
            stage="router",
            title=router_title,
            detail=decision.reason,
            status=router_status,
            data=router_data,
        )
    )

    # 4. Rewriter
    if decision.route_type == "aggregate" and decision.aggregate_id:
        rewriter_target = f"aggregate table {decision.aggregate_id}"
    elif decision.route_type == "pocket" and decision.pocket_id:
        rewriter_target = f"pocket table {decision.pocket_id}"
    else:
        rewriter_target = "source tables"
    steps.append(
        TraceStep(
            stage="rewriter",
            title=f"Rewrite for {rewriter_target.split(' ')[0]}",
            detail=f"Generated SQL targeting {rewriter_target}.",
            status="ok",
            data={"rewritten_sql": decision.rewritten_query},
        )
    )

    # 5. Executor — only present when we actually ran the query
    if executed:
        steps.append(
            TraceStep(
                stage="executor",
                title="Execute on source system",
                detail="Sent the rewritten SQL to the underlying connection and streamed the rows back.",
                status="ok",
                data={},
            )
        )

    # Target system info — use the source the executor actually ran
    # against, or fall back to a best-effort resolution for
    # explain/validate paths. CR-002 Finding 7: the old .limit(1)
    # lookup could disagree with the executor on multi-source models.
    target_system: Optional[TargetSystemInfo] = None
    src: DataSource | DataTarget | None = chosen_source
    if src is None and not executed:
        try:
            src = await _resolve_query_source(bound.model.id, db, bound=bound)
        except HTTPException:
            src = None
    if src is not None:
        # Bug-5325 fail-closed: this is metadata-only (no SQL is executed
        # here), but resolve through the shared guard so a cross-project row
        # never surfaces another project's connection details even in trace.
        try:
            conn = await resolve_endpoint_connection(
                db, src, expected_project_id=bound.model.project_id
            )
        except ValueError:
            conn = None
        location = ""
        if isinstance(src.config, dict):
            location = (
                src.config.get("schema")
                or src.config.get("dataset")
                or src.config.get("database")
                or ""
            )
        fallback_type = getattr(src, "source_type", None) or getattr(src, "target_type", None) or ""
        target_system = TargetSystemInfo(
            name=src.display_name,
            type=conn.connection_type if conn else fallback_type,
            location=str(location) if location else None,
        )

    # Aggregate-used info — load the matched aggregate definition for tooltip
    aggregate_used: Optional[AggregateUsedInfo] = None
    pocket_used: Optional[PocketUsedInfo] = None
    if decision.route_type == "aggregate" and decision.aggregate_id:
        from shared.db.models import AggregateDefinition

        agg = await db.get(AggregateDefinition, decision.aggregate_id)
        if agg is not None:
            aggregate_used = AggregateUsedInfo(
                id=str(agg.id),
                physical_table_name=agg.physical_table_name,
                grain=list(agg.grain or []),
                creation_reason=agg.creation_reason,
                status=agg.status,
            )
    elif decision.route_type == "pocket" and decision.pocket_id:
        from shared.db.models import PocketDefinition

        try:
            pocket_uuid = uuid.UUID(str(decision.pocket_id))
        except (TypeError, ValueError):
            pocket_uuid = None
        pocket = await db.get(PocketDefinition, pocket_uuid) if pocket_uuid else None
        if pocket is not None:
            pocket_used = PocketUsedInfo(
                id=str(pocket.id),
                physical_table_name=pocket.physical_table_name,
                status=pocket.status,
                refresh_policy=pocket.refresh_policy,
            )

    return PipelineTrace(
        steps=steps,
        target_system=target_system,
        aggregate_used=aggregate_used,
        pocket_used=pocket_used,
    )


def _detect_kpi_table(logical_query: LogicalQuery) -> bool:
    """Return True if the query references a $KPIs virtual table.

    F-001-04: PostgreSQL folds unquoted identifiers to lowercase, so a client
    issuing ``modelx$KPIs`` unquoted sends ``modelx$kpis``. Match the suffix
    case-insensitively so the scorecard table is reachable either way.
    """
    for table in logical_query.from_tables:
        if table.lower().endswith("$kpis"):
            return True
    return False


def _parse_persona_allowed_measure_ids(persona: Any | None) -> set[str] | None:
    """Return the persona's included measure-id allow-list as a string set.

    Returns ``None`` when there is no persona or its measure allow-list is
    empty (unrestricted — every measure is visible). Fail-closed: a malformed
    id collapses the persona to "allows nothing" rather than silently widening
    visibility.

    DELIBERATE DIVERGENCE (Phase 7 review F-P7-02): model-service
    ``parse_allowed_ids`` raises HTTP 500 ("Persona configuration error") on a
    malformed id; here, on the JDBC ``$KPIs`` read path, we instead collapse to
    an empty allow-set and return 200 with every measure-bearing KPI withheld
    (lineage-free KPIs still served). Both are fail-closed (no measure-bearing
    KPI leaks). We keep the graceful-degradation form here so a single corrupt
    persona id does not hard-fail an entire BI client's ``$KPIs`` catalogue
    fetch; the malformed id is logged at ERROR for operability.
    """
    raw = getattr(persona, "included_measure_ids", None) if persona else None
    if not raw:
        return None
    out: set[str] = set()
    for v in raw:
        try:
            out.add(str(uuid.UUID(str(v))))
        except (TypeError, ValueError):
            logger.error("Malformed UUID in persona measure allow-list: %s", v)
            return set()
    return out


def _kpi_allowed_by_persona(
    kpi: Any,
    allowed_measure_ids: set[str] | None,
    measure_name_to_id: dict[str, str],
) -> bool:
    """Return True when every measure a KPI depends on is in persona scope.

    Mirrors model-service ``_kpi_visible_to_persona`` (the canonical KPI
    persona-visibility contract) so the $KPIs JDBC virtual table and the
    metadata API never diverge on what a persona may see. Lineage is resolved
    from the KPI's measure references:

      * ``expression`` / ``target_expression`` — DSL ``measure("Name")`` refs,
        resolved to ids via ``measure_name_to_id``.
      * ``target_measure_id`` — a direct measure id.

    Fail-closed: a referenced measure name that resolves to no id, or a
    target measure id outside the allow-list, withholds the KPI.

    ``allowed_measure_ids is None`` means unrestricted (serve as before).
    """
    if allowed_measure_ids is None:
        return True
    referenced_names: set[str] = set()
    for expression in (
        getattr(kpi, "expression", None),
        getattr(kpi, "target_expression", None),
    ):
        if expression:
            referenced_names.update(extract_measure_names(expression))
    for name in referenced_names:
        mid = measure_name_to_id.get(name)
        if mid is None or mid not in allowed_measure_ids:
            return False
    target_measure_id = getattr(kpi, "target_measure_id", None)
    if target_measure_id is not None:
        if str(target_measure_id) not in allowed_measure_ids:
            return False
    return True


async def _handle_kpi_table_query(
    db: AsyncSession,
    model_id: str,
    logical_query: LogicalQuery,
    *,
    persona: Any | None = None,
    user_identity: str = "",
    tenant_id: str = "",
    client_kind: Optional[str] = None,
) -> ExecuteResponse:
    """Handle a query against the $KPIs virtual table.

    Reads from kpi_latest (tenant meta schema) and returns results directly
    without going through the normal bind/route/execute pipeline.

    Bug-3613 — gate + observe. KPI scorecard values are derived from
    persona-restricted measures, so the served rows are gated on
    MEASURE-LINEAGE: a KPI whose underlying measure(s) the caller's persona
    does not allow is WITHHELD (fail-closed, omitted from the response). The
    response is then routed through ``record_query_success`` so $KPIs traffic
    produces a QueryLog / metrics / audit row like every other query, and the
    caller's ``row_limit`` (already folded into ``logical_query.limit`` at the
    clamp in step 1.2) is honoured.

    LIMITATION (out of scope): this gate enforces measure VISIBILITY only. It
    CANNOT enforce row-level security. ``kpi_latest`` holds a value already
    aggregated across all rows — there is no per-row data to filter at serve
    time — so a row-restricted persona viewing a global-total KPI built on an
    ALLOWED measure still sees the global number. Closing that would require a
    per-persona KPI recompute (or outright withholding any KPI on a
    row-restricted measure) and is a future item, not this fix.
    """
    from shared.db.models import KPI, KPILatest

    start_ms = time.monotonic()
    # F-017-05: $KPIs only exposes deployed KPIs. kpi_latest is upserted for
    # every evaluated KPI (incl. undeployed drafts opened in the scorecard), so
    # join to the KPI row and filter on is_deployed — an undeployed KPI is
    # absent from the JDBC $KPIs virtual table while remaining editable in the
    # builder.
    result = await db.execute(
        select(KPILatest, KPI)
        .join(KPI, KPI.id == KPILatest.kpi_id)
        .where(KPILatest.model_id == model_id)
        .where(KPI.is_deployed.is_(True))
    )
    kpi_pairs = list(result.all())

    # Bug-3613: resolve the persona measure allow-list once, then gate each
    # KPI on its measure lineage. measure_name_to_id is only loaded when a
    # restriction is in force (empty allow-list = unrestricted = no lookup).
    allowed_measure_ids = _parse_persona_allowed_measure_ids(persona)
    measure_name_to_id: dict[str, str] = {}
    if allowed_measure_ids is not None:
        meas_result = await db.execute(
            select(Measure.name, Measure.id).where(Measure.model_id == model_id)
        )
        measure_name_to_id = {name: str(mid) for name, mid in meas_result.all()}

    rows: list[dict[str, Any]] = []
    for kpi_latest, kpi in kpi_pairs:
        if not _kpi_allowed_by_persona(kpi, allowed_measure_ids, measure_name_to_id):
            # Fail-closed: the persona cannot see this KPI's underlying
            # measure(s); withhold the row entirely.
            continue
        rows.append({
            "kpi_name": kpi_latest.kpi_name,
            "value": float(kpi_latest.value) if kpi_latest.value is not None else None,
            "target": float(kpi_latest.target) if kpi_latest.target is not None else None,
            "status": kpi_latest.status,
            "status_label": kpi_latest.status_label,
            "trend_pct": float(kpi_latest.trend_pct) if kpi_latest.trend_pct is not None else None,
            "formatted_value": kpi_latest.formatted_value,
            "evaluated_at": kpi_latest.evaluated_at.isoformat() if kpi_latest.evaluated_at else None,
        })

    # Bug-3613: honour the caller's row_limit. The clamp in _handle_execute
    # step 1.2 already folded body.row_limit into logical_query.limit (the
    # smaller of the query LIMIT and the caller cap), so applying it here
    # gives $KPIs the same row-cap behaviour as every other route.
    if logical_query.limit is not None and len(rows) > logical_query.limit:
        rows = rows[: logical_query.limit]

    columns = [
        "kpi_name", "value", "target", "status",
        "status_label", "trend_pct", "formatted_value", "evaluated_at",
    ]
    elapsed_ms = int((time.monotonic() - start_ms) * 1000)

    # Bug-3613: observe $KPIs like every other query. record_query_success
    # writes the QueryLog row, emits the Prometheus counters, the query-audit
    # log line and the ``query.execute`` platform audit record. We build a
    # minimal BoundQuery (the real Model + the parsed LogicalQuery; no
    # resolved measures/dimensions — $KPIs is a virtual metadata table) and a
    # RouteDecision tagged ``kpi_metadata``. log_miss=False: $KPIs never runs
    # a source query, so it is not an optimizer miss.
    routed_sql = f"SELECT * FROM kpi_latest WHERE model_id = '{model_id}'"
    # Eager-load project: record_query_success reads model.project.display_name
    # for the per-model metrics labels, and a lazy relationship access outside
    # the load scope raises MissingGreenlet under async SQLAlchemy.
    from sqlalchemy.orm import selectinload

    model_result = await db.execute(
        select(Model)
        .where(Model.id == uuid.UUID(model_id))
        .options(selectinload(Model.project))
    )
    model = model_result.scalar_one_or_none()
    if model is None:
        # Bug-5413: a since-deleted model must leave an audit trail and
        # fail closed rather than silently skipping observation.
        logger.warning(
            "$KPIs query for deleted model %s by user=%s — returning 404",
            model_id, user_identity,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model {model_id} not found.",
        )

    bound = BoundQuery(
        logical_query=logical_query,
        model=model,
        resolved_measures=[],
        resolved_dimensions=[],
        resolved_filters=[],
    )
    decision = RouteDecision(
        route_type="kpi_metadata",
        rewritten_query=routed_sql,
        reason="$KPIs virtual table — reading from kpi_latest",
    )
    await record_query_success(
        db,
        bound=bound,
        decision=decision,
        elapsed_ms=elapsed_ms,
        rows_returned=len(rows),
        bytes_processed=0,
        user_identity=user_identity,
        tenant_id=tenant_id,
        persona=persona,
        client_kind=client_kind,
        log_miss=False,
    )

    return ExecuteResponse(
        rows=rows,
        columns=columns,
        route_type="kpi_metadata",
        reason="$KPIs virtual table — reading from kpi_latest",
        aggregate_id=None,
        pocket_id=None,
        execution_ms=elapsed_ms,
        bytes_processed=0,
        rows_returned=len(rows),
        routed_sql=routed_sql,
    )


def _parse(body: ExecuteRequest):
    if body.protocol == "dax":
        return parse_dax_to_ir(body.raw_query, body.model_id, parsed_dax=body.parsed_dax)
    # B10 round-2 (telemetry attribution): "mcp" is SQL over HTTP with the
    # exact JDBC strictness semantics — the parser's strict-syntax and
    # GROUP BY enforcement branches key off protocol == "jdbc", so parse
    # as jdbc and restore the caller's label afterwards so QueryLog /
    # metrics can attribute MCP traffic.
    parser_protocol = "jdbc" if body.protocol == "mcp" else body.protocol
    logical_query = parse_sql_to_ir(
        body.raw_query,
        body.model_id,
        protocol=parser_protocol,
        input_dialect=body.dialect,
    )
    if parser_protocol != body.protocol:
        logical_query.protocol = body.protocol
    return logical_query


async def _log_discover_members_early_exit(
    db: AsyncSession,
    model_id: str,
    user_identity: str,
    fingerprint: str,
    dim_name: str,
    *,
    error_type: str,
    error_detail: str,
) -> None:
    """Bug-5415: persist a QueryLog row for discover-members early exits.

    The normal success path logs via ``log_query`` with a full BoundQuery.
    The early-exit paths (SemanticBindingError, no resolved dimensions)
    have no bound query, so this lightweight helper writes a minimal row
    so the failure is observable in query history and audit logs.
    """
    try:
        entry = QueryLog(
            model_id=uuid.UUID(model_id),
            user_identity=user_identity,
            protocol="discover_members",
            raw_query=f"DISCOVER_MEMBERS({dim_name})",
            query_fingerprint=fingerprint,
            route_type="discover_members",
            execution_ms=0,
            rows_returned=0,
            bytes_processed=0,
            status="error",
            error_type=error_type,
            error_detail=error_detail[:4000],
        )
        db.add(entry)
        await db.commit()
    except Exception:
        logger.warning("Failed to log discover_members early exit", exc_info=True)
        await db.rollback()


_DISCOVER_DISPLAY_ALIAS = "__display_caption"


async def _augment_discover_with_display_column(
    bound: Any,
    db: AsyncSession,
) -> str | None:
    """Bug-5434: if the discovered (flat) dimension carries a distinct DISPLAY
    column, add it to the bound query so the source rewriter selects it DISTINCT
    alongside the key.

    Appends:
      - a synthetic resolved dimension whose ``source_column_id`` is the display
        column (so ``_build_dimension_select_pieces`` emits the column), named
        with a fixed internal alias that cannot collide with a user dimension;
      - a passthrough ``SelectExpression`` for that alias so the rewriter marks
        it a standalone SELECT column (otherwise dimensions are GROUP-BY-only).

    Returns the internal alias the result rows will carry the display value under,
    or ``None`` when the dimension has no display column (the legacy path).

    Engine-safe: only the bound query (data) is mutated here; the binder, router
    and rewriter are not modified.
    """
    if not bound.resolved_dimensions:
        return None
    key_dim = bound.resolved_dimensions[0]
    display_column_id = getattr(key_dim, "display_column_id", None)
    if display_column_id is None:
        return None
    disp_col = await db.get(ModelColumn, display_column_id)
    if disp_col is None:
        return None
    alias = _DISCOVER_DISPLAY_ALIAS
    # Synthetic resolved dimension for the display column. Mirrors the fields the
    # source rewriter reads on a resolved dimension (name, source_column_id,
    # user_defined_attribute_id, calc_expression, is_invalid).
    display_dim = SimpleNamespace(
        id=None,
        name=alias,
        source_column_id=display_column_id,
        user_defined_attribute_id=None,
        calc_expression=None,
        is_invalid=False,
    )
    bound.resolved_dimensions.append(display_dim)
    # Passthrough SELECT expression so the rewriter treats the display column as a
    # standalone SELECT item (not GROUP-BY-only) and aliases it by ``alias``.
    bound.logical_query.select_expressions.append(
        SelectExpression(
            raw_text=alias,
            alias=None,
            classification="passthrough",
            agg_function=None,
            inner_column=alias,
            inner_literal=None,
        )
    )
    return alias


async def _handle_discover_members(
    body: DiscoverMembersRequest,
    db: AsyncSession,
    *,
    current_user: CurrentUser,
    persona: Any | None = None,
) -> DiscoverMembersResponse:
    import hashlib

    dim_name = body.dimension_name
    fp = hashlib.sha256(
        f"discover:{body.model_id}:{dim_name}".encode()
    ).hexdigest()

    logical_query = LogicalQuery(
        model_id=body.model_id,
        protocol="discover_members",
        raw_query=f"DISCOVER_MEMBERS({dim_name})",
        requested_measures=[],
        requested_dimensions=[dim_name],
        filters=[],
        grain=[],
        order_by=[(dim_name, "asc")],
        # Bug-5436a: was a silent 1000-member cap that truncated large dimension
        # filter dropdowns; now configurable (default 100000).
        limit=_get_settings().MEMBER_DISCOVERY_LIMIT,
        offset=None,
        query_fingerprint=fp,
        has_distinct=True,
        select_expressions=[
            SelectExpression(
                raw_text=dim_name,
                alias=None,
                classification="passthrough",
                agg_function=None,
                inner_column=dim_name,
                inner_literal=None,
            ),
        ],
    )

    try:
        bound = await bind_query_to_model(logical_query, db)
    except ModelNotDeployedError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except SemanticBindingError as e:
        # Bug-5415: log the binding failure before returning empty so the
        # failed member discovery is observable in QueryLog.
        await _log_discover_members_early_exit(
            db, body.model_id, current_user.email, fp,
            dim_name, error_type="binding_error", error_detail=str(e),
        )
        return DiscoverMembersResponse(members=[], levels=[])

    if not bound.resolved_dimensions:
        # Bug-5415: no resolved dimensions — log before returning empty.
        await _log_discover_members_early_exit(
            db, body.model_id, current_user.email, fp,
            dim_name, error_type="no_resolved_dimensions",
            error_detail=f"Dimension '{dim_name}' resolved to zero dimensions",
        )
        return DiscoverMembersResponse(members=[], levels=[])

    # F-14: When persona is already resolved by the route handler, skip the
    # redundant DB lookup in apply_persona_gate and enforce directly.
    if persona is not None:
        await enforce_persona_gate(db, persona=persona, model_id=body.model_id, bound=bound)
        merge_default_filters(persona, bound)
    elif body.persona_id:
        persona = await apply_persona_gate(
            db, model_id=body.model_id, persona_id=body.persona_id, bound=bound,
        )
        if persona is not None:
            merge_default_filters(persona, bound)

    # Bug-5434: flat-dim display attribute. When the resolved key dimension
    # declares a distinct DISPLAY column, select it alongside the key so the
    # discovered members carry a caption that differs from the key. This is an
    # engine-safe augmentation of the CONSUMER (this handler): we append a
    # synthetic resolved dimension + a passthrough select expression for the
    # display column so the source rewriter emits both DISTINCT columns. The
    # binder/router/rewriter are untouched. The display alias is internal and
    # never collides with the user-facing dimension name.
    display_alias = await _augment_discover_with_display_column(bound, db)

    principal = Principal.from_current_user(current_user)
    try:
        # force_route="source": member discovery requires exact distinct values
        # from the source table. Aggregates may contain only a subset of dimension
        # values (those appearing in the aggregate grain), so routing through an
        # aggregate could return incomplete member lists.
        decision = await route_query(
            bound, db, principal=principal,
            force_route="source", persona=persona,
        )
    except RowSecurityCompileError as e:
        # F-007-04: member discovery routes through route_query too, so a
        # misconfigured rule must fail closed here with a typed error rather
        # than a generic 500.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": (
                    "A row-level security rule on this model is misconfigured "
                    f"and could not be compiled: {e}."
                ),
                "error_type": "row_security_misconfigured",
            },
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e),
        )

    start_ms = time.monotonic()
    tenant_id = current_user.tenant_id
    persona_uuid = persona.id if persona else None

    try:
        # Bug-1045 round-3: dimension-anchored filter-presence audit.
        filter_anchors = await resolve_filter_anchors(bound, db)
        audit_filters_present(
            bound, decision.rewritten_query, decision.route_type,
            filter_anchors=filter_anchors,
        )
    except SecurityAuditError as e:
        raise await _security_audit_block(
            db, current_user.email, tenant_id, bound, decision, start_ms, e,
            audit_layer="filter_presence", persona_id=persona_uuid,
        )

    try:
        rows, bytes_processed, columns, _ = await execute_routed_query(bound, decision, db)
    except QueryTimeoutError as exc:
        await _log_query_failure(
            db, current_user.email, tenant_id, bound, decision,
            start_ms, "timeout", str(exc), persona_id=persona_uuid,
        )
        raise HTTPException(
            status_code=status.HTTP_408_REQUEST_TIMEOUT, detail=str(exc),
        )
    except Exception as exc:
        logger.error("Failed to query members: %s", exc, exc_info=True)
        await _log_query_failure(
            db, current_user.email, tenant_id, bound, decision,
            start_ms, "execution_error", str(exc), persona_id=persona_uuid,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Failed to query members: {sanitize_error_for_client(exc)}",
        )
    elapsed_ms = int((time.monotonic() - start_ms) * 1000)

    try:
        audit_result_columns(bound, columns, persona)
    except SecurityAuditError as e:
        raise await _security_audit_block(
            db, current_user.email, tenant_id, bound, decision, start_ms, e,
            audit_layer="result_columns", persona_id=persona_uuid,
        )

    await log_query(
        db=db,
        bound_query=bound,
        decision=decision,
        execution_ms=elapsed_ms,
        rows_returned=len(rows),
        bytes_processed=bytes_processed,
        user_identity=current_user.email,
        persona_id=persona_uuid,
    )

    logger.info(
        "[SOURCE_AUDIT] discoverMembers model=%s dim=%s user=%s",
        body.model_id, dim_name, current_user.email,
    )

    dim = bound.resolved_dimensions[0]
    source_column_id = getattr(dim, "source_column_id", None)
    if source_column_id:
        level_result = await db.execute(
            select(HierarchyLevel.name)
            .join(HierarchyDefinition, HierarchyLevel.hierarchy_id == HierarchyDefinition.id)
            .where(HierarchyDefinition.model_id == body.model_id)
            .where(HierarchyLevel.key_attribute_id == source_column_id)
            .where(HierarchyLevel.key_attribute_source == "physical_column")
            .order_by(HierarchyLevel.ordinal)
        )
        levels = [row[0] for row in level_result.all()]
    else:
        levels = []
    if not levels:
        levels = [dim_name]

    members = []
    for i, row in enumerate(rows):
        val = row.get(dim_name)
        if val is None:
            first_val = next(iter(row.values()), None) if row else None
            val = first_val if first_val is not None else ""
        str_val = str(val)
        # Bug-5434: when a distinct display column was selected, surface its value
        # as the member CAPTION (the gateway emits it as MEMBER_NAME) while the key
        # value remains the member identity (MEMBER_KEY / name). When no display
        # column is configured, caption falls back to the key (legacy behaviour,
        # the gateway's `mem.get("caption") or mname` already handles a missing
        # caption, but we set it explicitly for clarity).
        member = {
            "name": str_val,
            "key": str_val,
            "level": levels[0],
            "ordinal": i,
            "parent": "",
        }
        if display_alias is not None:
            disp_val = row.get(display_alias)
            member["caption"] = str(disp_val) if disp_val is not None else str_val
        members.append(member)

    return DiscoverMembersResponse(members=members, levels=levels)


async def _handle_query_rewrites(
    db: AsyncSession,
    *,
    model_id: Optional[str],
    limit: int,
) -> QueryRewritesResponse:
    stmt = (
        select(
            QueryLog.model_id,
            QueryLog.query_fingerprint,
            QueryLog.raw_query,
            QueryLog.rewritten_query,
            QueryLog.route_type,
            func.count(QueryLog.id).label("hit_count"),
            func.max(QueryLog.created_at).label("last_seen"),
            func.min(QueryLog.created_at).label("first_seen"),
        )
        .group_by(
            QueryLog.model_id,
            QueryLog.query_fingerprint,
            QueryLog.raw_query,
            QueryLog.rewritten_query,
            QueryLog.route_type,
        )
        .order_by(func.max(QueryLog.created_at).desc())
        .limit(limit)
    )
    if model_id:
        try:
            model_uuid = uuid.UUID(model_id)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"model_id must be a UUID; got {model_id!r}",
            )
        stmt = stmt.where(QueryLog.model_id == model_uuid)

    result = await db.execute(stmt)
    rows: list[QueryRewriteRow] = []
    for row in result.all():
        rows.append(
            QueryRewriteRow(
                model_id=str(row.model_id) if row.model_id else None,
                query_fingerprint=row.query_fingerprint,
                raw_query=row.raw_query,
                rewritten_query=row.rewritten_query,
                route_type=row.route_type,
                hit_count=int(row.hit_count),
                last_seen=row.last_seen.isoformat() if row.last_seen else "",
                first_seen=row.first_seen.isoformat() if row.first_seen else "",
            )
        )
    return QueryRewritesResponse(rows=rows)


async def _collect_touched_source_ids(bound, db: AsyncSession) -> set:
    """Walk resolved dimensions + measures, map each through its model_column
    to the owning model_table, and collect the distinct set of source_ids the
    query actually touches.

    Phase D of the code-review remediation plan: this replaces the old
    ``.limit(1)`` data-source lookup that silently picked the first source
    regardless of which tables the query touched. Cross-source queries now
    surface as HTTP 400 with a stable error code instead of executing
    against the wrong backend.

    Bug-903: also includes filter-only dimensions (resolved_filters) and
    order-only columns (logical_query.order_by) which can live on a different
    source than the selected columns.
    """
    from shared.db.models import Dimension as DimensionORM, Measure as MeasureORM
    from sqlalchemy import select

    # Phase 1 — source_column_ids from objects in the SELECT/GROUP BY list.
    column_ids: set = {
        obj.source_column_id
        for obj in (list(bound.resolved_dimensions) + list(bound.resolved_measures))
        if getattr(obj, "source_column_id", None) is not None
    }

    # Phase 2 — look up filter-only dimensions/measures by semantic name.
    filter_names = {
        f.dimension_name
        for f in getattr(bound, "resolved_filters", [])
    }
    # Phase 3 — look up order-only columns by semantic name.
    order_names = {
        col
        for col, _ in (
            getattr(getattr(bound, "logical_query", None), "order_by", None) or []
        )
    }

    extra_names = (filter_names | order_names) - {
        getattr(obj, "name", "") for obj in (
            list(getattr(bound, "resolved_dimensions", [])) +
            list(getattr(bound, "resolved_measures", []))
        )
    }

    if extra_names:
        model_id = bound.model.id
        dim_result = await db.execute(
            select(DimensionORM.source_column_id)
            .where(
                DimensionORM.model_id == model_id,
                DimensionORM.name.in_(extra_names),
                DimensionORM.source_column_id.isnot(None),
            )
        )
        column_ids |= {row[0] for row in dim_result.all()}

        meas_result = await db.execute(
            select(MeasureORM.source_column_id)
            .where(
                MeasureORM.model_id == model_id,
                MeasureORM.name.in_(extra_names),
                MeasureORM.source_column_id.isnot(None),
            )
        )
        column_ids |= {row[0] for row in meas_result.all()}

    if not column_ids:
        return set()

    result = await db.execute(
        select(ModelTable.source_id)
        .join(ModelColumn, ModelColumn.model_table_id == ModelTable.id)
        .where(ModelColumn.id.in_(column_ids))
        .distinct()
    )
    return {row[0] for row in result.all() if row[0] is not None}


async def _resolve_query_source(
    model_id: str,
    db: AsyncSession,
    *,
    bound: BoundQuery | None = None,
    touched_source_ids: set | None = None,
) -> DataSource:
    """Return the single ``DataSource`` a query will execute against.

    One of ``bound`` or ``touched_source_ids`` must be supplied. If both
    are provided, ``touched_source_ids`` wins (used by paths that know
    the source directly, e.g. ``/discover/members``).

    Policy (unchanged from Phase D / CR-004):
        - 0 sources touched and the model has exactly one configured
          DataSource → that source is returned.
        - 0 sources touched and the model has zero or > 1 configured
          DataSource → HTTP 400 ``NO_SOURCE_RESOLVABLE`` or
          ``CROSS_SOURCE_UNSUPPORTED`` respectively.
        - 1 source touched → returned directly.
        - > 1 source touched → HTTP 400 ``CROSS_SOURCE_UNSUPPORTED``.

    CR-002 Finding 7: the trace builder used to do its own ``.limit(1)``
    lookup against ``DataSource``, which could report a different source
    than the one the executor actually ran against. Both the executor
    and the trace builder now go through this helper so the two views
    are guaranteed consistent.
    """
    from sqlalchemy import select

    if touched_source_ids is None and bound is not None:
        touched_source_ids = await _collect_touched_source_ids(bound, db)

    if touched_source_ids and len(touched_source_ids) > 1:
        touched = await db.execute(
            select(DataSource).where(DataSource.id.in_(touched_source_ids))
        )
        slugs = sorted(s.display_name or str(s.id) for s in touched.scalars().all())
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "CROSS_SOURCE_UNSUPPORTED",
                "detail": (
                    "Cross-source queries are not supported. Detected sources: "
                    f"{slugs}. Each query must be fully contained in one source."
                ),
                "sources": slugs,
            },
        )

    if touched_source_ids and len(touched_source_ids) == 1:
        (chosen_id,) = tuple(touched_source_ids)
        source = await db.get(DataSource, chosen_id)
        if source is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error_code": "NO_SOURCE_RESOLVABLE",
                    "detail": f"Touched DataSource {chosen_id} not found.",
                },
            )
        return source

    # No bound context yielded any touched sources — pick the first
    # configured source (ordered by creation time).  This is safe for
    # queries that reference no tables/columns (e.g. SELECT 1, SELECT
    # CURRENT_TIMESTAMP) and for passthrough queries where the binder
    # couldn't resolve measures to specific source columns.
    source_rows = await db.execute(
        select(DataSource)
        .where(DataSource.model_id == model_id)
        .order_by(DataSource.created_at)
    )
    sources = source_rows.scalars().all()
    if len(sources) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "NO_SOURCE_RESOLVABLE",
                "detail": f"No data source configured for model {model_id}.",
            },
        )
    return sources[0]


async def execute_routed_query(
    bound,
    decision: RouteDecision,
    db: AsyncSession,
) -> tuple[list[dict], int, list[str], DataSource | DataTarget]:
    """Execute the rewritten query against the right connection.

    For ``source`` routes, resolves the model's DataSource. For
    ``aggregate`` and ``pocket`` routes, loads the definition's
    DataTarget so the query runs where the materialised table lives
    (Bug-368).

    Returns ``(rows, bytes_processed, columns, execution_endpoint)``
    where ``execution_endpoint`` is either a DataSource or DataTarget —
    the caller passes it to ``_build_trace``.
    """
    if decision.route_type == "aggregate" and decision.aggregate_id:
        from shared.db.models import AggregateDefinition
        agg = await db.get(AggregateDefinition, decision.aggregate_id)
        if agg is None:
            raise ValueError(
                f"AggregateDefinition {decision.aggregate_id} not found — "
                "cannot execute aggregate-routed query"
            )
        target = await db.get(DataTarget, agg.target_id)
        if target is None:
            raise ValueError(
                f"DataTarget {agg.target_id} not found for aggregate "
                f"{decision.aggregate_id}"
            )
        # Bug-5325 fail-closed: reject an aggregate target whose connection
        # belongs to a different project than the model owning the query.
        conn = await resolve_endpoint_connection(
            db, target, expected_project_id=bound.model.project_id
        )
        rows, bytes_processed, columns = await execute_on_connection(
            decision.rewritten_query, conn, db
        )
        return rows, bytes_processed, columns, target

    if decision.route_type == "pocket" and decision.pocket_id:
        from shared.db.models import PocketDefinition
        pocket = await db.get(PocketDefinition, decision.pocket_id)
        if pocket is None:
            raise ValueError(
                f"PocketDefinition {decision.pocket_id} not found — "
                "cannot execute pocket-routed query"
            )
        target = await db.get(DataTarget, pocket.target_id)
        if target is None:
            raise ValueError(
                f"DataTarget {pocket.target_id} not found for pocket "
                f"{decision.pocket_id}"
            )
        # Bug-5325 fail-closed: reject a pocket target whose connection belongs
        # to a different project than the model owning the query.
        conn = await resolve_endpoint_connection(
            db, target, expected_project_id=bound.model.project_id
        )
        rows, bytes_processed, columns = await execute_on_connection(
            decision.rewritten_query, conn, db
        )
        return rows, bytes_processed, columns, target

    source = await _resolve_query_source(bound.model.id, db, bound=bound)
    # Bug-5325 fail-closed: reject a source whose connection belongs to a
    # different project than the model owning the query, before executing
    # any gateway SQL against another project's source database.
    conn = await resolve_endpoint_connection(
        db, source, expected_project_id=bound.model.project_id
    )
    rows, bytes_processed, columns = await execute_on_connection(
        decision.rewritten_query, conn, db
    )
    return rows, bytes_processed, columns, source
