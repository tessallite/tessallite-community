"""Auto-split from pydantic_models.py — Data Quality Rules, Query Logs, Lineage, Metrics, Model Export/Import, RBAC — User Access Bindings, AI Optimiser schemas, Glossary (semantic-layer Phase 3), Audit events, Schema drift, Downstream Assets (Impact Analysis — Phase 9 Block G), Data Tagging + Column-Level Persona Security (Phase 9 Block H), Notification routes, Named Sets, KPIs, Version History, Saved Queries"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, Optional, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..measure_formats import (
    HIERARCHY_TIME_CALCS as _HIERARCHY_TIME_CALCS,
    HIERARCHY_TIME_UNITS as _HIERARCHY_TIME_UNITS,
    MEASURE_FORMAT_TOKENS as _MEASURE_FORMAT_TOKENS,
    TIME_VARIANT_NAMES as _TIME_VARIANT_NAMES,
)

from ._base import OrmBase
# Bug-8259: the LLM provider ``config`` bag is plaintext JSONB echoed back to
# any project viewer, exactly like the connection ``config`` bag (F-014-01 /
# Bug-7983). Reuse that gate rather than growing a second key policy.
from .tenants_projects import _validate_non_sensitive_config

# Bug-6586(2)/Bug-6264: certification_status is a controlled enum (NOT NULL,
# DB default "draft"), gated to this closed set by the KPI and Named-Set write
# handlers (_ALLOWED_CERTIFICATION_STATUSES in api/kpis.py + api/named_sets.py).
# Typed as a Literal here so the enum is enforced at the schema boundary for
# every consumer (raw-API callers, importers, snapshot rehydration) rather than
# only in those two handlers. "shared" != "certified" (sharing does not imply
# certification — the gateway XMLA catalogue must not mark shared as certified).
CertificationStatus = Literal["draft", "shared", "certified", "deprecated"]
_CERTIFICATION_STATUSES: tuple[str, ...] = get_args(CertificationStatus)


def _coerce_certification_status_for_read(value):
    """Fail-safe read coercion for Response models.

    New writes are gated to the enum on the Create/Update surface, but a row
    persisted before the enum gate existed could carry an out-of-set value. On
    read, coerce any unknown value to the safe default "draft" rather than
    letting one legacy row raise ValidationError and 500 an entire list
    response. "draft" is the least-privileged status, so this never
    over-claims certification.
    """
    if value not in _CERTIFICATION_STATUSES:
        return "draft"
    return value


def _validate_cron(value: str | None) -> str | None:
    """Validate a cron expression. None/empty passes through unchanged.

    Delegates to the single import-safe cron gate in aggregates_security.py so
    there is one cron validator (Bug-6588): ``croniter`` is a transitive dep of
    the shared package and may be absent in a built service image, so a hard
    ``import croniter`` here would 500 the request. The shared validator uses a
    structural fallback when croniter is not importable.
    """
    from .aggregates_security import _validate_cron_expression

    return _validate_cron_expression(value)

# ---------------------------------------------------------------------------
# Data Quality Rules
# ---------------------------------------------------------------------------

_DQ_RULE_TYPES = frozenset({"not_null", "unique", "range", "regex", "custom_sql"})
_DQ_TARGET_TYPES = frozenset({"dimension", "measure", "column"})
_DQ_SEVERITIES = frozenset({"info", "warn", "error"})


class DataQualityRuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    target_type: str
    target_id: uuid.UUID
    rule_type: str
    rule_config: Optional[dict] = None
    severity: str = "warn"
    is_enabled: bool = True
    block_on_failure: bool = False

    @field_validator("target_type")
    @classmethod
    def _validate_target_type(cls, v: str) -> str:
        if v not in _DQ_TARGET_TYPES:
            raise ValueError(f"target_type must be one of {sorted(_DQ_TARGET_TYPES)}")
        return v

    @field_validator("rule_type")
    @classmethod
    def _validate_rule_type(cls, v: str) -> str:
        if v not in _DQ_RULE_TYPES:
            raise ValueError(f"rule_type must be one of {sorted(_DQ_RULE_TYPES)}")
        return v

    @field_validator("severity")
    @classmethod
    def _validate_severity(cls, v: str) -> str:
        if v not in _DQ_SEVERITIES:
            raise ValueError(f"severity must be one of {sorted(_DQ_SEVERITIES)}")
        return v


class DataQualityRuleUpdate(BaseModel):
    name: Optional[str] = None
    rule_config: Optional[dict] = None
    severity: Optional[str] = None
    is_enabled: Optional[bool] = None
    block_on_failure: Optional[bool] = None

    @field_validator("severity")
    @classmethod
    def _validate_severity(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in _DQ_SEVERITIES:
            raise ValueError(f"severity must be one of {sorted(_DQ_SEVERITIES)}")
        return v


class DataQualityRuleResponse(OrmBase):
    id: uuid.UUID
    model_id: uuid.UUID
    name: str
    target_type: str
    target_id: uuid.UUID
    rule_type: str
    rule_config: Optional[dict] = None
    severity: str
    is_enabled: bool
    block_on_failure: bool
    last_checked_at: Optional[datetime] = None
    last_violation_count: Optional[int] = None
    created_at: datetime
    updated_at: datetime


class DataQualityViolationResponse(OrmBase):
    id: uuid.UUID
    rule_id: uuid.UUID
    detected_at: datetime
    violation_count: int
    sample_values: Optional[dict] = None
    aggregate_id: Optional[uuid.UUID] = None
    pocket_id: Optional[uuid.UUID] = None


class DataQualityValidateResponse(BaseModel):
    rules_checked: int
    violations_found: int
    rule_results: list[dict]


# ---------------------------------------------------------------------------
# Query Logs
# ---------------------------------------------------------------------------

class QueryLogResponse(OrmBase):
    id: uuid.UUID
    model_id: Optional[uuid.UUID]
    user_identity: Optional[str]
    protocol: str
    raw_query: str
    query_fingerprint: str
    route_type: str
    aggregate_id: Optional[uuid.UUID]
    pocket_id: Optional[uuid.UUID]
    # F-030-20: the row-security rules applied to this query were recorded on the
    # QueryLog row (models.py: security_rules_applied JSONB) but never surfaced,
    # so auditors could not see which rules fired without raw DB access. Exposed
    # here for the Diagnostics log detail dialog.
    security_rules_applied: Optional[Any] = None
    persona_id: Optional[uuid.UUID] = None
    client_kind: Optional[str] = None
    rewritten_query: Optional[str]
    execution_ms: Optional[int]
    rows_returned: Optional[int]
    bytes_processed: Optional[int]
    # Bug-6426: "cache_hit" means this row was re-served from the in-TTL result
    # cache — its route_type/execution_ms/bytes are the ORIGINAL route's, and
    # execution_ms=0 is a served-from-cache sentinel, not a measured latency.
    # Surfaced so the Diagnostics log viewer can distinguish a cache re-serve
    # from a real "aggregate / 0 ms" execution instead of showing them alike.
    cache_status: Optional[str] = None
    status: str = "success"
    error_type: Optional[str] = None
    error_detail: Optional[str] = None
    created_at: datetime


class PaginatedQueryLogResponse(BaseModel):
    items: list[QueryLogResponse]
    total: int
    page: int
    page_size: int


class QueryMissLogResponse(OrmBase):
    id: uuid.UUID
    model_id: Optional[uuid.UUID]
    query_fingerprint: str
    miss_reason: str
    normalized_query: Optional[str] = None
    requested_dimensions: Optional[list]
    requested_measures: Optional[list]
    requested_grain: Optional[list] = None
    occurrence_count: int
    first_seen_at: datetime
    last_seen_at: datetime
    candidate_aggregate_id: Optional[uuid.UUID]
    persona_id: Optional[uuid.UUID] = None


# F-004-11: ``RouteLogResponse`` was removed here. It was an orphaned schema —
# no API ever read route_logs (they are write-only, consumed only by the purge
# job and cascade delete), no router returned this model, and the frontend had
# zero references. The route-skip diagnostics it nominally surfaced are now
# delivered through the /explain pipeline trace (aggregate_skipped_reasons /
# pocket_skipped_reason in the router step) instead.


# ---------------------------------------------------------------------------
# Lineage
# ---------------------------------------------------------------------------

class LineageNode(BaseModel):
    id: str
    type: str  # source | semantic | aggregate | target | column | field
    label: str
    description: Optional[str] = None
    # Generic metadata bag rendered in tooltips. Keys/values are stringified
    # for safe JSON serialisation.
    meta: dict[str, str] = {}
    # Aggregate-specific fields
    creation_reason: Optional[str] = None  # manual | auto | ai
    status: Optional[str] = None
    last_refreshed_at: Optional[datetime] = None
    # Impact analysis
    downstream_asset_count: int = 0


class LineageEdge(BaseModel):
    source: str
    target: str
    label: Optional[str] = None


class LineageGraphResponse(BaseModel):
    nodes: list[LineageNode]
    edges: list[LineageEdge]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

class ModelMetricsResponse(BaseModel):
    query_count_24h: int
    aggregate_hit_rate: float
    avg_execution_ms: float
    estimated_bytes_avoided: int
    stale_aggregate_count: int
    active_aggregate_count: int
    refresh_failure_count_24h: int


# ---------------------------------------------------------------------------
# Model Export/Import
# ---------------------------------------------------------------------------

class HierarchyLevelAttributeExportResponse(BaseModel):
    id: uuid.UUID
    attribute_id: uuid.UUID
    attribute_source: str
    role: str


class HierarchyLevelExportResponse(BaseModel):
    id: uuid.UUID
    hierarchy_id: uuid.UUID
    name: str
    ordinal: int
    key_attribute_id: uuid.UUID
    key_attribute_source: str
    description: Optional[str]
    time_unit: Optional[str] = None
    allowed_time_calcs: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    attributes: list[HierarchyLevelAttributeExportResponse] = Field(default_factory=list)


class HierarchyExportResponse(BaseModel):
    id: uuid.UUID
    model_id: uuid.UUID
    name: str
    type: str
    dimension_kind: Optional[str] = None
    description: Optional[str]
    segment_config: Optional[dict[str, Any]]
    date_config: Optional[dict[str, Any]]
    created_at: datetime
    updated_at: datetime
    levels: list[HierarchyLevelExportResponse] = Field(default_factory=list)


class ModelExportResponse(BaseModel):
    model: ModelResponse
    sources: list[DataSourceResponse]
    targets: list[DataTargetResponse]
    dimensions: list[DimensionResponse]
    measures: list[MeasureResponse]
    joins: list[JoinResponse]
    aggregates: list[AggregateDefinitionResponse]
    hierarchies: list[HierarchyExportResponse]
    exported_at: datetime


class ModelImportRequest(BaseModel):
    measures: list[MeasureResponse] = Field(default_factory=list)
    hierarchies: list[HierarchyExportResponse] = Field(default_factory=list)
    replace_hierarchies: bool = True


class ModelImportResponse(BaseModel):
    imported_hierarchies: int
    imported_levels: int
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# RBAC — User Access Bindings
# ---------------------------------------------------------------------------

class UserAccessBindingCreate(BaseModel):
    user_identity: str = Field(
        description="User identifier matching JWT sub claim (email or user_id)"
    )
    role: str = Field(description="admin | modeler | viewer | model_viewer")
    model_id: Optional[uuid.UUID] = Field(
        None,
        description="Optional model-level scope; omit for project-level binding",
    )

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: str) -> str:
        """Bug-5735: validate role against the canonical project role set.

        The set now includes ``model_viewer`` (Bug-8101), a viewer-level
        read-only consumer role.
        """
        from shared.auth.roles import PROJECT_ROLE_SET
        if v not in PROJECT_ROLE_SET:
            raise ValueError(
                f"role must be one of {sorted(PROJECT_ROLE_SET)}, got {v!r}"
            )
        return v


class AccessSupersedePreflight(BaseModel):
    """Dry-run request for the Modeller/Model-viewer supersession check
    (Bug-8101). Mirrors the fields of a grant that matter to the invariant."""

    user_identity: str = Field(
        description="User identifier matching JWT sub claim (email or user_id)"
    )
    role: str = Field(description="admin | modeler | viewer | model_viewer")
    model_id: Optional[uuid.UUID] = Field(
        None,
        description="Optional model-level scope; omit for project-level binding",
    )

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: str) -> str:
        from shared.auth.roles import PROJECT_ROLE_SET
        if v not in PROJECT_ROLE_SET:
            raise ValueError(
                f"role must be one of {sorted(PROJECT_ROLE_SET)}, got {v!r}"
            )
        return v


class AccessSupersedePreflightResponse(BaseModel):
    """Whether granting the requested role would trigger the Modeller-supersedes
    -Model-viewer rule, so the admin UI can show a confirmation before the real
    grant."""

    supersedes: bool = Field(
        description="True when a Modeller/Model-viewer supersession will occur"
    )
    removed_model_viewer_count: int = Field(
        0,
        description="Model-viewer bindings that would be removed on confirm",
    )
    grant_is_redundant: bool = Field(
        False,
        description=(
            "True when the incoming model_viewer grant is fully covered by an "
            "existing overlapping Modeller binding and will not be persisted"
        ),
    )


class UserAccessBindingResponse(OrmBase):
    id: uuid.UUID
    project_id: Optional[uuid.UUID]
    model_id: Optional[uuid.UUID]
    user_identity: str
    role: str
    # Bug-6598: surface binding provenance so the access API can distinguish a
    # manually granted role from one materialised by SSO group sync
    # ("sso_group"). Defaults to "manual" for any legacy row/caller that does
    # not carry the column.
    source: str = "manual"
    created_at: datetime


# ---------------------------------------------------------------------------
# AI Optimiser schemas
# ---------------------------------------------------------------------------

class LLMProviderConfigCreate(BaseModel):
    provider: str
    display_name: str
    base_url: Optional[str] = None
    api_key: str
    model_name: str
    max_tokens: int = 4096
    temperature: float = 0.2
    timeout_seconds: int = 60
    config: dict = {}

    @field_validator("config")
    @classmethod
    def _check_config(cls, v: dict) -> dict:
        # Bug-8259: secrets must never enter the plaintext config bag. The
        # provider API key has its own Fernet-encrypted column.
        return _validate_non_sensitive_config(v)


class LLMProviderConfigUpdate(BaseModel):
    provider: Optional[str] = None
    display_name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model_name: Optional[str] = None
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    timeout_seconds: Optional[int] = None
    config: Optional[dict] = None

    @field_validator("config")
    @classmethod
    def _check_config(cls, v: Optional[dict]) -> Optional[dict]:
        # Bug-8259: same gate on PATCH; ``None`` means "field not being changed".
        return _validate_non_sensitive_config(v)


class LLMProviderConfigResponse(OrmBase):
    id: uuid.UUID
    project_id: uuid.UUID
    provider: str
    display_name: str
    base_url: Optional[str] = None
    model_name: str
    max_tokens: int
    temperature: float
    timeout_seconds: int
    config: dict = {}
    has_api_key: bool = False
    created_at: datetime
    updated_at: datetime


class LLMConnectionTestRequest(BaseModel):
    provider: str
    base_url: Optional[str] = None
    api_key: str
    model_name: str
    max_tokens: int = 4096
    temperature: float = 0.2
    timeout_seconds: int = 60
    config: dict = {}

    @field_validator("config")
    @classmethod
    def _check_config(cls, v: dict) -> dict:
        # Bug-8259: the ad-hoc test bag is not persisted, but it IS forwarded to
        # the optimizer service, so the same key policy applies — mirrors
        # ``ConnectionTestRequest`` on the connection side.
        return _validate_non_sensitive_config(v)


class LLMConnectionTestResponse(BaseModel):
    success: bool
    message: str
    latency_ms: Optional[float] = None


class ModelAISchedulerConfigCreate(BaseModel):
    ai_enabled: bool = False
    cron_expression: str = "0 5 * * *"
    lookback_hours: int = Field(default=168, ge=1, le=8760)
    max_creates_per_run: int = Field(default=3, ge=0, le=100)
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    dry_run: bool = False
    enable_ai_aggregation: bool = True
    llm_config_id: Optional[uuid.UUID] = None
    glossary_llm_config_id: Optional[uuid.UUID] = None

    @field_validator("cron_expression")
    @classmethod
    def _check_cron(cls, v: str) -> str:
        result = _validate_cron(v)
        if result is None:
            raise ValueError("cron_expression must not be empty")
        return result


class ModelAISchedulerConfigUpdate(BaseModel):
    ai_enabled: Optional[bool] = None
    cron_expression: Optional[str] = None
    lookback_hours: Optional[int] = Field(default=None, ge=1, le=8760)
    max_creates_per_run: Optional[int] = Field(default=None, ge=0, le=100)
    min_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    dry_run: Optional[bool] = None
    enable_ai_aggregation: Optional[bool] = None
    llm_config_id: Optional[uuid.UUID] = None
    glossary_llm_config_id: Optional[uuid.UUID] = None

    @field_validator("cron_expression")
    @classmethod
    def _check_cron(cls, v: Optional[str]) -> Optional[str]:
        return _validate_cron(v)


class ModelAISchedulerConfigResponse(OrmBase):
    id: uuid.UUID
    model_id: uuid.UUID
    ai_enabled: bool
    cron_expression: str
    lookback_hours: int
    max_creates_per_run: int
    min_confidence: float
    dry_run: bool
    enable_ai_aggregation: bool
    llm_config_id: Optional[uuid.UUID] = None
    glossary_llm_config_id: Optional[uuid.UUID] = None
    created_at: datetime
    updated_at: datetime


class AIOptimizerRunTriggerRequest(BaseModel):
    model_id: uuid.UUID
    dry_run: bool = False


class AIOptimizerRunStartResponse(BaseModel):
    """F-011-17 — 202 acknowledgement for a manual AI optimiser run.

    The actual run executes in the background; the caller polls the run history
    (Diagnostics) to follow progress. ``poll_after_seconds`` is a hint for how
    long to wait before the first poll.
    """

    id: uuid.UUID
    model_id: uuid.UUID
    status: str
    accepted: bool = True
    poll_after_seconds: int = 30


class AIAggregateRecommendationResponse(OrmBase):
    # The fingerprint / hit-rate / priority / impact chain was removed with the
    # grouped-pattern telemetry redesign (F-011-04): the LLM never produces
    # these, so they are no longer served. The ORM columns are retained but not
    # surfaced.
    id: uuid.UUID
    model_id: uuid.UUID
    optimizer_run_id: uuid.UUID
    grain: list
    measures: list
    rationale: Optional[str] = None
    status: str
    aggregate_definition_id: Optional[uuid.UUID] = None
    created_at: datetime


class AIOptimizerRunResponse(OrmBase):
    id: uuid.UUID
    model_id: uuid.UUID
    triggered_by: str
    status: str
    is_dry_run: bool
    started_at: datetime
    completed_at: Optional[datetime] = None
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    telemetry_snapshot_id: Optional[uuid.UUID] = None
    recommendations_count: int
    aggregates_created: int
    aggregates_skipped: int
    # F-011-04: provider-reported per-run spend (tokens). NULL when the run
    # failed before the provider call or the provider reported no usage.
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    error_message: Optional[str] = None
    raw_llm_response: Optional[str] = None
    diagnostics_log: Optional[list] = None
    analysis_notes: Optional[str] = None
    recommendations: list[AIAggregateRecommendationResponse] = []


class TelemetrySnapshotResponse(OrmBase):
    id: uuid.UUID
    model_id: uuid.UUID
    snapshot_at: datetime
    lookback_hours: int
    snapshot_json: dict
    total_miss_patterns: int
    total_miss_occurrences: int
    top_cost_score: Optional[float] = None
    triggered_by: str


# ---------------------------------------------------------------------------
# Glossary (semantic-layer Phase 3)
# ---------------------------------------------------------------------------

class GlossaryAttachmentResponse(OrmBase):
    id: uuid.UUID
    entry_id: uuid.UUID
    target_type: str  # dimension | measure | column | concept
    target_id: Optional[uuid.UUID] = None
    # F-018-21: the curation panel showed "dimension (3fa85f64…)". The list
    # endpoint resolves the human name so the modeller can tell which
    # dimension/measure a term describes without cross-referencing IDs.
    target_name: Optional[str] = None


# Valid glossary attachment targets. The agent retrieval gate
# (agent-service/src/retrieval/glossary.py) and the frontend chips only
# understand these — F-018-16 rejects anything else at the API boundary so
# unknown values cannot be persisted.
_GLOSSARY_TARGET_TYPES = {"dimension", "measure", "column", "concept"}
_GLOSSARY_VISIBILITY = {"show", "hide", "review"}
_GLOSSARY_CONFIDENCE = {"high", "medium", "low"}


def _validate_glossary_visibility(v: Optional[str]) -> Optional[str]:
    if v is not None and v not in _GLOSSARY_VISIBILITY:
        raise ValueError("visibility must be 'show', 'hide', or 'review'")
    return v


def _validate_glossary_confidence(v: Optional[str]) -> Optional[str]:
    if v is not None and v not in _GLOSSARY_CONFIDENCE:
        raise ValueError("confidence must be 'high', 'medium', or 'low'")
    return v


class GlossaryEntryCreate(BaseModel):
    term: str = Field(max_length=255)
    definition: str
    context_notes: Optional[str] = None
    synonyms: list[str] = Field(default_factory=list)
    proposed_is_hidden: Optional[bool] = None
    visibility: Optional[str] = None
    confidence: Optional[str] = None
    target_type: str = Field(description="dimension | measure | column | concept")
    target_id: Optional[uuid.UUID] = None

    # F-018-16: Create now validates the same fields Update does, plus
    # target_type, so the create path cannot persist values the consumers
    # (agent retrieval gate, frontend chips) do not understand.
    @field_validator("visibility")
    @classmethod
    def _check_visibility(cls, v: Optional[str]) -> Optional[str]:
        return _validate_glossary_visibility(v)

    @field_validator("confidence")
    @classmethod
    def _check_confidence(cls, v: Optional[str]) -> Optional[str]:
        return _validate_glossary_confidence(v)

    @field_validator("target_type")
    @classmethod
    def _check_target_type(cls, v: str) -> str:
        if v not in _GLOSSARY_TARGET_TYPES:
            raise ValueError(
                f"target_type must be one of {sorted(_GLOSSARY_TARGET_TYPES)}"
            )
        return v


class GlossaryEntryUpdate(BaseModel):
    term: Optional[str] = None
    definition: Optional[str] = None
    context_notes: Optional[str] = None
    synonyms: Optional[list[str]] = None
    proposed_is_hidden: Optional[bool] = None
    visibility: Optional[str] = None
    confidence: Optional[str] = None

    @field_validator("visibility")
    @classmethod
    def _check_visibility(cls, v: Optional[str]) -> Optional[str]:
        return _validate_glossary_visibility(v)

    @field_validator("confidence")
    @classmethod
    def _check_confidence(cls, v: Optional[str]) -> Optional[str]:
        return _validate_glossary_confidence(v)


class GlossaryEntryResponse(OrmBase):
    id: uuid.UUID
    model_id: uuid.UUID
    term: str
    definition: str
    context_notes: Optional[str] = None
    source: str
    status: str
    version: int
    superseded_by: Optional[uuid.UUID] = None
    created_by: Optional[uuid.UUID] = None
    proposed_is_hidden: Optional[bool] = None
    visibility: Optional[str] = None
    confidence: Optional[str] = None
    # Bug-7251: explicit flag so consumers can immediately tell if an entry
    # was generated by the heuristic fallback (no LLM). The ``source`` and
    # ``confidence`` fields already carry this information, but they require
    # the consumer to know the internal vocabulary ("heuristic", "low").
    # This boolean makes the distinction first-class in the response.
    is_heuristic_fallback: bool = False
    created_at: datetime
    updated_at: datetime
    sample_values: Optional[list] = None
    synonyms: list[str] = Field(default_factory=list)
    attachments: list[GlossaryAttachmentResponse] = Field(default_factory=list)


class GlossaryBootstrapResponse(BaseModel):
    proposed_count: int
    updated_count: int = 0
    skipped_count: int = 0
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    llm_error: Optional[str] = None
    used_llm: bool = False
    fallback_count: int = 0
    job_id: Optional[uuid.UUID] = None
    job_status: Optional[str] = None
    message: Optional[str] = None


class GlossaryBulkDeleteRequest(BaseModel):
    # all = every entry; heuristic = source == "heuristic";
    # non_manual = every entry not created by a user (source != "user").
    scope: Literal["all", "heuristic", "non_manual"]


class GlossaryBulkDeleteResponse(BaseModel):
    deleted_count: int


class GlossaryBulkApproveResponse(BaseModel):
    approved_count: int


class GlossaryCsvImportRequest(BaseModel):
    """Validated payload for the glossary CSV import endpoint (Bug-5697).

    Replaces the raw ``dict[str, Any]`` parameter so FastAPI rejects
    malformed requests before the handler runs.
    """
    csv: str = Field(min_length=1, description="Raw CSV text with header 'term,description'")


# ---------------------------------------------------------------------------
# Audit events
# ---------------------------------------------------------------------------

class AuditEventResponse(OrmBase):
    id: uuid.UUID
    timestamp: datetime
    actor_id: Optional[uuid.UUID] = None
    actor_email: Optional[str] = None
    action: str
    target_type: Optional[str] = None
    target_id: Optional[uuid.UUID] = None
    target_name: Optional[str] = None
    severity: str
    detail: Optional[dict[str, Any]] = None
    ip_address: Optional[str] = None


class AuditEventListResponse(BaseModel):
    items: list[AuditEventResponse]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Schema drift
# ---------------------------------------------------------------------------

class SchemaChangeEventResponse(OrmBase):
    id: uuid.UUID
    model_id: uuid.UUID
    source_id: Optional[uuid.UUID] = None
    table_name: Optional[str] = None
    change_type: str
    is_breaking: bool
    detail: dict[str, Any]
    detected_at: datetime
    acknowledged_at: Optional[datetime] = None


class SchemaChangeEventListResponse(BaseModel):
    items: list[SchemaChangeEventResponse]
    total: int


# ---------------------------------------------------------------------------
# Downstream Assets (Impact Analysis — Phase 9 Block G)
# ---------------------------------------------------------------------------

_ALLOWED_ASSET_TYPES = ("dashboard", "report", "ml_job", "api", "other")


class DownstreamAssetCreate(BaseModel):
    asset_type: str
    asset_name: str = Field(max_length=512)
    asset_url: Optional[str] = None
    owner: Optional[str] = Field(default=None, max_length=255)
    notes: Optional[str] = None
    column_ids: list[uuid.UUID] = Field(default_factory=list)

    @field_validator("asset_type")
    @classmethod
    def _check_asset_type(cls, v: str) -> str:
        if v not in _ALLOWED_ASSET_TYPES:
            raise ValueError(
                f"asset_type must be one of {_ALLOWED_ASSET_TYPES}, got {v!r}"
            )
        return v


class DownstreamAssetUpdate(BaseModel):
    asset_type: Optional[str] = None
    asset_name: Optional[str] = Field(default=None, max_length=512)
    asset_url: Optional[str] = None
    owner: Optional[str] = Field(default=None, max_length=255)
    notes: Optional[str] = None
    column_ids: Optional[list[uuid.UUID]] = None

    @field_validator("asset_type")
    @classmethod
    def _check_asset_type(cls, v: str | None) -> str | None:
        if v is not None and v not in _ALLOWED_ASSET_TYPES:
            raise ValueError(
                f"asset_type must be one of {_ALLOWED_ASSET_TYPES}, got {v!r}"
            )
        return v


class DownstreamAssetResponse(OrmBase):
    id: uuid.UUID
    model_id: uuid.UUID
    asset_type: str
    asset_name: str
    asset_url: Optional[str] = None
    owner: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    column_ids: list[uuid.UUID] = Field(default_factory=list)


class DownstreamAssetSummaryResponse(BaseModel):
    total: int
    by_type: dict[str, int]


class GatewayQueryReferenceResponse(OrmBase):
    id: uuid.UUID
    model_id: uuid.UUID
    queried_table: str
    query_user: Optional[str] = None
    query_text_hash: str
    last_seen_at: datetime
    hit_count: int


class ImpactScanResponse(BaseModel):
    references_upserted: int
    tables_matched: int
    # Distinct model COLUMNS (not column names) resolved as used in this pass.
    # A column reachable through two calendar-alias tables is counted per table.
    columns_matched: int = 0
    # Query-log rows examined by this pass. The scan is incremental: it resumes
    # from the newest usage it has already recorded and reads a bounded window,
    # so a second press legitimately examines fewer rows — or none.
    logs_scanned: int = 0
    # True when the window filled, i.e. more unscanned log rows remain. Without
    # it a second press reports "0 tables checked" and reads as "nothing is
    # used", which is the opposite of what the feature is asked.
    more_remaining: bool = False


class ColumnUsageItem(BaseModel):
    """One column's usage summary extracted from gateway query logs (Bug-7458)."""

    column_name: str
    # Physical table the usage was attributed to. Empty string when the
    # reference was unqualified and more than one model table carries this
    # column name — see ``ambiguous`` (Bug-8074).
    table_name: str
    # Total bound semantic references for stable-ID logs; legacy logs retain
    # SQL token-occurrence counting through the parser fallback (Bug-8074).
    hit_count: int
    # Distinct queries referencing this (table, column).
    query_count: int
    last_seen_at: Optional[datetime] = None
    # Bug-8074: True when the reference could not be attributed to one table.
    # The usage is real but the owning table is unknown from the query text, so
    # it must be shown as such rather than charged to an arbitrary table.
    ambiguous: bool = False
    # Physical tables that carry this column name, populated only when
    # ``ambiguous`` is True.
    candidate_tables: list[str] = Field(default_factory=list)


class ColumnUsageResponse(BaseModel):
    """Column-level usage from stable bind traces plus legacy SQL fallback."""

    model_id: str
    total_queries_parsed: int
    total_queries_skipped: int
    # Successful query-log rows that exist for this model, regardless of how many
    # were examined. The endpoint reads only the newest window.
    logs_available: int = 0
    # True when older rows exist beyond the window. Without it a column used only
    # in older traffic produces NO row at all and the panel reads "no usage
    # found" — a false negative in a tool whose whole purpose is to say whether a
    # column is safe to drop.
    truncated: bool = False
    columns: list[ColumnUsageItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Data Tagging + Column-Level Persona Security (Phase 9 Block H)
# ---------------------------------------------------------------------------


class DataTagCreate(BaseModel):
    tag_name: str = Field(max_length=128)
    description: Optional[str] = None
    column_ids: list[uuid.UUID] = Field(default_factory=list)


class DataTagUpdate(BaseModel):
    tag_name: Optional[str] = Field(default=None, max_length=128)
    description: Optional[str] = None
    column_ids: Optional[list[uuid.UUID]] = None


class DataTagColumnInfo(BaseModel):
    column_id: uuid.UUID
    table_name: str
    column_name: str


class DataTagResponse(OrmBase):
    id: uuid.UUID
    model_id: uuid.UUID
    tag_name: str
    description: Optional[str] = None
    created_at: datetime
    columns: list[DataTagColumnInfo] = Field(default_factory=list)


class PersonaTagRestrictionRequest(BaseModel):
    tag_ids: list[uuid.UUID]


class PersonaTagRestrictionResponse(BaseModel):
    tag_id: uuid.UUID
    tag_name: str
    description: Optional[str] = None
    column_count: int


# ---------------------------------------------------------------------------
# Notification routes
# ---------------------------------------------------------------------------

class NotificationRouteCreate(BaseModel):
    event_type: str
    channel_type: str
    channel_config: dict
    enabled: bool = True


class NotificationRouteUpdate(BaseModel):
    event_type: Optional[str] = None
    channel_type: Optional[str] = None
    channel_config: Optional[dict] = None
    enabled: Optional[bool] = None


class NotificationRouteResponse(OrmBase):
    id: uuid.UUID
    project_id: Optional[uuid.UUID] = None
    event_type: str
    channel_type: str
    channel_config: dict
    enabled: bool
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Named Sets
# ---------------------------------------------------------------------------

# F-018-24: the only named-set modes the compiler (named_list_compiler.py)
# can actually build. The schema previously advertised `relative_time` and
# `exception`, which the API accepted and stored but the compiler silently
# treated as raw `advanced_mdx` — a scope cut visible only in a docstring.
# These are validated on create/update so an unsupported mode fails loud.
NAMED_SET_LIST_TYPES = ("fixed", "dynamic_top_n", "filtered", "advanced_mdx", "sql_fixed")


class NamedSetCreate(BaseModel):
    name: str = Field(max_length=255)
    display_name: Optional[str] = None
    description: Optional[str] = None
    display_folder: Optional[str] = None
    scope: int = Field(default=1, description="1=SESSION, 2=GLOBAL per XMLA spec")
    expression: Optional[str] = Field(default=None, description="Raw MDX expression; auto-generated from builder_definition if omitted")
    dimensions: Optional[str] = None
    builder_definition: Optional[dict] = Field(default=None, description="Structured builder JSON compiled to MDX on save")
    list_type: Optional[str] = Field(default="advanced_mdx", description="fixed | dynamic_top_n | filtered | advanced_mdx | sql_fixed")
    owner_user_id: Optional[str] = None

    @field_validator("list_type")
    @classmethod
    def _validate_list_type(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in NAMED_SET_LIST_TYPES:
            raise ValueError(
                f"Unsupported list_type {v!r}; must be one of "
                f"{', '.join(NAMED_SET_LIST_TYPES)}"
            )
        return v


class NamedSetUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=255)
    display_name: Optional[str] = None
    description: Optional[str] = None
    display_folder: Optional[str] = None
    scope: Optional[int] = None
    expression: Optional[str] = None
    dimensions: Optional[str] = None
    builder_definition: Optional[dict] = None
    list_type: Optional[str] = None
    certification_status: Optional[CertificationStatus] = None
    owner_user_id: Optional[str] = None

    @field_validator("list_type")
    @classmethod
    def _validate_list_type(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in NAMED_SET_LIST_TYPES:
            raise ValueError(
                f"Unsupported list_type {v!r}; must be one of "
                f"{', '.join(NAMED_SET_LIST_TYPES)}"
            )
        return v


class NamedSetValidateRequest(BaseModel):
    builder_definition: Optional[dict] = None
    expression: Optional[str] = None


class NamedSetValidateResponse(BaseModel):
    is_valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    explanation: Optional[str] = None
    compiled_expression: Optional[str] = None
    estimated_cost_band: Optional[str] = None


class NamedSetPreviewResponse(BaseModel):
    items: list[dict] = Field(default_factory=list)
    total_count: int = 0
    truncated: bool = False
    explanation: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)


class NamedSetResponse(OrmBase):
    id: uuid.UUID
    model_id: uuid.UUID
    name: str
    display_name: Optional[str]
    description: Optional[str]
    display_folder: Optional[str]
    scope: int
    expression: str
    dimensions: Optional[str]
    builder_definition: Optional[dict] = None
    list_type: Optional[str] = None
    certification_status: CertificationStatus = "draft"
    replacement_id: Optional[uuid.UUID] = None
    owner_user_id: Optional[str] = None
    # Shared freshness/source/owner metadata. For sql_fixed named lists,
    # last_refreshed_at is the vintage of the stored members in this response
    # (live draft for the model builder, deployed snapshot for BI callers).
    trust_meta: Optional[dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime

    @field_validator("certification_status", mode="before")
    @classmethod
    def _read_cert_status(cls, v):
        return _coerce_certification_status_for_read(v)


# ---------------------------------------------------------------------------
# Named Queries
# ---------------------------------------------------------------------------

# Output-column type domain, shared between the derived output_columns
# manifest (create/validate time) and the refresh-time row manifest.
NAMED_QUERY_OUTPUT_TYPES = ("string", "number", "boolean", "date", "timestamp")
NAMED_QUERY_SHAPES = ("projection", "aggregated")
NAMED_QUERY_REFRESH_POLICIES = ("schedule", "manual")
NAMED_QUERY_ARTIFACT_STATUSES = ("fresh", "stale", "invalidating", "failed")


class NamedQueryOutputColumn(BaseModel):
    """One derived output column of a Named Query definition."""

    name: str
    type: str

    @field_validator("type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        if v not in NAMED_QUERY_OUTPUT_TYPES:
            raise ValueError(
                f"Named Query output type must be one of "
                f"{', '.join(NAMED_QUERY_OUTPUT_TYPES)}; got {v!r}"
            )
        return v


class NamedQueryCreate(BaseModel):
    name: str = Field(max_length=255)
    display_name: Optional[str] = None
    description: Optional[str] = None
    display_folder: Optional[str] = None
    # Model-bound semantic SQL over the model's LOGICAL surface (model slug +
    # measures + dimensions), never raw dialect SQL.
    definition_sql: str
    # Per-NQ overrides of the system caps. Enforced (reject, never truncate):
    # row_cap at refresh, column_cap at create/validate.
    row_cap: Optional[int] = Field(default=None, ge=1)
    column_cap: Optional[int] = Field(default=None, ge=1)
    certification_status: CertificationStatus = "draft"
    refresh_policy: str = "manual"
    refresh_cron: Optional[str] = None
    refresh_policy_enabled: Optional[bool] = None

    @field_validator("refresh_policy")
    @classmethod
    def _check_refresh_policy(cls, v: str) -> str:
        if v not in NAMED_QUERY_REFRESH_POLICIES:
            raise ValueError(
                "refresh_policy must be one of "
                f"{sorted(NAMED_QUERY_REFRESH_POLICIES)}; got {v!r}"
            )
        return v

    @field_validator("refresh_cron")
    @classmethod
    def _check_refresh_cron(cls, v: Optional[str]) -> Optional[str]:
        return _validate_cron(v)

    @model_validator(mode="after")
    def _require_cron_when_scheduled(self) -> "NamedQueryCreate":
        if self.refresh_policy == "schedule" and not (
            self.refresh_cron and self.refresh_cron.strip()
        ):
            raise ValueError(
                "refresh_policy 'schedule' requires a non-empty refresh_cron"
            )
        return self


class NamedQueryUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=255)
    display_name: Optional[str] = None
    description: Optional[str] = None
    display_folder: Optional[str] = None
    definition_sql: Optional[str] = None
    row_cap: Optional[int] = Field(default=None, ge=1)
    column_cap: Optional[int] = Field(default=None, ge=1)
    certification_status: Optional[CertificationStatus] = None


class NamedQueryValidateRequest(BaseModel):
    definition_sql: str


class NamedQueryValidateResponse(BaseModel):
    is_valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    output_columns: list[NamedQueryOutputColumn] = Field(default_factory=list)
    shape: Optional[str] = None


class NamedQueryArtifactResponse(BaseModel):
    id: uuid.UUID
    target_id: uuid.UUID
    physical_table_name: str
    target_schema: Optional[str] = None
    row_count: Optional[int] = None
    status: str = "stale"
    failure_reason: Optional[str] = None
    last_refresh_at: Optional[datetime] = None
    retired_at: Optional[datetime] = None


class NamedQueryRefreshPolicyUpsert(BaseModel):
    cron_expression: Optional[str] = None
    is_enabled: Optional[bool] = None

    @field_validator("cron_expression")
    @classmethod
    def _check_cron(cls, v: Optional[str]) -> Optional[str]:
        return _validate_cron(v)


class NamedQueryRefreshPolicyResponse(BaseModel):
    cron_expression: Optional[str] = None
    is_enabled: bool = False


class NamedQueryRefreshRunResponse(OrmBase):
    id: uuid.UUID
    named_query_id: uuid.UUID
    refresh_mode: str
    status: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    rows_written: Optional[int] = None
    bytes_processed: Optional[int] = None
    error_message: Optional[str] = None
    triggered_by: str = "scheduler"


class NamedQueryResponse(OrmBase):
    id: uuid.UUID
    model_id: uuid.UUID
    name: str
    display_name: Optional[str]
    description: Optional[str]
    display_folder: Optional[str]
    definition_sql: str
    output_columns: Optional[list[NamedQueryOutputColumn]] = None
    shape: str = "projection"
    row_cap: Optional[int] = None
    column_cap: Optional[int] = None
    certification_status: CertificationStatus = "draft"
    created_by: Optional[str] = None
    artifact: Optional[NamedQueryArtifactResponse] = None
    refresh_policy: Optional[NamedQueryRefreshPolicyResponse] = None
    created_at: datetime
    updated_at: datetime

    @field_validator("certification_status", mode="before")
    @classmethod
    def _read_cert_status(cls, v):
        return _coerce_certification_status_for_read(v)


# ---------------------------------------------------------------------------
# KPIs
# ---------------------------------------------------------------------------

# Bug-5923: canonical enum values for KPI mode fields that affect threshold,
# trend, and composite evaluation semantics. These are validated on
# create/update so that typos (e.g. "lower_is_beter") are rejected at the
# API boundary instead of silently falling through to higher_is_better.
#
# Note: kpi_type is intentionally NOT constrained — it is an open-ended
# classification tag (simple_measure, ratio, composite, share_rank, etc.)
# set by the business builder and importers.
_KPI_DIRECTIONS = {"higher_is_better", "lower_is_better", "closer_is_better"}
# Bug-7237: the spec (Section 4.2) lists "None" as a first-class target type
# (KPI has no target, trend-only).  The wizard sends target_type="none"; the
# validator normalises it to None so it persists as SQL NULL.
_KPI_TARGET_TYPES = {"static", "measure", "expression", "prior_period", "none"}
_KPI_INDICATOR_TYPES = {"none", "traffic_light", "gauge", "scorecard"}
# Bug-7241: presentation_type was an unrestricted string — an unknown value
# silently renders no visual on the scorecard.  Constrain to the set the
# frontend switch-case handles so typos are caught at the API boundary.
_KPI_PRESENTATION_TYPES = {
    "traffic_light", "progress_ring", "thermometer",
    "gauge", "speedometer", "reverse_gauge",
    "bullet_chart", "rag_bar",
}
# Bug-7241: evaluation_type lives inside presentation_meta and determines
# how value-vs-target is scored.  An unknown value falls through to the
# default percentage_of_target path without error.
_KPI_EVALUATION_TYPES = {
    "percentage_of_target", "absolute_variance", "percentage_variance",
    "absolute_value", "z_score", "percentile_rank",
}
# Bug-7236: the spec (Section 5.4) defines five aggregation modes and the
# compiler implements all of them.  The enum was inadvertently narrowed to
# {automatic, manual, semi_additive} which rejects the four modes the wizard
# offers.  Restore the spec-canonical set; keep "manual" and "semi_additive"
# for backward compatibility with any persisted data.
_KPI_CALC_AGG_MODES = {
    "automatic", "aggregate_first", "row_first",
    "aggregate_of_aggregate", "pre_aggregated",
    "manual", "semi_additive",
}
# Bug-6252 [silent wrong numbers]: ``non_additive_agg`` selects HOW a
# semi-additive KPI reduces its per-grain-bucket values. It was an unvalidated
# free-form string while every sibling enum on this model was gated, and the
# compiler's ``_semi_additive_expr`` ends in an unconditional
# ``return SUM(col)`` fallback. Any token it does not recognise therefore SUMS
# a column of period balances — the exact wrong number semi-additive support
# exists to prevent (a daily-balance or inventory-level KPI reports the sum of
# every day's balance instead of the closing/average balance).
#
# The reachable shapes were not hypothetical typos: the canonical MEASURE-level
# vocabulary (``VALID_SEMI_ADDITIVE_BEHAVIORS``: last_non_empty /
# first_non_empty / avg_of_children / min / max) is a DIFFERENT vocabulary from
# the compiler's, so an operator or importer reusing the measure tokens got a
# silent SUM for every one of them. (``by_account`` was a measure token too until
# #10 removed it as unsupported; it never had a KPI-compiler equivalent, so the
# note below about it still holds.)
#
# This set is exactly what ``kpi_compiler`` implements. ``last``/``first``/
# ``avg`` are the spec-documented options (architecture_kpi-requirements-
# specification.md); ``min``/``max``/``sum`` are additionally implemented and
# are now documented rather than undeclared.
_KPI_NON_ADDITIVE_AGGS = {"last", "first", "avg", "min", "max", "sum"}
# Measure-level semi-additive tokens that have an exact KPI-compiler
# equivalent. Bridged rather than rejected so an operator using the vocabulary
# the Measures panel taught them gets the behaviour they asked for instead of a
# silent SUM. ``by_account`` has NO compiler equivalent (it needs a per-row
# account-type column the KPI compiler never reads) and is deliberately absent,
# so it fails the enum gate loudly.
_KPI_NON_ADDITIVE_AGG_SYNONYMS = {
    "last_non_empty": "last",
    "first_non_empty": "first",
    "avg_of_children": "avg",
    "average": "avg",
}


def _validate_kpi_non_additive_agg(value: str | None) -> str | None:
    """Canonicalise + gate ``non_additive_agg`` (Bug-6252).

    ``None`` (field not supplied / semi-additive not in use) passes through.
    """
    if value is None:
        return None
    token = str(value).strip().lower()
    token = _KPI_NON_ADDITIVE_AGG_SYNONYMS.get(token, token)
    if token not in _KPI_NON_ADDITIVE_AGGS:
        raise ValueError(
            f"Invalid non_additive_agg '{value}'. Must be one of: "
            f"{sorted(_KPI_NON_ADDITIVE_AGGS)}. An unrecognised value would "
            "silently SUM the per-period values instead of reducing them."
        )
    return token


# Bug-8573: restrict at_grain to the five spec keywords so a typo or a raw
# column name does not silently change what the SQL groups by.
_KPI_AT_GRAIN_KEYWORDS = frozenset({"day", "week", "month", "quarter", "year"})


def _validate_kpi_at_grain(value: str | None) -> str | None:
    """Gate + canonicalise ``at_grain`` (Bug-8573).

    ``None`` (field not supplied / semi-additive not in use) passes through.
    """
    if value is None:
        return None
    token = str(value).strip().lower()
    if token not in _KPI_AT_GRAIN_KEYWORDS:
        raise ValueError(
            f"Invalid at_grain '{value}'. Must be one of: "
            f"{sorted(_KPI_AT_GRAIN_KEYWORDS)}. A non-keyword value would "
            "silently change what the SQL groups by instead of reducing "
            "per period."
        )
    return token


def _validate_kpi_enum(value: str | None, allowed: set[str], field_name: str) -> str | None:
    """Validate a KPI enum string against the allowed set (Bug-5923)."""
    if value is None:
        return value
    if value not in allowed:
        raise ValueError(
            f"Invalid {field_name} '{value}'. Must be one of: {sorted(allowed)}"
        )
    # Bug-7237: normalise "none" target_type to None so it persists as SQL
    # NULL, matching the spec's representation (target_type: null).
    if field_name == "target_type" and value == "none":
        return None
    return value


def _validate_kpi_statistical_inputs(
    presentation_meta: dict | None,
    snapshot_frequency: str | None,
    *,
    snapshot_frequency_provided: bool,
    is_create: bool,
) -> None:
    """F-017-08: fail loud at SAVE when a statistical evaluation type is missing
    the data dependency it needs to ever classify, instead of silently returning
    a grey "No Data" badge on every surface.

    - ``z_score`` needs at least two historical snapshots, which only exist when
      the KPI has a ``snapshot_frequency`` cron.
    - ``percentile_rank`` needs a ``peer_dimension`` in ``presentation_meta`` to
      rank the value against peers.

    On create both inputs are fully known from the payload. On update this is a
    partial payload, so ``snapshot_frequency`` is only enforced when it is
    actually being set (``snapshot_frequency_provided``) — an unrelated field
    edit must not be rejected for a value that already lives on the row. The
    ``peer_dimension`` requirement always applies because it lives inside the
    same ``presentation_meta`` dict being written.
    """
    if not isinstance(presentation_meta, dict):
        return
    evaluation_type = presentation_meta.get("evaluation_type")
    if evaluation_type == "z_score":
        if (is_create or snapshot_frequency_provided) and not snapshot_frequency:
            raise ValueError(
                "A z_score KPI requires snapshot_frequency so history can be "
                "captured — z-score needs at least two snapshots to classify."
            )
    elif evaluation_type == "percentile_rank":
        peer_dimension = presentation_meta.get("peer_dimension")
        if not (isinstance(peer_dimension, str) and peer_dimension.strip()):
            raise ValueError(
                "A percentile_rank KPI requires presentation_meta.peer_dimension "
                "to rank the value against its peer group."
            )


class KPICreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=255)
    display_name: Optional[str] = None
    description: Optional[str] = None
    display_folder: Optional[str] = None
    # v2 expression DSL
    kpi_type: Optional[str] = None
    expression: Optional[str] = None
    calc_agg_mode: str = "automatic"
    inner_agg: Optional[str] = None
    inner_grain: Optional[str] = None
    outer_agg: Optional[str] = None
    # Semi-additive
    at_grain: Optional[str] = None
    non_additive_agg: Optional[str] = None
    carry_forward: bool = False
    # Target
    target_type: Optional[str] = None
    target_value: Optional[float] = None
    target_measure_id: Optional[uuid.UUID] = None
    target_expression: Optional[str] = None
    target_period: Optional[str] = None
    # Direction and thresholds
    direction: str = "higher_is_better"
    presentation_type: Optional[str] = None
    presentation_meta: Optional[dict] = None
    # Trend
    trend_period: str = "month"
    trend_threshold: float = 0.01
    trend_sparkline_periods: int = 12
    # Formatting
    format_token: Optional[str] = None
    format_custom: Optional[str] = None
    unit_label: Optional[str] = None
    null_display_value: str = "N/A"
    # Hierarchy / composition
    weight: Optional[float] = None
    parent_kpi_id: Optional[uuid.UUID] = None
    indicator_type: str = "none"
    # Time dimension binding
    time_dimension_id: Optional[uuid.UUID] = None
    # Business builder definition (v3)
    business_definition: Optional[dict] = None
    # Governance
    # Bug-6893: certification_status must be declared on KPICreate so
    # extra="forbid" does not reject it when passed in the POST body.
    # DB default is "draft"; the create handler may override it.
    certification_status: Optional[CertificationStatus] = None
    owner_user_id: Optional[str] = None
    # Snapshots
    snapshot_frequency: Optional[str] = None
    snapshot_retention: int = 90
    status_graphic: str = "Traffic Light"
    trend_graphic: str = "Standard Arrow"

    # Bug-5923: reject invalid enum strings at the API boundary.
    @field_validator("direction")
    @classmethod
    def _check_direction(cls, v: str) -> str:
        return _validate_kpi_enum(v, _KPI_DIRECTIONS, "direction")  # type: ignore[return-value]

    @field_validator("target_type")
    @classmethod
    def _check_target_type(cls, v: str | None) -> str | None:
        return _validate_kpi_enum(v, _KPI_TARGET_TYPES, "target_type")

    @field_validator("indicator_type")
    @classmethod
    def _check_indicator_type(cls, v: str) -> str:
        return _validate_kpi_enum(v, _KPI_INDICATOR_TYPES, "indicator_type")  # type: ignore[return-value]

    @field_validator("calc_agg_mode")
    @classmethod
    def _check_calc_agg_mode(cls, v: str) -> str:
        return _validate_kpi_enum(v, _KPI_CALC_AGG_MODES, "calc_agg_mode")  # type: ignore[return-value]

    # Bug-6252: gate + canonicalise the semi-additive reducer so an
    # unrecognised token cannot reach the compiler's silent SUM fallback.
    @field_validator("non_additive_agg")
    @classmethod
    def _check_non_additive_agg(cls, v: str | None) -> str | None:
        return _validate_kpi_non_additive_agg(v)

    # Bug-8573: gate at_grain to the five spec keywords so a typo ("monthly",
    # "Month") or a raw column name does not silently change what the SQL
    # groups by.  For first/last a non-keyword at_grain picks one arbitrary
    # row instead of reducing per period; making this fire a ValidationError
    # at create/update prevents the silent wrong number.
    @field_validator("at_grain")
    @classmethod
    def _check_at_grain(cls, v: str | None) -> str | None:
        return _validate_kpi_at_grain(v)

    # Bug-7241: reject unknown presentation_type at the API boundary.
    @field_validator("presentation_type")
    @classmethod
    def _check_presentation_type(cls, v: str | None) -> str | None:
        return _validate_kpi_enum(v, _KPI_PRESENTATION_TYPES, "presentation_type")

    # Bug-7241: reject unknown evaluation_type inside presentation_meta.
    @model_validator(mode="after")
    def _check_presentation_meta_evaluation_type(self) -> "KPICreate":
        if self.presentation_meta and isinstance(self.presentation_meta, dict):
            et = self.presentation_meta.get("evaluation_type")
            if et is not None:
                _validate_kpi_enum(et, _KPI_EVALUATION_TYPES, "presentation_meta.evaluation_type")
        # F-017-08: statistical evaluation types must carry their data dependency.
        _validate_kpi_statistical_inputs(
            self.presentation_meta,
            self.snapshot_frequency,
            snapshot_frequency_provided="snapshot_frequency" in self.model_fields_set,
            is_create=True,
        )
        return self


class KPIUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=255)
    display_name: Optional[str] = None
    description: Optional[str] = None
    display_folder: Optional[str] = None
    # v2 expression DSL
    kpi_type: Optional[str] = None
    expression: Optional[str] = None
    calc_agg_mode: Optional[str] = None
    inner_agg: Optional[str] = None
    inner_grain: Optional[str] = None
    outer_agg: Optional[str] = None
    # Semi-additive
    at_grain: Optional[str] = None
    non_additive_agg: Optional[str] = None
    carry_forward: Optional[bool] = None
    # Target
    target_type: Optional[str] = None
    target_value: Optional[float] = None
    target_measure_id: Optional[uuid.UUID] = None
    target_expression: Optional[str] = None
    target_period: Optional[str] = None
    # Direction and thresholds
    direction: Optional[str] = None
    presentation_type: Optional[str] = None
    presentation_meta: Optional[dict] = None
    # Trend
    trend_period: Optional[str] = None
    trend_threshold: Optional[float] = None
    trend_sparkline_periods: Optional[int] = None
    # Formatting
    format_token: Optional[str] = None
    format_custom: Optional[str] = None
    unit_label: Optional[str] = None
    null_display_value: Optional[str] = None
    # Hierarchy / composition
    weight: Optional[float] = None
    parent_kpi_id: Optional[uuid.UUID] = None
    indicator_type: Optional[str] = None
    # Time dimension binding
    time_dimension_id: Optional[uuid.UUID] = None
    # Business builder definition (v3)
    business_definition: Optional[dict] = None
    # Governance
    certification_status: Optional[CertificationStatus] = None
    owner_user_id: Optional[str] = None
    # Deployment
    is_deployed: Optional[bool] = None
    # Snapshots
    snapshot_frequency: Optional[str] = None
    snapshot_retention: Optional[int] = None
    status_graphic: Optional[str] = None
    trend_graphic: Optional[str] = None

    # Bug-5923: reject invalid enum strings on update too.
    @field_validator("direction")
    @classmethod
    def _check_direction(cls, v: str | None) -> str | None:
        return _validate_kpi_enum(v, _KPI_DIRECTIONS, "direction")

    @field_validator("target_type")
    @classmethod
    def _check_target_type(cls, v: str | None) -> str | None:
        return _validate_kpi_enum(v, _KPI_TARGET_TYPES, "target_type")

    @field_validator("indicator_type")
    @classmethod
    def _check_indicator_type(cls, v: str | None) -> str | None:
        return _validate_kpi_enum(v, _KPI_INDICATOR_TYPES, "indicator_type")

    @field_validator("calc_agg_mode")
    @classmethod
    def _check_calc_agg_mode(cls, v: str | None) -> str | None:
        return _validate_kpi_enum(v, _KPI_CALC_AGG_MODES, "calc_agg_mode")

    # Bug-6252: gate + canonicalise the semi-additive reducer on update too.
    # A PATCH that re-points non_additive_agg is the same silent-SUM exposure
    # as a create; partial-update semantics keep None a no-op.
    @field_validator("non_additive_agg")
    @classmethod
    def _check_non_additive_agg(cls, v: str | None) -> str | None:
        return _validate_kpi_non_additive_agg(v)

    # Bug-8573: gate at_grain on update too.  Same silent-SUM exposure as
    # create; partial-update semantics keep None a no-op.
    @field_validator("at_grain")
    @classmethod
    def _check_at_grain(cls, v: str | None) -> str | None:
        return _validate_kpi_at_grain(v)

    # Bug-7241: reject unknown presentation_type on update too.
    @field_validator("presentation_type")
    @classmethod
    def _check_presentation_type(cls, v: str | None) -> str | None:
        return _validate_kpi_enum(v, _KPI_PRESENTATION_TYPES, "presentation_type")

    # Bug-7241: reject unknown evaluation_type inside presentation_meta on update.
    @model_validator(mode="after")
    def _check_presentation_meta_evaluation_type(self) -> "KPIUpdate":
        if self.presentation_meta and isinstance(self.presentation_meta, dict):
            et = self.presentation_meta.get("evaluation_type")
            if et is not None:
                _validate_kpi_enum(et, _KPI_EVALUATION_TYPES, "presentation_meta.evaluation_type")
        # F-017-08: on a partial update, only enforce snapshot_frequency when it
        # is actually being written (an unrelated edit must not fail for a value
        # already stored); peer_dimension is always enforced when the meta is set.
        _validate_kpi_statistical_inputs(
            self.presentation_meta,
            self.snapshot_frequency,
            snapshot_frequency_provided="snapshot_frequency" in self.model_fields_set,
            is_create=False,
        )
        return self


class KPITrendPoint(BaseModel):
    period: str
    value: Optional[float] = None


KPICompositeStatus = Literal["ok", "degraded", "error", "restricted"]


class KPIEvaluateResponse(BaseModel):
    kpi_id: Optional[uuid.UUID] = None
    value: Optional[float] = None
    value_str: Optional[str] = None
    target: Optional[float] = None
    status: Optional[int] = None
    status_label: Optional[str] = None
    status_color: Optional[str] = None
    # Bug-1226: the authoritative gauge position and the bands it was matched
    # against. ``status_position`` is the exact ratio/variance/z/percentile value
    # the backend matched to ``status``; ``status_bands`` are the bands used for
    # that match (custom or resolved default preset). The frontend gauge plots
    # its needle from ``status_position`` against ``status_bands`` so the needle,
    # band colour and status badge share one scale and cannot disagree.
    status_position: Optional[float] = None
    status_bands: Optional[list[dict]] = None
    trend: Optional[int] = None
    trend_label: Optional[str] = None
    trend_pct: Optional[float] = None
    # Bug-7238: direction-normalised percentage — positive means improving,
    # negative means declining, regardless of direction preference.
    trend_pct_normalised: Optional[float] = None
    formatted_value: Optional[str] = None
    formatted_target: Optional[str] = None
    formatted_variance: Optional[str] = None
    trend_series: Optional[list[KPITrendPoint]] = None
    evaluation_ms: Optional[int] = None
    compiled_expression: Optional[str] = None
    compiled_scope: Optional[dict] = None
    # Actual SQL sent to the query gateway for execution. Multiple statements
    # (separated by blank lines) for time-intelligence KPIs that decompose into
    # several per-period queries.
    compiled_sql: Optional[str] = None
    # Bug-4255: composite-KPI health. ``composite_status`` is "ok" (or None for
    # non-composites), "degraded" when the score was computed but at least one
    # child KPI's evaluation FAILED (broken input — not no-data), "error" when
    # every child errored and no real score could be produced, or "restricted"
    # when row security withheld at least one child and a partial score would be
    # misleading.
    # ``errored_children`` lists the failed children with a per-child reason so
    # the scorecard/UI can show which input is broken rather than silently
    # dropping it. A no-data child stays silently excluded and is NOT listed.
    composite_status: Optional[KPICompositeStatus] = None
    errored_children: Optional[list[dict]] = None
    # Bug-8449 / Bug-8427: True when this KPI produced no value BECAUSE the
    # model's row-level security denied the caller every row (the F-007-01
    # fail-closed coverage predicate), rather than because the slice is empty
    # or the expression is broken. Without it the three states collapse into
    # one ``value: null`` / "N/A" and a modeller cannot tell a governance
    # denial from missing data. None/False means row security did not deny the
    # caller — it does NOT mean the caller saw every row (a narrowing rule may
    # still have applied; the value is then correct FOR THAT CALLER).
    row_security_restricted: Optional[bool] = None
    # F-017-09: which engine produced this value, so a divergence between the SQL
    # compiler and the Python fallback is visible to the user and to operators
    # (charter C7 — "the fallback engages loudly") instead of being a silent
    # internal control-flow sentinel. "sql" = compiled and executed on the
    # gateway; "python" = the SQL compiler could not handle the expression (time
    # intelligence / kpi() references) so the model-service Python evaluator
    # answered; "refused" = a correctness guard declined to serve (e.g. a
    # semi-additive reduction the Python path cannot apply). ``fallback_reason``
    # carries a human-readable cause when the path is not "sql".
    evaluation_path: str = "sql"
    fallback_reason: Optional[str] = None
    # Legacy fields for v1 compatibility
    goal: Optional[float] = None
    formatted_goal: Optional[str] = None


class KPIValidationDiagnostic(BaseModel):
    code: str
    message: str
    position: Optional[dict] = None
    suggestion: Optional[str] = None


class KPIValidationResponse(BaseModel):
    valid: bool
    errors: list[KPIValidationDiagnostic] = []
    warnings: list[KPIValidationDiagnostic] = []
    referenced_measures: list[str] = []
    referenced_kpis: list[str] = []
    referenced_dimensions: list[str] = []
    has_time_intelligence: bool = False
    requires_time_dimension: bool = False
    detected_agg_mode: Optional[str] = None
    expression_tree: Optional[dict] = None
    compiled_sql_preview: Optional[str] = None


class KPIValidateExpressionRequest(BaseModel):
    expression: str
    target_expression: Optional[str] = None


class KPIAdhocRequest(BaseModel):
    expression: Optional[str] = None
    target_expression: Optional[str] = None
    calc_agg_mode: str = "automatic"
    direction: str = "higher_is_better"
    threshold_preset: Optional[str] = None
    format_token: Optional[str] = None
    format_custom: Optional[str] = None
    unit_label: Optional[str] = None
    presentation_meta: Optional[dict] = None
    trend_period: Optional[str] = None
    filters: Optional[list[dict]] = None
    time_dimension: Optional[str] = None
    business_definition: Optional[dict] = None


class KPIBatchRequest(BaseModel):
    kpi_ids: list[uuid.UUID]
    filters: Optional[list[dict]] = None
    # Bug-7982 R6 (findings 1/7/8): the within-epoch kpi_latest write-ordering
    # marker, captured once when the evaluation began. Only HONOURED for an
    # internal-service publish call (the scheduler sweep sets it); a regular user
    # call never publishes kpi_latest, so this is ignored for it. Threading it
    # from the sweep makes the sweep's own _upsert_kpi_latest and this handler's
    # publish share ONE marker (so the sweep's write is not falsely suppressed),
    # and it is sourced from the DB server clock — a single monotonic source for
    # all writers, immune to cross-process wall-clock skew.
    eval_started_at: Optional[datetime] = None
    # Bug-7982 R7 finding 2: the AUTHORITATIVE within-epoch ordering token — a
    # strictly-increasing value from the tenant sequence ``kpi_eval_generation_seq``,
    # allocated once at evaluation start. ``eval_started_at`` (clock_timestamp) is
    # not unique, so a tie there reopened last-commit-wins. Threaded by the sweep
    # for the same reason the timestamp is: so the sweep's own kpi_latest write and
    # this handler's publish share ONE token and neither suppresses the other.
    # Honoured only for an internal-service publish call, and CLAMPED to a
    # freshly-allocated generation so a bogus far-future token cannot wedge the row.
    eval_generation: Optional[int] = Field(default=None, ge=1)

    @field_validator("eval_started_at")
    @classmethod
    def _require_tz_aware(cls, v: Optional[datetime]) -> Optional[datetime]:
        """A NAIVE marker would be silently timezone-shifted by the DB server on
        persist, which could invert the within-epoch ordering (a wrong-number
        risk). Coerce a naive value to UTC so the ordering key is unambiguous. The
        legitimate producer (the sweep) always sends a tz-aware DB-clock value."""
        if v is not None and v.tzinfo is None:
            from datetime import timezone as _tz
            return v.replace(tzinfo=_tz.utc)
        return v


class KPIBatchResponse(BaseModel):
    results: list[KPIEvaluateResponse]
    evaluation_ms: Optional[int] = None
    # Bug-7982 R7 finding 5: an HTTP 200 is NOT proof that kpi_latest was
    # published. Per-row upsert failures are deliberately isolated so one bad KPI
    # cannot starve its siblings, and the failure used to be logged and dropped —
    # while both outbox-clearing paths (kpi_reeval_trigger._clear_pending_outbox
    # and sweep._drain_pending_kpi_reeval) deleted the DURABLE pending_kpi_reeval
    # row on the strength of that 200. A publish failure therefore destroyed its
    # own safety net. The outcome is now explicit and the clearers gate on it.
    #
    # ``None`` = this request never attempted a publish (a regular user render, a
    # persona-narrowed evaluation, or an undeployed model). Consumers MUST treat
    # a missing/None value as "not published" (fail-closed): leaving the outbox
    # row costs one extra re-evaluation, dropping it costs $KPIs availability
    # with no operator signal.
    kpi_latest_published: Optional[bool] = None
    kpi_latest_failed: int = 0
    kpi_latest_persisted: int = 0
    kpi_latest_suppressed: int = 0


class KPISnapshotResponse(BaseModel):
    id: uuid.UUID
    kpi_id: uuid.UUID
    snapshot_at: datetime
    value: Optional[float] = None
    target: Optional[float] = None
    status: Optional[int] = None
    status_label: Optional[str] = None
    trend_pct: Optional[float] = None
    filters_applied: Optional[dict] = None
    evaluation_ms: Optional[int] = None
    created_at: datetime


class KPIResponse(OrmBase):
    id: uuid.UUID
    model_id: uuid.UUID
    name: str
    display_name: Optional[str]
    description: Optional[str]
    display_folder: Optional[str]
    # v2 fields
    kpi_type: Optional[str] = None
    expression: Optional[str] = None
    calc_agg_mode: str = "automatic"
    inner_agg: Optional[str] = None
    inner_grain: Optional[str] = None
    outer_agg: Optional[str] = None
    at_grain: Optional[str] = None
    non_additive_agg: Optional[str] = None
    carry_forward: bool = False
    target_type: Optional[str] = None
    target_value: Optional[float] = None
    target_measure_id: Optional[uuid.UUID] = None
    target_expression: Optional[str] = None
    target_period: Optional[str] = None
    direction: str = "higher_is_better"
    presentation_type: Optional[str] = None
    presentation_meta: Optional[dict] = None
    trend_period: str = "month"
    trend_threshold: float = 0.01
    trend_sparkline_periods: int = 12
    format_token: Optional[str] = None
    format_custom: Optional[str] = None
    unit_label: Optional[str] = None
    null_display_value: str = "N/A"
    weight: Optional[float] = None
    parent_kpi_id: Optional[uuid.UUID] = None
    indicator_type: Optional[str] = "none"
    evaluation_order: Optional[int] = None
    time_dimension_id: Optional[uuid.UUID] = None
    business_definition: Optional[dict] = None
    certification_status: CertificationStatus = "draft"
    replacement_id: Optional[uuid.UUID] = None
    owner_user_id: Optional[str] = None
    is_deployed: Optional[bool] = False
    deployed_at: Optional[datetime] = None
    snapshot_frequency: Optional[str] = None
    snapshot_retention: Optional[int] = 90
    created_at: datetime
    updated_at: datetime
    created_by: Optional[str] = None
    # Legacy v1 fields — deprecated, retained for API response backward compatibility
    value_measure_id: Optional[uuid.UUID] = None
    goal_measure_id: Optional[uuid.UUID] = None
    status_expression: Optional[str] = None
    trend_expression: Optional[str] = None
    status_graphic: Optional[str] = "Traffic Light"
    trend_graphic: Optional[str] = "Standard Arrow"

    @field_validator("certification_status", mode="before")
    @classmethod
    def _read_cert_status(cls, v):
        return _coerce_certification_status_for_read(v)


# ---------------------------------------------------------------------------
# Version History
# ---------------------------------------------------------------------------

class VersionResponse(BaseModel):
    id: uuid.UUID
    version_number: int
    changed_by: Optional[str] = None
    changed_at: datetime
    change_summary: Optional[str] = None
    snapshot: dict


class CertifyRequest(BaseModel):
    pass


class DeprecateRequest(BaseModel):
    replacement_id: Optional[uuid.UUID] = None


class EntityUsageCreate(BaseModel):
    workbook_id: Optional[str] = None
    worksheet: Optional[str] = None
    cell_reference: Optional[str] = None
    usage_type: str


class EntityUsageResponse(BaseModel):
    id: uuid.UUID
    workbook_id: Optional[str] = None
    worksheet: Optional[str] = None
    cell_reference: Optional[str] = None
    usage_type: str
    reported_by: Optional[str] = None
    reported_at: datetime


class UserPreferenceToggle(BaseModel):
    # A closed vocabulary on purpose: an unknown entity_type writes a row the
    # read path silently filters out, accumulating unreadable junk. Validate at
    # write time instead (F-029-12).
    #
    # "model" is the odd member and is scoped differently from the other two
    # (Bug-8183 / Bug-8899). A KPI or named set proves it belongs to the route's
    # model through its own ``model_id`` column; a MODEL *is* that scope, and the
    # ``Model`` ORM class has no ``model_id`` attribute at all, so it proves
    # membership by identity — ``entity_id`` must equal the model in the path.
    # ``preferences._validate_entity_exists`` branches on exactly that; running
    # ``Model`` through the generic branch raises AttributeError -> HTTP 500.
    entity_type: Literal["kpi", "named_set", "model"]
    entity_id: uuid.UUID


class UserPreferencesResponse(BaseModel):
    favourites: dict[str, list[uuid.UUID]]
    recently_used: dict[str, list[uuid.UUID]]


class FavouriteModelsResponse(BaseModel):
    """Every model in ONE project the calling user has favourited.

    The per-model preferences route answers for a single model, which is the
    right shape for the panels that toggle a KPI or a named set. The Explorer
    ranks a whole project's model list at once, and asking the per-model route
    once per card is an N+1 that grows with the project. This is a read
    projection over the SAME ``user_entity_preference`` rows that route writes —
    not a second store.
    """

    model_ids: list[uuid.UUID]


# ---------------------------------------------------------------------------
# Saved Queries
# ---------------------------------------------------------------------------

class SavedQueryCreate(BaseModel):
    name: str = Field(max_length=255)
    description: Optional[str] = None
    query_text: str
    # The Query Panel runs saved queries through the SQL/DAX gateway; an
    # arbitrary query_type stored here would render as garbage in the panel
    # badge and could never be routed. Constrain to the supported set (F-029-16).
    query_type: Literal["sql", "dax"] = "sql"


class SavedQueryUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=255)
    description: Optional[str] = None
    query_text: Optional[str] = None
    query_type: Optional[Literal["sql", "dax"]] = None


class SavedQueryResponse(OrmBase):
    id: uuid.UUID
    model_id: uuid.UUID
    name: str
    description: Optional[str]
    query_text: str
    query_type: str
    created_by: str
    created_at: datetime
    updated_at: datetime
    is_owner: Optional[bool] = None
    # Bug-5983: distinct from ``is_owner`` -- saved queries can also be
    # mutated by a modeler+ who is not the owner (``_require_owner_or_modeler``
    # in ``saved_queries.py``), unlike pivot views' strict owner-only
    # mutation. The frontend must gate edit/delete controls on this field,
    # not ``is_owner``, or a modeler's own legitimate permission is hidden.
    can_edit: Optional[bool] = None


# ---------------------------------------------------------------------------
# Solidatus Integration
# ---------------------------------------------------------------------------


class SolidatusConnectionCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=255)
    base_url: str = Field(min_length=1)
    # Bug-7523: constrain to supported values.
    auth_type: Literal["bearer_token"] = "bearer_token"
    token: str = Field(min_length=1)  # plaintext — encrypted before storage
    workspace_id: Optional[str] = None
    model_ref: Optional[str] = None
    sync_scope: Literal["model"] = "model"


class SolidatusConnectionUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=255)
    base_url: Optional[str] = None
    auth_type: Optional[Literal["bearer_token"]] = None
    token: Optional[str] = None
    workspace_id: Optional[str] = None
    model_ref: Optional[str] = None
    sync_scope: Optional[Literal["model"]] = None
    is_active: Optional[bool] = None


class SolidatusConnectionResponse(OrmBase):
    id: uuid.UUID
    project_id: uuid.UUID
    model_id: uuid.UUID
    display_name: str
    base_url: str
    auth_type: str
    workspace_id: Optional[str] = None
    model_ref: Optional[str] = None
    sync_scope: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class SolidatusValidateRequest(BaseModel):
    connection_id: uuid.UUID


class SolidatusValidateResponse(BaseModel):
    # ok is tri-state: True = verified live, False = verified failed,
    # None = simulated (connector not contacted). simulated=True flags the
    # placeholder path so the UI never renders an uncontacted check as a pass.
    ok: Optional[bool] = None
    simulated: bool = False
    base_url: str
    workspace_found: Optional[bool] = None
    model_ref_found: Optional[bool] = None
    warnings: list[str] = []


class SolidatusExportPreviewRequest(BaseModel):
    connection_id: Optional[uuid.UUID] = None
    include_technical: bool = True
    include_aggregates: bool = True
    include_downstream_assets: bool = True
    include_glossary: bool = True
    include_security_tags: bool = True
    # Bug-7719: expose include_hidden_objects.
    include_hidden_objects: bool = True
    export_draft: bool = False


class SolidatusExportPreviewResponse(BaseModel):
    nodes_total: int
    edges_total: int
    by_type: dict[str, int] = {}
    warnings: list[dict] = []


class SolidatusSyncRequest(BaseModel):
    connection_id: uuid.UUID
    # Only the two modes /sync actually performs are advertised. Connection
    # validation lives on /validate; export inspection on /export-preview.
    mode: Literal["dry_run", "push"] = "push"
    dry_run: bool = False
    include_technical: bool = True
    include_aggregates: bool = True
    include_downstream_assets: bool = True
    include_glossary: bool = True
    include_security_tags: bool = True
    include_hidden_objects: bool = True
    export_draft: bool = False
    deprecate_missing: bool = True


class SolidatusSyncResponse(BaseModel):
    run_id: uuid.UUID
    status: str
    nodes_total: int = 0
    edges_total: int = 0
    nodes_created: int = 0
    nodes_updated: int = 0
    edges_created: int = 0
    edges_updated: int = 0
    # Bug-7522: governance-quality warnings, matching CollibraSyncResponse.
    warnings: list[dict] = []
    error_message: Optional[str] = None


class SolidatusSyncRunResponse(OrmBase):
    id: uuid.UUID
    connection_id: uuid.UUID
    project_id: uuid.UUID
    model_id: Optional[uuid.UUID] = None
    mode: str
    status: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    tessallite_snapshot_hash: Optional[str] = None
    solidatus_target_ref: Optional[str] = None
    nodes_total: int
    edges_total: int
    nodes_created: int
    nodes_updated: int
    edges_created: int
    edges_updated: int
    error_message: Optional[str] = None


class SolidatusObjectMappingResponse(OrmBase):
    id: uuid.UUID
    connection_id: uuid.UUID
    tessallite_object_type: str
    tessallite_object_id: str
    tessallite_stable_key: str
    solidatus_object_id: Optional[str] = None
    solidatus_object_ref: Optional[str] = None
    last_payload_hash: str
    last_synced_at: datetime
    last_sync_run_id: Optional[uuid.UUID] = None


# ---------------------------------------------------------------------------
# Collibra Integration
# ---------------------------------------------------------------------------


class CollibraConnectionCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=255)
    base_url: str = Field(min_length=1)
    auth_type: Literal["bearer_token"] = "bearer_token"
    token: str = Field(min_length=1)
    community_id: Optional[str] = None
    domain_id: Optional[str] = None
    asset_type_mapping: dict[str, str] = {}
    relation_type_mapping: dict[str, str] = {}
    responsibility_mapping: dict[str, str] = {}
    sync_scope: Literal["model"] = "model"
    sync_mode: Literal["rest_api"] = "rest_api"


class CollibraConnectionUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=255)
    base_url: Optional[str] = None
    auth_type: Optional[Literal["bearer_token"]] = None
    token: Optional[str] = None
    community_id: Optional[str] = None
    domain_id: Optional[str] = None
    asset_type_mapping: Optional[dict[str, str]] = None
    relation_type_mapping: Optional[dict[str, str]] = None
    responsibility_mapping: Optional[dict[str, str]] = None
    sync_scope: Optional[Literal["model"]] = None
    sync_mode: Optional[Literal["rest_api"]] = None
    is_active: Optional[bool] = None


class CollibraConnectionResponse(OrmBase):
    id: uuid.UUID
    project_id: uuid.UUID
    model_id: uuid.UUID
    display_name: str
    base_url: str
    auth_type: str
    community_id: Optional[str] = None
    domain_id: Optional[str] = None
    sync_scope: str
    sync_mode: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class CollibraValidateRequest(BaseModel):
    connection_id: uuid.UUID


class CollibraValidateResponse(BaseModel):
    # ok is tri-state: True = verified live, False = verified failed,
    # None = simulated (connector not contacted). simulated=True flags the
    # placeholder path so the UI never renders an uncontacted check as a pass.
    ok: Optional[bool] = None
    simulated: bool = False
    base_url: str
    community_found: Optional[bool] = None
    domain_found: Optional[bool] = None
    missing_asset_types: list[str] = []
    missing_relation_types: list[str] = []
    warnings: list[str] = []


class CollibraExportPreviewRequest(BaseModel):
    connection_id: Optional[uuid.UUID] = None
    include_business_assets: bool = True
    include_technical_assets: bool = True
    include_hidden_objects: bool = True
    include_glossary: bool = True
    include_downstream_assets: bool = True
    include_aggregates: bool = True
    include_data_tags: bool = True
    include_responsibilities: bool = True
    export_draft: bool = False


class CollibraExportPreviewResponse(BaseModel):
    assets_total: int
    relations_total: int
    attributes_total: int = 0
    responsibilities_total: int = 0
    by_asset_type: dict[str, int] = {}
    warnings: list[dict] = []


class CollibraSyncRequest(BaseModel):
    connection_id: uuid.UUID
    dry_run: bool = False
    # Only flags /sync actually forwards to the graph builder are advertised.
    # Business assets and hidden objects are always included by the sync path;
    # scoped subsets (excluding them) are available via /export-preview.
    include_technical_assets: bool = True
    include_glossary: bool = True
    include_downstream_assets: bool = True
    include_aggregates: bool = True
    include_data_tags: bool = True
    include_responsibilities: bool = True
    deprecate_missing: bool = True
    export_draft: bool = False


class CollibraSyncResponse(BaseModel):
    run_id: uuid.UUID
    status: str
    assets_total: int = 0
    relations_total: int = 0
    attributes_total: int = 0
    responsibilities_total: int = 0
    assets_created: int = 0
    assets_updated: int = 0
    relations_created: int = 0
    relations_updated: int = 0
    warnings: list[dict] = []
    error_message: Optional[str] = None


class CollibraSyncRunResponse(OrmBase):
    id: uuid.UUID
    connection_id: uuid.UUID
    project_id: uuid.UUID
    model_id: Optional[uuid.UUID] = None
    mode: str
    status: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    tessallite_snapshot_hash: Optional[str] = None
    collibra_import_job_id: Optional[str] = None
    assets_total: int
    relations_total: int
    attributes_total: int
    responsibilities_total: int
    assets_created: int
    assets_updated: int
    relations_created: int
    relations_updated: int
    error_message: Optional[str] = None


class CollibraObjectMappingResponse(OrmBase):
    id: uuid.UUID
    connection_id: uuid.UUID
    tessallite_object_type: str
    tessallite_object_id: str
    tessallite_stable_key: str
    collibra_resource_type: str
    collibra_resource_id: Optional[str] = None
    collibra_full_name: Optional[str] = None
    last_payload_hash: str
    last_synced_at: datetime
    last_sync_run_id: Optional[uuid.UUID] = None
    is_deprecated: bool
