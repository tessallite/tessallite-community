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

from ._base import OrmBase

# Canonical valid semi_additive_behavior values. Single source of truth shared
# by the Measure validator below and the snapshot rehydrator's enum gate
# (F-020-09) so importers cannot store invalid enums bypassing the API layer.
VALID_SEMI_ADDITIVE_BEHAVIORS: frozenset[str] = frozenset({
    "last_non_empty", "first_non_empty", "avg_of_children",
    "min", "max", "by_account",
})

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
        description="Semi-additive aggregation across time: last_non_empty, first_non_empty, avg_of_children, min, max, by_account",
    )
    semi_additive_account_column_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Column that determines per-row aggregation type when semi_additive_behavior='by_account'",
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

    @model_validator(mode="after")
    def _check_semi_additive_fields(self):
        valid_behaviors = VALID_SEMI_ADDITIVE_BEHAVIORS
        if self.semi_additive_behavior is not None:
            if self.semi_additive_behavior not in valid_behaviors:
                raise ValueError(
                    f"semi_additive_behavior must be one of {sorted(valid_behaviors)}; "
                    f"got {self.semi_additive_behavior!r}"
                )
            if self.semi_additive_behavior == "by_account" and not self.semi_additive_account_column_id:
                raise ValueError("by_account requires semi_additive_account_column_id")
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


