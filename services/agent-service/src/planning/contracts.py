"""Dataclasses used by shape matching, quality checks, and trace output."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from src.planning.enums import (
    AnalyticalShape,
    AxisRole,
    TemporalAxisKind,
    TemporalOrderKind,
    ValueRole,
)


@dataclass(frozen=True)
class ShapeLimits:
    max_pie_segments: int = 7
    max_bar_categories: int = 25
    max_grouped_measures: int = 6
    max_line_series: int = 12
    max_line_measures: int = 6
    max_chart_rows: int = 500

    @classmethod
    def from_mapping(cls, values: dict[str, Any] | None) -> "ShapeLimits":
        if not values:
            return cls()
        defaults = cls()
        data: dict[str, int] = {}
        for name in cls.__dataclass_fields__:
            raw = values.get(name, getattr(defaults, name))
            if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
                raise ValueError(f"shape limit {name} must be a positive integer")
            data[name] = raw
        return cls(**data)

    def as_trace(self) -> dict[str, int]:
        return {
            "max_pie_segments": self.max_pie_segments,
            "max_bar_categories": self.max_bar_categories,
            "max_grouped_measures": self.max_grouped_measures,
            "max_line_series": self.max_line_series,
            "max_line_measures": self.max_line_measures,
            "max_chart_rows": self.max_chart_rows,
        }


@dataclass
class FieldRole:
    name: str
    role: AxisRole | ValueRole
    confidence: Literal["metadata", "expression", "result", "heuristic"]
    source: Literal["measure", "dimension", "expression", "computed"]
    notes: list[str] = field(default_factory=list)
    candidate_roles: list[AxisRole | ValueRole] = field(default_factory=list)

    @property
    def ambiguous(self) -> bool:
        return bool(self.candidate_roles)

    def as_trace(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role.value,
            "confidence": self.confidence,
            "source": self.source,
            "notes": list(self.notes),
            "candidate_roles": [r.value for r in self.candidate_roles],
        }


@dataclass
class TemporalAxisSpec:
    field_name: str
    kind: TemporalAxisKind
    display_field: str
    sort_field: str | None
    order_kind: TemporalOrderKind
    grain: str | None = None
    cycle_size: int | None = None
    stable_across_years: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class DataQualityFinding:
    code: str
    severity: Literal["info", "warning", "block_chart", "block_answer"]
    message: str
    affected_columns: list[str] = field(default_factory=list)

    def as_trace(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "affected_columns": list(self.affected_columns),
        }


@dataclass
class ShapeNarrationFacts:
    shape: AnalyticalShape
    row_count: int
    output_mode: str | None = None
    value_label: str | None = None
    value: Any | None = None
    computed_value: bool = False
    date_range: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    series_coverage: dict[str, dict[str, Any]] = field(default_factory=dict)
    ranking: dict[str, Any] | None = None
    breakdown: dict[str, Any] | None = None
    composition: dict[str, Any] | None = None
    matrix: dict[str, Any] | None = None
    table: dict[str, Any] | None = None
    extrema: dict[str, Any] = field(default_factory=dict)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    truncation: dict[str, Any] | None = None
    chart_notes: list[str] = field(default_factory=list)
    data_quality_findings: list[dict[str, Any]] = field(default_factory=list)
    unsupported_notes: list[str] = field(default_factory=list)
    separate_series: bool = False

    def as_trace(self) -> dict[str, Any]:
        return {
            "shape": self.shape.value,
            "row_count": self.row_count,
            "output_mode": self.output_mode,
            "value_label": self.value_label,
            "value": self.value,
            "computed_value": self.computed_value,
            "date_range": self.date_range,
            "series_coverage": self.series_coverage,
            "ranking": self.ranking,
            "breakdown": self.breakdown,
            "composition": self.composition,
            "matrix": self.matrix,
            "table": self.table,
            "extrema": self.extrema,
            "gaps": self.gaps,
            "truncation": self.truncation,
            "chart_notes": self.chart_notes,
            "data_quality_findings": self.data_quality_findings,
            "unsupported_notes": self.unsupported_notes,
            "separate_series": self.separate_series,
        }


@dataclass
class ShapeContract:
    shape: AnalyticalShape
    required_axes: list[AxisRole]
    optional_axes: list[AxisRole]
    value_role: ValueRole
    chart_preference: str | None
    table_required: bool
    renderer_binding: dict[str, str]
    data_quality_checks: list[str]
    narration_fact_contract: str
    temporal_axes: list[TemporalAxisSpec] = field(default_factory=list)
    insight_fact_contract: str | None = None
    needs_stable_period_key: bool = False
    max_chart_rows: int | None = None
    max_categories: int | None = None
    max_series: int | None = None
    notes: list[str] = field(default_factory=list)

    def as_trace(self) -> dict[str, Any]:
        return {
            "shape": self.shape.value,
            "required_axes": [a.value for a in self.required_axes],
            "optional_axes": [a.value for a in self.optional_axes],
            "value_role": self.value_role.value,
            "chart_preference": self.chart_preference,
            "table_required": self.table_required,
            "renderer_binding": dict(self.renderer_binding),
            "data_quality_checks": list(self.data_quality_checks),
            "narration_fact_contract": self.narration_fact_contract,
            "needs_stable_period_key": self.needs_stable_period_key,
            "notes": list(self.notes),
        }
