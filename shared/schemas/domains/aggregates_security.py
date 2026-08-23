"""Auto-split from pydantic_models.py — Drill-through sets (Phase 4C.1), Personas (Phase 8.B), Join, Aggregate, Legacy aliases, Row Security (Phase 5.1)"""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

from shared.drill_limits import DRILL_MAX_ROW_LIMIT
from shared.security.row_security_audit import ROW_SECURITY_VALID_SOURCES

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
        ge=1,
        le=DRILL_MAX_ROW_LIMIT,
        description=(
            "Per-measure cap on rows returned. Null = use the default page "
            f"size. Runtime clamps every drill page to {DRILL_MAX_ROW_LIMIT} "
            "rows (Bug-5935), so values above that ceiling are rejected here "
            "rather than silently clamped later."
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
            "'many-to-one' | 'one-to-many' | 'one-to-one' | 'mixed' — "
            "best-effort summary across the hops, read from each join's "
            "declared CARDINALITY (not its join type: those are separate "
            "properties). 'mixed' also covers a path whose fan-out is "
            "UNDECLARED on any hop. Pure heuristic; the planner does not rely "
            "on it."
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


def persona_filter_value_is_valid(raw: Any) -> bool:
    """F-008-16: one default_filters value shape check (no DB needed).

    Scalar -> eq, list -> in, dict -> {operator: value} with the operator in
    the canonical supported set and BETWEEN carrying exactly two bounds.
    Shared by save-time validation, YAML import, and snapshot rehydrate so
    an unknown operator cannot land through one writer and miss the others.
    """
    if isinstance(raw, dict):
        if len(raw) != 1:
            return False
        op, val = next(iter(raw.items()))
        if op not in PERSONA_FILTER_OPERATORS:
            return False
        if op == "between":
            return isinstance(val, (list, tuple)) and len(val) == 2
        return True
    return True


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
    # Bug-7051: optional tag restriction ids persisted atomically with the
    # persona row so a failure after create never leaves a persona without
    # its intended restrictions (security hole).
    restricted_tag_ids: Optional[list[uuid.UUID]] = None


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
    # Bug-7051: optional tag restriction ids persisted atomically with the
    # persona update so a failure never leaves stale restrictions.
    restricted_tag_ids: Optional[list[uuid.UUID]] = None


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
    # F-008-06: objects the CLS closure blocks for this persona (calculated
    # measures/dims with no single source_column_id). The canvas overlay
    # consumes these so preview matches the serving gate.
    cls_blocked_measure_ids: list[uuid.UUID] = Field(default_factory=list)
    cls_blocked_dimension_ids: list[uuid.UUID] = Field(default_factory=list)
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

# Bug-7775: every SQL builder recognises exactly these four join types
# (``shared/semantic/join_keyword.py::join_keyword`` maps inner/left/right/full
# to the ANSI keyword, flipping LEFT<->RIGHT on a reversed traversal; any other
# token is coerced to an un-flipped LEFT JOIN with a warning, silently changing
# the emitted SQL). The engine lane completed right/full JOIN support;
# this closes the producer/consumer contract so the WRITE API rejects an
# unsupported ``join_type`` at the boundary instead of persisting a value the
# rewriter cannot honour. Kept as a shared alias so the frontend allowed-set
# (JoinsPanel.getJoinConstraints / JoinCreate.join_type) mirrors one source.
JoinType = Literal["inner", "left", "right", "full"]

# Join CARDINALITY is a SEPARATE property from join type (join-orientation
# contract, invariant 3): ``join_type`` answers "which rows survive",
# ``cardinality`` answers "how many rows on each side match". They used to
# share one column, so ``many_to_one`` — the old storage default — was both the
# most common value and not a join type at all. Cardinality never changes the
# rendered SQL keyword; it is fan-out metadata. ``None`` means undeclared.
JoinCardinality = Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"]

# Join POPULATION PARTICIPATION (Bug-8615, governance phase G1) — a THIRD,
# independent property of the same ``Join``. ``join_type`` says which rows
# survive, ``cardinality`` says how many match, and this says whether the
# modeller INTENDS the join's row-filtering / row-multiplying effect to define
# the model's population. Contract:
# docs/architecture/architecture_join-population-governance.md (contract 2).
#
# This is the single source of truth for the vocabulary. The ORM
# (``shared/db/models.py::Join.population_participation``), migration 0190, the
# snapshot round-trip, the deploy-time validator and the Joins panel all key off
# these exact strings.
POPULATION_PARTICIPATION_PRESERVE_BASE_ROWS = "preserve_base_rows"
POPULATION_PARTICIPATION_POPULATION_DEFINING = "population_defining"
POPULATION_PARTICIPATION_ENRICHMENT_ONLY = "enrichment_only"
POPULATION_PARTICIPATION_UNDECLARED = "undeclared"

#: The DEFAULT for every existing AND newly created join. Chosen so introducing
#: the field changes no served numbers: a ``preserve_base_rows`` join remains
#: elidable under exactly the pre-existing table-resolution rules.
DEFAULT_POPULATION_PARTICIPATION = POPULATION_PARTICIPATION_PRESERVE_BASE_ROWS

POPULATION_PARTICIPATION_VALUES: tuple[str, ...] = (
    POPULATION_PARTICIPATION_PRESERVE_BASE_ROWS,
    POPULATION_PARTICIPATION_POPULATION_DEFINING,
    POPULATION_PARTICIPATION_ENRICHMENT_ONLY,
    POPULATION_PARTICIPATION_UNDECLARED,
)

# Provenance is deliberately separate from participation.  A concrete
# ``preserve_base_rows`` value can either be the compatibility default or an
# explicit modeller decision; introspection may only revise the former.
POPULATION_PARTICIPATION_SOURCE_DEFAULT = "default"
POPULATION_PARTICIPATION_SOURCE_MANUAL = "manual"
POPULATION_PARTICIPATION_SOURCE_AUTO = "auto"
POPULATION_PARTICIPATION_SOURCE_VALUES: tuple[str, ...] = (
    POPULATION_PARTICIPATION_SOURCE_DEFAULT,
    POPULATION_PARTICIPATION_SOURCE_MANUAL,
    POPULATION_PARTICIPATION_SOURCE_AUTO,
)
PopulationParticipationSource = Literal["default", "manual", "auto"]


def coerce_population_participation_source(value: object) -> str:
    """Coerce imported/read provenance without granting auto ownership.

    Unknown provenance is treated as manual.  A malformed bundle must never
    make a row eligible for an automatic semantic decision.
    """
    token = str(value or "").strip().lower()
    if token in POPULATION_PARTICIPATION_SOURCE_VALUES:
        return token
    return POPULATION_PARTICIPATION_SOURCE_MANUAL

PopulationParticipation = Literal[
    "preserve_base_rows", "population_defining", "enrichment_only", "undeclared",
]


def coerce_population_participation(value: object) -> str:
    """Fold any stored/imported value onto the declared vocabulary.

    Mirrors the join-orientation contract's invariant 4 ("legacy tokens are
    coerced, never rejected, at read time"): a row written before this field
    existed, or a hand-edited/tampered bundle, must not make a read or a
    compile raise. An unrecognised value folds to ``undeclared`` — NOT to the
    ``preserve_base_rows`` default — because "we cannot tell what the modeller
    meant" is exactly what ``undeclared`` names, and it surfaces as a WARNING
    instead of being silently treated as an affirmative declaration. Both
    states are equally elidable, so the coercion never changes served numbers.
    """
    token = str(value or "").strip().lower()
    if token in POPULATION_PARTICIPATION_VALUES:
        return token
    return POPULATION_PARTICIPATION_UNDECLARED


class JoinCreate(BaseModel):
    left_table_id: uuid.UUID
    right_table_id: uuid.UUID
    join_type: JoinType = "inner"
    cardinality: Optional[JoinCardinality] = None
    # Defaults to ``preserve_base_rows`` so a caller that does not know about
    # this field (the current frontend until phase G2, any existing script)
    # creates exactly the join it created before.
    population_participation: PopulationParticipation = (
        DEFAULT_POPULATION_PARTICIPATION  # type: ignore[assignment]
    )
    left_column_name: str = Field(description="Column name on the left table")
    right_column_name: str = Field(description="Column name on the right table")


class JoinUpdate(BaseModel):
    # Bug-7775: constrain to the rewriter-supported set on the WRITE path.
    join_type: Optional[JoinType] = None
    cardinality: Optional[JoinCardinality] = None
    population_participation: Optional[PopulationParticipation] = None
    left_column_name: Optional[str] = None
    right_column_name: Optional[str] = None


class JoinResponse(OrmBase):
    id: uuid.UUID
    model_id: uuid.UUID
    left_table_id: uuid.UUID
    right_table_id: uuid.UUID
    join_type: str
    # Free-form on the READ path for the same reason ``join_type`` is: a
    # historical row may carry a value this Literal does not list, and a read
    # must not 500 on it.
    cardinality: Optional[str] = None
    # Same rule: free-form on read, Literal-constrained on write.
    population_participation: str = DEFAULT_POPULATION_PARTICIPATION
    population_participation_source: str = POPULATION_PARTICIPATION_SOURCE_DEFAULT
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


# Bug-6549: AggregateDefinition.status is a controlled lifecycle enum. A free
# string persisted via PATCH (e.g. {"status": "banana"}) silently removes the
# aggregate from every status-driven routing/refresh path, because those paths
# match on "active". Bug-4846 forbids extra KEYS but not bad VALUES. Restrict
# updates to the known lifecycle statuses.
#
# Bug-6817: split user-settable statuses from system-managed ones. Modelers
# should NOT be able to PATCH an aggregate to "invalid" or "pending" — those
# are system-managed lifecycle states set by the refresh engine, rehydrator, and
# optimizer. Allowing them via the API lets a user bypass the lifecycle guard
# (e.g. setting a broken aggregate to "pending" hides the failure from the
# operator, or setting it to "invalid" triggers spurious revalidation).
# Internal callers (refresh engine, rehydrator) set status directly on the ORM
# object and are unaffected by this API-layer restriction.
USER_SETTABLE_AGGREGATE_STATUSES: tuple[str, ...] = (
    "active",
    "disabled",
    "retired",
)

# The full set of valid lifecycle states, including system-managed ones.
# Used by internal callers that need to validate any status value.
ALLOWED_AGGREGATE_STATUSES: tuple[str, ...] = (
    "active",
    "disabled",
    "invalid",
    "pending",
    "retired",
)


class AggregateDefinitionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Optional[str] = None
    include_quantiles: Optional[bool] = None
    include_stats: Optional[bool] = None
    target_schema: Optional[str] = None

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v: Optional[str]) -> Optional[str]:
        # Bug-6817: only user-settable statuses are accepted via the API.
        # System-managed statuses (invalid, pending) are set directly on
        # the ORM by internal callers, not through this update schema.
        if v is not None and v not in USER_SETTABLE_AGGREGATE_STATUSES:
            raise ValueError(
                "status must be one of: "
                + ", ".join(USER_SETTABLE_AGGREGATE_STATUSES)
            )
        return v


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
    # Derived-grain artifact manifests (Bug-7359, §5.2/§5.3). Descriptive build
    # metadata only in Phase 3 — no route reads them yet (serving is shadow-only
    # through Phase 4). active_refresh_run_id is the live pointer to the run whose
    # rows the manifest describes; it is NULL until a build re-earns trust.
    grain_keys: Optional[list] = None
    attribute_edges: Optional[list] = None
    passenger_columns: Optional[list] = None
    active_refresh_run_id: Optional[uuid.UUID] = None
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
    # Bug-7007: the COMPLETE initial refresh policy travels in the create
    # payload so pocket creation is a single atomic transaction. Previously the
    # create endpoint only wrote a PocketRefreshPolicy child row for a
    # ``schedule`` policy with a cron (and always enabled), forcing the UI to
    # issue a SECOND ``PUT .../refresh/policy`` request to configure the policy
    # — a non-atomic two-request workflow where a failure of the second request
    # left the pocket persisted with a policy that differed from the submitted
    # form. ``refresh_policy_enabled`` lets the caller set the schedule's
    # enabled state at create time; None means "use the create default"
    # (enabled for a scheduled pocket). The policy PUT is reserved for edits to
    # an existing pocket.
    refresh_policy_enabled: Optional[bool] = None
    # Deprecated: the new full-refresh flow schedules via PocketRefreshPolicy.
    # Retained on the schema for backwards compatibility with older callers;
    # the current UI does not send these.
    incremental_column: Optional[str] = Field(default=None, deprecated=True)
    incremental_lookback_hours: Optional[int] = Field(default=None, deprecated=True)
    # Bug-6993: a TTL of 0 or negative is invalid — 0 means the pocket expires
    # the instant it is written (evicted before it can ever serve a query) and
    # a negative value has no sane interpretation at all. Reject both at write
    # time rather than letting the eviction janitor silently treat them as
    # "expire on the very next sweep".
    ttl_days: int = Field(default=14, gt=0)
    predicates: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("refresh_policy")
    @classmethod
    def _check_refresh_policy(cls, v: str) -> str:
        # Bug-6107: reject a garbage policy token at the schema boundary.
        if v not in VALID_POCKET_REFRESH_POLICIES:
            raise ValueError(
                "refresh_policy must be one of "
                f"{sorted(VALID_POCKET_REFRESH_POLICIES)}; got {v!r}"
            )
        return v

    @field_validator("refresh_cron")
    @classmethod
    def _check_refresh_cron(cls, v: Optional[str]) -> Optional[str]:
        # Bug-6107/6588: reject an unparseable cron at write time (import-safe).
        return _validate_cron_expression(v)

    @model_validator(mode="after")
    def _require_cron_when_scheduled(self) -> "PocketDefinitionCreate":
        # Bug-6107: a 'schedule' pocket with no cron is never swept — the child
        # PocketRefreshPolicy row (which the scheduler's due-query filters on) is
        # only created when both refresh_policy=='schedule' and refresh_cron are
        # present, so the schedule would silently never fire. Reject at create.
        if self.refresh_policy == "schedule" and not (
            self.refresh_cron and self.refresh_cron.strip()
        ):
            raise ValueError(
                "A 'schedule' refresh_policy requires a refresh_cron; otherwise "
                "the pocket would never be scheduled for refresh."
            )
        return self


class PocketDefinitionUpdate(BaseModel):
    defining_sql: Optional[str] = None
    refresh_policy: Optional[str] = None
    refresh_cron: Optional[str] = None
    # Deprecated: see PocketDefinitionCreate.
    incremental_column: Optional[str] = Field(default=None, deprecated=True)
    incremental_lookback_hours: Optional[int] = Field(default=None, deprecated=True)
    # Bug-6993: same positivity rule as PocketDefinitionCreate.ttl_days — a
    # supplied 0/negative value is rejected; None (field omitted) still means
    # "leave the existing TTL unchanged" on this partial PATCH.
    ttl_days: Optional[int] = Field(default=None, gt=0)

    @field_validator("refresh_policy")
    @classmethod
    def _check_refresh_policy(cls, v: Optional[str]) -> Optional[str]:
        # Bug-6107: validate only the supplied token (partial PATCH). The
        # cross-field 'schedule requires cron' rule is not enforced here because
        # a PATCH may set refresh_policy while the row already carries a cron;
        # the PATCH handler reconciles the child policy row.
        if v is not None and v not in VALID_POCKET_REFRESH_POLICIES:
            raise ValueError(
                "refresh_policy must be one of "
                f"{sorted(VALID_POCKET_REFRESH_POLICIES)}; got {v!r}"
            )
        return v

    @field_validator("refresh_cron")
    @classmethod
    def _check_refresh_cron(cls, v: Optional[str]) -> Optional[str]:
        # Bug-6107/6588: reject an unparseable cron at write time (import-safe).
        return _validate_cron_expression(v)
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

    Bug-8111: this is a full upsert (every field is always written via
    ``body.model_dump()`` in ``pockets.py::upsert_pocket_refresh_policy``, on
    both the insert and the update branch) — it is not a partial PATCH, so it
    must carry the same write-time guards as the aggregate side's
    ``RefreshPolicyCreate`` (the POST path). Before this fix the PUT path had
    NO validators at all: an unparseable cron, or an enabled policy with no
    cron, was persisted silently and only surfaced later as a swallowed
    sweep-time log line — the pocket would never actually refresh.
    """

    cron_expression: Optional[str] = None
    is_enabled: bool = False

    @field_validator("cron_expression")
    @classmethod
    def _check_cron(cls, v: Optional[str]) -> Optional[str]:
        # Bug-8111: mirror RefreshPolicyCreate._check_cron (import-safe).
        return _validate_cron_expression(v)

    @model_validator(mode="after")
    def _require_cron_when_enabled(self) -> "PocketRefreshPolicyUpsert":
        # Bug-8111: mirror RefreshPolicyCreate._require_cron_when_enabled — an
        # enabled policy with no cron is never due, so the scheduler's
        # refresh_due_pockets sweep (which filters on enabled
        # PocketRefreshPolicy rows with a cron) would never pick it up.
        if self.is_enabled and not (self.cron_expression and self.cron_expression.strip()):
            raise ValueError(
                "An enabled refresh policy must have a cron_expression; "
                "otherwise the pocket would never be scheduled for refresh."
            )
        return self


class PocketCompoundEdit(BaseModel):
    """Definition and refresh policy written as one history transaction."""

    definition: PocketDefinitionUpdate = Field(default_factory=PocketDefinitionUpdate)
    policy: PocketRefreshPolicyUpsert


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
    # Bug-8453: ``row_security`` means the probe could not read any row because
    # row-level security denied the caller everything — distinct from a SQL
    # fault at "parse"/"subset"/"probe".
    stage: str = Field(description='"parse" | "subset" | "probe" | "row_security"')
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
    # Bug-8719: server-derived, READ-ONLY. Auto-set when incremental is
    # configured and the fact PK is hidden; the Pocket drawer shows an
    # informational message, never a checkbox. Deliberately absent from
    # PocketDefinitionCreate/Update: the POST path does not read it (so it
    # would be accepted-and-ignored) and the PATCH path writes the body
    # straight through with setattr (so it would be client-settable,
    # letting an arbitrary caller flip whether a materialised pocket
    # exposes the HIDDEN fact primary key).
    include_fact_key: bool = False
    ttl_days: int
    status: str
    population_eligibility: str = "unknown"
    population_eligibility_reason: Optional[str] = None
    population_proof_fingerprint: Optional[str] = None
    failure_reason: Optional[str]
    last_refresh_at: Optional[datetime]
    last_access_at: Optional[datetime]
    last_match_at: Optional[datetime]
    hit_count: int
    time_saved_ms_total: int
    # Derived-grain row manifest (Bug-7359, §5.2/§5.3). SECURITY-LOAD-BEARING
    # since Bug-8018/Bug-8393: ``row_manifest["columns"]`` records the output
    # columns the built pocket table exposes and is what admits a pocket under
    # active row-level security. ``active_refresh_run_id`` is the live pointer to
    # the refresh run those columns describe; the pair is only trustworthy while
    # the two agree.
    row_manifest: Optional[dict] = None
    active_refresh_run_id: Optional[uuid.UUID] = None
    created_at: datetime
    updated_at: datetime
    retired_at: Optional[datetime]
    predicates: list[PocketPredicateResponse] = Field(default_factory=list)
    refresh_policy_row: Optional[PocketRefreshPolicyResponse] = None
    # Bug-8113: set when the create endpoint auto-enqueues a one-shot refresh
    # so the frontend can observe the lifecycle to terminal state.
    refresh_run_id: Optional[uuid.UUID] = None


class AggregateColumnResponse(OrmBase):
    id: uuid.UUID
    aggregate_definition_id: uuid.UUID
    measure_id: Optional[uuid.UUID]
    physical_col_name: str
    stat_type: str
    created_at: datetime


# Bug-6588: structural fallback for cron validation when ``croniter`` is not
# importable. ``croniter`` is a transitive dep of the shared package
# (pythonpath-wired into each service, not pip-installed with its own deps), so
# it can be absent in a built service image (e.g. model-service). The validator
# must never hard-fail (ImportError -> 500) on a request that merely validates a
# refresh cron; it falls back to a 5/6-field structural check that catches
# obvious garbage. The scheduler's own is_due computation remains the ultimate
# safety net for a subtly-invalid-but-structurally-legal cron.
#
# Bug-6748: the original regex accepted any string of cron-legal characters
# without checking field boundaries. Tighten: validate each field's numeric
# range (minute 0-59, hour 0-23, day 1-31, month 1-12, dow 0-7) and reject
# tokens that are clearly out of bounds. Named months/days are left to the
# letter-class check (the regex allows A-Za-z for JAN-DEC, MON-SUN).
_CRON_STRUCTURAL_FIELD_RE = re.compile(r"[0-9*/,\-A-Za-z]+")

# Boundaries per standard cron field (0-indexed position).
_CRON_FIELD_BOUNDS: list[tuple[int, int]] = [
    (0, 59),   # minute
    (0, 23),   # hour
    (1, 31),   # day of month
    (1, 12),   # month
    (0, 7),    # day of week (0 and 7 = Sunday)
]


def _cron_field_in_bounds(token: str, lo: int, hi: int) -> bool:
    """Check that numeric literals in a cron field token fall within [lo, hi].

    Handles: plain numbers, ranges (``1-5``), step values (``*/2``, ``1-5/2``),
    and comma-separated lists (``1,5,10``). Alphabetic tokens (``JAN``,
    ``MON``) are accepted unconditionally — the letter-class already screens
    them structurally, and exact mapping is left to croniter when available.
    """
    for sub in token.split(","):
        # Strip step suffix (e.g. "*/2" -> "*", "1-5/2" -> "1-5").
        base = sub.split("/")[0]
        if base == "*":
            continue
        # Check range endpoints.
        parts = base.split("-")
        for part in parts:
            # Skip alphabetic tokens (named months/days).
            if not part.isdigit():
                if part.isalpha():
                    continue
                # Mixed alpha-numeric or special chars — reject.
                return False
            val = int(part)
            if val < lo or val > hi:
                return False
    return True


def _validate_cron_expression(value: Optional[str]) -> Optional[str]:
    """F-012-08 / Bug-6588: reject a cron expression that cannot be parsed.

    An unparseable cron was previously accepted and only surfaced at sweep time
    as a logged error, leaving the aggregate silently never refreshed. Validate
    at write time so the admin gets immediate feedback (422). ``None``/empty is
    left to the enabled-policy guard below.

    Import-safe: uses ``croniter`` for exact validation when importable, and a
    structural 5/6-field check otherwise, so no consumer service can 500 with an
    ImportError merely because ``croniter`` is not installed in its image.
    """
    if value is None:
        return value
    trimmed = value.strip()
    if not trimmed:
        return None
    try:
        from croniter import croniter  # type: ignore

        ok = bool(croniter.is_valid(trimmed))
    except ImportError:
        parts = trimmed.split()
        # Bug-6748: tightened structural validation. Check field count (5 or 6),
        # character class, AND numeric range boundaries per field.
        if len(parts) not in (5, 6):
            ok = False
        elif not all(_CRON_STRUCTURAL_FIELD_RE.fullmatch(p) for p in parts):
            ok = False
        else:
            # Validate the first 5 fields against standard cron boundaries.
            # A 6th field (seconds or year) is accepted structurally but not
            # range-checked — it varies by cron implementation.
            ok = all(
                _cron_field_in_bounds(parts[i], lo, hi)
                for i, (lo, hi) in enumerate(_CRON_FIELD_BOUNDS)
                if i < len(parts)
            )
    if not ok:
        raise ValueError(f"Invalid cron expression: {value!r}")
    return trimmed


# Bug-6107: canonical universe of pocket refresh policies. Single source of
# truth mirrored from the config registry's ``_validate_refresh_policy_list``
# ("schedule"=cron sweep, "manual"=Refresh now, "event"=re-materialise on
# source-schema drift). The per-tenant ``pocket.allowed_refresh_policies``
# setting may NARROW this set at the API layer; this schema-level gate is
# defence-in-depth so an importer or raw-API caller cannot persist a garbage
# policy token (which the sweep would silently never act on) bypassing the API.
VALID_POCKET_REFRESH_POLICIES: frozenset[str] = frozenset(
    {"schedule", "manual", "event"}
)


# Refresh modes accepted on a policy. ``scheduled`` (cron-driven full refresh)
# and ``incremental`` (watermark refresh) are the only supported values.
_REFRESH_MODES = frozenset({"scheduled", "incremental"})


class RefreshPolicyCreate(BaseModel):
    refresh_mode: str = "scheduled"
    cron_expression: Optional[str] = None
    incremental_column: Optional[str] = None
    incremental_lookback: Optional[int] = None
    incremental_append_only: StrictBool = False
    full_rebuild_interval_days: Optional[int] = Field(default=None, ge=1)
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
        if self.refresh_mode == "incremental":
            if not self.incremental_column:
                raise ValueError(
                    "An incremental refresh policy requires an incremental_column."
                )
            if not self.incremental_append_only:
                raise ValueError(
                    "An incremental refresh policy requires an explicit "
                    "append-only source declaration."
                )
        elif self.incremental_append_only or self.full_rebuild_interval_days is not None:
            raise ValueError(
                "Append-only and periodic full-rebuild settings require "
                "refresh_mode='incremental'."
            )
        return self


class RefreshPolicyUpdate(BaseModel):
    refresh_mode: Optional[str] = None
    cron_expression: Optional[str] = None
    incremental_column: Optional[str] = None
    incremental_lookback: Optional[int] = None
    incremental_append_only: Optional[StrictBool] = None
    full_rebuild_interval_days: Optional[int] = Field(default=None, ge=1)
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
    incremental_append_only: bool
    full_rebuild_interval_days: Optional[int]
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
# Consolidated onto the single canonical definition in
# ``shared.security.row_security_audit`` (Bug-6034 follow-up: valid-source
# consolidation). This alias is the SAME frozenset object the rehydrator import
# guard and the backfill normaliser use, so the three guards can no longer
# drift. Membership checks (``v not in ...``) work identically against the
# frozenset; error messages sort it for a stable, readable order.
_ROW_SECURITY_ATTRIBUTE_SOURCES = ROW_SECURITY_VALID_SOURCES


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
                f"attribute_source must be one of {sorted(_ROW_SECURITY_ATTRIBUTE_SOURCES)}, got {v!r}"
            )
        return v

    @field_validator("attribute_claim_name")
    @classmethod
    def _normalize_claim_name(cls, v: Optional[str]) -> Optional[str]:
        # Bug-5904 hardening: trim so a padded-but-non-blank claim name
        # (e.g. "department ") is stored the same way it will be compared
        # at runtime. predicate_compiler does an exact-key lookup
        # (principal.claims.get(claim_name)) — an untrimmed key would never
        # match the real JWT claim, reproducing the same silently-inert
        # rule class this bug fixes, just via a near-miss instead of a
        # blank. Whitespace-only collapses to "" here so the
        # _shape_consistency falsy check below still catches it exactly as
        # if it had been left unset.
        if v is None:
            return v
        return v.strip()

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
            # Bug-5904: a claim/scope-sourced rule with no claim name never
            # resolves a subject at match time
            # (predicate_compiler._resolve_principal_attribute returns an
            # empty set for saml_claim/oidc_scope when attribute_claim_name
            # is falsy), so the rule silently never fires. Fail closed at
            # save time instead of persisting a rule that looks enabled but
            # provides no protection. Blank/whitespace-only names are
            # rejected the same as missing ones.
            if self.attribute_source in ("saml_claim", "oidc_scope") and not (
                self.attribute_claim_name or ""
            ).strip():
                raise ValueError(
                    "attribute_claim_name is required and cannot be blank "
                    f"when attribute_source={self.attribute_source!r}; a "
                    "claim/scope-sourced rule without a claim name will "
                    "never match any principal at query time"
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
            # Bug-5905: user_mapping rules always key by user_identity at
            # runtime (predicate_compiler.py) -- attribute_source and
            # attribute_claim_name are not consumed for this rule type.
            # Accepting a non-default value here and silently discarding it
            # would let a modeler configure a rule that "looks" group- or
            # claim-keyed in the UI but is actually keyed by user identity,
            # which is exactly the mismatch the review flagged. Fail closed
            # instead so the contract mismatch is caught at save time.
            if self.attribute_source != "jwt_role":
                raise ValueError(
                    "attribute_source must be left at its default ('jwt_role') "
                    "when rule_type='user_mapping'; this rule type always keys "
                    "by user_identity and does not consume attribute_source"
                )
            if self.attribute_claim_name is not None:
                raise ValueError(
                    "attribute_claim_name must be null when rule_type='user_mapping'"
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
                f"attribute_source must be one of {sorted(_ROW_SECURITY_ATTRIBUTE_SOURCES)}, got {v!r}"
            )
        return v

    @field_validator("attribute_claim_name")
    @classmethod
    def _normalize_claim_name(cls, v: Optional[str]) -> Optional[str]:
        # Bug-5904 hardening (update-path companion to the create-schema
        # validator above): trim so a padded-but-non-blank claim name
        # doesn't silently fail to match the real JWT claim key at runtime.
        # An explicit "" or whitespace-only value collapses to "" here, so
        # row_security.py's effective-value blank check (which runs after
        # this schema validation) sees the same falsy value it would for an
        # omitted claim name.
        if v is None:
            return v
        return v.strip()

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
    # F-007-02: an optional probe query. When supplied, the simulate endpoint
    # runs the REAL enforcement path (bind -> route -> RLS inject -> execute)
    # as the simulated principal through the query-router and returns the rows
    # that principal would actually see — not just the compiled predicate text.
    probe_query: Optional[str] = None
    # The persona the simulated principal would carry (admin impersonation of a
    # catalogue persona). Forwarded to the query-router so persona allow-list /
    # CLS gating composes with RLS exactly as it would at query time.
    persona_id: Optional[uuid.UUID] = None


class RowSecuritySimulateResponse(BaseModel):
    user_identity: str
    roles: list[str]
    active_rule_ids: list[uuid.UUID]
    compiled_predicate: Optional[str]
    # F-007-02: real-result simulation fields, populated only when a probe_query
    # is supplied and executed. ``executed`` distinguishes a compiled-only
    # preview (executed=False) from a live row simulation (executed=True).
    executed: bool = False
    route_type: Optional[str] = None
    columns: Optional[list[str]] = None
    rows: Optional[list[list]] = None
    row_count: Optional[int] = None
    applied_rules: Optional[list[dict]] = None
    # Bug-7027: non-None when the connector could not be resolved definitively
    # and the compiler default (postgresql) was used. The previewed predicate
    # may quote identifiers differently from the runtime — tell the modeller
    # rather than letting the fallback pass silently.
    connector_note: Optional[str] = None


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
