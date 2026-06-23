"""Auto-split from pydantic_models.py — Data Quality Rules, Query Logs, Lineage, Metrics, Model Export/Import, RBAC — User Access Bindings, AI Optimiser schemas, Glossary (semantic-layer Phase 3), Audit events, Schema drift, Downstream Assets (Impact Analysis — Phase 9 Block G), Data Tagging + Column-Level Persona Security (Phase 9 Block H), Notification routes, Named Sets, KPIs, Version History, Saved Queries"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..measure_formats import (
    HIERARCHY_TIME_CALCS as _HIERARCHY_TIME_CALCS,
    HIERARCHY_TIME_UNITS as _HIERARCHY_TIME_UNITS,
    MEASURE_FORMAT_TOKENS as _MEASURE_FORMAT_TOKENS,
    TIME_VARIANT_NAMES as _TIME_VARIANT_NAMES,
)

from ._base import OrmBase


def _validate_cron(value: str | None) -> str | None:
    """Validate a cron expression using croniter.

    Re-uses the same logic as the refresh-policy cron gate in
    aggregates_security.py. None/empty passes through unchanged.
    """
    if value is None:
        return value
    trimmed = value.strip()
    if not trimmed:
        return None
    from croniter import croniter

    if not croniter.is_valid(trimmed):
        raise ValueError(f"Invalid cron expression: {value!r}")
    return trimmed

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
    role: str = Field(description="admin | modeler | viewer")
    model_id: Optional[uuid.UUID] = Field(
        None,
        description="Optional model-level scope; omit for project-level binding",
    )


class UserAccessBindingResponse(OrmBase):
    id: uuid.UUID
    project_id: Optional[uuid.UUID]
    model_id: Optional[uuid.UUID]
    user_identity: str
    role: str
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
NAMED_SET_LIST_TYPES = ("fixed", "dynamic_top_n", "filtered", "advanced_mdx")


class NamedSetCreate(BaseModel):
    name: str = Field(max_length=255)
    display_name: Optional[str] = None
    description: Optional[str] = None
    display_folder: Optional[str] = None
    scope: int = Field(default=1, description="1=SESSION, 2=GLOBAL per XMLA spec")
    expression: Optional[str] = Field(default=None, description="Raw MDX expression; auto-generated from builder_definition if omitted")
    dimensions: Optional[str] = None
    builder_definition: Optional[dict] = Field(default=None, description="Structured builder JSON compiled to MDX on save")
    list_type: Optional[str] = Field(default="advanced_mdx", description="fixed | dynamic_top_n | filtered | advanced_mdx")
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
    certification_status: Optional[str] = None
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
    certification_status: str = "draft"
    replacement_id: Optional[uuid.UUID] = None
    owner_user_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# KPIs
# ---------------------------------------------------------------------------

class KPICreate(BaseModel):
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
    owner_user_id: Optional[str] = None
    # Snapshots
    snapshot_frequency: Optional[str] = None
    snapshot_retention: int = 90
    status_graphic: str = "Traffic Light"
    trend_graphic: str = "Standard Arrow"


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
    certification_status: Optional[str] = None
    owner_user_id: Optional[str] = None
    # Deployment
    is_deployed: Optional[bool] = None
    # Snapshots
    snapshot_frequency: Optional[str] = None
    snapshot_retention: Optional[int] = None
    status_graphic: Optional[str] = None
    trend_graphic: Optional[str] = None


class KPITrendPoint(BaseModel):
    period: str
    value: Optional[float] = None


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
    # child KPI's evaluation FAILED (broken input — not no-data), or "error"
    # when every child errored and no real score could be produced.
    # ``errored_children`` lists the failed children with a per-child reason so
    # the scorecard/UI can show which input is broken rather than silently
    # dropping it. A no-data child stays silently excluded and is NOT listed.
    composite_status: Optional[str] = None
    errored_children: Optional[list[dict]] = None
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


class KPIBatchResponse(BaseModel):
    results: list[KPIEvaluateResponse]
    evaluation_ms: Optional[int] = None


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
    certification_status: str = "draft"
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
    # Only KPIs and named sets carry favourite / recently-used preferences; an
    # unknown entity_type writes a row the read path silently filters out,
    # accumulating unreadable junk. Validate at write time instead (F-029-12).
    entity_type: Literal["kpi", "named_set"]
    entity_id: uuid.UUID


class UserPreferencesResponse(BaseModel):
    favourites: dict[str, list[uuid.UUID]]
    recently_used: dict[str, list[uuid.UUID]]


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


# ---------------------------------------------------------------------------
# Solidatus Integration
# ---------------------------------------------------------------------------


class SolidatusConnectionCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=255)
    base_url: str = Field(min_length=1)
    auth_type: str = "bearer_token"
    token: str = Field(min_length=1)  # plaintext — encrypted before storage
    workspace_id: Optional[str] = None
    model_ref: Optional[str] = None
    sync_scope: str = "model"


class SolidatusConnectionUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=255)
    base_url: Optional[str] = None
    auth_type: Optional[str] = None
    token: Optional[str] = None
    workspace_id: Optional[str] = None
    model_ref: Optional[str] = None
    sync_scope: Optional[str] = None
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
    auth_type: str = "bearer_token"
    token: str = Field(min_length=1)
    community_id: Optional[str] = None
    domain_id: Optional[str] = None
    asset_type_mapping: dict[str, str] = {}
    relation_type_mapping: dict[str, str] = {}
    responsibility_mapping: dict[str, str] = {}
    sync_scope: str = "model"
    sync_mode: str = "rest_api"


class CollibraConnectionUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=255)
    base_url: Optional[str] = None
    auth_type: Optional[str] = None
    token: Optional[str] = None
    community_id: Optional[str] = None
    domain_id: Optional[str] = None
    asset_type_mapping: Optional[dict[str, str]] = None
    relation_type_mapping: Optional[dict[str, str]] = None
    responsibility_mapping: Optional[dict[str, str]] = None
    sync_scope: Optional[str] = None
    sync_mode: Optional[str] = None
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
