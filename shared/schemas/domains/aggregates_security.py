"""Auto-split from pydantic_models.py — Drill-through sets (Phase 4C.1), Personas (Phase 8.B), Join, Aggregate, Legacy aliases, Row Security (Phase 5.1)"""
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

# ---------------------------------------------------------------------------
# Drill-through sets (Phase 4C.1)
# ---------------------------------------------------------------------------

class DrillThroughSetUpdate(BaseModel):
    """Partial update for an implicit drill-through row.

    All fields are optional. Null means "apply the implicit default" —
    the drill builder resolves the fact table, all source columns, and
    pagination at query time.
    """

    source_table_id: Optional[uuid.UUID] = Field(
        default=None,
        description=(
            "Override the fact table used for drill-through. Null = derive "
            "from the measure's source column at query time."
        ),
    )
    detail_columns: Optional[list[uuid.UUID]] = Field(
        default=None,
        description=(
            "Concrete list of ModelColumn ids to include in the drill-through "
            "payload. Null = include every column on the fact table."
        ),
    )
    joined_dimension_ids: Optional[list[uuid.UUID]] = Field(
        default=None,
        description=(
            "Dimension ids to expand via joins alongside the fact-table "
            "columns. Null = use the dimensions from the originating query."
        ),
    )
    row_limit_override: Optional[int] = Field(
        default=None,
        description=(
            "Per-measure cap on rows returned. Null = use the default page "
            "size. Reserved for Phase 8 curation."
        ),
    )
    source_join_path: Optional[list[uuid.UUID]] = Field(
        default=None,
        description=(
            "Ordered list of Join ids from the override source table back "
            "to the fact table. Required when the override needs more than "
            "one hop; null = single-path auto-resolvable."
        ),
    )


class DrillThroughSetResponse(OrmBase):
    id: uuid.UUID
    measure_id: uuid.UUID
    source_table_id: Optional[uuid.UUID] = None
    detail_columns: Optional[list[uuid.UUID]] = None
    joined_dimension_ids: Optional[list[uuid.UUID]] = None
    row_limit_override: Optional[int] = None
    source_join_path: Optional[list[uuid.UUID]] = None
    created_at: datetime
    updated_at: datetime


class DrillThroughSetColumnInfo(BaseModel):
    id: uuid.UUID
    name: str
    display_name: Optional[str] = None
    source_type: str = "column"


class DrillThroughSetEnrichedResponse(BaseModel):
    id: uuid.UUID
    measure_id: uuid.UUID
    source_table_id: Optional[uuid.UUID] = None
    detail_columns: list["DrillThroughSetColumnInfo"] = []
    joined_dimension_ids: Optional[list[uuid.UUID]] = None
    row_limit_override: Optional[int] = None
    source_join_path: Optional[list[uuid.UUID]] = None


class DrillJoinPathHop(BaseModel):
    join_id: uuid.UUID
    left_table_id: uuid.UUID
    right_table_id: uuid.UUID


class DrillJoinPath(BaseModel):
    hops: list[DrillJoinPathHop]
    cardinality_hint: str = Field(
        description=(
            "'many-to-one' | 'one-to-many' | 'mixed' — best-effort summary "
            "across the hops. Pure heuristic; the planner does not rely on it."
        ),
    )


class DrillJoinPathsResponse(BaseModel):
    paths: list[DrillJoinPath]


# ---------------------------------------------------------------------------
# Personas (Phase 8.B)
# ---------------------------------------------------------------------------

# F-008-16: the single canonical set of operators a persona ``default_filters``
# value may use. Save-time validation (model-service) and the query-time merge
# (query-router ``persona_gate._SUPPORTED_OPERATORS``, which imports this) read
# the same set so the two can never drift apart.
PERSONA_FILTER_OPERATORS: frozenset[str] = frozenset(
    {
        "eq", "neq", "gt", "gte", "lt", "lte",
        "in", "not_in", "between", "like", "not_like",
        "is_null", "is_not_null",
    }
)


class PersonaCreate(BaseModel):
    """Create a persona. Empty include lists mean "unrestricted".

    Populated lists become allow-lists. ``default_filters`` is the
    slicer-chip-shaped JSONB the pivot seeds when this persona is
    active. ``audience_roles`` are free-form strings matched against
    the caller's JWT roles. ``slug`` is the catalog suffix the gateway
    exposes as ``<model.slug>_<persona.slug>``.
    """

    name: str = Field(max_length=255)
    # F-008-21: the slug is the catalogue suffix the gateway exposes as
    # ``<model.slug>_<persona.slug>`` — it must be a safe relation token.
    # The pattern was previously enforced only in the frontend, so the API
    # could mint malformed catalogue names.
    slug: str = Field(max_length=64, pattern=r"^[a-z0-9_]+$")
    description: Optional[str] = None
    included_measure_ids: list[uuid.UUID] = Field(default_factory=list)
    included_dimension_ids: list[uuid.UUID] = Field(default_factory=list)
    included_hierarchy_ids: list[uuid.UUID] = Field(default_factory=list)
    audience_roles: list[str] = Field(default_factory=list)
    default_filters: dict[str, Any] = Field(default_factory=dict)
    bypass_row_security: bool = False
    includes_hidden_columns: bool = False


class PersonaUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=255)
    slug: Optional[str] = Field(default=None, max_length=64, pattern=r"^[a-z0-9_]+$")
    description: Optional[str] = None
    included_measure_ids: Optional[list[uuid.UUID]] = None
    included_dimension_ids: Optional[list[uuid.UUID]] = None
    included_hierarchy_ids: Optional[list[uuid.UUID]] = None
    audience_roles: Optional[list[str]] = None
    default_filters: Optional[dict[str, Any]] = None
    bypass_row_security: Optional[bool] = None
    includes_hidden_columns: Optional[bool] = None


class PersonaResponse(OrmBase):
    id: uuid.UUID
    model_id: uuid.UUID
    name: str
    slug: str
    description: Optional[str] = None
    included_measure_ids: list[uuid.UUID] = Field(default_factory=list)
    included_dimension_ids: list[uuid.UUID] = Field(default_factory=list)
    included_hierarchy_ids: list[uuid.UUID] = Field(default_factory=list)
    audience_roles: list[str] = Field(default_factory=list)
    default_filters: dict[str, Any] = Field(default_factory=dict)
    bypass_row_security: bool = False
    includes_hidden_columns: bool = False
    # F-008-05 residual: model_column ids restricted for this persona via
    # data-tag restrictions. The gateway uses this to drop restricted
    # column names from the persona's catalogue metadata.
    restricted_column_ids: list[uuid.UUID] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class PersonaResolution(BaseModel):
    """Helper for the frontend to validate scope before running a query.

    Backward compatible with the measure-only shape (``measure_id`` +
    ``measure_allowed``). F-008-22 adds a generic object explainer:
    ``object_kind`` / ``object_id`` / ``allowed`` cover measures,
    dimensions, hierarchies and data-tag restrictions through one endpoint.
    """

    persona_id: uuid.UUID
    # Generic object explainer (F-008-22).
    object_kind: Optional[str] = None  # measure | dimension | hierarchy | tag
    object_id: Optional[uuid.UUID] = None
    allowed: Optional[bool] = None
    # Legacy measure fields (kept for the existing frontend caller).
    measure_id: Optional[uuid.UUID] = None
    measure_allowed: Optional[bool] = None
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Join
# ---------------------------------------------------------------------------

class JoinCreate(BaseModel):
    left_table_id: uuid.UUID
    right_table_id: uuid.UUID
    join_type: str = "inner"
    left_column_name: str = Field(description="Column name on the left table")
    right_column_name: str = Field(description="Column name on the right table")


class JoinUpdate(BaseModel):
    join_type: Optional[str] = None
    left_column_name: Optional[str] = None
    right_column_name: Optional[str] = None


class JoinResponse(OrmBase):
    id: uuid.UUID
    model_id: uuid.UUID
    left_table_id: uuid.UUID
    right_table_id: uuid.UUID
    join_type: str
    left_column_id: uuid.UUID
    right_column_id: uuid.UUID
    left_column_name: Optional[str] = None
    right_column_name: Optional[str] = None
    created_at: datetime
    warnings: Optional[list[str]] = None


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

class AggregateDefinitionCreate(BaseModel):
    target_id: uuid.UUID
    target_schema: Optional[str] = None
    grain: list[str] = Field(default_factory=list)
    measure_names: list[str] = Field(default_factory=list)
    creation_reason: str = "manual"
    include_quantiles: bool = False
    include_stats: bool = False
    confirm_redundant_grain: bool = False


class AggregateDefinitionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Optional[str] = None
    include_quantiles: Optional[bool] = None
    include_stats: Optional[bool] = None
    target_schema: Optional[str] = None


class AggregateDefinitionResponse(OrmBase):
    id: uuid.UUID
    model_id: uuid.UUID
    target_id: uuid.UUID
    # Phase 8.C.2 — persona scope. NULL means global (serves any
    # query); a populated id scopes this aggregate to one persona.
    persona_id: Optional[uuid.UUID] = None
    physical_table_name: str
    target_schema: Optional[str]
    status: str
    grain: list
    grain_physical_cols: Optional[list] = None
    invalid_reason: Optional[str] = None
    measure_names: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    source_row_count: Optional[int]
    agg_row_count: Optional[int]
    estimated_hit_rate: Optional[float]
    creation_reason: str
    # F-010-18 — when a predictive aggregate's grain later receives real query
    # traffic, the feedback sweep stamps this timestamp. The UI shows a
    # "Validated" badge on predictive cards that carry it. NULL otherwise.
    predictive_validated_at: Optional[datetime] = None
    # Populated for AI-created aggregates (creation_reason == "ai") from the
    # latest AIAggregateRecommendation that produced this aggregate. NULL for
    # all other creation reasons. (F-011-03)
    rationale: Optional[str] = None
    include_quantiles: bool
    include_stats: bool = False
    created_at: datetime
    updated_at: datetime
    last_refreshed_at: Optional[datetime]
    retired_at: Optional[datetime]
    # F-010 freshness flag — an aggregate marked stale (e.g. its stat/quantile
    # coverage changed) is not routed until rebuilt. Serialized so the UI can
    # show "Outdated" and so health reflects actual serving state. Optional in
    # the pydantic shape because the column's server_default fires at flush, so
    # a row serialized before refresh carries None (same as canvas_layout /
    # predictive_requires_approval above) — treated as "not stale".
    is_stale: Optional[bool] = False
    # Derived health signal (not a DB column). "healthy" = serving or
    # intentionally paused-but-materialised; "unhealthy" = not serving.
    # An ACTIVE aggregate that is stale or has never been refreshed
    # (last_refreshed_at is None) is NOT routable by the query-router
    # (aggregate_matcher requires status=active + non-null last_refreshed_at),
    # so it counts as unhealthy even though its status is "active". Disabled
    # aggregates stay healthy (intentionally paused, still materialised).
    # NOTE: this UI "serving-now" health is deliberately stricter than the
    # optimizer LLM-dedup rule (HEALTHY_AGGREGATE_STATUSES): a stale aggregate
    # still *covers* its grain (it will refresh), so the LLM must not propose a
    # duplicate for it — hence dedup is status-only while UI health adds
    # freshness.
    health: Literal["healthy", "unhealthy"] = "healthy"

    @model_validator(mode="after")
    def _derive_health(self) -> "AggregateDefinitionResponse":
        if self.status not in HEALTHY_AGGREGATE_STATUSES:
            self.health = "unhealthy"
        elif self.status == "active" and (self.is_stale or self.last_refreshed_at is None):
            # Active but not actually serving (stale or never built).
            self.health = "unhealthy"
        else:
            self.health = "healthy"
        return self


# Single source of truth for aggregate health (shared by the response model
# above and the optimizer LLM-dedup filter). An aggregate "covers" a grain
# and "serves" queries only when its status is one of these.
HEALTHY_AGGREGATE_STATUSES: frozenset[str] = frozenset({"active", "disabled"})


class PocketDefinitionCreate(BaseModel):
    target_id: uuid.UUID
    defining_sql: str
    refresh_policy: str = "manual"
    refresh_cron: Optional[str] = None
    # Deprecated: the new full-refresh flow schedules via PocketRefreshPolicy.
    # Retained on the schema for backwards compatibility with older callers;
    # the current UI does not send these.
    incremental_column: Optional[str] = Field(default=None, deprecated=True)
    incremental_lookback_hours: Optional[int] = Field(default=None, deprecated=True)
    ttl_days: int = 14
    predicates: list[dict[str, Any]] = Field(default_factory=list)


class PocketDefinitionUpdate(BaseModel):
    defining_sql: Optional[str] = None
    refresh_policy: Optional[str] = None
    refresh_cron: Optional[str] = None
    # Deprecated: see PocketDefinitionCreate.
    incremental_column: Optional[str] = Field(default=None, deprecated=True)
    incremental_lookback_hours: Optional[int] = Field(default=None, deprecated=True)
    ttl_days: Optional[int] = None
    # F-005-09 (Bug-2252): `status` is NOT a client-editable field. Lifecycle
    # status is owned by the refresh engine (stale → invalidating → fresh|failed)
    # and the eviction janitor. Accepting a client-supplied `status` let a caller
    # resurrect an unmaterialised pocket into the matcher pool (status="fresh"
    # with no physical table) or write a non-enum string that violates the DB
    # CHECK and surfaces as an unhandled 500. The PATCH handler still sets
    # status="stale" itself when `defining_sql` changes (a new slice needs a
    # rebuild). Field removed so it cannot be set out of band.


class PocketPredicateResponse(OrmBase):
    id: uuid.UUID
    pocket_definition_id: uuid.UUID
    column_name: str
    operator: str
    value_json: dict[str, Any]
    created_at: datetime


class PocketRefreshRunResponse(OrmBase):
    id: uuid.UUID
    pocket_definition_id: uuid.UUID
    refresh_mode: str
    status: str
    started_at: datetime
    completed_at: Optional[datetime]
    rows_written: Optional[int]
    bytes_processed: Optional[int]
    error_message: Optional[str]
    triggered_by: str


class PocketRefreshPolicyUpsert(BaseModel):
    """Payload for PUT ``/pockets/{id}/refresh/policy``.

    Full-refresh-only, so no ``refresh_mode`` / ``incremental_*``.  A NULL
    ``cron_expression`` plus ``is_enabled=False`` means "never refresh".
    """

    cron_expression: Optional[str] = None
    is_enabled: bool = False


class PocketRefreshPolicyResponse(OrmBase):
    id: uuid.UUID
    pocket_definition_id: uuid.UUID
    cron_expression: Optional[str]
    is_enabled: bool
    created_at: datetime
    updated_at: datetime


class PocketValidateRequest(BaseModel):
    defining_sql: str
    target_id: Optional[uuid.UUID] = None


class PocketViolationItem(BaseModel):
    code: str
    message: str
    suggestion: str


class PocketValidateResponse(BaseModel):
    ok: bool
    stage: str = Field(description='"parse" | "subset" | "probe"')
    error: Optional[str] = None
    warning: Optional[str] = None
    columns: Optional[list[str]] = None
    violations: Optional[list[PocketViolationItem]] = None


class PocketDryRunRequest(BaseModel):
    defining_sql: str
    target_id: Optional[uuid.UUID] = None


class PocketDryRunResponse(BaseModel):
    ok: bool
    row_count: Optional[int] = None
    elapsed_ms: Optional[int] = None
    error: Optional[str] = None


class PocketDefinitionResponse(OrmBase):
    id: uuid.UUID
    model_id: uuid.UUID
    target_id: uuid.UUID
    physical_table_name: str
    target_schema: Optional[str]
    defining_sql: str
    query_fingerprint: str
    predicate_set_hash: str
    row_count: Optional[int]
    storage_bytes: Optional[int]
    refresh_policy: str
    refresh_cron: Optional[str]
    incremental_column: Optional[str]
    incremental_lookback_hours: Optional[int]
    ttl_days: int
    status: str
    failure_reason: Optional[str]
    last_refresh_at: Optional[datetime]
    last_access_at: Optional[datetime]
    last_match_at: Optional[datetime]
    hit_count: int
    time_saved_ms_total: int
    created_at: datetime
    updated_at: datetime
    retired_at: Optional[datetime]
    predicates: list[PocketPredicateResponse] = Field(default_factory=list)
    refresh_policy_row: Optional[PocketRefreshPolicyResponse] = None


class AggregateColumnResponse(OrmBase):
    id: uuid.UUID
    aggregate_definition_id: uuid.UUID
    measure_id: Optional[uuid.UUID]
    physical_col_name: str
    stat_type: str
    created_at: datetime


def _validate_cron_expression(value: Optional[str]) -> Optional[str]:
    """F-012-08: reject a cron expression that croniter cannot parse.

    An unparseable cron was previously accepted and only surfaced at sweep time
    as a logged error, leaving the aggregate silently never refreshed. Validate
    at write time so the admin gets immediate feedback (422). ``None``/empty is
    left to the enabled-policy guard below.
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


# Refresh modes accepted on a policy. ``scheduled`` (cron-driven full refresh)
# and ``incremental`` (watermark refresh) are the only supported values.
_REFRESH_MODES = frozenset({"scheduled", "incremental"})


class RefreshPolicyCreate(BaseModel):
    refresh_mode: str = "scheduled"
    cron_expression: Optional[str] = None
    incremental_column: Optional[str] = None
    incremental_lookback: Optional[int] = None
    is_enabled: bool = True

    @field_validator("refresh_mode")
    @classmethod
    def _check_mode(cls, v: str) -> str:
        if v not in _REFRESH_MODES:
            raise ValueError(
                f"refresh_mode must be one of {sorted(_REFRESH_MODES)}, got {v!r}"
            )
        return v

    @field_validator("cron_expression")
    @classmethod
    def _check_cron(cls, v: Optional[str]) -> Optional[str]:
        return _validate_cron_expression(v)

    @model_validator(mode="after")
    def _require_cron_when_enabled(self) -> "RefreshPolicyCreate":
        # F-012-08: an enabled policy with no cron is never scheduled — the
        # sweep can never compute a due time, so the aggregate would silently
        # never refresh. Reject it at write time.
        if self.is_enabled and not self.cron_expression:
            raise ValueError(
                "An enabled refresh policy must have a cron_expression; "
                "otherwise the aggregate would never be scheduled for refresh."
            )
        return self


class RefreshPolicyUpdate(BaseModel):
    refresh_mode: Optional[str] = None
    cron_expression: Optional[str] = None
    incremental_column: Optional[str] = None
    incremental_lookback: Optional[int] = None
    is_enabled: Optional[bool] = None

    @field_validator("refresh_mode")
    @classmethod
    def _check_mode(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in _REFRESH_MODES:
            raise ValueError(
                f"refresh_mode must be one of {sorted(_REFRESH_MODES)}, got {v!r}"
            )
        return v

    @field_validator("cron_expression")
    @classmethod
    def _check_cron(cls, v: Optional[str]) -> Optional[str]:
        return _validate_cron_expression(v)


class RefreshPolicyResponse(OrmBase):
    id: uuid.UUID
    aggregate_definition_id: uuid.UUID
    refresh_mode: str
    cron_expression: Optional[str]
    incremental_column: Optional[str]
    incremental_lookback: Optional[int]
    is_enabled: bool
    created_at: datetime
    updated_at: datetime


class RefreshRunResponse(OrmBase):
    id: uuid.UUID
    aggregate_definition_id: uuid.UUID
    aggregate_table: Optional[str] = None
    refresh_mode: str
    status: str
    started_at: datetime
    completed_at: Optional[datetime]
    duration_ms: Optional[int] = None
    rows_written: Optional[int]
    bytes_processed: Optional[int]
    error_message: Optional[str]
    triggered_by: str


# Legacy aliases
AggregateRefreshPolicyResponse = RefreshPolicyResponse
AggregateRefreshRunResponse = RefreshRunResponse
PolicyUpdate = RefreshPolicyUpdate


class RefreshRequest(BaseModel):
    refresh_mode: str = "full"  # full | incremental | on_demand


# ---------------------------------------------------------------------------
# Row Security (Phase 5.1)
# ---------------------------------------------------------------------------

_ROW_SECURITY_RULE_TYPES = ("role_predicate", "user_mapping")
_ROW_SECURITY_ATTRIBUTE_SOURCES = ("jwt_role", "idp_group", "saml_claim", "oidc_scope")


class RowSecurityRuleBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    dimension_path: str = Field(min_length=1, max_length=255)
    rule_type: str
    predicate_expression: Optional[str] = None
    applies_to_roles: Optional[list[str]] = None
    mapping_table_id: Optional[uuid.UUID] = None
    mapping_user_column: Optional[str] = Field(default=None, max_length=255)
    mapping_value_column: Optional[str] = Field(default=None, max_length=255)
    is_enabled: bool = True
    attribute_source: str = "jwt_role"
    attribute_claim_name: Optional[str] = None

    @field_validator("attribute_source")
    @classmethod
    def _attribute_source_allowed(cls, v: str) -> str:
        if v not in _ROW_SECURITY_ATTRIBUTE_SOURCES:
            raise ValueError(
                f"attribute_source must be one of {_ROW_SECURITY_ATTRIBUTE_SOURCES}, got {v!r}"
            )
        return v

    @field_validator("rule_type")
    @classmethod
    def _rule_type_allowed(cls, v: str) -> str:
        if v not in _ROW_SECURITY_RULE_TYPES:
            raise ValueError(
                f"rule_type must be one of {_ROW_SECURITY_RULE_TYPES}, got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _shape_consistency(self) -> "RowSecurityRuleBase":
        if self.rule_type == "role_predicate":
            if not self.predicate_expression:
                raise ValueError(
                    "predicate_expression is required when rule_type='role_predicate'"
                )
            if not self.applies_to_roles:
                raise ValueError(
                    "applies_to_roles is required when rule_type='role_predicate'"
                )
            if (
                self.mapping_table_id is not None
                or self.mapping_user_column is not None
                or self.mapping_value_column is not None
            ):
                raise ValueError(
                    "mapping_* fields must be null when rule_type='role_predicate'"
                )
        else:  # user_mapping
            if (
                self.mapping_table_id is None
                or not self.mapping_user_column
                or not self.mapping_value_column
            ):
                raise ValueError(
                    "mapping_table_id, mapping_user_column, and mapping_value_column "
                    "are required when rule_type='user_mapping'"
                )
            if self.predicate_expression is not None or self.applies_to_roles is not None:
                raise ValueError(
                    "predicate_expression / applies_to_roles must be null "
                    "when rule_type='user_mapping'"
                )
        return self


class RowSecurityRuleCreate(RowSecurityRuleBase):
    pass


class RowSecurityRuleUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    dimension_path: Optional[str] = Field(default=None, min_length=1, max_length=255)
    predicate_expression: Optional[str] = None
    applies_to_roles: Optional[list[str]] = None
    mapping_user_column: Optional[str] = Field(default=None, max_length=255)
    mapping_value_column: Optional[str] = Field(default=None, max_length=255)
    is_enabled: Optional[bool] = None
    attribute_source: Optional[str] = None
    attribute_claim_name: Optional[str] = None

    @field_validator("attribute_source")
    @classmethod
    def _attribute_source_allowed(cls, v: Optional[str]) -> Optional[str]:
        # F-007-08 companion: the update path bypassed every shape check on
        # the create schema. An attribute_source outside the allow-list would
        # silently pass through and resolve to no subject at match time
        # (rule never fires). Reject it loudly instead.
        if v is not None and v not in _ROW_SECURITY_ATTRIBUTE_SOURCES:
            raise ValueError(
                f"attribute_source must be one of {_ROW_SECURITY_ATTRIBUTE_SOURCES}, got {v!r}"
            )
        return v

    @field_validator("applies_to_roles")
    @classmethod
    def _applies_to_roles_non_empty(
        cls, v: Optional[list[str]]
    ) -> Optional[list[str]]:
        # F-007-08: an empty list is NOT NULL (passes the DB CHECK) but
        # matches no role and carries no wildcard, so the rule silently
        # stops firing — a protective rule neutralised by accident. The
        # create schema requires a non-empty list for role_predicate rules;
        # the update path bypassed it. Reject an explicitly-supplied empty
        # list here too (fail closed — never let a rule go silently inert).
        # ``None`` (field omitted) is left untouched: it means "don't change
        # the roles", which is safe.
        if v is not None and len(v) == 0:
            raise ValueError(
                "applies_to_roles cannot be set to an empty list — a "
                "role_predicate rule with no roles (and no '*' wildcard) "
                "would silently match nothing and stop filtering. Supply at "
                "least one role, or '*' to apply to every caller."
            )
        return v


class RowSecurityRuleResponse(OrmBase):
    id: uuid.UUID
    model_id: uuid.UUID
    name: str
    dimension_path: str
    rule_type: str
    predicate_expression: Optional[str]
    applies_to_roles: Optional[list[str]]
    mapping_table_id: Optional[uuid.UUID]
    mapping_user_column: Optional[str]
    mapping_value_column: Optional[str]
    is_enabled: bool
    attribute_source: str = "jwt_role"
    attribute_claim_name: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class RowSecuritySimulateRequest(BaseModel):
    user_identity: str = Field(min_length=1)
    roles: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    claims: dict = Field(default_factory=dict)


class RowSecuritySimulateResponse(BaseModel):
    user_identity: str
    roles: list[str]
    active_rule_ids: list[uuid.UUID]
    compiled_predicate: Optional[str]


class SecurityAuditEntry(BaseModel):
    query_log_id: uuid.UUID
    model_id: Optional[uuid.UUID]
    user_identity: Optional[str]
    protocol: str
    route_type: str
    security_rules_applied: list[dict]
    created_at: datetime


class SecurityAuditListResponse(BaseModel):
    items: list[SecurityAuditEntry]
    total: int


