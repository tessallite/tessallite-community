"""Auto-split from pydantic_models.py — Dimension, Measure"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..measure_formats import (
    CANONICAL_TIME_VARIANTS as _CANONICAL_TIME_VARIANTS,
    HIERARCHY_TIME_CALCS as _HIERARCHY_TIME_CALCS,
    HIERARCHY_TIME_UNITS as _HIERARCHY_TIME_UNITS,
    MEASURE_FORMAT_TOKENS as _MEASURE_FORMAT_TOKENS,
    TIME_VARIANT_ALIASES as _TIME_VARIANT_ALIASES,
    TIME_VARIANT_NAMES as _TIME_VARIANT_NAMES,
)

from ...aggregate_quantiles import QUANTILE_STAT_TYPES as _QUANTILE_STAT_TYPES

from ._base import OrmBase

# Canonical valid semi_additive_behavior values. Single source of truth shared
# by the Measure validator below and the snapshot rehydrator's enum gate
# (F-020-09) so importers cannot store invalid enums bypassing the API layer.
#
# Wave 2 scope enforcement (#10): ``by_account`` was REMOVED from this set. It is
# not a supported product behaviour — per-account aggregation dispatch was never
# implemented, and the query rewriter already fails loud on it
# (``query-router/rewrite/source_sql.py`` / ``rewrite/calendar_support.py``).
# Removing it from the single source of truth means the forward create/update API
# rejects new by_account measures, and the rehydrate/import boundary
# (``rehydrator._validate_measure_enums``) imports any already-persisted
# by_account measure as a DISABLED measure (is_invalid + reason), the same
# mechanism used for any other unresolvable semi-additive token. Existing live
# rows are flagged by migration ``0215_disable_by_account_semi_additive``.
VALID_SEMI_ADDITIVE_BEHAVIORS: frozenset[str] = frozenset({
    "last_non_empty", "first_non_empty", "avg_of_children",
    "min", "max",
})

# Canonical valid default_agg values (Bug-6223). The additive/base set that the
# aggregate builder (grain_resolver.AGG_TEMPLATES) and query-time renderer
# (source_sql._wrap_agg) understand, plus the quantile stat suffixes
# (p01..p99) used by median/percentile measures.
#
# F-015-19: this set is a SUPERSET of what the frontend Measures panel offers.
# The panel's ``AGG_OPTIONS`` exposes only the six base aggregates
# (sum/avg/min/max/count/count_distinct); the quantile suffixes are accepted by
# the API and understood by materialisation but are NOT authorable in the
# drawer today (see G-015-02 / Bug-5891 for the median-routing decision). Do not
# assume UI parity from this comment.
#
# VALIDATION CONTRACT (two boundaries, deliberately asymmetric):
#   * FORWARD (strict, per-model-and-per-field): the ``_validate_default_agg``
#     validator runs strictly on ``MeasureCreate`` and tolerantly on
#     ``MeasureUpdate``. Create rejects legacy synonyms so fresh data stays
#     canonical; update canonicalizes only known historical synonyms
#     (average->avg, median->p50) so round-trip edits of old persisted rows do
#     not 422. A raw-API caller still cannot persist a free-form
#     aggregate (e.g. "total") that saves cleanly then fails-late at the source
#     with "TOTAL(...) does not exist" on either path. The semi-additive
#     validator (``_check_semi_additive_fields``) is wired on BOTH
#     ``MeasureCreate`` and ``MeasureUpdate`` (Bug-6620). Create rejects
#     non-canonical values strictly; Update validates when the field is supplied
#     (partial-update semantics: None = not supplied = no-op). The rehydrator
#     read-coercion below is the backstop that keeps such a persisted value from
#     bricking a later revert/import.
#   * READ / REHYDRATE (tolerant, read-coercion): the snapshot rehydrator
#     inserts measure rows directly, bypassing these validators, so it CANNOT be
#     strict — legacy saved versions and exported bundles carry pre-normalisation
#     tokens that a hard reject would brick (un-revertable / un-importable). Both
#     default_agg AND semi_additive_behavior are therefore read-coerced at
#     ``model_snapshot/rehydrator.py::_validate_measure_enums``: a known legacy
#     token maps to its canonical form (default_agg average->avg, median->p50;
#     semi_additive last_value->last_non_empty, ...); an unresolvable token
#     imports as a DISABLED measure (safe default + is_invalid + reason), never a
#     crash. Ecosystem mappers (atscale/dbt/cube) normalise on their own import
#     path so fresh imports already arrive canonical.
VALID_DEFAULT_AGGS: frozenset[str] = frozenset(
    {"sum", "avg", "min", "max", "count", "count_distinct"}
) | frozenset(_QUANTILE_STAT_TYPES)

LEGACY_DEFAULT_AGG_SYNONYMS: dict[str, str] = {
    "average": "avg",
    "median": "p50",
}

# ---------------------------------------------------------------------------
# Effective additivity (Bug-8257)
# ---------------------------------------------------------------------------
# ``Measure.is_additive`` is a NOT NULL column that defaults to True, so an
# untouched measure carries True regardless of what it actually computes. A
# True flag is therefore indistinguishable from "nobody set it", and every
# consumer that trusts it (client-side pivot totals, aggregate column planning,
# the matcher's re-aggregation gate, the agent prompt catalogue) inherits a
# wrong-numbers risk on measures that are mathematically non-additive.
#
# This is the SINGLE definition of effective additivity. The producers below
# (MeasureCreate, the model-service update path, the snapshot rehydrator) coerce
# with it so the persisted flag is trustworthy; the agent prompt renderer reads
# the same helper so the catalogue and the database can never disagree.
#
# PRECEDENCE (mathematical nature wins over the declared flag, one direction
# only):
#   1. non-additive aggregation (avg/min/max/count_distinct/quantiles) -> False
#   2. semi-additive measure (a declared semi_additive_behavior)        -> False
#      A last-non-empty balance is the textbook non-summable measure: adding up
#      each day's closing balance is exactly the wrong number semi-additive
#      support exists to prevent. This rule was MISSING from the first version
#      of this helper (deep-review R5 finding 2) even though the sibling gate
#      ``aggregate_matcher.compute_has_non_additive`` already implemented it and
#      the column sits immediately beside ``is_additive`` in the ORM. The
#      Measures panel defaults the Additive toggle to true and sends
#      ``semi_additive_behavior`` independently, so a balance measure created
#      through the shipped UI persisted is_additive=True and the Explorer pivot
#      grand total summed the daily balances.
#   3. time-variant measure (PY/YTD/trailing/moving windows)           -> False
#      ("non-additive across periods": stacking across periods double-counts
#      even when the base aggregation is a plain sum)
#   4. calculated measure (ratios etc. do not re-aggregate)            -> False
#   5. otherwise, the declared flag stands.
# A declared FALSE is never overridden: a modeller marking a plain sum measure
# non-additive (a semi-additive balance, a rate stored as a sum) is stating
# something the shape cannot prove, and that statement is the safe direction.
#
# NOTE on min/max, and on what this flag does NOT decide (deep-review R3
# finding 4). MIN(MIN(x)) = MIN(x), so min/max ARE re-aggregatable, and avg is
# derivable from a stored sum/count pair. They are still non-additive HERE
# because ``is_additive`` means "safe to combine by ADDITION" — the client
# totals algorithm only knows how to sum, so summing a column of maxima is a
# wrong number.
#
# Be aware that this coercion DOES tighten serve-time rollup as a side effect:
# ``aggregate_matcher.compute_has_non_additive`` short-circuits on
# ``not is_additive`` BEFORE it consults the aggregate-function registry's
# routing class, so a coerced measure becomes exact-grain-only rather than
# being classified as ``mappable``/``derivable``. That costs acceleration, never
# correctness (the query falls back to source). Whether to separate the two
# questions properly is an open product decision — see
# ``docs/questions/questions_measure-additivity-vs-rollup.md``.
ADDITIVE_AGGS: frozenset[str] = frozenset({"sum", "count"})
NON_ADDITIVE_AGGS: frozenset[str] = VALID_DEFAULT_AGGS - ADDITIVE_AGGS


def derive_is_additive(
    *,
    default_agg: Optional[str],
    measure_type: Optional[str] = "standard",
    variant_kind: Optional[str] = None,
    semi_additive_behavior: Optional[str] = None,
    declared: Optional[bool] = None,
) -> bool:
    """Effective additivity of a measure — see the precedence note above.

    ``declared`` is the modeller-supplied flag (``None`` means "not supplied",
    treated as the True default). Returns the value that should be PERSISTED,
    so every reader of ``Measure.is_additive`` gets a trustworthy answer without
    having to re-derive this precedence for itself.
    """
    agg = (default_agg or "").strip().lower()
    agg = LEGACY_DEFAULT_AGG_SYNONYMS.get(agg, agg)
    if agg in NON_ADDITIVE_AGGS:
        return False
    if semi_additive_behavior:
        return False
    if variant_kind:
        return False
    if (measure_type or "standard") == "calculated":
        return False
    return True if declared is None else bool(declared)


def _validate_default_agg(
    value: Optional[str],
    *,
    allow_legacy_synonyms: bool = False,
) -> Optional[str]:
    """Reject unknown aggregate functions at the schema boundary.

    Case-insensitive (query-time ``_wrap_agg`` upper-cases before use) and
    canonicalizes to lowercase so the persisted value matches the
    ``{name}__{default_agg}`` physical-column convention used by the
    aggregate/variant column builders. ``None`` (MeasureUpdate no-op) passes
    through unchanged. ``MeasureUpdate`` enables the legacy-synonym bridge so
    safe old rows can be edited and saved back as canonical values.
    """
    if value is None:
        return None
    canonical = value.strip().lower()
    if allow_legacy_synonyms:
        canonical = LEGACY_DEFAULT_AGG_SYNONYMS.get(canonical, canonical)
    if canonical not in VALID_DEFAULT_AGGS:
        raise ValueError(
            f"default_agg must be one of {sorted(VALID_DEFAULT_AGGS)}; "
            f"got {value!r}"
        )
    return canonical

# ---------------------------------------------------------------------------
# Dimension attribute relationship (derived-grain routing, spec §5.3)
# ---------------------------------------------------------------------------
# A modeller-declared key-to-detail relationship on a dimension. Explicit and
# multi-row (one dimension key may govern several details). Kept STRICTLY
# separate from ``display_column_id`` (a caption choice); a display column never
# declares 1:1 and never creates one of these. Phase 1b persists + round-trips
# the declaration; NO serving/verification (Phase 2+).

# Cardinality (spec §5.3 / I14). A bijection is an exact partition relabel; an
# N:1 edge is a real coarsening. Never opportunistically upgraded N:1 -> exact.
ATTRIBUTE_RELATIONSHIP_CARDINALITIES: frozenset[str] = frozenset(
    {"BIJECTION", "FUNCTIONAL_N_TO_1"}
)

# Current verification status projected into API responses (spec §5.3, §10.3).
# ``DECLARED`` is the pre-verify default; the verifier writes
# VERIFIED/BROKEN/STALE/ERROR. Bug-7894 adds PENDING: a text (VARCHAR/CHAR/STRING)
# BIJECTION detail proven 1:1 by data at deploy but whose serve-collation
# fold-safety can be certified only at artifact-build time. PENDING is
# non-serving (the router trust predicate admits only VERIFIED) and clears to
# VERIFIED after the passenger aggregate is built and re-verified — never a defect
# (BROKEN) or fault (ERROR). Defined here so the frontend type and the verifier
# share one vocabulary.
ATTRIBUTE_RELATIONSHIP_STATUSES: frozenset[str] = frozenset(
    {"DECLARED", "PENDING", "VERIFIED", "BROKEN", "STALE", "ERROR"}
)


class DimensionAttributeRelationshipCreate(BaseModel):
    """Declare a key-to-detail relationship on a dimension (spec §5.3).

    ``key_column_name`` is optional: when omitted the API pins the owning
    dimension's current key column. The detail column must be a physical column
    in the same governed model relation.
    """

    detail_column_name: str = Field(
        description="Physical detail column (in the governed model relation) mapped from the dimension key.",
    )
    cardinality: str = Field(
        description="BIJECTION (exact 1:1 relabel) or FUNCTIONAL_N_TO_1 (many keys -> one detail).",
    )
    key_column_name: Optional[str] = Field(
        default=None,
        description="Optional explicit key column; defaults to the dimension's current key column.",
    )
    enabled: bool = True

    @field_validator("cardinality")
    @classmethod
    def _check_cardinality(cls, v: str) -> str:
        u = (v or "").strip().upper()
        if u not in ATTRIBUTE_RELATIONSHIP_CARDINALITIES:
            raise ValueError(
                f"cardinality must be one of {sorted(ATTRIBUTE_RELATIONSHIP_CARDINALITIES)}; got {v!r}"
            )
        return u


class DimensionAttributeRelationshipUpdate(BaseModel):
    """Partial update. Changing the detail column or cardinality changes the
    declaration hash and (in Phase 2) stales all prior verification evidence."""

    detail_column_name: Optional[str] = None
    cardinality: Optional[str] = None
    key_column_name: Optional[str] = None
    enabled: Optional[bool] = None

    @field_validator("cardinality")
    @classmethod
    def _check_cardinality(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        u = v.strip().upper()
        if u not in ATTRIBUTE_RELATIONSHIP_CARDINALITIES:
            raise ValueError(
                f"cardinality must be one of {sorted(ATTRIBUTE_RELATIONSHIP_CARDINALITIES)}; got {v!r}"
            )
        return u


class DimensionAttributeRelationshipResponse(OrmBase):
    id: uuid.UUID
    model_id: uuid.UUID
    dimension_id: uuid.UUID
    key_column_id: Optional[uuid.UUID] = None
    key_column_name: Optional[str] = None
    detail_column_id: Optional[uuid.UUID] = None
    detail_column_name: Optional[str] = None
    cardinality: str
    null_policy: str = "REJECT_NULL"
    enabled: bool = True
    declaration_hash: str
    # Denormalised current verification status for the UI. Always ``DECLARED`` in
    # Phase 1b (no verifier yet); the Phase-2 verifier projects the real status.
    verification_status: str = "DECLARED"
    verified_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Dimension
# ---------------------------------------------------------------------------

class DimensionCreate(BaseModel):
    name: str = Field(max_length=255)
    display_name: Optional[str] = None
    description: Optional[str] = None
    display_folder: Optional[str] = None
    source_table_id: Optional[uuid.UUID] = Field(default=None, description="ModelTable the column belongs to")
    source_column_name: Optional[str] = Field(default=None, description="Column name to resolve to source_column_id")
    display_column_name: Optional[str] = Field(
        default=None,
        description=(
            "Bug-5434: optional DISPLAY column (in the same source table) whose value "
            "is surfaced as the member caption, distinct from the key column. "
            "Only valid for a physical-column flat dimension."
        ),
    )
    data_type: Optional[str] = Field(default=None, description="Column data type; used to populate ModelColumn metadata")
    user_defined_attribute_id: Optional[uuid.UUID] = Field(default=None, description="User-defined attribute id")
    is_time_dim: bool = False
    time_grain: Optional[str] = None
    calc_expression: Optional[str] = Field(
        default=None,
        description="SQL expression for calculated dimensions (e.g. CASE WHEN x > 0 THEN 'A' ELSE 'B' END)",
    )

    @model_validator(mode="after")
    def _check_calc_dimension_fields(self):
        has_source = self.source_table_id is not None or self.source_column_name is not None
        has_uda = self.user_defined_attribute_id is not None
        has_calc = self.calc_expression is not None
        if has_calc and (has_source or has_uda):
            raise ValueError(
                "calc_expression is mutually exclusive with source_column/source_table and user_defined_attribute"
            )
        return self


class DimensionUpdate(BaseModel):
    name: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    display_folder: Optional[str] = None
    source_table_id: Optional[uuid.UUID] = None
    source_column_name: Optional[str] = None
    # Bug-5434: set to a column name to attach a distinct display column; pass an
    # empty string / null (explicitly, exclude_unset-tracked) to clear it.
    display_column_name: Optional[str] = None
    user_defined_attribute_id: Optional[uuid.UUID] = None
    is_time_dim: Optional[bool] = None
    time_grain: Optional[str] = None
    calc_expression: Optional[str] = None


class ModelAlertResponse(OrmBase):
    """Serialised ``ModelAlert`` row consumed by the Model Health tab."""

    id: uuid.UUID
    model_id: uuid.UUID
    severity: str
    category: str
    title: str
    detail: Optional[str] = None
    related_object_type: Optional[str] = None
    related_object_id: Optional[uuid.UUID] = None
    first_seen_at: datetime
    last_seen_at: datetime
    occurrence_count: int
    resolved_at: Optional[datetime] = None
    dismissed_at: Optional[datetime] = None
    dismissed_by: Optional[uuid.UUID] = None


class ModelRevalidationReportResponse(BaseModel):
    """Summary counts returned by the manual revalidate endpoint."""

    invalid_dimension_count: int
    invalid_measure_count: int
    invalid_aggregate_count: int
    newly_valid_dimension_count: int
    newly_valid_measure_count: int
    newly_valid_aggregate_count: int
    unresolved_hierarchy_issue_count: int = 0
    failed_pocket_count: int = 0
    unacknowledged_schema_drift_count: int = 0
    latest_recorded_schema_drift_at: Optional[datetime] = None
    live_source_checked: bool = False
    measure_warnings: list[MeasureWarningResponse] = []


class RedundantPartnerInfo(BaseModel):
    """Hint that a semantic attribute is redundant to add to an aggregate.

    Surfaced on DimensionResponse / MeasureResponse so the aggregate
    picker UI can dim the entry and show a tooltip pointing at the
    fact-side canonical column. The API POST /aggregates endpoint also
    inspects this to reject redundant grains unless the caller passes
    ``confirm_redundant_grain=true``.
    """

    partner_column_name: str
    partner_table_name: str
    partner_physical_table: str
    join_type: str
    reason: str


class DimensionResponse(OrmBase):
    id: uuid.UUID
    model_id: uuid.UUID
    name: str
    display_name: Optional[str]
    description: Optional[str] = None
    effective_description: Optional[str] = None  # glossary > description, used by gateway
    display_folder: Optional[str] = None
    is_hidden: bool = False
    source_column_id: Optional[uuid.UUID]
    source_column_name: Optional[str] = None
    # Bug-5434: distinct display column (caption source) for a flat dimension.
    display_column_id: Optional[uuid.UUID] = None
    display_column_name: Optional[str] = None
    data_type: Optional[str] = None
    source_table_id: Optional[uuid.UUID] = None
    source_table_alias: Optional[str] = None
    source_table_display_name: Optional[str] = None
    user_defined_attribute_id: Optional[uuid.UUID] = None
    user_defined_attribute_name: Optional[str] = None
    is_time_dim: bool
    time_grain: Optional[str]
    calc_expression: Optional[str] = None
    calc_expression_tables: Optional[list] = None
    is_invalid: bool = False
    invalid_reason: Optional[str] = None
    redundant_partner: Optional[RedundantPartnerInfo] = None
    high_cardinality: Optional[bool] = None
    warnings: list[str] = Field(default_factory=list)
    # Provenance: when a dimension was auto-added as a detail of another
    # dimension's bijection relationship, these record the source relationship
    # and owning dimension. Null for independently created dimensions.
    detail_of_relationship_id: Optional[uuid.UUID] = None
    detail_of_dimension_id: Optional[uuid.UUID] = None
    detail_of_dimension_name: Optional[str] = None
    # Derived-grain routing (spec §5.3): declared key-to-detail relationships on
    # this dimension. Distinct from ``display_column_id``. Empty for dimensions
    # with no declared relationship. Diagnostic/declaration only in Phase 1b.
    attribute_relationships: list[DimensionAttributeRelationshipResponse] = Field(
        default_factory=list
    )
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Measure
# ---------------------------------------------------------------------------

_VARIANTS_REQUIRING_N: frozenset[str] = frozenset({"trailing_n", "moving_avg_n"})

CALCULATED_AGG_MODES: frozenset[str] = frozenset(
    {"expression_as_written", "per_row_then_aggregate"}
)
MEASURE_TYPES: frozenset[str] = frozenset({"standard", "calculated"})


def _validate_variant_fields(
    variant_kind: Optional[str],
    variant_of_measure_id: Optional[uuid.UUID],
    variant_n: Optional[int],
) -> Optional[str]:
    """Cross-field check: variant_kind and variant_of must be paired; variant_n
    is only meaningful for parametric variants.

    Returns the (possibly canonicalized) variant_kind so alias kinds are
    folded to their canonical counterpart at the API boundary.

    Canonicalization is applied on create only — MeasureUpdate has no
    variant_kind field, so variant_kind is effectively immutable after
    creation (treat as create-time).
    """
    if (variant_kind is None) != (variant_of_measure_id is None):
        raise ValueError(
            "variant_kind and variant_of_measure_id must both be set or both be null"
        )
    if variant_kind is not None:
        # Fold known aliases to their canonical kind (e.g.
        # "period_to_date" -> "ytd").
        canonical = _TIME_VARIANT_ALIASES.get(variant_kind, variant_kind)
        # Accept any kind in TIME_VARIANT_NAMES (the permissive validation
        # surface).  This intentionally includes the undocumented extras
        # (lead, cagr, pct_change) so that pre-existing measures and
        # raw-API callers carrying those kinds continue to validate.
        # See measure_formats.py:210-217 for the design contract.
        if canonical not in _TIME_VARIANT_NAMES:
            raise ValueError(
                f"variant_kind {variant_kind!r} is not a supported time-variant; "
                f"accepted: {sorted(_TIME_VARIANT_NAMES)}"
            )
        variant_kind = canonical
    if variant_n is not None and variant_kind not in _VARIANTS_REQUIRING_N:
        raise ValueError(
            "variant_n is only valid for parametric variants (trailing_n, moving_avg_n)"
        )
    # Bug-7181: variant_n must be explicitly supplied for parametric
    # variants. The previous behaviour silently defaulted to 12 or 30,
    # which assumes monthly grain. By requiring the caller to supply
    # variant_n, the modeller chooses a window size appropriate for the
    # data's actual grain.
    if variant_kind in _VARIANTS_REQUIRING_N and variant_n is None:
        raise ValueError(
            f"variant_n is required for {variant_kind} variants. "
            "Specify the number of periods for the rolling window "
            "(e.g. variant_n=12 for a 12-period trailing window)."
        )
    # Bug-7181: enforce a sensible range on variant_n.
    if variant_n is not None:
        if variant_n < 1:
            raise ValueError(
                f"variant_n must be >= 1; got {variant_n}"
            )
        if variant_n > 1000:
            raise ValueError(
                f"variant_n must be <= 1000 (got {variant_n}); "
                "extremely large window frames can cause performance "
                "issues on some database engines."
            )
    return variant_kind


class MeasureCreate(BaseModel):
    name: str = Field(max_length=255)
    display_name: Optional[str] = None
    description: Optional[str] = None
    display_folder: Optional[str] = None
    source_table_id: Optional[uuid.UUID] = Field(default=None, description="ModelTable the column belongs to")
    source_column_name: Optional[str] = Field(default=None, description="Column name to resolve to source_column_id")
    user_defined_attribute_id: Optional[uuid.UUID] = Field(default=None, description="User-defined attribute id")
    measure_type: str = "standard"
    expression: Optional[str] = None
    calc_agg_mode: Optional[str] = Field(
        default=None,
        description=(
            "Aggregation semantics for calculated measures: "
            "'expression_as_written' (combine pre-aggregated measures) or "
            "'per_row_then_aggregate' (evaluate at fact grain, then aggregate). "
            "Required when measure_type='calculated'; null otherwise."
        ),
    )
    data_type: str = "numeric"
    default_agg: str = "sum"
    format: Optional[str] = Field(default=None, description="Presentation format token; see shared/schemas/measure_formats.py")
    variant_kind: Optional[str] = Field(
        default=None,
        description="Time-variant kind; one of TIME_VARIANT_NAMES. Set together with variant_of_measure_id.",
    )
    variant_of_measure_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Base measure this variant derives from. Required when variant_kind is set.",
    )
    variant_n: Optional[int] = Field(
        default=None,
        description="N parameter for trailing_n / moving_avg_n variants; null means use the system default.",
    )
    is_additive: bool = True
    semi_additive_behavior: Optional[str] = Field(
        default=None,
        description="Semi-additive aggregation across time: last_non_empty, first_non_empty, avg_of_children, min, max",
    )
    semi_additive_account_column_id: Optional[uuid.UUID] = Field(
        default=None,
        description=(
            "Retained for pre-existing data only. It was the per-account "
            "aggregation column used by the now-unsupported 'by_account' "
            "behaviour (#10); no supported behaviour reads it."
        ),
    )
    calendar_model_table_id: Optional[uuid.UUID] = Field(
        default=None,
        description=(
            "ModelTable (calendar alias) used by the time-variant resolver. "
            "Required at the API layer when the measure has any time-variant "
            "enabled. The linked ModelTable must have calendar_table_id set."
        ),
    )
    hierarchy_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Hierarchy used by period-boundary variants to resolve the calendar.",
    )
    date_dimension_column_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Date column for window variants (trailing_n, lag_n, moving_avg_n); used directly for ORDER BY.",
    )
    cross_model_source_model_id: Optional[uuid.UUID] = Field(
        default=None,
        description=(
            "UUID of a model in the same project that provides the source measure. "
            "Must be set together with cross_model_source_measure_id or both must be null."
        ),
    )
    cross_model_source_measure_id: Optional[uuid.UUID] = Field(
        default=None,
        description=(
            "UUID of the measure in the cross-model source model. "
            "Must be set together with cross_model_source_model_id or both must be null."
        ),
    )

    @field_validator("format")
    @classmethod
    def _check_format_token(cls, value: Optional[str]) -> Optional[str]:
        if value is None or value == "":
            return value
        if value not in _MEASURE_FORMAT_TOKENS:
            raise ValueError(
                f"format must be one of {sorted(_MEASURE_FORMAT_TOKENS)}; got {value!r}"
            )
        return value

    @field_validator("default_agg")
    @classmethod
    def _check_default_agg(cls, value: str) -> str:
        return _validate_default_agg(value)

    @model_validator(mode="after")
    def _check_semi_additive_fields(self):
        # #10: ``by_account`` is no longer in VALID_SEMI_ADDITIVE_BEHAVIORS, so a
        # by_account authoring attempt is rejected here as an invalid enum. No
        # account-column requirement branch is needed any more (it was only
        # reachable for by_account, which never reaches this point).
        valid_behaviors = VALID_SEMI_ADDITIVE_BEHAVIORS
        if self.semi_additive_behavior is not None:
            if self.semi_additive_behavior not in valid_behaviors:
                raise ValueError(
                    f"semi_additive_behavior must be one of {sorted(valid_behaviors)}; "
                    f"got {self.semi_additive_behavior!r}"
                )
        return self

    @model_validator(mode="after")
    def _check_variant_fields(self):
        self.variant_kind = _validate_variant_fields(
            self.variant_kind,
            self.variant_of_measure_id,
            self.variant_n,
        )
        return self

    @model_validator(mode="after")
    def _check_measure_type_fields(self):
        if self.measure_type not in MEASURE_TYPES:
            raise ValueError(
                f"measure_type must be one of {sorted(MEASURE_TYPES)}; got {self.measure_type!r}"
            )
        if self.measure_type == "calculated":
            if not self.expression or not self.expression.strip():
                raise ValueError("calculated measures require a non-empty expression")
            if self.source_table_id or self.source_column_name or self.user_defined_attribute_id:
                raise ValueError(
                    "calculated measures cannot have source_table_id, source_column_name, or user_defined_attribute_id"
                )
            if self.variant_kind is not None:
                raise ValueError("variants on calculated measures are not supported in v1")
            if self.calc_agg_mode is None:
                raise ValueError(
                    "calculated measures require calc_agg_mode "
                    f"({sorted(CALCULATED_AGG_MODES)})"
                )
            if self.calc_agg_mode not in CALCULATED_AGG_MODES:
                raise ValueError(
                    f"calc_agg_mode must be one of {sorted(CALCULATED_AGG_MODES)}; "
                    f"got {self.calc_agg_mode!r}"
                )
        else:
            if self.expression is not None:
                raise ValueError("expression is only valid when measure_type='calculated'")
            if self.calc_agg_mode is not None:
                raise ValueError("calc_agg_mode is only valid when measure_type='calculated'")
        return self

    @model_validator(mode="after")
    def _check_cross_model_ref(self) -> "MeasureCreate":
        has_model = self.cross_model_source_model_id is not None
        has_measure = self.cross_model_source_measure_id is not None
        if has_model != has_measure:
            raise ValueError(
                "cross_model_source_model_id and cross_model_source_measure_id "
                "must both be set or both be null"
            )
        return self

    @model_validator(mode="after")
    def _coerce_is_additive(self) -> "MeasureCreate":
        """Bug-8257: never persist ``is_additive=True`` on a measure whose own
        shape proves it cannot be summed.

        Coercion rather than rejection: ``is_additive`` defaults to True in the
        schema AND in the Measures panel's toggle, so a modeller picking ``avg``
        would otherwise get a 422 for a default they never chose. The response
        carries the coerced value, so the UI shows what was actually stored.

        A variant's ``default_agg`` is inherited from its base at persistence
        time and is not visible here, but ``variant_kind`` alone already forces
        False, so the variant leg needs no lookup.
        """
        self.is_additive = derive_is_additive(
            default_agg=self.default_agg,
            measure_type=self.measure_type,
            variant_kind=self.variant_kind,
            semi_additive_behavior=self.semi_additive_behavior,
            declared=self.is_additive,
        )
        return self


class MeasureUpdate(BaseModel):
    name: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    display_folder: Optional[str] = None
    source_table_id: Optional[uuid.UUID] = None
    source_column_name: Optional[str] = None
    user_defined_attribute_id: Optional[uuid.UUID] = None
    measure_type: Optional[str] = None
    expression: Optional[str] = None
    calc_agg_mode: Optional[str] = None
    data_type: Optional[str] = None
    default_agg: Optional[str] = None
    format: Optional[str] = None
    variant_n: Optional[int] = None
    is_additive: Optional[bool] = None
    semi_additive_behavior: Optional[str] = None
    semi_additive_account_column_id: Optional[uuid.UUID] = None
    calendar_model_table_id: Optional[uuid.UUID] = None
    hierarchy_id: Optional[uuid.UUID] = None
    date_dimension_column_id: Optional[uuid.UUID] = None
    cross_model_source_model_id: Optional[uuid.UUID] = None
    cross_model_source_measure_id: Optional[uuid.UUID] = None

    @field_validator("format")
    @classmethod
    def _check_format_token(cls, value: Optional[str]) -> Optional[str]:
        if value is None or value == "":
            return value
        if value not in _MEASURE_FORMAT_TOKENS:
            raise ValueError(
                f"format must be one of {sorted(_MEASURE_FORMAT_TOKENS)}; got {value!r}"
            )
        return value

    @field_validator("default_agg")
    @classmethod
    def _check_default_agg(cls, value: Optional[str]) -> Optional[str]:
        return _validate_default_agg(value, allow_legacy_synonyms=True)

    @model_validator(mode="after")
    def _check_semi_additive_fields(self):
        """Bug-6620: validate semi_additive_behavior on update, not just create.

        A PATCH previously bypassed the canonical-enum gate, allowing free-form
        values like ``last_value`` to persist and fail late (at query time or
        aggregate build). The validator mirrors MeasureCreate but is tolerant of
        None (partial update: field not supplied -> no-op).
        """
        # #10: ``by_account`` is no longer valid, so a PATCH that sets it is
        # rejected here as an invalid enum — a persisted by_account measure
        # cannot be edited to stay by_account, and no supported behaviour needs
        # the account column, so no account-column branch remains.
        if self.semi_additive_behavior is not None:
            valid_behaviors = VALID_SEMI_ADDITIVE_BEHAVIORS
            if self.semi_additive_behavior not in valid_behaviors:
                raise ValueError(
                    f"semi_additive_behavior must be one of {sorted(valid_behaviors)}; "
                    f"got {self.semi_additive_behavior!r}"
                )
        return self

    @model_validator(mode="after")
    def _check_variant_n_range(self) -> "MeasureUpdate":
        """Bug-7181 (codex F3): validate variant_n range on PATCH too.

        MeasureCreate requires variant_n for parametric variants and
        rejects out-of-range values. MeasureUpdate must apply the same
        range check when variant_n is supplied (partial-update: None
        means not supplied, no-op).
        """
        if self.variant_n is not None:
            if self.variant_n < 1:
                raise ValueError(
                    f"variant_n must be >= 1; got {self.variant_n}"
                )
            if self.variant_n > 1000:
                raise ValueError(
                    f"variant_n must be <= 1000 (got {self.variant_n}); "
                    "extremely large window frames can cause performance "
                    "issues on some database engines."
                )
        return self

    @model_validator(mode="after")
    def _check_cross_model_ref_update(self) -> "MeasureUpdate":
        has_model = self.cross_model_source_model_id is not None
        has_measure = self.cross_model_source_measure_id is not None
        if has_model != has_measure:
            raise ValueError(
                "cross_model_source_model_id and cross_model_source_measure_id "
                "must both be set or both be null"
            )
        return self


class MeasureResponse(OrmBase):
    id: uuid.UUID
    model_id: uuid.UUID
    name: str
    display_name: Optional[str]
    description: Optional[str] = None
    effective_description: Optional[str] = None  # glossary > description, used by gateway
    display_folder: Optional[str] = None
    is_hidden: bool = False
    source_column_id: Optional[uuid.UUID]
    source_column_name: Optional[str] = None
    source_table_id: Optional[uuid.UUID] = None
    user_defined_attribute_id: Optional[uuid.UUID] = None
    user_defined_attribute_name: Optional[str] = None
    measure_type: str
    expression: Optional[str]
    calc_agg_mode: Optional[str] = None
    data_type: str
    default_agg: str
    format: Optional[str] = None
    variant_kind: Optional[str] = Field(
        default=None,
        description="Time-variant kind on this row; null for plain/UDA measures.",
    )
    variant_of_measure_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Base measure this variant derives from; null for plain/UDA measures.",
    )
    variant_n: Optional[int] = Field(
        default=None,
        description="Resolved N for parametric variants (trailing_n, moving_avg_n).",
    )
    calendar_model_table_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Calendar alias used by the time-variant resolver; null on plain measures with no variants.",
    )
    hierarchy_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Hierarchy used by period-boundary variants to resolve the calendar.",
    )
    resolved_calendar_id: Optional[uuid.UUID] = None
    resolved_date_col_id: Optional[uuid.UUID] = None
    date_dimension_column_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Date column for window variants (trailing_n, lag_n, moving_avg_n).",
    )
    is_additive: bool
    semi_additive_behavior: Optional[str] = None
    semi_additive_account_column_id: Optional[uuid.UUID] = None
    is_invalid: bool = False
    invalid_reason: Optional[str] = None
    cross_model_source_model_id: Optional[uuid.UUID] = None
    cross_model_source_measure_id: Optional[uuid.UUID] = None
    redundant_partner: Optional[RedundantPartnerInfo] = None
    eligible_variant_kinds: Optional[list[str]] = Field(
        default=None,
        description=(
            "Variant kinds the frontend may offer as ticks on this base measure — "
            "intersection of the associated hierarchy's level capabilities with the "
            "source-level calendar binding. Populated only for non-variant base "
            "measures; null for variant rows and measures without a time hierarchy."
        ),
    )
    created_at: datetime
    updated_at: datetime


class CalculatedExpressionValidateRequest(BaseModel):
    expression: str = Field(description="The calculated-measure DSL expression to validate.")
    self_measure_id: Optional[uuid.UUID] = Field(
        default=None,
        description=(
            "When validating an edit to an existing calculated measure, pass its id "
            "so the cycle detector excludes the row's current expression."
        ),
    )


class CalculatedExpressionValidateResponse(BaseModel):
    valid: bool
    referenced_measure_ids: list[uuid.UUID] = Field(default_factory=list)
    referenced_measure_names: list[str] = Field(default_factory=list)
    error: Optional[str] = Field(
        default=None,
        description="Populated when valid=false with a human-readable reason.",
    )
