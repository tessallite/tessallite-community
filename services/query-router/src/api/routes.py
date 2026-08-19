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
import re
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.middleware import (
    CurrentUser,
    enforce_model_scope,
    require_capability,
    require_capability_or_service_scope,
    require_service_scope_or_tenant_admin,
    require_tenant_admin,
)
from shared.auth.service_principal import SCOPE_CACHE_EVICT, SCOPE_POCKET_REFRESH
from shared.auth.project_access import load_authorized_model
from shared.connection_scope import (
    CrossProjectConnectionError,
    resolve_endpoint_connection,
)
from shared.db.models import (
    AggregateColumn,
    AggregateDefinition,
    AggregateRefreshPolicy,
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
    PocketDefinition,
    PocketRefreshPolicy,
    QueryLog,
    UserDefinedAttribute,
    data_tag_columns,
)
from shared.db.session import get_tenant_db
from shared.query_log_client_kinds import RequestClientKindLiteral
from shared.semantic.field_compatibility import (
    HIDDEN_FIELD_UNAVAILABLE,
    PERSONA_FIELD_UNAVAILABLE,
    FieldAccessPolicy,
    evaluate_field_compatibility,
)
from shared.semantic.kpi_expression import (
    _collect_references as _kpi_collect_references,
    parse_kpi_expression,
)
from src.api._sql_disclosure import row_security_misconfigured_detail
from src.api._simulate import (
    persona_current_user_for_principal,
    resolve_principal,
    simulate_headers_present,
)
from src.execution.dispatcher import execute_on_connection
from src.ir.logical_query import (
    BoundQuery,
    CrossModelNotResolvedError,
    DeployedSnapshotUnavailableError,
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
from src.params.named_list_resolver import load_named_lists, expand_named_lists
from src.params.resolver import ParameterError, apply_parameters, placeholder_spans
from src.parsing.dax_normalizer import parse_dax_to_ir
from src.parsing.sql_parser import GroupByError, SyntaxErrorInSQL, parse_sql_to_ir
from src.rewrite.query_rewriter import (
    dialect_to_connector,
    invalidate_join_graph_cache,
    resolve_target_dialect_for_bound,
    rewrite_for_source,
)
from src.routing.aggregate_matcher import record_aggregate_hit
from src.routing.aggregate_generation_guard import (
    assert_aggregate_route_admissible,
    read_aggregate_generation,
)
from src.routing.aggregate_generation_guard import (
    assert_generation_unchanged as assert_aggregate_generation_unchanged,
)
from src.routing.artifact_generation_guard import ArtifactGenerationChangedError
from src.routing.pocket_generation_guard import (
    assert_generation_unchanged,
    assert_pocket_route_admissible,
    read_pocket_generation,
)
from src.routing.named_query_resolver import (
    NamedQueryError,
    NamedQueryUnknownReference,
    NamedQueryUnsupportedShape,
    NamedQueryWrongType,
    load_named_queries,
    named_query_reference_name,
    projection_security_proof_holds,
    sql_references_named_query_position,
)
# NQ-2/Bug-9161: the population contract and the star expansion live in
# dependency-light shared modules (Bug-9174/NQ2R1-F7) so the serve path never
# imports the heavyweight build-side refresh module.
from shared.named_query.population_contract import (
    NQ_CANONICAL_DIALECT,
    NQ_CANONICAL_FORCE_ROUTE,
    NQ_CANONICAL_INCLUDE_HIDDEN,
    NQ_CANONICAL_PROTOCOL,
    named_query_population_fingerprint,
    named_query_population_manifest_matches,
)
from shared.named_query.star_expansion import (
    expand_named_query_star_definition,
    is_expandable_star_definition,
)
from src.routing.router import (
    _inject_security_where,
    _without_security_owners,
    route_query,
)
from src.security import CompiledPredicate, Principal, RowSecurityCompileError, compile_row_security, has_active_rules
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
from shared.staleness_gate import artifact_overdue, resolve_overdue_grace_seconds

logger = logging.getLogger(__name__)
_query_audit_logger = logging.getLogger("tessallite.query_audit")

router = APIRouter(tags=["query"])

_cache = ResultCache(
    ttl_seconds=_get_settings().QUERY_CACHE_TTL_SECONDS,
    max_entries=_get_settings().QUERY_CACHE_MAX_ENTRIES,
)
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

# Bug-7331: strong-reference set for background tasks spawned from the
# request path (same pattern as shared/webhooks/dispatcher.py). Without
# this the event loop may garbage-collect the Task mid-flight.
_background_tasks: set[asyncio.Task] = set()


def _spawn_background(coro) -> asyncio.Task:
    """Fire-and-forget with a strong reference so the GC can't reap it."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


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


def _preexec_error_type(exc: HTTPException) -> str:
    """Classify a pre-execution HTTPException for the QueryLog error_type.

    Bug-7674: parse / bind / persona-gate / route-stage failures raise
    HTTPException before a BoundQuery exists, so ``_log_query_failure`` (which
    keys much of its metadata off ``bound``) was never called for them. This
    maps the raised status + typed detail to a stable ``error_type`` label so
    the log viewer's status=error filter, failure-spike alerting, and CSV
    exports see the same failure classes the execution boundary already
    records. The values mirror the Fable F-030-02 recommendation
    (parse_error / binding_error / not_deployed / persona_denied /
    routing_rejected), plus ``snapshot_unavailable`` for the typed 503.

    Bug-8520: 503 had no mapping, so every blocked-deployment failure fell
    through to the catch-all ``routing_rejected`` and was indistinguishable in
    QueryLog from a genuine "no route for this query" rejection — defeating the
    whole point of the typed 503. In this service 503 is raised ONLY for
    ``DeployedSnapshotUnavailableError`` (bind stage and rewrite stage, on every
    serving surface), so the code maps unambiguously to one condition:
    a deployment needs repair, not a user query needs fixing.
    """
    detail = exc.detail
    error_type = None
    if isinstance(detail, dict):
        error_type = detail.get("error_type")
    if error_type:
        return str(error_type)[:64]
    code = exc.status_code
    if code == status.HTTP_400_BAD_REQUEST:
        return "parse_error"
    if code == status.HTTP_409_CONFLICT:
        return "not_deployed"
    if code == status.HTTP_503_SERVICE_UNAVAILABLE:
        return "snapshot_unavailable"
    if code == status.HTTP_403_FORBIDDEN:
        return "persona_denied"
    if code == status.HTTP_422_UNPROCESSABLE_CONTENT:
        return "binding_error"
    if code == status.HTTP_500_INTERNAL_SERVER_ERROR:
        return "parse_error"
    return "routing_rejected"


async def _log_preexec_failure(
    db: AsyncSession,
    user_identity: str,
    tenant_id: str,
    raw_query: str,
    protocol: str,
    exc: HTTPException,
    start_ms: float,
    persona_id: uuid.UUID | None = None,
    client_kind: str | None = None,
) -> None:
    """Persist a QueryLog error row for a pre-execution failure (Bug-7674).

    Best-effort and never raises: a failure to record the observability row must
    not convert a clean 4xx into a 500. Writes with ``bound=None`` (no model
    resolved yet), the raw request query, and a classified ``error_type``. Uses
    ``log_query_failure`` directly (it already tolerates ``bound_query=None``,
    defaulting protocol/raw_query/route_type) so the miss-log is untouched — a
    pre-execution failure is not a route miss. Emits the same query-audit
    warning line and failure-spike check the execution boundary emits.
    """
    error_type = _preexec_error_type(exc)
    detail = exc.detail
    if isinstance(detail, dict):
        error_detail = str(detail.get("message") or detail)
    else:
        error_detail = str(detail)
    elapsed_ms = int((time.monotonic() - start_ms) * 1000)
    _query_audit_logger.warning(
        "query_failed",
        extra={
            "user_email": user_identity,
            "tenant_id": tenant_id,
            "model_id": "",
            "model_name": "",
            "route_type": "",
            "duration_ms": elapsed_ms,
            "row_count": 0,
            "error_type": error_type,
            "error_detail": error_detail[:500],
            "query_fingerprint": "",
            "protocol": protocol or "",
        },
    )
    try:
        await log_query_failure(
            db=db,
            bound_query=None,
            decision=None,
            execution_ms=elapsed_ms,
            user_identity=user_identity,
            persona_id=persona_id,
            client_kind=client_kind,
            error_type=error_type,
            error_detail=error_detail,
            raw_query_override=raw_query,
            protocol_override=protocol,
        )
    except Exception:
        logger.warning("Failed to persist pre-execution query failure log", exc_info=True)
    try:
        await _check_failure_spike(db, tenant_id, project_id=None)
    except Exception:
        logger.warning("Pre-execution failure-spike check failed", exc_info=True)


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
    # Bug-7331: the dispatch fans out to N notification routes, each of which
    # may block for up to 30 s (SMTP) or 10 s (Slack). Running inline on the
    # query-failure response path held the BI client's error response hostage
    # for the entire fan-out duration. Move dispatch to a background task with
    # its own DB session so the error response returns immediately.
    _spawn_background(
        _dispatch_spike_alerts(tenant_id, count, project_scopes)
    )


async def _dispatch_spike_alerts(
    tenant_id: str,
    count: int,
    project_scopes: list[tuple],
) -> None:
    """Bug-7331: background dispatch of query-failure-spike alerts.

    Opens its own DB session so the request-scoped session is not held open
    for the duration of the fan-out. Failures are logged but never raised.
    """
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
        async for db in get_tenant_db(tenant_id):
            for scoped_project_id, scope_html, slack_scope_text in dispatch_scopes:
                await dispatch_alert(
                    db,
                    event_type="query_failure_spike",
                    project_id=scoped_project_id,
                    # F-022-03: each scope (tenant-wide vs per-project) is a
                    # distinct incident; the dedup window still limits repeats.
                    incident_key=f"query_failure_spike:{scoped_project_id}",
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
    # Bug-5889: this used to be an unconstrained `str`. `_parse()` below
    # keys strict JDBC syntax/GROUP BY enforcement off `protocol == "jdbc"`
    # (see `parsing/sql_parser.py`), so any caller-supplied value other than
    # the three recognised ones (including a stray case variant or typo)
    # silently fell through the parser's lax non-JDBC path -- a malformed or
    # under-specified aggregate query that should fail loud instead got
    # parsed into a semantic shape. Constraining the field to the closed set
    # rejects unrecognised values at the HTTP boundary (422) before parsing,
    # without touching the guarded parser itself.
    protocol: Literal["jdbc", "dax", "mcp"] = "jdbc"
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
    # authorization or routing decisions. Bug-6430: "drill" tags the internal
    # drill-through REST executor so its traffic is attributable in QueryLog
    # /metrics rather than blending into BI JDBC traffic (protocol stays
    # "jdbc" so the generated GROUP BY SQL keeps strict-parser treatment).
    # Bug-8070: the accepted set is derived from the single canonical domain
    # (shared/query_log_client_kinds.py) rather than restated here — restating it
    # is what let the KPI bridge ship with no origin and made "headless"/"agent"/
    # "mcp" writable long before the log filter accepted them (Bug-7451).
    client_kind: Optional[RequestClientKindLiteral] = None
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
    # Bug-8285: member-caption projection signal. The XMLA gateway sets this to
    # the list of dimension names whose members should carry a friendly CAPTION
    # (the dimension's declared display column) alongside their key. For each
    # named dimension that resolves and declares a display column, the execute
    # handler augments the bound query to project ``<display_col> AS
    # "<dim>__caption"`` as a resolved companion column. The gateway's Execute
    # axis builder (mdx_execute._normalize_member_captions) reads that companion
    # column to render UName=key, Caption=display. Producer (gateway
    # router_client.execute_query) and consumer (this handler) MUST use the same
    # field name. Empty/None means the legacy behaviour (caption == key).
    caption_dimensions: Optional[list[str]] = None
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


class ResultFreshness(BaseModel):
    """Freshness of the rows actually served by an execute response."""

    last_refreshed_at: Optional[datetime] = None
    is_live: bool
    is_stale: bool


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
    # Bug-7998 / F-027-02 [CRITICAL]: explicit completeness for callers that
    # supply a ``row_limit`` (the MCP server passes ``TESSALLITE_ROW_LIMIT``).
    # When a row_limit is applied we fetch cap+1 and trim, so ``truncated``
    # tells the caller that MORE rows matched than were returned — a capped
    # extract must never be presented as the whole result. ``row_limit`` is
    # the effective cap (None when the caller applied no row_limit).
    # Backwards-compatible defaults keep JDBC/XMLA/DAX consumers unaffected.
    truncated: bool = False
    row_limit: Optional[int] = None
    # Agent Phase B2 (F3): the rewritten physical SQL the executor ran,
    # surfaced on every response so the conversational agent can persist
    # `agent_turns.routed_sql` and the trace drawer can render the
    # physical query without a second /explain call. Backwards-compatible
    # default keeps existing JDBC/XMLA consumers unaffected.
    # ``routed_sql_redacted`` / ``reason_redacted`` were removed 2026-08-11 with
    # the embed withhold that was their only writer (decision option C). A
    # "was this withheld" flag that can only ever report ``false`` is false
    # assurance, so it goes with the control rather than surviving it.
    routed_sql: Optional[str] = None
    # Bug-8449 / Bug-8427: the active row-security rule ids the router applied to
    # THIS execution (empty when none fired). The same datum ``ExplainResponse``
    # has always carried, published on the execute path too because an executing
    # consumer needs it more than an explaining one: without it, a result set
    # emptied by a row-security predicate is indistinguishable from a genuinely
    # empty one, and a caller that renders the scalar (the KPI builder) shows a
    # governance denial as "No Data". Carries the ``__deny_all__`` sentinel when
    # the F-007-01 fail-closed coverage gate denied every row, so a consumer can
    # branch structurally instead of regex-matching the prose ``reason``.
    # Rule IDS ONLY — never predicate SQL or rule names, so the field discloses
    # that a policy applied, never what it filters on.
    security_rules_applied: list[str] = []
    # Bug-8365 / Bug-8103: result-level freshness, derived from the artifact
    # that THIS route actually served. Source/raw routes are live. Accelerated
    # routes carry the persisted materialisation timestamp and current stale /
    # overdue verdict. None means the artifact metadata could not be proven;
    # consumers must not invent an "as of" time in that case.
    freshness: Optional[ResultFreshness] = None
    trace: PipelineTrace = PipelineTrace()
    field_compatibility: Optional[FieldCompatibilityFeedback] = None


# Sentinel rule id emitted by ``predicate_compiler._deny_all_predicate`` when a
# model is role-governed but the principal matched no rule. Re-exported here as
# the execute-path contract constant so consumers do not hardcode the string.
DENY_ALL_RULE_ID = "__deny_all__"


def _security_rule_ids(decision: RouteDecision) -> list[str]:
    """Rule ids applied by *decision*, in the shape both API responses publish.

    Single helper so the execute and explain paths cannot drift: a consumer that
    branches on ``__deny_all__`` from one endpoint must see the identical value
    from the other.

    Reads the attribute defensively, matching the precedent already set by
    ``logging.query_logger`` — several internal callers build a duck-typed
    decision, and a DIAGNOSTIC field must never be able to break the execution
    path it merely describes.
    """
    return [
        str(r.get("rule_id"))
        for r in (getattr(decision, "security_rules_applied", None) or [])
        if isinstance(r, dict) and r.get("rule_id")
    ]


async def _result_freshness(
    decision: RouteDecision,
    db: AsyncSession,
    *,
    model_id: Any,
    materialization_timestamp: datetime | None = None,
) -> ResultFreshness | None:
    """Return freshness for the exact route/artifact that served the result.

    The artifact lookup is constrained by both artifact id and model id. This
    prevents a signed-in caller (or a malformed internal decision) from using a
    cross-project artifact id as a metadata oracle. A lookup failure is
    diagnostic-only and fails closed to ``None`` rather than breaking an
    otherwise successful data query or fabricating freshness. Cache-hit callers
    pass the timestamp of the materialization that produced the cached rows;
    current artifact status and policy are then applied to that original point
    in time instead of relabelling old rows with a newer refresh timestamp.

    The artifact read runs inside a SAVEPOINT (the same best-effort pattern the
    Bug-5346 / Bug-6986 blocks in this module use). Swallowing the exception in
    Python is NOT sufficient to fail closed: a DB-level fault aborts the whole
    transaction, and on the RESULT-CACHE HIT path this SELECT is the request's
    FIRST statement -- the route stage is skipped -- so the abort would then
    take down ``record_query_cache_hit`` -> ``log_query`` (an unguarded
    ``add``/``flush``/``commit``) and turn a query whose rows were already in
    cache into a 500. The SAVEPOINT keeps the failure contained to this
    diagnostic read, which is what "fails closed to ``None``" has to mean.
    """
    route_type = str(getattr(decision, "route_type", "") or "").lower()
    if route_type in {"source", "raw"}:
        return ResultFreshness(is_live=True, is_stale=False)
    if route_type not in {"aggregate", "pocket"}:
        return None

    try:
        artifact_id = uuid.UUID(
            str(
                decision.aggregate_id
                if route_type == "aggregate"
                else decision.pocket_id
            )
        )
        scoped_model_id = uuid.UUID(str(model_id))

        if route_type == "aggregate":
            async with db.begin_nested():
                result = await db.execute(
                    select(
                        AggregateDefinition.last_refreshed_at,
                        AggregateDefinition.status,
                        AggregateDefinition.is_stale,
                        AggregateRefreshPolicy.cron_expression,
                        AggregateRefreshPolicy.is_enabled,
                    )
                    .outerjoin(
                        AggregateRefreshPolicy,
                        AggregateRefreshPolicy.aggregate_definition_id
                        == AggregateDefinition.id,
                    )
                    .where(
                        AggregateDefinition.id == artifact_id,
                        AggregateDefinition.model_id == scoped_model_id,
                    )
                )
            row = result.one_or_none()
            if row is None:
                return None
            served_timestamp = (
                materialization_timestamp
                if materialization_timestamp is not None
                else row.last_refreshed_at
            )
            if served_timestamp is None:
                return None
            grace = resolve_overdue_grace_seconds("aggregate")
            cron = row.cron_expression if row.is_enabled else None
            overdue = bool(
                grace is not None
                and artifact_overdue(
                    cron,
                    served_timestamp,
                    datetime.now(timezone.utc),
                    grace,
                )
            )
            return ResultFreshness(
                last_refreshed_at=served_timestamp,
                is_live=False,
                is_stale=bool(
                    row.is_stale or row.status != "active" or overdue
                ),
            )

        async with db.begin_nested():
            result = await db.execute(
                select(
                    PocketDefinition.last_refresh_at,
                    PocketDefinition.status,
                    PocketRefreshPolicy.cron_expression,
                    PocketRefreshPolicy.is_enabled,
                )
                .outerjoin(
                    PocketRefreshPolicy,
                    PocketRefreshPolicy.pocket_definition_id
                    == PocketDefinition.id,
                )
                .where(
                    PocketDefinition.id == artifact_id,
                    PocketDefinition.model_id == scoped_model_id,
                )
            )
        row = result.one_or_none()
        if row is None:
            return None
        served_timestamp = (
            materialization_timestamp
            if materialization_timestamp is not None
            else row.last_refresh_at
        )
        if served_timestamp is None:
            return None
        grace = resolve_overdue_grace_seconds("pocket")
        cron = row.cron_expression if row.is_enabled else None
        overdue = bool(
            grace is not None
            and artifact_overdue(
                cron,
                served_timestamp,
                datetime.now(timezone.utc),
                grace,
            )
        )
        return ResultFreshness(
            last_refreshed_at=served_timestamp,
            is_live=False,
            is_stale=bool(row.status != "fresh" or overdue),
        )
    except Exception as exc:
        logger.warning(
            "Could not prove result freshness for route=%s artifact=%s model=%s: %s",
            route_type,
            getattr(decision, "aggregate_id", None)
            or getattr(decision, "pocket_id", None),
            model_id,
            exc,
        )
        return None


async def _cached_artifact_still_servable(
    cached: ExecuteResponse,
    db: AsyncSession,
    *,
    model_id: Any,
    route_type: str,
) -> bool:
    """Bug-8581: is the aggregate/pocket this cached response names still there?

    False when the artifact row is GONE (deleted) or is in a state the matcher
    would refuse (aggregate not ``active``, pocket not ``fresh``/``stale`` —
    i.e. retired). A cached response naming such an artifact must not be
    replayed: its ``routed_sql`` may reference a physical table that has been
    dropped, and its route metadata tells the operator an artifact is serving
    when it is not.

    Scoped by artifact id AND model id, matching ``_result_freshness``, so a
    cross-project artifact id cannot be used as an existence oracle.

    Runs inside a SAVEPOINT for the same reason ``_result_freshness`` does: on
    the cache-hit path this is the request's FIRST statement, so a DB-level
    fault would abort the whole transaction and turn a query whose rows were
    already cached into a 500. On an unprovable lookup we FAIL OPEN (serve the
    cached entry) rather than turning a transient database blip into a cache
    stampede against the source — the artifact gates on the re-routed path would
    catch a genuinely dead artifact on the next miss anyway.
    """
    artifact_ref = (
        cached.aggregate_id if route_type == "aggregate" else cached.pocket_id
    )
    if artifact_ref is None:
        return True
    try:
        artifact_id = uuid.UUID(str(artifact_ref))
        scoped_model_id = uuid.UUID(str(model_id))
        if route_type == "aggregate":
            async with db.begin_nested():
                result = await db.execute(
                    select(
                        AggregateDefinition.status,
                        AggregateDefinition.is_stale,
                        AggregateDefinition.invalid_reason,
                    ).where(
                        AggregateDefinition.id == artifact_id,
                        AggregateDefinition.model_id == scoped_model_id,
                    )
                )
            row = result.one_or_none()
            if row is None:
                return False
            if str(getattr(row, "status", "")) != "active":
                return False
            if bool(getattr(row, "is_stale", False)):
                return False
            invalid = getattr(row, "invalid_reason", None)
            if invalid is not None and str(invalid).strip():
                return False
            return True
        async with db.begin_nested():
            result = await db.execute(
                select(PocketDefinition.status, PocketDefinition.retired_at).where(
                    PocketDefinition.id == artifact_id,
                    PocketDefinition.model_id == scoped_model_id,
                )
            )
        row = result.one_or_none()
        if row is None:
            return False
        return (
            getattr(row, "retired_at", None) is None
            and str(getattr(row, "status", "")) in {"fresh", "stale"}
        )
    except Exception as exc:
        logger.warning(
            "Bug-8581: could not prove the cached %s artifact %s is still "
            "servable for model %s (%s) — serving the cached result; the next "
            "cache miss re-routes through the matcher gates",
            route_type, artifact_ref, model_id, exc,
        )
        return True


async def _cached_response_with_current_freshness(
    cached: ExecuteResponse,
    db: AsyncSession,
    *,
    model_id: Any,
) -> ExecuteResponse | None:
    """Return a detached cache-hit response with a current stale verdict, or
    ``None`` when the artifact the cached response names is no longer servable
    (Bug-8581) and the caller must treat the entry as a cache MISS.

    Cached rows retain the materialization timestamp captured when they were
    produced. Aggregate/pocket status, policy and overdue state are read again
    at serve time. The shared cached object is never mutated, and an unprovable
    artifact lookup removes diagnostic freshness from only this response.
    """
    route_type = str(cached.route_type or "").lower()
    if route_type not in {"aggregate", "pocket"}:
        return cached.model_copy(deep=True)

    # Bug-8581: before anything else, prove the artifact this cached response
    # NAMES is still servable. Deleting or retiring a pocket (or an aggregate)
    # changes nothing in the cache key — an artifact is not part of the model
    # snapshot, so neither ``deployed_version_id`` nor ``deploy_epoch`` moves —
    # and this fast path returns BEFORE ``route_query``, so the matcher's
    # built_for gate never runs on a hit. Observed live (LIVE-POCKET-RLS-001,
    # 2026-08-04): a pocket was DELETEd, its physical table verified gone from
    # the target, and for 15+ seconds the same query kept returning
    # route_type=pocket with the deleted pocket's id and a routed SQL naming a
    # table that no longer existed. Two harms: route metadata and lineage
    # advertise an artifact that is gone, so an operator cannot tell whether the
    # delete took effect; and an operator who deleted the pocket BECAUSE it was
    # serving wrong numbers keeps being served them, with no signal.
    #
    # Returning None here makes the caller treat the entry as a MISS and re-route
    # for real. Checked at SERVE time on every replica, so it needs no eviction
    # message and no cross-replica coordination — the same reason the built_for
    # gate lives at serve time rather than at build time.
    if not await _cached_artifact_still_servable(
        cached, db, model_id=model_id, route_type=route_type
    ):
        return None

    original = cached.freshness
    if original is None or original.last_refreshed_at is None:
        return cached.model_copy(deep=True, update={"freshness": None})

    decision = RouteDecision(
        route_type=route_type,
        rewritten_query=cached.routed_sql or "",
        reason=cached.reason,
        aggregate_id=cached.aggregate_id,
        pocket_id=cached.pocket_id,
    )
    freshness = await _result_freshness(
        decision,
        db,
        model_id=model_id,
        materialization_timestamp=original.last_refreshed_at,
    )
    return cached.model_copy(deep=True, update={"freshness": freshness})


class ExplainResponse(BaseModel):
    route_type: str
    aggregate_id: Optional[str]
    pocket_id: Optional[str] = None
    reason: str
    # ``rewritten_query_redacted`` / ``reason_redacted`` removed 2026-08-11 —
    # see ExecuteResponse. Nothing redacts this surface any more.
    rewritten_query: Optional[str] = None
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
    # F-004-05 / F-004-06: skip-token honesty for explain consumers. JDBC NOTICE
    # for route_type is a CP-02 consumer of these fields.
    aggregate_skipped_reasons: list[str] = []
    filter_columns_missing: list[str] = []


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
    # Bug-8453 / R4 finding 3: member discovery runs through route_query with
    # the caller's principal, so RLS applies. Without this field a deny-all
    # returns zero members and every member picker in the product (the
    # named-set builder, the Excel CUBEMEMBER wizard, the Report Builder)
    # renders "this dimension has no members" -- the same false statement
    # Bug-8453 removed from the /execute surfaces, on a route the original
    # enumeration never covered because both guards were route-shaped.
    # Rule IDS only, never predicate SQL.
    security_rules_applied: list[str] = []


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
    current_user: CurrentUser = Depends(
        require_capability_or_service_scope("query", SCOPE_POCKET_REFRESH)
    ),
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
    _simulated = simulate_headers_present(
        x_simulate_principal, x_simulate_roles, x_simulate_groups, x_simulate_claims,
    )
    async for db in get_tenant_db(current_user.tenant_id):
        await load_authorized_model(
            db, current_user, model_id=body.model_id, min_role="viewer",
            service_scope_verified=True,  # Bug-8613: scope verified by require_capability_or_service_scope
        )
        # Bug-8301: resolve the effective persona against the SIMULATED identity
        # (when simulate-as is active) so persona default-filters / persona-CLS
        # are faithfully simulated, matching the RLS principal above. Cannot
        # escalate — entitlement is gated on the simulated user's stated roles.
        effective_persona = await resolve_execution_persona(
            db,
            current_user=persona_current_user_for_principal(
                current_user, principal, simulated=_simulated,
            ),
            model_id=body.model_id,
            requested_persona_id=body.persona_id,
        )
        # Bug-7674: persist a QueryLog error row for pre-execution failures
        # (parse / bind / persona-gate / route-typed-rejection) that raise
        # before a BoundQuery exists. Execution-boundary failures self-mark
        # (already logged) and are skipped here to avoid a double row.
        _preexec_start = time.monotonic()
        try:
            # Every authenticated caller receives the same physical detail
            # (decision 2026-08-11, option C — see _sql_disclosure's module
            # docstring). The embed withhold that used to wrap this return
            # keyed off the token TYPE, not the caller's entitlement.
            return await _handle_execute(
                body,
                db,
                user_identity=principal.user_identity,
                principal=principal,
                persona_id=str(effective_persona.id) if effective_persona else None,
                persona=effective_persona,
                tenant_id=current_user.tenant_id,
            )
        except HTTPException as _exc:
            if not getattr(_exc, "_tessallite_failure_logged", False):
                await _log_preexec_failure(
                    db,
                    principal.user_identity,
                    current_user.tenant_id,
                    body.raw_query,
                    body.protocol,
                    _exc,
                    _preexec_start,
                    persona_id=effective_persona.id if effective_persona else None,
                    client_kind=body.client_kind,
                )
            raise


@router.post("/explain", response_model=ExplainResponse)
async def explain_query(
    body: ExecuteRequest,
    current_user: CurrentUser = Depends(
        require_capability_or_service_scope("query", SCOPE_POCKET_REFRESH)
    ),
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
    _simulated = simulate_headers_present(
        x_simulate_principal, x_simulate_roles, x_simulate_groups, x_simulate_claims,
    )
    async for db in get_tenant_db(current_user.tenant_id):
        await load_authorized_model(
            db, current_user, model_id=body.model_id, min_role="viewer",
            service_scope_verified=True,  # Bug-8613: scope verified by require_capability_or_service_scope
        )
        # Bug-8301: persona resolution runs against the SIMULATED identity so
        # /explain reflects the same persona surface /execute would serve for
        # that user (default-filters / persona-CLS), matching the RLS principal.
        effective_persona = await resolve_execution_persona(
            db,
            current_user=persona_current_user_for_principal(
                current_user, principal, simulated=_simulated,
            ),
            model_id=body.model_id,
            requested_persona_id=body.persona_id,
        )
        # Same as /execute: no token-type withhold. /explain exists to show what
        # happened, and it shows it identically to every authenticated caller.
        return await _handle_explain(
            body, db, principal=principal,
            persona_id=str(effective_persona.id) if effective_persona else None,
            persona=effective_persona,
        )


@router.post("/validate", response_model=ValidateResponse)
async def validate_query(
    body: ExecuteRequest,
    current_user: CurrentUser = Depends(
        require_capability_or_service_scope("query", SCOPE_POCKET_REFRESH)
    ),
) -> ValidateResponse:
    """Parse + bind without routing or executing.

    Always returns 200; failures live inside the response body so the UI can
    render a friendly error message instead of toasting an HTTP error.
    """
    enforce_model_scope(current_user, body.model_id)
    async for db in get_tenant_db(current_user.tenant_id):
        await load_authorized_model(
            db, current_user, model_id=body.model_id, min_role="viewer",
            service_scope_verified=True,  # Bug-8613: scope verified by require_capability_or_service_scope
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
    dependencies=[Depends(require_service_scope_or_tenant_admin(SCOPE_CACHE_EVICT))],
)
async def evict_model_cache(model_id: str) -> None:
    """Evict all cached state for a model. Called by model-service after deploy.

    F-006-05: in addition to the result cache, evict the in-process join-graph
    cache (tables/joins/columns, 60s TTL). A deploy can change the model's
    tables, joins, or physical column names; without this, queries could be
    rewritten against the previous model graph for up to the TTL window after a
    deploy — contradicting the immutable-deploy contract and risking join
    errors or stale physical names. The cache is per-replica (an in-process
    dict), so this clears the calling replica immediately. Cross-replica
    correctness does NOT depend on this call or on the TTL (Bug-8273): the cache
    is keyed by (model_id, deployed epoch), so every other replica self-heals on
    its FIRST post-deploy query — inserting the new deployment's key evicts that
    replica's superseded entries. The 60s TTL is only a memory bound, not the
    correctness mechanism, as documented on ``join_graph_cache``.
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
    from src.routing.aggregate_population import (
        invalidate_aggregate_population_cache,
    )
    from src.routing.pocket_matcher import (
        invalidate_model_join_graph_cache,
        invalidate_model_table_cache,
    )
    _invalidate_snapshot_cache(model_id)
    _invalidate_live_metadata_cache(model_id)
    invalidate_canonical_dim_cache(model_id)
    # Bug-7000: evict the pocket model-table identifier cache so a
    # deploy that changes tables/aliases is reflected immediately.
    invalidate_model_table_cache(model_id)
    # Bug-8580: same for the pocket row-population join graph. A DEPLOYED
    # model's cache key carries the deploy pointer and self-invalidates, but an
    # UNDEPLOYED model keys on (id, "", 0) — without this, changing a join from
    # ``left`` to ``inner`` would keep the pocket route open on the previous,
    # now-wrong population proof for up to the cache TTL.
    invalidate_model_join_graph_cache(model_id)
    # Bug-8664: and the AGGREGATE half of the same proof. Its object index maps
    # each grain dimension / measure to its owning relation, and for an
    # UNDEPLOYED model it reads LIVE rows under the same (id, "", 0) key — so
    # re-binding a measure's source column to another relation would otherwise
    # keep an under-estimated plan bound, and therefore an unearned population
    # proof, cached for the full TTL. Same exposure, same eviction.
    invalidate_aggregate_population_cache(model_id)


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------

_ALLOWED_FORCE_ROUTES = frozenset({"source", "aggregate", "pocket", "raw"})


def _validate_force_route(value: Optional[str]) -> None:
    """Reject unsupported ``force_route`` values at the HTTP boundary.

    Accepted values: ``source``, ``aggregate``, ``pocket``, ``raw``.

    Semantics (F-004-08): ``force_route`` pins one SPECIFIC route.
    ``"aggregate"`` runs the aggregate path only (the pocket matcher is
    skipped, so a matching pocket never wins); ``"pocket"`` runs the pocket
    path only (the aggregate matcher is skipped); ``"source"`` bypasses both.
    ``"raw"`` builds JOINs without aggregation and returns flat rows from
    the source — used for ungrouped queries (Power BI Import mode).
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
    """Resolve and bind model parameters and named lists into
    ``body.raw_query`` in place.

    F-029-01: runs before parse, on the canonical SQL, so sqlglot
    transpilation handles the final dialect literal form. The ``@param``
    tokens sqlglot's parser would otherwise reject are replaced with
    type-safe typed literals here. Resolution order is persona default
    filter > JDBC session variable > model default value.

    Named list expansion runs after parameter substitution, on remaining
    ``@name`` placeholders that match deployed named lists with
    ``list_type == "sql_fixed"``. Members render as sqlglot typed literals
    inside the author's ``IN (@Name)`` clause.

    Only the SQL protocols (jdbc / mcp) carry ``@param`` tokens and
    ``SET app.*`` session variables; the DAX path is left untouched.
    Short-circuits inside ``apply_parameters`` when the model declares no
    parameters, so non-parameterised queries pay only one indexed lookup.
    """
    if body.protocol == "dax":
        return

    # No ``@`` token -> nothing to bind. Skip persona load and the
    # parameter probe entirely (matches apply_parameters' fast path).
    if "@" not in body.raw_query:
        return

    # Load named lists from the deployed snapshot (cached). Their names
    # are passed to apply_parameters as extra_declared_names so that
    # named list placeholders are not rejected as unknown parameters.
    named_lists: dict = await load_named_lists(body.model_id, db)

    named_list_declared: set[str] = set()
    for key, nlist in named_lists.items():
        named_list_declared.add(key)
        canon = f"@{nlist.name}" if not nlist.name.startswith("@") else nlist.name
        named_list_declared.add(canon)

    # Named Query references: an ``@name`` in FROM position is consumed by
    # the step-1.6 interceptor, NOT by parameter substitution or named list
    # expansion. Load the deployed definitions (cached, snapshot-backed) and
    # whitelist the referenced name so substitute_parameters does not reject
    # it as an unknown parameter, and the post-expansion leftover check does
    # not reject it before the interceptor can produce its specific error.
    _nq_from_ref = sql_references_named_query_position(body.raw_query)
    _nq_definitions: dict = {}
    _nq_declared: set[str] = set()
    if _nq_from_ref is not None:
        try:
            _nq_definitions = await load_named_queries(body.model_id, db)
        except NamedQueryError as _nq_err:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=_nq_err.message,
            )
        _nq_lower = _nq_from_ref.lower()
        # Whitelist the FROM-position name UNCONDITIONALLY: a model
        # parameter or named list never resolves in FROM position, and the
        # step-1.6 interceptor owns the specific error surface for every
        # outcome (served, unknown reference, wrong type, unsupported shape).
        _nq_declared = {_nq_lower, f"@{_nq_lower}"}

    # Law 6 (namespace collision): detect names that match BOTH a declared
    # parameter AND a named list, scoped to the placeholders actually present
    # in THIS query. A model-wide check would break all parameterized queries
    # when a single misconfigured list name collides with a parameter.
    if named_list_declared:
        from shared.db.models import ModelParameter as _MP
        _pr = await db.execute(
            select(_MP).where(_MP.model_id == body.model_id)
        )
        declared_param_names = {p.name for p in _pr.scalars().all()}
        param_lower = {n.lower() for n in declared_param_names}

        # Only check placeholders actually used in this query.
        query_spans = placeholder_spans(body.raw_query, "postgres")
        query_placeholder_lower = {name.lower() for _, _, name in query_spans}

        for lower_name in query_placeholder_lower:
            if lower_name in param_lower and lower_name in named_lists:
                # Look up the authored name for a clear error message.
                authored = named_lists[lower_name].name
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"'@{authored}' matches both a model parameter and a named list. "
                        f"Rename one to avoid ambiguity."
                    ),
                )
    else:
        declared_param_names = set()

    # Law 6 (namespace collision, named queries): a FROM-position @name that
    # matches BOTH a declared parameter and a deployed Named Query is
    # ambiguous — fail loud at query time (legacy cross-table data; create-
    # time checks prevent new collisions).
    if _nq_declared and declared_param_names:
        param_lower = {n.lower() for n in declared_param_names}
        if _nq_from_ref is not None and _nq_from_ref.lower() in param_lower:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"'@{_nq_from_ref}' matches both a model parameter and a "
                    f"Named Query. Rename one to avoid ambiguity."
                ),
            )

    # Persona default filters take top precedence. Load the persona once
    # here to read its ``default_filters`` (keyed by dimension name without
    # the ``@`` prefix). The resolver matches parameters by their declared
    # ``@name``, so we re-key the dict to add the ``@`` prefix before
    # passing it in (Bug-7660: without this, persona parameter overrides
    # never resolve because the bare-name key never matches the ``@name``
    # the resolver looks up). The later persona gate reuses the same
    # session-cached row, so this is not a redundant round trip.
    persona_filters: dict[str, Any] = {}
    if persona_id:
        persona = await load_persona(
            db, model_id=body.model_id, persona_id=persona_id
        )
        raw_filters = persona.default_filters or {}
        # Bug-7660: default_filters are keyed by dimension name (e.g.
        # "Region"); the resolver expects ``@``-prefixed parameter names
        # (e.g. "@Region"). Re-key so persona overrides actually match.
        # Keys that already carry ``@`` (future-proof) are left as-is.
        #
        # Only scalar/list values are valid as parameter overrides.
        # Operator-shaped dicts (e.g. {"gte": 100}) are dimension-level
        # WHERE filters handled by merge_default_filters, not parameter
        # values -- forwarding them would cause a type-coercion failure
        # in the resolver.  Date-range {from,to} dicts ARE valid
        # parameter overrides (Bug-6413), so we allow those through.
        persona_filters = {}
        for k, v in raw_filters.items():
            if isinstance(v, dict) and not ("from" in v and "to" in v):
                # Operator dict (e.g. {"gte": 100}) -- skip, not a param.
                continue
            key = k if k.startswith("@") else f"@{k}"
            persona_filters[key] = v

    # F-029-01 / Bug-6422: load parameter definitions from the deployed
    # snapshot when the model is deployed, so draft default changes cannot
    # alter production results before redeployment. Mirrors the fail-closed
    # discipline of load_named_lists: deployed -> snapshot, undeployed -> live.
    # F-029-01 / Bug-6422 (fail-closed): for a deployed model, load parameter
    # definitions from the deployed snapshot so draft default changes cannot
    # alter production results before redeployment. On transient DB failure,
    # fail closed (empty list = no deployed params) so queries with @param
    # tokens get a clear "unknown/unresolved" error rather than silently
    # resolving from live draft defaults (Fable FINDING-2).
    _deployed_params: list[dict[str, Any]] | None = None
    try:
        from shared.db.models import Model as _Model, ModelVersion as _MV
        _param_model = await db.get(_Model, body.model_id)
        if _param_model is not None:
            _dvid = getattr(_param_model, "deployed_version_id", None)
            if _dvid is not None:
                _deployed_params = []  # fail-closed default: no deployed params
                _version = await db.get(_MV, _dvid)
                if (
                    _version is not None
                    and isinstance(_version.snapshot_json, dict)
                ):
                    _snap_params = _version.snapshot_json.get("model_parameters")
                    if _snap_params is not None:
                        _deployed_params = _snap_params
    except Exception:
        # DB failure during deployed snapshot lookup — fail closed. Leave
        # _deployed_params as [] (set above when dvid is not None) so a query
        # with @param tokens fails with "unresolved parameter" rather than
        # silently using live draft defaults. If dvid was None (undeployed),
        # _deployed_params is None -> live ORM is the legitimate authority.
        pass

    try:
        _extra_declared: set[str] | None = None
        if named_list_declared or _nq_declared:
            _extra_declared = set(named_list_declared) | set(_nq_declared)
        body.raw_query = await apply_parameters(
            model_id=body.model_id,
            sql=body.raw_query,
            session_vars=body.session_vars,
            persona_filters=persona_filters,
            db=db,
            dialect="postgres",
            extra_declared_names=_extra_declared,
            deployed_params=_deployed_params,
        )
    except ParameterError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    # Named list expansion: remaining @name placeholders that match
    # deployed sql_fixed named lists are expanded to typed IN-list literals.
    # declared_param_names is already loaded above (Law 6 collision check).
    if "@" in body.raw_query and named_lists:
        try:
            body.raw_query, audit_entries = expand_named_lists(
                body.raw_query,
                named_lists,
                dialect="postgres",
                declared_param_names=declared_param_names,
            )
            if audit_entries:
                logger.info(
                    "Named list expansion: %s (model=%s)",
                    ", ".join(audit_entries),
                    body.model_id,
                )
        except ParameterError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
            )

    # Post-expansion leftover check: if @-placeholders remain after both
    # parameter substitution and named list expansion, raise a clear error
    # rather than letting the parser produce a cryptic syntax error.
    # This covers the case where the model has no declared parameters
    # (apply_parameters returns early) but the query has @-placeholders
    # that match neither parameters nor named lists.
    # A FROM-position @name is EXEMPT: it is a Named Query reference (exact
    # or decorated) and the step-1.6 interceptor owns its specific error.
    if "@" in body.raw_query:
        leftover = placeholder_spans(body.raw_query, "postgres")
        if leftover:
            _nq_exempt = (
                sql_references_named_query_position(body.raw_query)
            )
            names = sorted({
                name for _, _, name in leftover
                if not (
                    _nq_exempt
                    and name.lstrip("@").lower() == _nq_exempt.lower()
                )
            })
            if names:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Unknown placeholder(s) {names} in query. "
                        f"These do not match any declared model parameter or named list."
                    ),
            )


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


def _client_parse_error_detail(exc: BaseException) -> str | dict[str, str]:
    """F-003-15: typed 400 body for GROUP BY / syntax; generic otherwise."""
    if isinstance(exc, GroupByError):
        return {"message": str(exc), "error_type": "group_by_error"}
    if isinstance(exc, SyntaxErrorInSQL):
        return {"message": str(exc), "error_type": "syntax_error"}
    return "Parse failed"


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
    except GroupByError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_client_parse_error_detail(e),
        )
    except SyntaxErrorInSQL as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_client_parse_error_detail(e),
        )
    except ValueError as e:
        # Bug-6553: unexpected ValueError stays generic — no raw leak.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_client_parse_error_detail(e),
        )
    except Exception:
        # Bug-6553: non-parse exceptions are server faults — 500 without
        # exception detail to prevent information leakage.
        logger.exception("Unexpected error during query parse")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error during query parse",
        )
    if drill_join_path_ids:
        logical_query.drill_join_path_ids = [
            str(join_id) for join_id in drill_join_path_ids if join_id
        ]

    # 1.2 Row-limit clamp (F-027-02): the smaller of the query's own
    # LIMIT and the caller's row_limit wins. Applied before the cache
    # key is computed so limited and unlimited shapes never collide.
    #
    # Bug-7998 / F-027-02 [CRITICAL]: when the caller's ``row_limit`` is the
    # binding cap, fetch cap+1 rows so we can tell the caller (the MCP server
    # renders this) that MORE rows matched than were returned — a capped
    # extract must never be presented as complete. The probe row is trimmed
    # off the response below. ``_server_row_cap`` is the effective cap (None
    # when no row_limit applied); ``_probe_for_truncation`` marks that we
    # over-fetched by one. The query's OWN LIMIT is the caller's explicit
    # intent, not a server cap, so it does not trigger the truncation marker.
    _server_row_cap: Optional[int] = None
    _probe_for_truncation = False
    if body.row_limit is not None:
        _query_own_limit = logical_query.limit
        if _query_own_limit is None or body.row_limit <= _query_own_limit:
            # row_limit is the binding cap → over-fetch by one to detect
            # server truncation.
            _server_row_cap = body.row_limit
            _probe_for_truncation = True
            logical_query.limit = body.row_limit + 1
        else:
            # The query's own smaller LIMIT wins; no server truncation.
            logical_query.limit = _query_own_limit

    # 1.5 Intercept $KPIs virtual table queries
    kpi_table_hit = _detect_kpi_table(logical_query)
    if kpi_table_hit:
        # Bug-6610 (defence-in-depth): this intercept runs BEFORE the step-2.5
        # persona-load fallback, so a caller that supplies only ``persona_id``
        # (not a pre-loaded ``persona`` object) would otherwise reach the handler
        # with ``persona=None`` and the $KPIs CLS/allow-list gate inert. Resolve
        # the persona here exactly as step 2.5 does, so $KPIs enforcement never
        # depends on the caller's persona-passing convention.
        kpi_persona = persona
        if kpi_persona is None and persona_id:
            kpi_persona = await load_persona(
                db, model_id=body.model_id, persona_id=persona_id
            )
        return await _handle_kpi_table_query(
            db,
            body.model_id,
            logical_query,
            persona=kpi_persona,
            principal=principal,
            user_identity=user_identity,
            tenant_id=tenant_id,
            client_kind=body.client_kind,
            server_row_cap=_server_row_cap,
        )

    # 1.6 Intercept Named Query references (`SELECT * FROM @name`).
    # Recognition is a pre-parse STRUCTURAL check on the raw SQL (token-level,
    # mirroring the Named List lexer-span technique — not how sqlglot parses
    # ``@x`` in FROM position). The exact v1 shape is the whole-statement
    # reference; a decorated shape (projection subset, join, WHERE against it,
    # nested) dispatches here too so the resolver can raise the specific
    # NQ_UNSUPPORTED_SHAPE 400 instead of a generic parse/bind failure.
    if body.protocol != "dax" and "@" in body.raw_query:
        _nq_ref_name = named_query_reference_name(body.raw_query)
        if _nq_ref_name is None:
            _nq_ref_name = sql_references_named_query_position(body.raw_query)
        if _nq_ref_name is not None:
            # Bug-6610 parity: resolve the persona exactly as the $KPIs path
            # does, so the Named Query CLS/RLS gates are never inert for a
            # caller that supplied only ``persona_id``.
            nq_persona = persona
            if nq_persona is None and persona_id:
                nq_persona = await load_persona(
                    db, model_id=body.model_id, persona_id=persona_id
                )
            return await _handle_named_query_reference(
                db,
                body,
                logical_query,
                ref_name=_nq_ref_name,
                persona=nq_persona,
                principal=principal,
                user_identity=user_identity,
                tenant_id=tenant_id,
                client_kind=body.client_kind,
                server_row_cap=_server_row_cap,
                force_route=body.force_route,
                row_limit=body.row_limit,
            )

    # 2. Bind to semantic model
    try:
        bound = await bind_query_to_model(
            logical_query, db, include_hidden=body.include_hidden
        )
    except DeployedSnapshotUnavailableError as e:
        # Bug-7979 / F-013-05: deployed model with corrupt/missing/empty snapshot.
        # 503 tells the gateway/BI tool the deployment is temporarily unusable,
        # distinct from 409 (not deployed at all) and 422 (bad query).
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e),
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
    except HTTPException as _persona_exc:
        # F-008-22: a persona deny must not be replaced by a compatibility
        # 422 that confirms the denied object exists (existence/type oracle).
        if (
            _persona_exc.status_code == status.HTTP_403_FORBIDDEN
            and isinstance(_persona_exc.detail, dict)
            and _persona_exc.detail.get("error_code") in (
                "OBJECT_NOT_AVAILABLE",
                "PERSONA_COMPLEX_SQL_NOT_ALLOWED",
            )
        ):
            raise
        if persona is None and persona_id:
            persona = await load_persona(
                db, model_id=body.model_id, persona_id=persona_id
            )
        if body.force_route != "raw":
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

    # Bug-5877: must be pre-initialized — the gate below is skipped for
    # force_route="raw" (the JDBC gateway always sends it) but the response
    # still references the variable.
    field_compatibility = None
    if body.force_route != "raw":
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

    # 2.56 Bug-8285: member-caption projection. Runs AFTER the persona gate and
    # field-compatibility check (so only authorised axis dimensions are
    # captioned) and BEFORE the cache key + routing (so the companion column is
    # part of the cached shape and is selected by the rewriter). No-op unless the
    # gateway set ``caption_dimensions`` and a named dimension declares a display
    # column.
    if body.caption_dimensions:
        await _augment_execute_with_caption_columns(
            bound, db, body.caption_dimensions
        )

    # 2.55 Pre-compile row security BEFORE cache lookup (Bug-7038).
    # The RLS policy hash must be part of the cache key so that tightening a
    # rule (create/update/delete) invalidates stale cached rows even on
    # sibling replicas that never received the best-effort eviction request.
    # The compiled predicate is deterministic across replicas for the same
    # database state (same rules + principal attributes -> same SQL + IDs).
    # Compiling here also avoids double-compilation inside route_query.
    _compiled_rls: CompiledPredicate | None = None
    _rls_policy_hash = ""
    if principal is not None:
        _target_dialect_for_rls = await resolve_target_dialect_for_bound(db, bound)
        _connector_for_rls = dialect_to_connector(_target_dialect_for_rls)
        try:
            _compiled_rls = await compile_row_security(
                bound.model.id, principal, db, connector=_connector_for_rls,
            )
        except RowSecurityCompileError as e:
            # F-007-02 / Bug-9020: cache-key hoist moved compile out of
            # route_query without the typed handler. Map here so /execute
            # is 422 not 500.
            await _log_query_failure(
                db,
                user_identity,
                tenant_id,
                bound,
                None,
                time.monotonic(),
                "row_security_misconfigured",
                str(e),
                persona_id=persona.id if persona is not None else None,
                client_kind=body.client_kind,
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=row_security_misconfigured_detail(e, surface="/execute"),
            )
        if _compiled_rls is not None and has_active_rules(_compiled_rls):
            _rls_policy_hash = _compiled_rls.policy_hash
            # Bug-7039: validate that user_mapping rules reference mapping
            # tables on the same source as the fact query. A mapping table on
            # a different source produces a predicate subquery that references
            # a relation not visible on the fact query's execution connection,
            # causing an opaque "relation does not exist" error at the source
            # database. Fail with a clear 422 before execution.
            if _compiled_rls.mapping_source_ids:
                _fact_source_ids = await _collect_touched_source_ids(bound, db)
                # Bug-7039-F2: when _fact_source_ids is empty (SELECT *
                # shapes where no resolved dimension/measure carries a
                # source_column_id), resolve fact source(s) from the
                # model's fact ModelTable rows instead of silently skipping
                # validation. An empty set means the query will execute
                # against some source -- if we cannot determine which one,
                # the mapping subquery may reference a foreign table,
                # causing an opaque 502. Fail with the same 422.
                if not _fact_source_ids:
                    try:
                        _model_fact_sources = await db.execute(
                            select(ModelTable.source_id)
                            .where(
                                ModelTable.model_id == bound.model.id,
                                ModelTable.source_id.isnot(None),
                            )
                            .distinct()
                        )
                        _fact_source_ids = {
                            row[0] for row in _model_fact_sources.all()
                            if row[0] is not None
                        }
                    except Exception:
                        pass  # fall through -- empty set triggers 422 below
                if _fact_source_ids:
                    _bad = [
                        sid for sid in _compiled_rls.mapping_source_ids
                        if sid not in {str(s) for s in _fact_source_ids}
                    ]
                    if _bad:
                        raise HTTPException(
                            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                            detail={
                                "message": (
                                    "A row-level security rule on this model uses "
                                    "a user-mapping table that lives on a different "
                                    "source connection than the query's fact tables. "
                                    "The mapping subquery cannot execute on the fact "
                                    "connection. Move the mapping table to the same "
                                    "source, or reconfigure the rule."
                                ),
                                "error_type": "rls_cross_source_mapping",
                            },
                        )
                else:
                    # Cannot determine fact source(s) at all -- fail with
                    # the same 422 rather than proceeding to inject a
                    # mapping subquery against an indeterminate connection.
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail={
                            "message": (
                                "A row-level security rule on this model uses "
                                "a user-mapping table, but the fact source "
                                "connection could not be determined for this "
                                "query shape. Ensure the model has at least one "
                                "fact table with a configured source connection."
                            ),
                            "error_type": "rls_cross_source_mapping",
                        },
                    )

    # 2.6 Cache lookup — keyed by model, tenant, principal, query shape,
    # include_hidden, force_route, deployed version, and RLS policy hash.
    # Persona-scoped queries bypass cache entirely because persona policy
    # is mutable and can change within TTL.
    # Bug-7762: queries governed by user_mapping RLS rules also bypass the
    # cache. Mapping-table rows live on the source database and can be
    # mutated out-of-band (no API hook, no eviction signal). The
    # policy_hash captures only rule IDs + compiled SQL template, NOT the
    # mapping-table contents, so a revoked mapping row would still hit a
    # stale cached result — a data leak. Bypassing is the fail-closed
    # choice, matching the persona pattern.
    _has_user_mapping_rls = (
        _compiled_rls is not None
        and bool(getattr(_compiled_rls, "mapping_source_ids", ()))
    )
    if persona_id or _has_user_mapping_rls:
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
        # Bug-8250 re-gate (finding 5): the deployed VERSION ID alone does not
        # identify the deployed definition. A revert to the currently-deployed
        # version, and a redeploy of the same version after draft edits, both
        # leave ``deployed_version_id`` unchanged and bump ``deploy_epoch``
        # (Bug-7140) — so a result cached before the move still matched its key
        # and was replayed with the OLD numbers. This fast path returns before
        # ``route_query``, so NEITHER the aggregate nor the pocket built-for gate
        # ever runs on a cache hit; the key is the only thing standing between a
        # superseded result and the client. Eviction cannot cover it either:
        # ``DELETE /cache/models/{id}`` only clears the replica that receives it,
        # while key discrimination misses on every replica at once (the
        # cross-replica mechanism documented in shared/cache/result_cache.py).
        # ``Model.deploy_epoch``'s own docstring already specified this key.
        deploy_epoch = str(getattr(bound.model, "deploy_epoch", "") or "")
        # Bug-7038: include the RLS policy hash so that tightening a rule
        # (editing, creating, or deleting) causes a cache miss on all
        # replicas — the new compiled predicate produces a different hash,
        # and the old cached entry's key no longer matches. When no RLS
        # rules apply the hash is "" (empty string), preserving the pre-fix
        # key space for non-RLS queries.
        # Bug-7998 / F-027-02 (Fable review): include the caller's row_limit
        # in the cache key so a probe-clamped request (limit = cap+1) and an
        # intent-limited request (same logical limit, no cap) cannot alias.
        # Without this, the first-cached variant's truncated/row_limit flags
        # would be replayed verbatim to a different caller, hiding or
        # fabricating truncation.
        # Bug-8250: the key is assembled by ``ResultCache.make_cache_key``, in the
        # same module as the invalidation contract it has to satisfy. Building it
        # inline here is how ``deploy_epoch`` came to be missing while the
        # contract said it was present.
        cache_key = ResultCache.make_cache_key(
            model_id=body.model_id,
            tenant_id=tenant_id,
            principal_hash=principal_hash,
            query_hash=query_hash,
            force_route=body.force_route,
            include_hidden=body.include_hidden,
            deployed_version_id=deployed_ver,
            rls_policy_hash=_rls_policy_hash,
            row_limit=body.row_limit,
            deploy_epoch=deploy_epoch,
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
            served_cached = await _cached_response_with_current_freshness(
                cached,
                db,
                model_id=bound.model.id,
            )
            # Bug-8581: the artifact the cached response names is gone or no
            # longer servable (a deleted/retired pocket or aggregate). Drop the
            # entry and fall through to a real route so the query is answered
            # from whatever CAN serve it now, instead of replaying route metadata
            # for an artifact that no longer exists and a routed_sql that may
            # name a dropped table. No cache-hit log is written: nothing was
            # served from cache.
            if served_cached is None:
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
                cached=served_cached,
                user_identity=user_identity,
                tenant_id=tenant_id,
                persona=persona,
                client_kind=body.client_kind,
            )
            return served_cached

    # 3. Route
    _route_start_ms = time.monotonic()
    try:
        decision = await route_query(
            bound,
            db,
            principal=principal,
            force_route=body.force_route,
            persona=persona,
            row_security=_compiled_rls,
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
        # Bug-8809: the compiler's own message names the dimension path / rule
        # id / mapping table that failed. It is logged, not published.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=row_security_misconfigured_detail(e, surface="/execute"),
        )
    except DeployedSnapshotUnavailableError as e:
        # Bug-8515: the same condition the BIND stage already maps to 503 can
        # surface at the REWRITE stage too — Bug-7981 made the join-graph
        # loader fail closed inside ``route_query`` -> rewrite_for_source /
        # raw_sql -> _load_model_graph. It is a temporarily unusable
        # DEPLOYMENT, not a bad query, so the retry semantic and the operator
        # signal must both be 503, exactly as at the bind stage (and as
        # /headless/query and /plugin/execute already do). This catch MUST
        # precede ``except ValueError`` — DeployedSnapshotUnavailableError
        # subclasses SemanticBindingError which subclasses ValueError, so the
        # generic handler below would otherwise shadow it back to 422 ("your
        # query is invalid"), which is the wrong signal to a BI client.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e),
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
        _mapped = HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=sanitize_error_for_client(e),
        )
        # Bug-7674: already persisted above — mark so the pre-execution
        # failure wrapper does not double-log this row.
        _mapped._tessallite_failure_logged = True  # type: ignore[attr-defined]
        raise _mapped

    # 3.5 — 5. Execute through the shared observed pipeline (security
    # audits + execution + QueryLog/miss/metrics/audit). F-030-01: this
    # block is shared with /headless/query and /plugin/execute so those
    # paths can never skip logging again.
    # Bug-7674: every failure raised by execute_with_observation is already
    # persisted (it calls _log_query_failure / _security_audit_block before
    # raising). Mark those HTTPExceptions so the pre-execution failure wrapper
    # does not double-log them — the wrapper logs ONLY unmarked (truly
    # pre-execution) failures.
    try:
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
    except HTTPException as _exec_exc:
        _exec_exc._tessallite_failure_logged = True  # type: ignore[attr-defined]
        raise

    # Bug-7998 / F-027-02: if we over-fetched by one to probe for server-cap
    # truncation, trim the probe row and report ``truncated`` so the caller
    # (MCP) never renders a capped extract as complete. The +1 probe row was
    # already counted by the observed pipeline's telemetry; that single-row
    # over-count on truncated queries is intentional and negligible.
    _truncated = False
    if _probe_for_truncation and _server_row_cap is not None:
        _truncated = len(rows) > _server_row_cap
        if _truncated:
            rows = rows[:_server_row_cap]

    trace = await _build_trace(
        body, logical_query, bound, decision, db,
        executed=True, chosen_source=chosen_source,
    )
    freshness = await _result_freshness(
        decision,
        db,
        model_id=bound.model.id,
    )
    response = ExecuteResponse(
        rows=rows,
        columns=columns,
        truncated=_truncated,
        row_limit=_server_row_cap,
        route_type=decision.route_type,
        reason=decision.reason,
        aggregate_id=decision.aggregate_id,
        pocket_id=decision.pocket_id,
        execution_ms=elapsed_ms,
        bytes_processed=bytes_processed,
        rows_returned=len(rows),
        routed_sql=decision.rewritten_query,
        security_rules_applied=_security_rule_ids(decision),
        freshness=freshness,
        trace=trace,
        field_compatibility=field_compatibility,
    )
    if not persona_id and not _has_user_mapping_rls:
        _cache.set(cache_key, response)
    return response


def _is_missing_relation_error(exc: Exception) -> bool:
    text = str(exc).lower()
    if 'relation "' in text and '" does not exist' in text:
        return True
    if "not found" in text and ("dataset" in text or "table" in text):
        return True
    return False


def _extract_missing_relation_name(exc: Exception) -> str | None:
    """Extract the relation/table name from a missing-relation error.

    PostgreSQL:  relation "schema.table" does not exist
    BigQuery:    Not found: Table project:dataset.table
    """
    text = str(exc)
    m = re.search(r'relation "([^"]+)" does not exist', text, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r'Not found:.*?Table\s+(\S+)', text, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def _missing_source_table_detail(relation_name: str) -> str:
    return (
        f"Source table '{relation_name}' is no longer accessible by the "
        f"model. The table may have been dropped, renamed, or the connection "
        f"may point to a different database. Check the model's source "
        f"connection and verify the table exists."
    )


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
    cache_status: Optional[str] = None,
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
        cache_status=cache_status,
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
        # Bug-6726 (b): miss-log telemetry must never fail the user query.
        # The upsert itself is now race-safe (ON CONFLICT), but ANY
        # bookkeeping failure (connection hiccup, schema drift, etc.) is
        # caught, logged, and swallowed so the successful query result is
        # still returned to the caller.
        try:
            # F-009-01 / F-030-03 / F-101-06 / F-102-03 (Bug-8773 / Bug-9099):
            # forward the matcher's required_grain so the miss log records the
            # grain the matcher actually needs (including DISTINCT / DATE_TRUNC
            # substitutions), not the logger's re-derived lq.grain | filter_dims.
            # Without this the optimizer builds a table the matcher will never
            # select for the query that generated the miss. ``None`` when the
            # matcher early-returned keeps the logger's documented fallback.
            await log_query_miss(
                db,
                bound,
                miss_reason,
                persona_id=persona_uuid,
                required_grain=getattr(decision, "required_grain", None),
            )
        except Exception:
            logger.warning(
                "Bug-6726: miss-log telemetry failed (swallowed); "
                "query result is unaffected",
                exc_info=True,
            )


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

    A cache hit is recorded so it counts toward volume / top-users analytics,
    with ``execution_ms=0`` as the served-from-cache signal (no source/
    aggregate/pocket execution happened). No miss row is written: the underlying
    route already ran (and was logged, including any miss) when the result was
    first cached — a cache hit is a HIT, never a miss, regardless of the route
    the cached value originally took.

    The original route is preserved by reconstructing the ``RouteDecision``
    from the cached response so the row keeps ``route_type="aggregate"`` (etc.)
    for volume analytics.

    Bug-6426: the row is stamped ``cache_status="cache_hit"`` so downstream
    ACCELERATION and COST-SAVINGS rollups can EXCLUDE it. A cache re-serve is
    NOT a new acceleration event — counting it as one inflates the acceleration
    rate shown to a customer/CFO, and averaging its zero ``execution_ms``/
    ``bytes_processed`` into the savings understates real per-hit cost. The
    ``route_type`` is retained for volume/top-user analytics, but the rollup
    reads ``cache_status`` to keep cache-serve and real acceleration distinct.
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
        cache_status="cache_hit",
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
        # Bug-8392 (pocket) / Bug-8457 (aggregate): a cache artifact whose
        # physical generation changed between admission and scan takes the SAME
        # recovery as a missing cache table — discard and re-route to source with
        # the compiled row-security predicate re-injected. It is NOT a missing
        # table, so the "flag it unservable" legs below are skipped for BOTH
        # artifact kinds: the artifact is healthy, it was simply refreshed
        # underneath us, and demoting it would force a needless rebuild.
        _generation_changed = isinstance(e, ArtifactGenerationChangedError)
        if _generation_changed:
            # The guard raises with the SPECIFIC cause (no longer servable,
            # location rebound, built for another deployed version, storage
            # re-pointed, no longer provably RLS-safe, not the admitted
            # generation, or the stamp moved across the scan). Log it: without
            # this an operator seeing repeated cache fallbacks cannot tell which
            # gate fired, and the recorded route reason below is necessarily
            # generic.
            logger.warning(
                "Bug-8392/Bug-8457: %s route refused at execution time "
                "(model=%s): %s",
                decision.route_type, getattr(bound.model, "slug", None), e,
            )
        if decision.route_type in ("aggregate", "pocket") and (
            _is_missing_relation_error(e) or _generation_changed
        ):
            # Bug-5346: the routed aggregate's physical table is missing (it was
            # never materialised, or was dropped, while the definition stayed
            # `active`). Best-effort flag the (still-`active`) definition `pending`
            # so the aggregate matcher stops routing to a vanished table (ending
            # the repeated source-fallback) and the optimizer sweep rebuilds it.
            # Isolated in a SAVEPOINT and fully swallowed so it can NEVER affect
            # the already-correct source-fallback response below.
            _missing_agg_id = (
                decision.aggregate_id
                # Bug-8457: a generation change is NOT a missing table. Flagging
                # a healthy, freshly-rebuilt aggregate "pending" here would take
                # it out of the serving pool and queue a redundant rebuild on
                # every lost race — the same reason the pocket leg below already
                # excludes it.
                if decision.route_type == "aggregate" and not _generation_changed
                else None
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
            # Bug-6986: mirror the aggregate leg for pockets. When a pocket
            # route fails because its physical table is missing, transition
            # the pocket out of "fresh" so the matcher stops re-selecting it.
            # Without this the pocket stays "fresh" and every subsequent
            # matching query pays a failed pocket execution + source fallback.
            _missing_pocket_id = (
                decision.pocket_id
                if decision.route_type == "pocket" and not _generation_changed
                else None
            )
            if _missing_pocket_id:
                from shared.db.models import PocketDefinition
                try:
                    async with db.begin_nested():
                        await db.execute(
                            update(PocketDefinition)
                            .where(
                                PocketDefinition.id == _missing_pocket_id,
                                PocketDefinition.status == "fresh",
                            )
                            .values(
                                status="stale",
                                failure_reason=(
                                    "Physical pocket table missing at query time; "
                                    "fell back to source (Bug-6986)"
                                ),
                            )
                        )
                except Exception:
                    logger.warning(
                        "Bug-6986: could not flag missing pocket %s as stale",
                        _missing_pocket_id,
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
            # Bug-7808: wrap the fallback re-rewrite (rewrite_for_source +
            # _inject_security_where) in failure logging so a non-HTTPException
            # there produces a QueryLog row and a proper error, instead of
            # propagating as a bare 500 with no observability.
            try:
                _fallback_sql = await rewrite_for_source(bound, db, target_dialect=_fallback_source_dialect)
                # Codex R1 fix: when the original aggregate route was chosen
                # under active RLS (Bug-7033), the compiled predicate must be
                # re-injected into the fallback source SQL. Without this, a
                # missing aggregate table causes the fallback to serve
                # unfiltered rows — a data-exposure regression.
                _sec_compiled = getattr(decision, "security_compiled", None)
                if _sec_compiled is not None:
                    _fallback_sql = _inject_security_where(
                        _fallback_sql, _sec_compiled,
                        dialect=_fallback_source_dialect or "postgres",
                    )
            except HTTPException:
                raise
            except DeployedSnapshotUnavailableError as _snapshot_err:
                # Bug-8515: the fallback re-rewrite goes through the same
                # join-graph loader as the primary route, so it can fail closed
                # on an unusable deployed snapshot too. Bug-7808's generic
                # branch below mapped that to a 502 "could not be rewritten for
                # the source connection — check the model configuration", which
                # both mislabels the condition (the deployment needs repair,
                # nothing is misconfigured) and gives the wrong retry semantic.
                # Keep the Bug-7808 observability contract (a QueryLog row for
                # every fallback-rewrite failure) but with the typed status and
                # error_type. This catch MUST precede ``except Exception``.
                logger.error(
                    "Bug-8515: fallback re-rewrite hit an unusable deployed "
                    "snapshot: %s (model=%s)",
                    _snapshot_err, bound.model.slug,
                )
                await _log_query_failure(
                    db, user_identity, tenant_id, bound, decision,
                    start_ms, "snapshot_unavailable", str(_snapshot_err),
                    persona_id=persona_uuid, client_kind=client_kind,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=str(_snapshot_err),
                )
            except Exception as _rewrite_err:
                logger.error(
                    "Bug-7808: fallback re-rewrite failed: %s (model=%s)",
                    _rewrite_err, bound.model.slug, exc_info=True,
                )
                await _log_query_failure(
                    db, user_identity, tenant_id, bound, decision,
                    start_ms, "routing_error", str(_rewrite_err),
                    persona_id=persona_uuid, client_kind=client_kind,
                )
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=(
                        "The query could not be rewritten for the source "
                        "connection after a cache-table fallback. "
                        "Check the model configuration."
                    ),
                )
            decision = RouteDecision(
                route_type="source",
                rewritten_query=_fallback_sql,
                reason=(
                    # R5 finding F4: the generation-guard exception text
                    # carries (schema=... table=...), and this reason reaches
                    # the caller on ExecuteResponse.reason AND in
                    # trace.steps[router].detail -- two channels the embed
                    # withhold does not cover, so interpolating it disclosed a
                    # physical schema and table to an embed session on the
                    # refresh race. The exception is already logged in full;
                    # the reason states the routing FACT without the
                    # identifiers, matching the sibling branch below.
                    (
                        f"Cache artifact "
                        f"{decision.aggregate_id or decision.pocket_id} was not "
                        f"servable at execution time; fell back to source"
                    )
                    if _generation_changed
                    else (
                        f"Routed cache table missing physical table "
                        f"({decision.aggregate_id or decision.pocket_id}); "
                        "fell back to source"
                    )
                ),
                aggregate_id=None,
                pocket_id=None,
                target_dialect=_fallback_source_dialect,
                source_dialect=_fallback_source_dialect,
                security_compiled=_sec_compiled,
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
                if _is_missing_relation_error(inner):
                    _mn = _extract_missing_relation_name(inner) or "unknown"
                    logger.warning(
                        "Source table inaccessible (fallback): %s (model=%s)",
                        _mn, bound.model.slug,
                    )
                    await _log_query_failure(
                        db, user_identity, tenant_id, bound, decision,
                        start_ms, "missing_source_table", _mn,
                        persona_id=persona_uuid, client_kind=client_kind,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail=_missing_source_table_detail(_mn),
                    )
                logger.error("Source query failed (fallback): %s", inner, exc_info=True)
                await _log_query_failure(db, user_identity, tenant_id, bound, decision, start_ms, "execution_error", str(inner), persona_id=persona_uuid, client_kind=client_kind)
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=sanitize_error_for_client(inner),
                )
        elif _is_missing_relation_error(e):
            _missing_name = _extract_missing_relation_name(e) or "unknown"
            logger.warning(
                "Source table inaccessible: %s (model=%s)",
                _missing_name, bound.model.slug,
            )
            await _log_query_failure(
                db, user_identity, tenant_id, bound, decision, start_ms,
                "missing_source_table", _missing_name,
                persona_id=persona_uuid, client_kind=client_kind,
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=_missing_source_table_detail(_missing_name),
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
    except GroupByError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_client_parse_error_detail(e),
        )
    except SyntaxErrorInSQL as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_client_parse_error_detail(e),
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_client_parse_error_detail(e),
        )
    except Exception:
        # Bug-6553: non-parse exceptions are server faults -> 500.
        logger.exception("Unexpected error during query parse (explain)")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error during query parse",
        )

    try:
        bound = await bind_query_to_model(
            logical_query, db, include_hidden=body.include_hidden
        )
    except DeployedSnapshotUnavailableError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e),
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
    except HTTPException as _persona_exc:
        # F-008-22: a persona deny must not be replaced by a compatibility
        # 422 that confirms the denied object exists (existence/type oracle).
        if (
            _persona_exc.status_code == status.HTTP_403_FORBIDDEN
            and isinstance(_persona_exc.detail, dict)
            and _persona_exc.detail.get("error_code") in (
                "OBJECT_NOT_AVAILABLE",
                "PERSONA_COMPLEX_SQL_NOT_ALLOWED",
            )
        ):
            raise
        if persona is None and persona_id:
            persona = await load_persona(
                db, model_id=body.model_id, persona_id=persona_id
            )
        if body.force_route != "raw":
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

    # Bug-5877: must be pre-initialized — the gate below is skipped for
    # force_route="raw" (the JDBC gateway always sends it) but the response
    # still references the variable.
    field_compatibility = None
    if body.force_route != "raw":
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
        # Bug-8809: identifier-free body; specifics go to the service log.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=row_security_misconfigured_detail(e, surface="/explain"),
        )
    except DeployedSnapshotUnavailableError as e:
        # Bug-8515 (shared-primitive sweep): /explain calls the same
        # ``route_query`` primitive as /execute, so the rewrite stage can raise
        # the same typed error here. The bind stage above already 503s; without
        # this the route stage would 422 and the Explorer's route-plan panel
        # would tell a modeller their query is invalid when the deployment is
        # the thing that needs repair. MUST precede ``except ValueError``.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e),
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
        security_rules_applied=_security_rule_ids(decision),
        trace=trace,
        field_compatibility=field_compatibility,
        aggregate_skipped_reasons=list(
            getattr(decision, "aggregate_skipped_reasons", None) or []
        ),
        filter_columns_missing=list(
            getattr(decision, "filter_columns_missing", None) or []
        ),
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
    except GroupByError as e:
        return ValidateResponse(ok=False, errors=[str(e)])
    except SyntaxErrorInSQL as e:
        return ValidateResponse(ok=False, errors=[str(e)])
    except ValueError:
        # Bug-6553: unexpected ValueError stays generic.
        return ValidateResponse(ok=False, errors=["Parse failed"])
    except Exception:
        # Bug-6553: non-parse exceptions -> generic message (no detail leak).
        logger.exception("Unexpected error during query parse (validate)")
        return ValidateResponse(ok=False, errors=["Internal server error during query parse"])

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
            if persona is not None and body.force_route != "raw":
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

    field_compatibility = None
    if body.force_route != "raw":
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


# KPI ORM fields carrying a v2 DSL expression whose measure()/kpi() references
# feed a value served by the ``$KPIs`` virtual table (value + target). The served
# ``status``/``trend_pct`` are derived from value vs target by the v2
# direction/threshold logic (``evaluate_threshold`` / ``evaluate_trend``), so
# they add no independent measure lineage. These are the lineage channels the
# CLS/allow-list gate must scan and fail closed on (Bug-6139).
#
# The legacy v1 ``status_expression``/``trend_expression`` columns are
# DELIBERATELY excluded. They ARE still read by the gateway XMLA/MDX path, but
# they are DAX over the KPI's OWN ``KpiValue``/``KpiGoal`` (not v2 ``measure()``
# references), so they introduce no NEW measure lineage beyond the value/goal
# lineage already scanned here — excluding them cannot open a leak. Including
# them would instead fail-close (withhold) an otherwise-clean v2 KPI that merely
# carries stale, non-v2-parseable legacy DAX text — an availability regression
# with no security benefit. The direct legacy measure-id bindings (value/goal)
# ARE gated below because those DO feed a v1 KPI's served value.
_KPI_EXPRESSION_FIELDS = (
    "expression",
    "target_expression",
)
# KPI ORM fields that bind a measure by id DIRECTLY (no expression). Includes the
# legacy v1 value/goal bindings that the original gate ignored (Bug-6139) — a KPI
# bound to a restricted-column measure via ``value_measure_id`` must still be
# withheld.
_KPI_DIRECT_MEASURE_ID_FIELDS = (
    "value_measure_id",
    "goal_measure_id",
    "target_measure_id",
)


def _extract_kpi_references(expression: str) -> tuple[list[str], list[str], bool]:
    """Return ``(measure_names, kpi_names, parse_ok)`` for a KPI DSL expression.

    Unlike ``extract_measure_names`` (which swallows parse errors and returns an
    empty list — indistinguishable from "no references"), this signals parse
    failure via ``parse_ok=False`` so the caller can FAIL CLOSED on an
    unparseable expression instead of serving it as if it had no lineage
    (Bug-6139). Also returns nested ``kpi()`` references so composite-KPI lineage
    can be followed transitively.
    """
    if not expression or not str(expression).strip():
        return [], [], True
    try:
        ast = parse_kpi_expression(expression)
    except Exception:
        return [], [], False
    measures, kpis, _dims = _kpi_collect_references(ast)
    return list(dict.fromkeys(measures)), list(dict.fromkeys(kpis)), True


def _kpi_lineage_measure_ids(
    kpi: Any,
    kpi_by_name: dict[str, Any],
    measure_name_to_id: dict[str, str],
    children_by_parent: dict[str, list[Any]] | None = None,
    _seen: set[str] | None = None,
) -> tuple[set[str], bool]:
    """Resolve the FULL transitive measure-id lineage of a KPI.

    Returns ``(measure_ids, fully_resolved)``. ``fully_resolved`` is False when
    ANY lineage channel could not be verified — an unparseable expression, an
    expression measure name that resolves to no id, or a nested ``kpi()`` whose
    name is not found — so the caller fails CLOSED (Bug-6139).

    Channels closed (all of them):
      * direct id bindings — legacy ``value_measure_id`` / ``goal_measure_id``
        and ``target_measure_id``;
      * the served-value DSL expression fields (``expression`` /
        ``target_expression``), for their ``measure()`` references;
      * nested ``kpi()`` references, followed transitively with cycle protection;
      * COMPOSITE children (Bug-6139 R1): a composite KPI's served ``kpi_latest``
        value is the weighted score of its children, loaded by
        ``parent_kpi_id`` — NOT from its own expression (which is a placeholder).
        So the composite's lineage is the UNION of its children's lineage. Walk
        every child whose ``parent_kpi_id`` is this KPI, transitively (a child may
        itself be a composite), cycle-guarded. Without this a composite over a
        restricted-column child leaks a restricted-derived score via ``$KPIs``.
    """
    seen = _seen if _seen is not None else set()
    children_by_parent = children_by_parent or {}
    kid = getattr(kpi, "id", None)
    if kid is not None:
        skid = str(kid)
        if skid in seen:
            # Cycle: this KPI's lineage is already being accounted for higher in
            # the recursion. Return no new ids (not a failure).
            return set(), True
        seen.add(skid)

    measure_ids: set[str] = set()
    fully_resolved = True

    for attr in _KPI_DIRECT_MEASURE_ID_FIELDS:
        v = getattr(kpi, attr, None)
        if v is not None:
            measure_ids.add(str(v))

    for field_name in _KPI_EXPRESSION_FIELDS:
        expression = getattr(kpi, field_name, None)
        measures, kpis, parse_ok = _extract_kpi_references(expression)
        if not parse_ok:
            # An unparseable expression could reference anything — fail closed.
            fully_resolved = False
            continue
        for name in measures:
            mid = measure_name_to_id.get(name)
            if mid is None:
                # A measure name that resolves to no id cannot be verified.
                fully_resolved = False
            else:
                measure_ids.add(mid)
        for kpi_name in kpis:
            nested = kpi_by_name.get(kpi_name) or kpi_by_name.get(kpi_name.lower())
            if nested is None:
                # A nested kpi() whose lineage we cannot inspect — fail closed.
                fully_resolved = False
                continue
            nested_ids, nested_ok = _kpi_lineage_measure_ids(
                nested, kpi_by_name, measure_name_to_id, children_by_parent, seen,
            )
            measure_ids |= nested_ids
            fully_resolved = fully_resolved and nested_ok

    # Composite children: fold in the lineage of every child KPI bound to this
    # KPI via parent_kpi_id (the actual source of a composite's served value).
    if kid is not None:
        for child in children_by_parent.get(str(kid), []):
            child_ids, child_ok = _kpi_lineage_measure_ids(
                child, kpi_by_name, measure_name_to_id, children_by_parent, seen,
            )
            measure_ids |= child_ids
            fully_resolved = fully_resolved and child_ok

    return measure_ids, fully_resolved


def _kpi_allowed_by_persona(
    kpi: Any,
    allowed_measure_ids: set[str] | None,
    measure_name_to_id: dict[str, str],
    cls_blocked_measure_ids: frozenset[str] | set[str] = frozenset(),
    kpi_by_name: dict[str, Any] | None = None,
    children_by_parent: dict[str, list[Any]] | None = None,
) -> bool:
    """Return True when a persona may see a KPI, given its measure lineage.

    Two independent, fail-closed gates:

    1. Persona measure allow-list (``allowed_measure_ids``): every measure the
       KPI depends on must be included. ``None`` means unrestricted.
    2. CLS column restrictions (``cls_blocked_measure_ids``, Bug-6139): a KPI
       whose lineage reaches a persona-restricted COLUMN is withheld. The
       ``$KPIs`` virtual table serves already-aggregated scorecard values, so a
       KPI built on a restricted column (e.g. a distinct count of a masked id,
       or a sum of a restricted amount) would otherwise leak a
       restricted-column-derived number that the same persona is forbidden to
       read on the base table — the CLS gate the normal query path enforces via
       ``_check_column_restrictions`` did not cover this seam.

    Lineage (Bug-6139 — ALL channels closed) is resolved by
    ``_kpi_lineage_measure_ids``: direct id bindings (incl. legacy
    ``value_measure_id`` / ``goal_measure_id``), the served-value DSL expression
    fields' ``measure()`` refs, nested ``kpi()`` refs followed transitively, and
    composite children bound via ``parent_kpi_id``.

    Fail-closed: an unparseable expression, a referenced measure name that
    resolves to no id, an unresolvable nested ``kpi()`` name, a lineage measure
    outside the allow-list, or any lineage measure that touches a restricted
    column, all withhold the KPI.
    """
    gate_active = allowed_measure_ids is not None or bool(cls_blocked_measure_ids)
    if not gate_active:
        return True

    referenced_ids, fully_resolved = _kpi_lineage_measure_ids(
        kpi, kpi_by_name or {}, measure_name_to_id, children_by_parent or {},
    )
    if not fully_resolved:
        # Lineage could not be fully verified — withhold rather than risk
        # serving a restricted-column-derived value.
        return False

    # CLS gate: any referenced measure reaching a restricted column withholds.
    if cls_blocked_measure_ids and (referenced_ids & set(cls_blocked_measure_ids)):
        return False

    # Persona allow-list gate.
    if allowed_measure_ids is not None:
        for mid in referenced_ids:
            if mid not in allowed_measure_ids:
                return False
    return True


async def _kpi_cls_blocked_measure_ids(
    db: AsyncSession, model_id: str, persona: Any | None,
) -> frozenset[str]:
    """Measure ids whose column closure reaches a persona-CLS-restricted column.

    Bug-6139: resolves the persona's tag restrictions to restricted model
    columns, then walks every measure's column closure (direct source column,
    UDA-backed, variant base, calculated-measure references) with the same
    engine the runtime CLS gate uses (``router._touches_restricted_columns``),
    so ``$KPIs`` withholding stays consistent with the base-table CLS gate. An
    empty set means no CLS restriction is in force (serve as before).
    """
    if persona is None:
        return frozenset()
    from shared.db.models import UserDefinedAttributeColumnRef
    from src.routing.router import (
        _ClsClosure,
        _restricted_physical_names,
        _touches_restricted_columns,
    )

    restriction_rows = (
        await db.execute(
            select(PersonaTagRestriction.data_tag_id)
            .where(PersonaTagRestriction.persona_id == persona.id)
        )
    ).scalars().all()
    if not restriction_rows:
        return frozenset()
    restricted_col_rows = (
        await db.execute(
            select(data_tag_columns.c.model_column_id)
            .where(data_tag_columns.c.tag_id.in_(restriction_rows))
        )
    ).scalars().all()
    if not restricted_col_rows:
        return frozenset()
    restricted_ids = {str(c) for c in restricted_col_rows}

    meas_rows = (
        await db.execute(select(Measure).where(Measure.model_id == model_id))
    ).scalars().all()

    ctx = _ClsClosure()
    for m in meas_rows:
        ctx.measures_by_id[str(m.id)] = m
        ctx.measures_by_name[m.name] = m
    uda_rows = (
        await db.execute(
            select(UserDefinedAttributeColumnRef.attribute_id)
            .where(UserDefinedAttributeColumnRef.column_id.in_(list(restricted_col_rows)))
        )
    ).scalars().all()
    ctx.restricted_uda_ids = {str(a) for a in uda_rows}
    ctx.restricted_physical_names = await _restricted_physical_names(
        list(restricted_col_rows), db,
    )

    blocked = {
        str(m.id)
        for m in meas_rows
        if _touches_restricted_columns(m, restricted_ids, ctx)
    }
    return frozenset(blocked)


def _named_query_excluded_level_attrs(
    snapshot: dict, allowed_dim_ids: set[str],
) -> set[str] | None:
    """Mirror ``persona_gate._get_excluded_level_attribute_ids`` over the
    deployed snapshot's own dimension rows (same rows, no DB round-trip).

    When ``included_dimension_ids`` is populated, any hierarchy level whose
    key attribute backs an excluded dimension is hidden.
    """
    if not allowed_dim_ids:
        return None
    excluded: set[str] = set()
    for dim in snapshot.get("dimensions") or []:
        if not isinstance(dim, dict):
            continue
        if str(dim.get("id")) in allowed_dim_ids:
            continue
        src = dim.get("source_column_id")
        if src is not None:
            excluded.add(str(src))
        uda = dim.get("user_defined_attribute_id")
        if uda is not None:
            excluded.add(str(uda))
    return excluded


def _named_query_cls_closure_from_snapshot(
    snapshot: dict, restricted_column_ids: set[str],
):
    """The shared CLS closure lookups, populated from the deployed snapshot.

    The SAME authority the star expansion enumerates — so the NQ narrowing's
    closure check can never drift from the field set it filters, and it needs
    no extra DB reads beyond the two restriction-id queries.
    """
    import types as _types

    from shared.security.restricted_column_closure import ClosureContext

    columns = [
        c for c in snapshot.get("columns") or [] if isinstance(c, dict)
    ]
    restricted_phys = {
        str(c["column_name"]).lower()
        for c in columns
        if str(c.get("id")) in restricted_column_ids and c.get("column_name")
    }
    known_phys = {
        str(c["column_name"]).lower() for c in columns if c.get("column_name")
    }
    tables = [
        t for t in snapshot.get("tables") or [] if isinstance(t, dict)
    ]
    table_identifiers: set[str] = set()
    for t in tables:
        if t.get("physical_name"):
            table_identifiers.add(str(t["physical_name"]).lower())
        if t.get("alias"):
            table_identifiers.add(str(t["alias"]).lower())
    refs = [
        r for r in snapshot.get("uda_column_refs") or []
        if isinstance(r, dict)
    ]
    restricted_uda_ids = {
        str(r["attribute_id"])
        for r in refs
        if r.get("attribute_id") is not None
        and str(r.get("column_id")) in restricted_column_ids
    }
    measures = [
        m for m in snapshot.get("measures") or [] if isinstance(m, dict)
    ]
    return ClosureContext(
        restricted_uda_ids=restricted_uda_ids,
        measures_by_id={
            str(m["id"]): _types.SimpleNamespace(**m)
            for m in measures if m.get("id") is not None
        },
        measures_by_name={
            str(m["name"]): _types.SimpleNamespace(**m)
            for m in measures if m.get("name")
        },
        restricted_physical_names=restricted_phys,
        known_physical_names=known_phys,
        table_identifiers=table_identifiers,
    )


def _named_query_allowed_star_fields(
    *,
    persona: Any,
    snapshot: dict,
    persona_allow_lists: bool,
    restricted_column_ids: set[str] | None,
) -> set[str]:
    """The persona/CLS-PERMITTED subset of the exposed star field set (NQ2C-F1).

    The expansion erases ``select_star`` — the flag persona_gate and the CLS
    gate use to choose NARROW-mode over DENY-mode — so the expanded explicit
    projection would hit their deny branches (403) for every disallowed field.
    This mirrors the STAR-mode narrowing of both gates over the SAME exposed
    field rows the expansion enumerates (``exposed_star_fields_by_kind``), so
    the NQ serve handler can pass the result as ``allowed_fields`` and the
    live compile under a restricted principal NARROWS instead of 403-ing.

    * Persona allow-lists: READ-ONLY mirror of
      ``persona_gate.enforce_persona``'s branches over the SAME rows the
      expansion enumerates. A plain measure is admitted only if it survives
      BOTH branches: the STAR branch (the measure id allow-list — what a raw
      star would hide) AND the explicit DENY branch (the dimension id
      allow-list — a projected plain measure binds as a measure-as-dimension
      and is checked by ``_dim_allowed``, so a measure whose id is not in the
      dimension allow-list would 403). The intersection can never 403 and
      never includes a field the star narrowing would have hidden.
    * CLS data tags: the shared closure module
      (``shared.security.restricted_column_closure`` — the SAME algorithm the
      runtime CLS gate uses) over snapshot-row namespaces, with lookups built
      from the snapshot itself.

    The build never narrows; restricted principals can never reach the
    materialised path (the serve gate refuses them), so live-only narrowing
    cannot break build == live for any servable artifact.
    """
    import types as _types

    from shared.named_query.star_expansion import exposed_star_fields_by_kind
    from shared.security.restricted_column_closure import (
        object_touches_restricted,
    )

    dim_fields, meas_fields = exposed_star_fields_by_kind(snapshot)
    if restricted_column_ids is None:
        # Fail closed (Bug-9019 parity): the restriction set could not be
        # enumerated (genuine DB error) -> no field can be proven permitted.
        return set()
    allowed: set[str] = set()

    def _admit(field: dict) -> None:
        name = field.get("name")
        if name:
            allowed.add(str(name))

    if persona_allow_lists:
        measure_allow = {
            str(v) for v in (persona.included_measure_ids or []) if v is not None
        }
        dimension_allow = {
            str(v) for v in (persona.included_dimension_ids or [])
            if v is not None
        }
        hierarchy_allow = {
            str(v) for v in (persona.included_hierarchy_ids or [])
            if v is not None
        }
        excluded_level_attrs = _named_query_excluded_level_attrs(
            snapshot, dimension_allow,
        )

        def _dim_allowed(d: dict) -> bool:
            # Mirror persona_gate.enforce_persona._dim_allowed verbatim.
            if str(d.get("id")) in dimension_allow:
                return True
            hid = d.get("hierarchy_id")
            if hid is not None:
                if excluded_level_attrs is not None:
                    src = d.get("source_column_id")
                    uda = d.get("user_defined_attribute_id")
                    if (src is not None and str(src) in excluded_level_attrs) or \
                       (uda is not None and str(uda) in excluded_level_attrs):
                        return False
                if hierarchy_allow:
                    return str(hid) in hierarchy_allow
                return True
            return False

        for d in dim_fields:
            if dimension_allow and not _dim_allowed(d):
                continue
            if hierarchy_allow:
                hid = d.get("hierarchy_id")
                if hid is not None and str(hid) not in hierarchy_allow:
                    continue
            _admit(d)
        for m in meas_fields:
            # Star branch: the measure id allow-list filters resolved_measures.
            if measure_allow and str(m.get("id")) not in measure_allow:
                continue
            # Deny branch: a projected plain measure binds as a
            # measure-as-dimension and is checked by the DIMENSION allow-list
            # (_dim_allowed requires the id or an allowed hierarchy — measures
            # have no hierarchy), so admit only when its id is allowed.
            if dimension_allow and str(m.get("id")) not in dimension_allow:
                continue
            _admit(m)
    else:
        for d in dim_fields:
            _admit(d)
        for m in meas_fields:
            _admit(m)

    if restricted_column_ids:
        ctx = _named_query_cls_closure_from_snapshot(
            snapshot, restricted_column_ids,
        )
        cls_allowed: set[str] = set()
        for field in [*dim_fields, *meas_fields]:
            name = field.get("name")
            if not name or str(name) not in allowed:
                continue
            obj = _types.SimpleNamespace(**field)
            if not object_touches_restricted(
                obj, restricted_column_ids, ctx,
            ):
                cls_allowed.add(str(name))
        allowed = cls_allowed
    return allowed


async def _named_query_persona_restricted_column_ids(
    db: AsyncSession, persona: Any,
) -> set[str]:
    """The persona's CLS-restricted model-column ids (mirror of
    ``router._persona_restricted_column_ids`` — the same two queries)."""
    restriction_rows = (
        await db.execute(
            select(PersonaTagRestriction.data_tag_id)
            .where(PersonaTagRestriction.persona_id == persona.id)
        )
    ).scalars().all()
    if not restriction_rows:
        return set()
    restricted_col_rows = (
        await db.execute(
            select(data_tag_columns.c.model_column_id)
            .where(data_tag_columns.c.tag_id.in_(restriction_rows))
        )
    ).scalars().all()
    return {str(c) for c in restricted_col_rows}


async def _handle_named_query_reference(
    db: AsyncSession,
    body: ExecuteRequest,
    logical_query: LogicalQuery,
    *,
    ref_name: str,
    persona: Any | None = None,
    principal: Any | None = None,
    user_identity: str = "",
    tenant_id: str = "",
    client_kind: Optional[str] = None,
    server_row_cap: Optional[int] = None,
    force_route: Optional[str] = None,
    row_limit: Optional[int] = None,
) -> ExecuteResponse:
    """Serve a Named Query reference (``SELECT * FROM @name``).

    Materialised-first, source-fallback (invariant 10): the materialised
    result table serves when it is fresh, not overdue, version-bound to the
    deployed model, and the shape's EXISTING security proof holds for this
    principal; otherwise the stored semantic definition is dispatched through
    the ordinary pipeline (live), where bind/route/security decisions are
    byte-identical to a hand-issued query.

    Security is consumed, never authored (invariant 4): the projection-shape
    proof reuses the pocket §5.1 rules verbatim (row-preserving ``SELECT *``
    definition, every security column materialised in the manifest,
    case-sensitive, no ``user_mapping`` rule) and injects the SAME compiled
    predicate the source route would use; an aggregated shape under active row
    security, an active CLS restriction, or a persona default filter always
    falls back to live execution under the consumer's context.
    """
    from sqlalchemy.orm import selectinload

    from shared.aggregate_connection import resolve_source_connection
    from shared.artifact_version_gate import artifact_built_for_current
    from shared.connector_qualify import quote_table_ref
    from shared.db.models import (
        DataTarget,
        Model,
        NamedQueryArtifact,
        NamedQueryRefreshPolicy,
        PersonaTagRestriction,
    )
    from shared.deploy_resolver_core import load_deployed_snapshot
    from shared.schemas.connection_type import normalize_connection_type
    from shared.source_executor import resolve_connector_type
    from shared.staleness_gate import artifact_overdue, resolve_overdue_grace_seconds
    from src.rewrite.dialects import _dialect_from_connection_type
    from src.routing.artifact_generation_guard import generation_from
    from src.routing.named_query_generation_guard import (
        NamedQueryGenerationChangedError,
        assert_named_query_generation_unchanged,
        assert_named_query_route_admissible,
        read_named_query_generation,
    )

    start_ms = time.monotonic()

    exact_reference = named_query_reference_name(body.raw_query) is not None

    # --- An undeployed model has no deployed snapshot: no Named Queries are
    # defined, and any query against it fails with the same 409 the binder
    # raises for every other query shape. ---
    model = await db.get(Model, body.model_id)
    if model is None or getattr(model, "deployed_version_id", None) is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(ModelNotDeployedError(
                f"Model {body.model_id} is not deployed; deploy it before "
                f"querying named objects."
            )),
        )

    # --- Resolve the definition from the deployed snapshot (invariant 7) ---
    try:
        definitions = await load_named_queries(body.model_id, db)
    except NamedQueryError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message)
    _key = f"@{ref_name}".lower() if not ref_name.startswith("@") else ref_name.lower()
    nq = definitions.get(_key)
    if nq is None:
        # Wrong object type: the name matches a named set/list (any kind).
        named_lists = await load_named_lists(body.model_id, db)
        if _key in named_lists:
            _nl = named_lists[_key]
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=NamedQueryWrongType(
                    ref_name, "named list"
                ).message,
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=NamedQueryUnknownReference(ref_name).message,
        )
    if not exact_reference:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=NamedQueryUnsupportedShape(ref_name).message,
        )

    # --- Live operational state (freshness/manifest/pointer are mutable; the
    # DEFINITION above is snapshot-pinned). The artifact's build binding must
    # match the model's deployed pointer (version gate) or it cannot serve. ---
    artifact = None
    policy = None
    try:
        _nq_id = uuid.UUID(nq.id)
    except (TypeError, ValueError):
        _nq_id = None
    if _nq_id is not None:
        artifact = (
            await db.execute(
                select(NamedQueryArtifact).where(
                    NamedQueryArtifact.named_query_id == _nq_id
                )
            )
        ).scalar_one_or_none()
        policy = (
            await db.execute(
                select(NamedQueryRefreshPolicy).where(
                    NamedQueryRefreshPolicy.named_query_id == _nq_id
                )
            )
        ).scalar_one_or_none()

    # --- Security context (compiled RLS + CLS + persona default filters) ---
    _compiled_rls = None
    _rls_active = False
    _rls_bypass = bool(persona is not None and persona.bypass_row_security)
    if principal is not None:
        try:
            _source_conn = await resolve_source_connection(body.model_id, db)
            _source_connector = await resolve_connector_type(_source_conn)
        except Exception:
            # F-007-22: never guess postgresql. A connector we cannot
            # prove would quote the security predicate in the wrong dialect.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "message": (
                        "Could not resolve the source connector to compile "
                        "row security. The query was blocked (fail closed)."
                    ),
                    "error_type": "row_security_misconfigured",
                },
            )
        try:
            _compiled_rls = await compile_row_security(
                body.model_id, principal, db, connector=_source_connector,
            )
        except RowSecurityCompileError as e:
            # NQ-1: a row-security rule that fails to compile must fail closed
            # with the same typed 422 every other surface raises (/execute,
            # /explain, /discover/members) — NEVER fall through to serving the
            # unfiltered materialised table.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=row_security_misconfigured_detail(e, surface="/named-query"),
            )
        _rls_active = bool(
            _compiled_rls is not None
            and has_active_rules(_compiled_rls)
            and not _rls_bypass
        )
    _cls_active = False
    if persona is not None:
        try:
            _cls_rows = (
                await db.execute(
                    select(PersonaTagRestriction.data_tag_id)
                    .where(PersonaTagRestriction.persona_id == persona.id)
                    .limit(1)
                )
            ).first()
            _cls_active = _cls_rows is not None
        except SQLAlchemyError:
            # Bug-9019: a genuine DB/operational error must still fail closed
            # (live fallback under the consumer's CLS), but a coding error
            # (AttributeError, KeyError, ...) must now SURFACE instead of being
            # silently converted to a plausible fail-closed constant.
            _cls_active = True
    _persona_default_filters = bool(
        persona is not None and (persona.default_filters or {})
    )
    _persona_allow_lists = bool(
        persona is not None
        and (
            getattr(persona, "included_measure_ids", None)
            or getattr(persona, "included_dimension_ids", None)
            or getattr(persona, "included_hierarchy_ids", None)
        )
    )

    # --- Deployed-snapshot authority for the EXPANDED definition + population
    # fingerprint (Bug-9161 corrected Phase 1). A row-preserving
    # ``SELECT * FROM model`` definition is expanded to its explicit exposed-
    # field projection so the canonical source compile joins the model's
    # DECLARED relations instead of collapsing to the anchor table (see
    # shared/named_query/star_expansion.py). Both the live compile and the
    # expected fingerprint derive from THIS snapshot, byte-identically to the
    # refresh build's inputs. Loaded AFTER the security-context compile so a
    # misconfigured row-security rule still surfaces its typed fail-closed 422
    # (NQ-1) instead of being masked by a snapshot error. ---
    class _DeployedNQSnapshotUnavailable(Exception):
        pass

    try:
        _deployed_snapshot = await load_deployed_snapshot(
            db, model, family="named_queries",
            error_cls=_DeployedNQSnapshotUnavailable,
        )
    except _DeployedNQSnapshotUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        )
    if _deployed_snapshot is None:  # pragma: no cover - model-deployed check above
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(ModelNotDeployedError(
                f"Model {body.model_id} is not deployed; deploy it before "
                f"querying named objects."
            )),
        )
    # --- NQ2C-F1: narrow BEFORE expanding for restricted principals. ---
    # ``select_star`` is not merely a projection shape: it is the flag the
    # persona allow-list gate (persona_gate.enforce_persona) and the CLS
    # data-tag gate (router._check_column_restrictions) use to choose
    # NARROW-mode over DENY-mode. Expanding the star erases the flag, so the
    # expanded explicit projection would hit their DENY branches and a
    # restricted reader would get 403 instead of a narrowed result. Build the
    # expansion from the persona/CLS-PERMITTED SUBSET of the exposed fields
    # instead (mirroring both gates' star-mode narrowing over the SAME
    # snapshot rows the expansion enumerates). The materialised path is
    # ALREADY refused for these principals below (cls_or_default_filters_live
    # / persona_allow_list_live), so live-only narrowing cannot break
    # build == live for any servable artifact.
    _allowed_nq_fields: set[str] | None = None
    if _persona_allow_lists or _cls_active:
        _nq_restricted_ids: set[str] | None = set()
        if _cls_active:
            try:
                _nq_restricted_ids = await _named_query_persona_restricted_column_ids(
                    db, persona,
                )
            except SQLAlchemyError:
                # Bug-9019 parity: a genuine DB/operational error fails CLOSED.
                # For the narrowing that means "no field can be proven
                # permitted" -> the CLS-style 403 below (the live CLS check
                # would hit the same error, so this converts an unclassified
                # 500 into the existing typed refusal). Coding errors surface.
                _nq_restricted_ids = None
        _allowed_nq_fields = _named_query_allowed_star_fields(
            persona=persona,
            snapshot=_deployed_snapshot,
            persona_allow_lists=_persona_allow_lists,
            restricted_column_ids=_nq_restricted_ids,
        )
        if (
            is_expandable_star_definition(nq.definition_sql)
            and not _allowed_nq_fields
        ):
            # The empty-narrowed case reproduces the EXISTING CLS 403
            # (router's star-fully-restricted shape: a raw SELECT * would
            # fall back to a full-star source scan and be blocked only AFTER
            # the restricted values were read) — NOT the expansion's
            # ValueError, which would read as a definition error.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error_code": "OBJECT_NOT_AVAILABLE",
                    "message": (
                        "No columns are available to return for this query "
                        "with your current access."
                    ),
                },
            )
    try:
        _expanded_definition = expand_named_query_star_definition(
            nq.definition_sql, _deployed_snapshot,
            allowed_fields=_allowed_nq_fields,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        )
    # The population fingerprint names the artifact's FULL population — the
    # build never narrows. The narrowed definition above is the LIVE dispatch
    # body only; restricted principals never reach the materialised gate, so
    # a narrowed fingerprint would only mislabel the skip reason.
    if _allowed_nq_fields is not None:
        _contract_definition = expand_named_query_star_definition(
            nq.definition_sql, _deployed_snapshot,
        )
    else:
        _contract_definition = _expanded_definition
    _expected_population_fingerprint = named_query_population_fingerprint(
        model_id=body.model_id,
        named_query_id=nq.id,
        deployed_version_id=getattr(model, "deployed_version_id", None),
        deploy_epoch=getattr(model, "deploy_epoch", 0),
        definition_sql=_contract_definition,
    )

    # --- Materialised-vs-live decision ---
    # NQ-2/Bug-9161 ordering: fresh -> version/epoch -> population contract ->
    # overdue -> security -> materialised. The population-contract gate (the
    # manifest's version + live-build binding + row_definition_fingerprint,
    # compared against the fingerprint derived from the DEPLOYED snapshot
    # above) admits only artifacts built by the current canonical compiler;
    # legacy raw-built artifacts (no manifest / no fingerprint) and artifacts
    # from a superseded contract fall back to live until rebuilt — no unsafe
    # grandfathering.
    serve_materialised = False
    _skip_reason = "no_artifact"
    _overdue = False
    if artifact is not None and artifact.status == "fresh":
        _skip_reason = "version_gate"
        deployed_vid = getattr(model, "deployed_version_id", None)
        if deployed_vid is not None and artifact_built_for_current(
            artifact.built_for_version_id,
            artifact.built_for_epoch,
            deployed_vid,
            getattr(model, "deploy_epoch", 0),
        ):
            _skip_reason = "population_contract_mismatch"
            if named_query_population_manifest_matches(
                manifest=artifact.row_manifest,
                active_refresh_run_id=artifact.active_refresh_run_id,
                expected_fingerprint=_expected_population_fingerprint,
            ):
                _skip_reason = "overdue"
                _grace = resolve_overdue_grace_seconds("named_query")
                _cron = (
                    (policy.cron_expression or None)
                    if policy is not None and policy.is_enabled
                    else None
                )
                if _grace is None or not artifact_overdue(
                    _cron,
                    artifact.last_refresh_at,
                    datetime.now(timezone.utc),
                    _grace,
                ):
                    _skip_reason = "security"
                    if _rls_active and nq.shape == "aggregated":
                        # A pre-aggregated table cannot be row-filtered after the
                        # fact without wrong numbers — live fallback re-aggregates
                        # under the consumer's row filter (spec §7.4).
                        _skip_reason = "rls_aggregated_live"
                    elif _cls_active or _persona_default_filters:
                        # CLS column projection of a shared cache and persona
                        # default filters are v1 live-only.
                        _skip_reason = "cls_or_default_filters_live"
                    elif _persona_allow_lists:
                        # A persona allow-list (included_*_ids) is enforced by
                        # enforce_persona_gate on the LIVE path only; the
                        # materialised fast path intercepts before bind and cannot
                        # narrow a shared-cache SELECT * in v1 (spec §7.4). Serve
                        # live so the allow-list is applied — same v1 live-only
                        # treatment as CLS/default filters. (Bug-9167 / NQ1R1-F1.)
                        _skip_reason = "persona_allow_list_live"
                    elif _rls_active and nq.shape == "projection":
                        # The security proof consumes the ORIGINAL deployed
                        # definition (the row-preserving star shape), not the
                        # expanded compile definition.
                        if projection_security_proof_holds(
                            definition_sql=nq.definition_sql,
                            manifest=artifact.row_manifest,
                            active_refresh_run_id=artifact.active_refresh_run_id,
                            security_columns=list(
                                getattr(
                                    _compiled_rls, "security_dimension_columns", ()
                                ) or ()
                            ),
                            user_mapping_active=bool(
                                getattr(_compiled_rls, "mapping_source_ids", ())
                            ),
                        ):
                            serve_materialised = True
                            _skip_reason = None
                    else:
                        serve_materialised = True
                        _skip_reason = None

    if not serve_materialised:
        # LIVE: dispatch the EXPANDED deployed definition over its canonical
        # SEMANTIC relation closure -- the SAME ``force_route="source"`` route
        # the refresh build materialises (NQ-2/Bug-9161), through the ONE
        # central live helper that builds a FRESH canonical ExecuteRequest.
        # The population is a function of the DEPLOYED DEFINITION alone; the
        # outer ``@name`` reference's shape, its gateway raw/source
        # classification, the caller's include_hidden/protocol/dialect, and
        # any caller force_route never influence it (Bug-9173/NQ2R1-F6). The
        # recursion is bounded because definitions
        # cannot reference ``@`` placeholders (create-time validation).
        # See docs/questions/questions_named-query-population.md.
        return await _execute_named_query_live(
            db,
            body,
            nq,
            _expanded_definition,
            persona=persona,
            principal=principal,
            user_identity=user_identity,
            tenant_id=tenant_id,
            skip_reason=_skip_reason,
        )

    # --- MATERIALISED: rewrite + serve the physical result table ---
    target = await db.get(DataTarget, artifact.target_id)
    if target is None or target.model_id != getattr(model, "id", None):
        return await _execute_named_query_live(
            db, body, nq, _expanded_definition,
            persona=persona, principal=principal, user_identity=user_identity,
            tenant_id=tenant_id, skip_reason="target_missing",
        )
    try:
        conn = await resolve_endpoint_connection(
            db, target, expected_project_id=model.project_id
        )
    except (CrossProjectConnectionError, ValueError):
        return await _execute_named_query_live(
            db, body, nq, _expanded_definition,
            persona=persona, principal=principal, user_identity=user_identity,
            tenant_id=tenant_id, skip_reason="cross_project_connection",
        )

    connector = normalize_connection_type(conn.connection_type)
    target_dialect = _dialect_from_connection_type(connector)
    schema = (artifact.target_schema or "").strip()
    table = (artifact.physical_table_name or "").strip()
    _rewritten = (
        quote_table_ref(connector, f"{schema}.{table}")
        if schema
        else quote_table_ref(connector, table)
    )
    _rewritten = f"SELECT * FROM {_rewritten}"
    if _rls_active and _compiled_rls is not None:
        # The SAME compiled predicate the source route would use, injected per
        # scan (pocket §5.1); injection only ever removes rows.
        #
        # R-001: ``_rewritten`` is ``SELECT * FROM <one materialised NQ table>``
        # — a single scan, never the SOURCE owner named in
        # ``security_column_owners``. Leaving the source owner populated would
        # fail the owner-not-scanned guard (Bug-8896) with a 403 (the same
        # single-materialised-scan exposure as the pocket/aggregate sites). A
        # bare predicate binds unambiguously on one scan, so suppress owners on
        # the injection copy ONLY; ``_compiled_rls`` keeps its owners for the
        # live source fallback (``security_compiled`` on the decision below).
        _rewritten = _inject_security_where(
            _rewritten, _without_security_owners(_compiled_rls),
            dialect=target_dialect,
            force_route=force_route,
        )

    _security_meta = list(_compiled_rls.applied_rules) if _compiled_rls else None
    decision = RouteDecision(
        route_type="named_query",
        rewritten_query=_rewritten,
        reason=(
            f"Named Query @{nq.name} served materialised "
            f"(status={artifact.status})"
        ),
        security_rules_applied=_security_meta,
        target_dialect=target_dialect,
        source_dialect=target_dialect,
        security_compiled=_compiled_rls if _rls_active else None,
        admitted_generation=generation_from(artifact),
    )

    try:
        _generation = await assert_named_query_route_admissible(
            db,
            named_query_id=_nq_id,
            artifact_id=artifact.id,
            decision=decision,
            model=model,
            target=target,
            conn=conn,
            admitted_definition_sql=nq.definition_sql,
            security_compiled=_compiled_rls if _rls_active else None,
            expected_population_fingerprint=_expected_population_fingerprint,
        )
        rows, bytes_processed, columns = await execute_on_connection(
            _rewritten, conn, db
        )
        await assert_named_query_generation_unchanged(
            _generation,
            await read_named_query_generation(db, artifact.id),
            artifact_id=artifact.id,
        )
    except (ArtifactGenerationChangedError, NamedQueryGenerationChangedError) as exc:
        # The artifact moved underneath us — discard the rows and fall back to
        # live under the consumer's security context (never a wrong number).
        logger.warning(
            "Named Query @%s materialised serving refused at execution time "
            "(model=%s): %s", nq.name, getattr(model, "slug", None), exc,
        )
        return await _execute_named_query_live(
            db, body, nq, _expanded_definition,
            persona=persona, principal=principal, user_identity=user_identity,
            tenant_id=tenant_id, skip_reason="generation_changed",
        )
    except Exception as exc:
        if _is_missing_relation_error(exc):
            return await _execute_named_query_live(
                db, body, nq, _expanded_definition,
                persona=persona, principal=principal,
                user_identity=user_identity,
                tenant_id=tenant_id, skip_reason="missing_table",
            )
        raise

    # Honour the caller's row cap with honest truncation (same N+1 contract as
    # $KPIs and the ordinary route).
    _truncated = False
    if server_row_cap is not None:
        _truncated = len(rows) > server_row_cap
        if _truncated:
            rows = rows[:server_row_cap]
    elif logical_query.limit is not None and len(rows) > logical_query.limit:
        rows = rows[: logical_query.limit]

    elapsed_ms = int((time.monotonic() - start_ms) * 1000)

    # Observation: QueryLog + metrics + audit, exactly like $KPIs does for its
    # virtual table (a minimal BoundQuery + a tagged RouteDecision).
    model_result = await db.execute(
        select(Model)
        .where(Model.id == body.model_id)
        .options(selectinload(Model.project))
    )
    obs_model = model_result.scalar_one_or_none()
    if obs_model is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model {body.model_id} not found.",
        )
    bound = BoundQuery(
        logical_query=logical_query,
        model=obs_model,
        resolved_measures=[],
        resolved_dimensions=[],
        resolved_filters=[],
    )
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
        log_miss=False,
    )

    return ExecuteResponse(
        rows=rows,
        columns=columns,
        truncated=_truncated,
        row_limit=server_row_cap,
        route_type="named_query",
        reason=decision.reason,
        aggregate_id=None,
        pocket_id=None,
        execution_ms=elapsed_ms,
        bytes_processed=bytes_processed,
        rows_returned=len(rows),
        routed_sql=_rewritten,
        security_rules_applied=_security_rule_ids(decision),
    )


async def _execute_named_query_live(
    db: AsyncSession,
    body: ExecuteRequest,
    nq,
    expanded_definition: str,
    *,
    persona: Any | None,
    principal: Any | None,
    user_identity: str,
    tenant_id: str,
    skip_reason: str,
) -> ExecuteResponse:
    """The ONE live path: re-dispatch the EXPANDED deployed definition through
    the ordinary pipeline over its canonical SEMANTIC closure.

    NQ-2/Bug-9161: the body is built FRESH from the canonical population
    contract (``shared/named_query/population_contract``) —
    force_route="source", protocol="jdbc", dialect="postgres",
    include_hidden=False, no session vars, no caption dimensions — NEVER a
    ``model_copy`` of the outer ``@name`` request, so the caller's
    include_hidden/protocol/dialect/session_vars/caption_dimensions can never
    change the compiled population (Bug-9173/NQ2R1-F6) and the compiled input
    is byte-identical to the refresh build's /explain body. Only model_id,
    persona_id, client_kind and row_limit carry over from the caller. The
    result MUST be the source route (load-bearing invariant): the canonical
    population is the definition-scoped source closure, identical to what the
    build materialised.
    """
    _live_body = ExecuteRequest(
        model_id=body.model_id,
        raw_query=expanded_definition,
        protocol=NQ_CANONICAL_PROTOCOL,
        dialect=NQ_CANONICAL_DIALECT,
        include_hidden=NQ_CANONICAL_INCLUDE_HIDDEN,
        force_route=NQ_CANONICAL_FORCE_ROUTE,
        persona_id=body.persona_id,
        client_kind=body.client_kind,
        row_limit=body.row_limit,
        session_vars=None,
        caption_dimensions=None,
    )
    _response = await _handle_execute(
        _live_body,
        db,
        user_identity,
        principal=principal,
        persona_id=persona.id if persona is not None else None,
        persona=persona,
        tenant_id=tenant_id,
    )
    if _response.route_type != NQ_CANONICAL_FORCE_ROUTE:
        # Load-bearing: the canonical population IS the source route. Any
        # other route (aggregate/pocket/raw) would mean the live population
        # and the materialised population are no longer identical by
        # construction — never a wrong number served silently. NQ2C-F8: raise
        # a TYPED, non-disclosing 422 (mirroring the /named-query
        # fail-closed pattern) so the gateway can attribute the error; the
        # specifics stay in the service log. Fail-closed: never converted to
        # a live fallback.
        logger.warning(
            "Named Query @%s live compile returned route_type=%r; "
            "force_route is pinned to %r — load-bearing invariant broken",
            nq.name, _response.route_type, NQ_CANONICAL_FORCE_ROUTE,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": (
                    "The Named Query's canonical live compile did not take "
                    "the expected source route, so its result could not be "
                    "verified against the deployed population. The query "
                    "was blocked (fail closed); contact support if this "
                    "persists."
                ),
                "error_type": "named_query_route_invariant",
            },
        )
    _response.reason = (
        f"Named Query @{nq.name} served live ({skip_reason})" + (
            f": {_response.reason}" if getattr(_response, "reason", "") else ""
        )
    )
    return _response


async def _handle_kpi_table_query(
    db: AsyncSession,
    model_id: str,
    logical_query: LogicalQuery,
    *,
    persona: Any | None = None,
    principal: Any | None = None,
    user_identity: str = "",
    tenant_id: str = "",
    client_kind: Optional[str] = None,
    server_row_cap: Optional[int] = None,
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

    Bug-6139 — the gate ALSO enforces COLUMN-level security (CLS): a KPI whose
    measure lineage reaches a persona-tag-restricted column is withheld, so a
    restricted-column-derived scorecard value (e.g. a distinct count of a
    masked id) is not served via ``$KPIs`` after being blocked on the base
    table.

    Bug-6930 — ROW-level security. ``kpi_latest`` holds a value already
    aggregated across ALL rows, so a row-restricted principal viewing a
    global-total KPI would otherwise see rows their row-security rules exclude.
    Because the pre-aggregated value cannot be re-filtered per-row at serve time,
    the fail-closed rule is: when the principal has ANY active row-security rule
    on this model AND the persona does not carry an authorised
    ``bypass_row_security``, WITHHOLD EVERY KPI row (serve an empty scorecard).
    Selective per-KPI serving would require a proven RLS-safe lineage recompute;
    until that exists, withholding all is the only sound fail-closed behaviour.
    An unrestricted principal (no active rules) is unaffected.
    """
    from shared.db.models import KPI, KPILatest

    start_ms = time.monotonic()

    # Bug-6930: fail closed on active row-level security. ``kpi_latest`` holds a
    # value pre-aggregated across ALL rows, so a row-restricted principal must not
    # read it. When the principal has any active row-security rule on this model
    # (and the persona carries no authorised bypass), withhold EVERY KPI row — the
    # global value cannot be re-filtered per-row at serve time. The empty
    # scorecard still flows through the normal observation/audit tail below.
    withhold_all_rls = False
    # Bug-8449: the rule ids behind a withhold, surfaced on the response so a
    # $KPIs consumer can tell "your row-security policy withheld the scorecard"
    # from "this model has no deployed KPIs". Empty when nothing was withheld.
    _kpi_security_rule_ids: list[str] = []
    if principal is not None:
        try:
            _kpi_rls = await compile_row_security(model_id, principal, db)
        except RowSecurityCompileError:
            # Cannot evaluate the principal's row security — fail closed.
            withhold_all_rls = True
            # The compile failed, so no rule id is knowable; report the
            # fail-closed sentinel rather than an empty (= "unrestricted") list.
            _kpi_security_rule_ids = [DENY_ALL_RULE_ID]
        else:
            _rls_bypass = bool(
                persona is not None and getattr(persona, "bypass_row_security", False)
            )
            withhold_all_rls = has_active_rules(_kpi_rls) and not _rls_bypass
            if withhold_all_rls:
                # Report the rule ids that caused the withhold. Read defensively:
                # this field is DIAGNOSTIC and must never be able to break the
                # fail-closed withhold itself. When the ids cannot be enumerated,
                # fall back to the sentinel so the list is non-empty — an empty
                # list is the contract's "no policy applied", which would be a
                # lie here.
                _kpi_security_rule_ids = [
                    str(rid)
                    for rid in (getattr(_kpi_rls, "active_rule_ids", None) or ())
                    if rid
                ] or [DENY_ALL_RULE_ID]
        if withhold_all_rls:
            logger.info(
                "Bug-6930: withholding all $KPIs rows for row-restricted principal "
                "user=%s model=%s (kpi_latest is pre-aggregated global data).",
                user_identity, model_id,
            )
    # F-017-05: $KPIs only exposes deployed KPIs. kpi_latest is upserted for
    # every evaluated KPI (incl. undeployed drafts opened in the scorecard), so
    # join to the KPI row and filter on is_deployed — an undeployed KPI is
    # absent from the JDBC $KPIs virtual table while remaining editable in the
    # builder.
    if withhold_all_rls:
        # Bug-6930: a row-restricted principal gets an empty scorecard. Skip the
        # kpi_latest fetch and the persona/CLS gating entirely; the observation
        # tail below still records the (zero-row) $KPIs read for audit.
        kpi_pairs: list = []
    else:
        # F-017-01 (Fable R1): also require the MODEL to be deployed. After
        # undeploy, deployed_version_id is NULL but KPI.is_deployed remains True
        # and kpi_latest rows survive — without this join predicate those
        # frozen-stale rows would be served indefinitely.
        result = await db.execute(
            select(KPILatest, KPI)
            .join(KPI, KPI.id == KPILatest.kpi_id)
            .join(Model, Model.id == KPI.model_id)
            .where(KPILatest.model_id == model_id)
            .where(KPI.is_deployed.is_(True))
            .where(Model.deployed_version_id.is_not(None))
            # Bug-7982 residual 2: serve a cached value ONLY when it was evaluated
            # for the model's CURRENT deploy epoch. A definition-changing revert
            # bumps ``deploy_epoch`` (Bug-7140), so a stale value computed under the
            # old definition no longer matches and is withheld (fail-closed) until
            # the next evaluation repopulates kpi_latest under the new epoch —
            # never a mixed-version wrong number. A NULL epoch (legacy/unstamped
            # row) also fails this predicate, which is the intended fail-closed.
            .where(KPILatest.evaluated_for_epoch == Model.deploy_epoch)
        )
        kpi_pairs = list(result.all())

    # Bug-3613: resolve the persona measure allow-list once, then gate each
    # KPI on its measure lineage. measure_name_to_id is only loaded when a
    # restriction is in force (empty allow-list = unrestricted = no lookup).
    allowed_measure_ids = _parse_persona_allowed_measure_ids(persona)
    # Bug-6139: CLS column restrictions must also gate $KPIs — a KPI whose
    # measure lineage reaches a persona-restricted column is withheld.
    # Bug-6930: when withholding all rows for RLS there is nothing to gate, so
    # skip the CLS closure resolution too.
    cls_blocked_measure_ids = (
        frozenset() if withhold_all_rls
        else await _kpi_cls_blocked_measure_ids(db, model_id, persona)
    )
    measure_name_to_id: dict[str, str] = {}
    kpi_by_name: dict[str, Any] = {}
    children_by_parent: dict[str, list[Any]] = {}
    if not withhold_all_rls and (allowed_measure_ids is not None or cls_blocked_measure_ids):
        meas_result = await db.execute(
            select(Measure.name, Measure.id).where(Measure.model_id == model_id)
        )
        measure_name_to_id = {name: str(mid) for name, mid in meas_result.all()}
        # Bug-6139: nested ``kpi()`` lineage may reference ANY KPI on the model,
        # including undeployed drafts, so build the name map from ALL KPIs (not
        # only the deployed rows served here). Exact and lower-cased keys mirror
        # the DSL's case handling; an unresolvable nested name fails closed.
        all_kpis = (
            await db.execute(select(KPI).where(KPI.model_id == model_id))
        ).scalars().all()
        for k in all_kpis:
            if k.name:
                kpi_by_name.setdefault(k.name, k)
                kpi_by_name.setdefault(k.name.lower(), k)
            # Bug-6139 R1: composite KPIs derive their served value from children
            # bound via parent_kpi_id (not their own expression), so index the
            # children so the lineage gate can fold in each child's lineage.
            pkid = getattr(k, "parent_kpi_id", None)
            if pkid is not None:
                children_by_parent.setdefault(str(pkid), []).append(k)

    # Bug-8305: $KPIs presentational metadata (the served kpi_name) must be
    # DEPLOYED-SNAPSHOT-AUTHORITATIVE, mirroring the deploy-pinning contract the
    # value already follows. ``kpi_latest.kpi_name`` is upserted from the LIVE
    # KPI row (kpi.name) on every evaluation — including draft evaluations opened
    # in the scorecard — so an undeployed KPI RENAME would surface in $KPIs before
    # redeploy. Source the name from the model's deployed version snapshot
    # (``snapshot_json["kpis"]``, frozen at deploy) keyed by kpi_id; fall back to
    # the live/latest name only when the snapshot has no entry for that KPI (e.g.
    # a legacy snapshot predating KPI serialisation). NOTE (scope_request): the
    # numeric ``formatted_value`` is likewise eval-time (live format token), but
    # re-formatting the deployed value requires the KPI value formatter, which
    # lives in model-service (not shared/), so it cannot be re-derived in the
    # query-router without either duplicating that formatter (band-aid) or
    # promoting it to shared/. That half is filed as a scope_request; this fix
    # closes the rename leak, the primary governance concern.
    _deployed_kpi_name_by_id: dict[str, str] = {}
    # Resolve the deployed version id from the model directly (the KPI join row
    # carries the KPI, not the Model). Load the model once. Best-effort: on any
    # load failure fall back to ``kpi_latest.kpi_name`` (the prior behaviour) —
    # this is presentational metadata (governance/cosmetic), never a value, so a
    # transient snapshot-load failure must not fail the scorecard read.
    if kpi_pairs:
        try:
            _mdl = await db.get(Model, model_id)
            _dvid = getattr(_mdl, "deployed_version_id", None) if _mdl is not None else None
            if _dvid is not None:
                _ver = await db.get(ModelVersion, _dvid)
                _snap = getattr(_ver, "snapshot_json", None) if _ver is not None else None
                if isinstance(_snap, dict):
                    for _sk in _snap.get("kpis", []) or []:
                        _skid = _sk.get("id")
                        _skname = _sk.get("name")
                        if _skid and _skname:
                            _deployed_kpi_name_by_id[str(_skid)] = _skname
        except Exception:
            # Fall back to the eval-time name for every KPI (prior behaviour).
            _deployed_kpi_name_by_id = {}

    rows: list[dict[str, Any]] = []
    for kpi_latest, kpi in kpi_pairs:
        if not _kpi_allowed_by_persona(
            kpi, allowed_measure_ids, measure_name_to_id, cls_blocked_measure_ids,
            kpi_by_name, children_by_parent,
        ):
            # Fail-closed: the persona cannot see this KPI's underlying
            # measure(s), or its lineage reaches a restricted column; withhold
            # the row entirely.
            continue
        _authoritative_kpi_name = _deployed_kpi_name_by_id.get(
            str(kpi_latest.kpi_id), kpi_latest.kpi_name
        )
        rows.append({
            "kpi_name": _authoritative_kpi_name,
            "value": float(kpi_latest.value) if kpi_latest.value is not None else None,
            "target": float(kpi_latest.target) if kpi_latest.target is not None else None,
            "status": kpi_latest.status,
            "status_label": kpi_latest.status_label,
            "trend_pct": float(kpi_latest.trend_pct) if kpi_latest.trend_pct is not None else None,
            "formatted_value": kpi_latest.formatted_value,
            "evaluated_at": kpi_latest.evaluated_at.isoformat() if kpi_latest.evaluated_at else None,
        })

    # Bug-3613 + Bug-7998 / F-027-02: honour the caller's row_limit with
    # honest truncation reporting. When a server_row_cap is active,
    # logical_query.limit is the probe value (cap+1) — trim to the REAL cap
    # and report truncation so the caller never presents a capped $KPIs
    # extract as complete (same N+1 contract as the normal route).
    _kpi_truncated = False
    if server_row_cap is not None:
        _kpi_truncated = len(rows) > server_row_cap
        if _kpi_truncated:
            rows = rows[:server_row_cap]
    elif logical_query.limit is not None and len(rows) > logical_query.limit:
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
        truncated=_kpi_truncated,
        row_limit=server_row_cap,
        routed_sql=routed_sql,
        security_rules_applied=_kpi_security_rule_ids,
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

# Bug-8285: the companion caption-column suffix. MUST stay byte-identical to the
# gateway consumer's ``mdx_execute._MEMBER_CAPTION_SUFFIX`` — the gateway builds
# the expected column name as ``f"{dim}{suffix}"`` and reads it off each result
# row; if the two drift, the projected caption column is never matched and the
# pivot silently falls back to raw keys.
_EXECUTE_CAPTION_SUFFIX = "__caption"


async def _augment_execute_with_caption_columns(
    bound: Any,
    db: AsyncSession,
    caption_dimensions: list[str] | None,
) -> list[str]:
    """Bug-8285: project each requested dimension's DISPLAY column into the bound
    execute query as a companion resolved column ``<dim>__caption`` so the XMLA
    Execute axis renders friendly member captions instead of raw keys.

    This is the PRODUCER half of the caption contract. The gateway
    (``xmla_server._mdx_to_sql``) emits ``SELECT "<dim>", <aggs> ... GROUP BY
    "<dim>"`` with NO display column — a raw unresolved projection would break the
    binder — so the display column is added here, AFTER binding, exactly the way
    ``_augment_discover_with_display_column`` (Bug-5434) does it for the flat
    member-discovery path, but for every axis dimension of the multi-dimension
    GROUP BY execute path:

      - a synthetic resolved dimension whose ``source_column_id`` is the display
        column, named ``<dim>__caption`` so the source rewriter emits it into
        SELECT (as a standalone item);
      - a passthrough ``SelectExpression`` aliased ``<dim>__caption`` so the
        rewriter treats it as a standalone SELECT item (dimensions are otherwise
        GROUP-BY-only);
      - the alias appended to ``logical_query.grain`` so the display column ALSO
        lands in GROUP BY. This is mandatory on the execute path (unlike the
        DISTINCT discover path, which emits no GROUP BY): the source rewriter
        builds GROUP BY strictly from ``grain`` (``source_sql`` grain_group_exprs),
        so a display column added to SELECT but NOT to grain would be an ungrouped,
        non-aggregated column under a GROUP BY — a hard source-DB error (42803),
        turning a working pivot into a fault. The display column is 1:1 with the
        key, so grouping by (key, display) yields the same rows as grouping by the
        key alone — no row multiplication.

    Because the display column enters ``grain`` (which also drives aggregate
    matching), a captioned pivot deterministically source-routes rather than
    matching an aggregate that lacks the display column. That is a performance
    trade-off, not a correctness one (source returns correct rows); tracked
    separately for optimisation.

    The consumer (``mdx_execute._normalize_member_captions``) reads the
    ``<dim>__caption`` column per row to emit UName=key, Caption=display.

    Engine-safe: only the bound query (data) is mutated; the binder, router and
    rewriter engines are untouched. Returns the list of caption aliases added
    (for logging / tests). A dimension is skipped silently when it is not
    resolved, is not part of the query's GROUP BY grain (so a grouped caption
    cannot be projected without changing results / faulting), declares no display
    column, or whose display column cannot be loaded.
    """
    added: list[str] = []
    if not caption_dimensions or not getattr(bound, "resolved_dimensions", None):
        return added
    lq = bound.logical_query
    grain = list(getattr(lq, "grain", None) or [])
    # The execute pivot path always carries a GROUP BY (it aggregates measures),
    # so grain is non-empty. Only caption a dimension that is actually in that
    # GROUP BY — projecting a display column for an ungrouped dimension would
    # fault (ungrouped column) or multiply rows. When there is no GROUP BY at all
    # (grain empty) there is nothing to add a companion grouped column to.
    if not grain:
        return added
    grain_set = set(grain)
    wanted = {str(d) for d in caption_dimensions}
    # Snapshot the original resolved dims — never iterate while appending.
    originals = list(bound.resolved_dimensions)
    seen_aliases: set[str] = set()
    for key_dim in originals:
        name = getattr(key_dim, "name", None)
        if name is None or str(name) not in wanted:
            continue
        if str(name) not in grain_set:
            continue
        display_column_id = getattr(key_dim, "display_column_id", None)
        if display_column_id is None:
            continue
        disp_col = await db.get(ModelColumn, display_column_id)
        if disp_col is None:
            continue
        alias = f"{name}{_EXECUTE_CAPTION_SUFFIX}"
        if alias in seen_aliases or alias in grain_set:
            continue
        seen_aliases.add(alias)
        display_dim = SimpleNamespace(
            id=None,
            name=alias,
            source_column_id=display_column_id,
            user_defined_attribute_id=None,
            calc_expression=None,
            is_invalid=False,
        )
        bound.resolved_dimensions.append(display_dim)
        lq.select_expressions.append(
            SelectExpression(
                raw_text=alias,
                alias=None,
                classification="passthrough",
                agg_function=None,
                inner_column=alias,
                inner_literal=None,
            )
        )
        grain.append(alias)
        grain_set.add(alias)
        added.append(alias)
    # Write the extended grain back so GROUP BY (built from grain) includes the
    # display columns. Reassigned (not just mutated) so the change is visible even
    # if grain was an immutable/foreign sequence.
    if added:
        lq.grain = grain
    return added


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
    except DeployedSnapshotUnavailableError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e),
        )
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
        # Member discovery routes like a normal SELECT DISTINCT <dim>: no
        # force_route. This lets a covering aggregate serve the member list
        # (a full GROUP BY over the fact carries every distinct value, so the
        # list stays complete) far faster than scanning the fact joined to the
        # dimension — the previous force_route="source" stalled Excel pivot
        # refreshes on high-cardinality dimensions.
        #
        # Completeness is preserved by the router's own gates, not by a hint:
        #   - Pockets are filtered slices. Member discovery carries no filters,
        #     so the pocket matcher rejects every pocket whose predicate columns
        #     are not a subset of the (empty) query filter set; only an
        #     unfiltered, complete pocket can match, and its DISTINCT is the full
        #     set. A filtered pocket can never truncate the member list.
        #   - Aggregates have no build-time WHERE predicate, so DISTINCT over a
        #     covering aggregate equals DISTINCT over the source fact.
        #   - Bug-8018/Bug-8393: a principal with active row-security is NO LONGER
        #     forced to source here. `_route_with_row_security` attempts an
        #     RLS-safe pocket first, and it may serve one — but only after proving
        #     the pocket is a row-preserving `SELECT *` whose own row_manifest
        #     shows every security column materialised, with the same compiled
        #     predicate injected per scan. The member list is therefore still
        #     narrowed exactly as it would be on the source, so this is not a
        #     bypass; and the cache generation is re-proved at execution time
        #     (Bug-8392 for pockets, Bug-8457 for aggregates), which is why this
        #     function handles ArtifactGenerationChangedError below.
        decision = await route_query(
            bound, db, principal=principal, persona=persona,
        )
    except RowSecurityCompileError as e:
        # F-007-04: member discovery routes through route_query too, so a
        # misconfigured rule must fail closed here with a typed error rather
        # than a generic 500.
        # Bug-8809: identifier-free body; specifics go to the service log.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=row_security_misconfigured_detail(
                e, surface="/discover/members",
            ),
        )
    except DeployedSnapshotUnavailableError as e:
        # Bug-8515 (shared-primitive sweep): member discovery routes through
        # ``route_query`` too, and it is an XMLA/BI-client surface — a 422 here
        # tells Excel the member request was malformed when the deployment is
        # unusable. Mirrors the bind-stage 503 a few lines above. MUST precede
        # ``except ValueError``.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e),
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
    except ArtifactGenerationChangedError as exc:
        # Bug-8392 (pocket) / Bug-8457 (aggregate): the cache artifact was
        # refreshed between admission and scan, so its member list may be a
        # different slice than the one proved to cover this query. Discard and
        # re-run the member discovery against the source — the same recovery
        # ``execute_with_observation`` applies, expressed here by forcing the
        # source route (member discovery has no cache-miss leg). Catches the
        # BASE error so the aggregate sibling takes the identical recovery
        # rather than escaping as a 502.
        logger.warning(
            "Bug-8392/Bug-8457: cache artifact generation changed during member "
            "discovery (%s); re-routing to source", exc,
        )
        try:
            decision = await route_query(
                bound, db, principal=principal, persona=persona,
                force_route="source",
            )
        except HTTPException:
            raise
        except DeployedSnapshotUnavailableError as snapshot_exc:
            # Bug-8515 (shared-primitive sweep): the Bug-8392 source re-route
            # re-enters ``route_query``, so it can fail closed on an unusable
            # deployed snapshot. Keep the Bug-7808 QueryLog contract but with
            # the typed 503 instead of the generic 502. MUST precede
            # ``except Exception``.
            logger.error(
                "Bug-8515: member-discovery source re-route hit an unusable "
                "deployed snapshot: %s", snapshot_exc,
            )
            await _log_query_failure(
                db, current_user.email, tenant_id, bound, decision,
                start_ms, "snapshot_unavailable", str(snapshot_exc),
                persona_id=persona_uuid,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(snapshot_exc),
            )
        except Exception as reroute_exc:
            # Bug-7808 discipline: a failure in the fallback re-route must still
            # produce a QueryLog row and a sanitized error, not a bare 500 with
            # no observability.
            logger.error(
                "Bug-8392: source re-route failed after a pocket generation "
                "change: %s", reroute_exc, exc_info=True,
            )
            await _log_query_failure(
                db, current_user.email, tenant_id, bound, decision,
                start_ms, "routing_error", str(reroute_exc),
                persona_id=persona_uuid,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    "Failed to query members: "
                    f"{sanitize_error_for_client(reroute_exc)}"
                ),
            )
        try:
            audit_filters_present(
                bound, decision.rewritten_query, decision.route_type,
                filter_anchors=filter_anchors,
            )
        except SecurityAuditError as sa_exc:
            raise await _security_audit_block(
                db, current_user.email, tenant_id, bound, decision, start_ms,
                sa_exc, audit_layer="filter_presence", persona_id=persona_uuid,
            )
        try:
            rows, bytes_processed, columns, _ = await execute_routed_query(
                bound, decision, db
            )
        except HTTPException:
            raise
        except Exception as retry_exc:
            logger.error(
                "Failed to query members after pocket re-route: %s",
                retry_exc, exc_info=True,
            )
            await _log_query_failure(
                db, current_user.email, tenant_id, bound, decision,
                start_ms, "execution_error", str(retry_exc),
                persona_id=persona_uuid,
            )
            raise HTTPException(
                status_code=502,
                detail=(
                    "Failed to query members: "
                    f"{sanitize_error_for_client(retry_exc)}"
                ),
            )
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

    return DiscoverMembersResponse(
        members=members,
        levels=levels,
        security_rules_applied=_security_rule_ids(decision),
    )


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
        # Bug-8457 (the aggregate sibling of Bug-8392): an aggregate table is
        # rebuilt in place, so this route names a table, not the GENERATION of
        # it the router proved admissible. Re-prove against live state here,
        # stamp the generation, scan, and re-stamp; a change across the scan
        # discards the rows and falls back to source (handled by the caller).
        # This branch previously had NEITHER half while the pocket branch below
        # had both — the shared-primitive gap CLAUDE.md's discipline exists to
        # catch. See ``routing/artifact_generation_guard``.
        _agg_generation = await assert_aggregate_route_admissible(
            db, bound=bound, decision=decision,
            # The target + connection this scan will actually run on, so the
            # guard can prove the aggregate was BUILT on that same storage.
            # Already resolved above — the check costs no extra query.
            target=target, conn=conn,
        )
        rows, bytes_processed, columns = await execute_on_connection(
            decision.rewritten_query, conn, db
        )
        assert_aggregate_generation_unchanged(
            _agg_generation,
            await read_aggregate_generation(db, decision.aggregate_id),
            aggregate_id=decision.aggregate_id,
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
        # Bug-8392 (TOCTOU): a pocket table is reused in place across refreshes,
        # so the route decision names a table, not the GENERATION of it that the
        # router proved admissible. Re-prove against live state here, stamp the
        # generation, scan, and re-stamp; a change across the scan discards the
        # rows and falls back to source (handled by the caller). See
        # ``routing/pocket_generation_guard`` for why both halves are required.
        _pkt_generation = await assert_pocket_route_admissible(
            db, bound=bound, decision=decision,
            # Bug-8473: the target + connection this scan will actually run on,
            # so the guard can prove the pocket was BUILT on that same storage.
            # Already resolved above — the check costs no extra query.
            target=target, conn=conn,
        )
        rows, bytes_processed, columns = await execute_on_connection(
            decision.rewritten_query, conn, db
        )
        assert_generation_unchanged(
            _pkt_generation,
            await read_pocket_generation(db, decision.pocket_id),
            pocket_id=decision.pocket_id,
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
