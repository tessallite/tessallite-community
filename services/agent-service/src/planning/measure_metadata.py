"""Measure metadata helpers for shape-safe planning."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from shared.schemas.measure_formats import (
    CANONICAL_TIME_VARIANT_ORDER,
    TIME_VARIANT_ALIASES,
    TIME_VARIANT_DEFAULT_MOVING_AVG_N,
    TIME_VARIANT_DEFAULT_TRAILING_N,
    TIME_VARIANT_FAMILY,
    TIME_VARIANT_NAMES,
    TIME_VARIANT_REQUIRED_UNIT,
    TIME_VARIANTS_NEEDING_CALENDAR,
    canonical_variant_kind,
)


@dataclass(frozen=True)
class MeasureRoleMetadata:
    name: str
    measure_type: Literal["standard", "calculated"] = "standard"
    source_kind: Literal[
        "physical_column",
        "user_defined_attribute",
        "calculated_expression",
        "time_variant",
        "cross_model",
        "unknown",
    ] = "unknown"
    data_type: str = "numeric"
    default_agg: str = "sum"
    format: str | None = None
    expression: str | None = None
    calc_agg_mode: str | None = None
    user_defined_attribute_name: str | None = None
    variant_kind: str | None = None
    variant_of_measure: str | None = None
    is_additive: bool | None = None
    # Bug-8257 (deep-review R6 finding 3): the additivity RULES this
    # metadata feeds are a documented mirror of
    # ``shared...dimensions_measures.derive_is_additive``. A semi-additive
    # behaviour is one of those rules, so the mirror has to carry it or the
    # catalogue announces a balance measure as additive.
    semi_additive_behavior: str | None = None
    time_window_kind: str | None = None
    window_size: int | None = None
    calendar_model_table_id: str | None = None
    hierarchy_id: str | None = None
    date_dimension_column_id: str | None = None
    resolved_calendar_id: str | None = None
    resolved_date_col_id: str | None = None
    cross_model_source_model_id: str | None = None
    cross_model_source_measure_id: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def canonical_variant_kind(self) -> str | None:
        return canonical_variant_kind(self.variant_kind)

    @property
    def is_time_variant(self) -> bool:
        return self.variant_kind in TIME_VARIANT_NAMES

    @property
    def time_variant_family(self) -> str | None:
        kind = self.canonical_variant_kind
        return TIME_VARIANT_FAMILY.get(kind) if kind else None

    @property
    def time_variant_required_unit(self) -> str | None:
        kind = self.canonical_variant_kind
        return TIME_VARIANT_REQUIRED_UNIT.get(kind) if kind else None

    @property
    def needs_calendar(self) -> bool:
        kind = self.variant_kind
        return bool(kind and kind in TIME_VARIANTS_NEEDING_CALENDAR)

    @property
    def resolved_window_size(self) -> int | None:
        kind = self.canonical_variant_kind
        if self.window_size is not None:
            return self.window_size
        if kind == "trailing_n":
            return TIME_VARIANT_DEFAULT_TRAILING_N
        if kind == "moving_avg_n":
            return TIME_VARIANT_DEFAULT_MOVING_AVG_N
        return None

    @property
    def additivity_known_safe(self) -> bool:
        return self.is_additive is True

    @property
    def additivity_notes(self) -> list[str]:
        notes: list[str] = []
        if self.is_time_variant and self.is_additive is not True:
            notes.append("time_variant_treated_non_additive")
        if self.measure_type == "calculated" and self.is_additive is not True:
            notes.append("calculated_measure_treated_non_additive")
        return notes

    @classmethod
    def from_mapping(cls, name: str, values: dict[str, Any]) -> "MeasureRoleMetadata":
        variant_kind = values.get("variant_kind")
        window_size = values.get("variant_n") or values.get("window_size")
        source_kind = values.get("source_kind") or "unknown"
        if variant_kind:
            source_kind = "time_variant"
        elif values.get("cross_model_source_model_id") or values.get("cross_model_source_measure_id"):
            source_kind = "cross_model"
        elif values.get("user_defined_attribute_name") or values.get("user_defined_attribute_id"):
            source_kind = "user_defined_attribute"
        elif values.get("expression"):
            source_kind = "calculated_expression"
        return cls(
            name=name,
            measure_type=values.get("measure_type") or "standard",
            source_kind=source_kind,
            data_type=values.get("data_type") or "numeric",
            default_agg=values.get("default_agg") or values.get("agg") or "sum",
            format=values.get("format"),
            expression=values.get("expression"),
            calc_agg_mode=values.get("calc_agg_mode"),
            user_defined_attribute_name=values.get("user_defined_attribute_name"),
            variant_kind=variant_kind,
            variant_of_measure=values.get("variant_of_measure")
            or values.get("variant_of_measure_id"),
            is_additive=values.get("is_additive"),
            semi_additive_behavior=values.get("semi_additive_behavior"),
            time_window_kind=values.get("time_window_kind"),
            window_size=window_size if isinstance(window_size, int) else None,
            calendar_model_table_id=values.get("calendar_model_table_id"),
            hierarchy_id=values.get("hierarchy_id"),
            date_dimension_column_id=values.get("date_dimension_column_id"),
            resolved_calendar_id=values.get("resolved_calendar_id"),
            resolved_date_col_id=values.get("resolved_date_col_id"),
            cross_model_source_model_id=values.get("cross_model_source_model_id"),
            cross_model_source_measure_id=values.get("cross_model_source_measure_id"),
            notes=list(values.get("notes") or []),
        )

    def as_trace(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "measure_type": self.measure_type,
            "source_kind": self.source_kind,
            "data_type": self.data_type,
            "default_agg": self.default_agg,
            "format": self.format,
            "variant_kind": self.variant_kind,
            "canonical_variant_kind": self.canonical_variant_kind,
            "variant_family": self.time_variant_family,
            "required_unit": self.time_variant_required_unit,
            "needs_calendar": self.needs_calendar,
            "window_size": self.resolved_window_size,
            "is_additive": self.is_additive,
            "cross_model_source_model_id": self.cross_model_source_model_id,
            "cross_model_source_measure_id": self.cross_model_source_measure_id,
            "notes": [*self.notes, *self.additivity_notes],
        }


@dataclass(frozen=True)
class CompoundFormulaMetadata:
    result_label: str
    expression_tree: dict[str, Any]
    referenced_steps: list[str]
    referenced_measures: list[str]
    operation_kinds: list[str]
    denominator_refs: list[str] = field(default_factory=list)
    null_on_zero_denominator: bool = False
    alignment_mode: str | None = None
    is_multi_row: bool = False

    def as_trace(self) -> dict[str, Any]:
        return {
            "result_label": self.result_label,
            "referenced_steps": list(self.referenced_steps),
            "referenced_measures": list(self.referenced_measures),
            "operation_kinds": list(self.operation_kinds),
            "denominator_refs": list(self.denominator_refs),
            "null_on_zero_denominator": self.null_on_zero_denominator,
            "alignment_mode": self.alignment_mode,
            "is_multi_row": self.is_multi_row,
        }


TIME_VARIANT_TRACE = {
    "validation_names": sorted(TIME_VARIANT_NAMES),
    "canonical_order": list(CANONICAL_TIME_VARIANT_ORDER),
    "aliases": dict(TIME_VARIANT_ALIASES),
}
