"""Auto-split from pydantic_models.py — Hierarchies"""
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
# Hierarchies
# ---------------------------------------------------------------------------

class HierarchyCreate(BaseModel):
    name: str = Field(max_length=255)
    type: str = Field(description="explicit | date_embedded | segment")
    dimension_kind: Optional[str] = Field(default=None, description="time | geo | entity")
    description: Optional[str] = None
    segment_config: Optional[dict[str, Any]] = None
    date_config: Optional[dict[str, Any]] = None
    calendar_type: Optional[str] = Field(default=None, description="standard | fiscal | iso_week | retail_445 | hijri | thai_buddhist (legacy 'iso' accepted, normalised to iso_week)")
    fiscal_year_start_month: Optional[int] = Field(default=None, description="1-12, required when calendar_type is fiscal")


class HierarchyUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=255)
    type: Optional[str] = Field(default=None, description="explicit | date_embedded | segment")
    dimension_kind: Optional[str] = Field(default=None, description="time | geo | entity")
    description: Optional[str] = None
    segment_config: Optional[dict[str, Any]] = None
    date_config: Optional[dict[str, Any]] = None
    calendar_type: Optional[str] = Field(default=None, description="standard | fiscal | iso_week | retail_445 | hijri | thai_buddhist (legacy 'iso' accepted, normalised to iso_week)")
    fiscal_year_start_month: Optional[int] = Field(default=None, description="1-12, required when calendar_type is fiscal")


class HierarchyAttributeRef(BaseModel):
    id: uuid.UUID
    name: str
    table_id: uuid.UUID
    table_name: str
    data_type: str
    source: str = Field(description="physical_column | user_defined_attribute")


class HierarchyLevelAttributeCreate(BaseModel):
    attribute_id: uuid.UUID
    attribute_source: str = Field(description="physical_column | user_defined_attribute")
    role: str = Field(description="display | filter")


class HierarchyLevelAttributeResponse(BaseModel):
    id: uuid.UUID
    attribute: HierarchyAttributeRef
    role: str


def _validate_time_unit(value: Optional[str]) -> Optional[str]:
    if value is None or value == "":
        return value
    if value not in _HIERARCHY_TIME_UNITS:
        raise ValueError(
            f"time_unit must be one of {sorted(_HIERARCHY_TIME_UNITS)}; got {value!r}"
        )
    return value


def _validate_time_calcs(value: Optional[list[str]]) -> Optional[list[str]]:
    if not value:
        return value
    bad = [t for t in value if t not in _HIERARCHY_TIME_CALCS]
    if bad:
        raise ValueError(
            f"allowed_time_calcs contains unknown tokens {bad!r}; "
            f"valid values are {sorted(_HIERARCHY_TIME_CALCS)}"
        )
    return value


class HierarchyLevelCreate(BaseModel):
    name: str = Field(max_length=255)
    ordinal: int = Field(ge=0)
    key_attribute_id: uuid.UUID
    key_attribute_source: str = Field(description="physical_column | user_defined_attribute")
    description: Optional[str] = None
    time_unit: Optional[str] = Field(default=None, description="year|half|quarter|month|week|day|hour|none")
    allowed_time_calcs: list[str] = Field(default_factory=list)
    attributes: list[HierarchyLevelAttributeCreate] = Field(default_factory=list)

    @field_validator("time_unit")
    @classmethod
    def _check_time_unit(cls, v: Optional[str]) -> Optional[str]:
        return _validate_time_unit(v)

    @field_validator("allowed_time_calcs")
    @classmethod
    def _check_time_calcs(cls, v: list[str]) -> list[str]:
        return _validate_time_calcs(v) or []


class HierarchyLevelUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=255)
    ordinal: Optional[int] = Field(default=None, ge=0)
    key_attribute_id: Optional[uuid.UUID] = None
    key_attribute_source: Optional[str] = Field(default=None, description="physical_column | user_defined_attribute")
    description: Optional[str] = None
    time_unit: Optional[str] = None
    allowed_time_calcs: Optional[list[str]] = None
    attributes: Optional[list[HierarchyLevelAttributeCreate]] = None

    @field_validator("time_unit")
    @classmethod
    def _check_time_unit(cls, v: Optional[str]) -> Optional[str]:
        return _validate_time_unit(v)

    @field_validator("allowed_time_calcs")
    @classmethod
    def _check_time_calcs(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        return _validate_time_calcs(v)


class HierarchyLevelResponse(BaseModel):
    id: uuid.UUID
    name: str
    ordinal: int
    key_attribute: HierarchyAttributeRef
    attributes: list[HierarchyLevelAttributeResponse] = Field(default_factory=list)
    description: Optional[str] = None
    time_unit: Optional[str] = None
    allowed_time_calcs: list[str] = Field(default_factory=list)


class HierarchySummaryResponse(OrmBase):
    id: uuid.UUID
    model_id: uuid.UUID
    name: str
    type: str
    dimension_kind: Optional[str] = None
    description: Optional[str]
    calendar_type: Optional[str] = None
    fiscal_year_start_month: Optional[int] = None
    level_count: int
    level_names: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class HierarchyDetailResponse(OrmBase):
    id: uuid.UUID
    model_id: uuid.UUID
    name: str
    type: str
    dimension_kind: Optional[str] = None
    description: Optional[str]
    segment_config: Optional[dict[str, Any]]
    date_config: Optional[dict[str, Any]]
    calendar_type: Optional[str] = None
    fiscal_year_start_month: Optional[int] = None
    levels: list[HierarchyLevelResponse] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class HierarchyReorderRequest(BaseModel):
    level_ids_in_order: list[uuid.UUID] = Field(default_factory=list)


class HierarchyGenerateDateRequest(BaseModel):
    name: str = Field(max_length=255)
    source_attribute_id: uuid.UUID
    source_attribute_source: str = Field(
        default="physical_column",
        description="physical_column | user_defined_attribute",
    )
    template: str = Field(description="y_m_d | y_q_m_d | y_h_q_m_d | y_w_d | y_m_w_d")
    description: Optional[str] = None
    calendar_type: Optional[str] = Field(
        default=None,
        description="standard | fiscal | iso_week | retail_445 | hijri | thai_buddhist (legacy 'iso' accepted, normalised to iso_week)",
    )
    fiscal_year_start_month: Optional[int] = Field(
        default=None,
        description="1-12, only used when calendar_type is fiscal",
    )


class HierarchySegmentLevelCreate(BaseModel):
    name: str = Field(max_length=255)


class HierarchySegmentSliceCreate(BaseModel):
    name: str = Field(max_length=255)
    start: int = Field(ge=1)
    length: int = Field(ge=1)


class HierarchyGenerateSegmentRequest(BaseModel):
    name: str = Field(max_length=255)
    source_attribute_id: uuid.UUID
    source_attribute_source: str = Field(
        default="physical_column",
        description="physical_column | user_defined_attribute",
    )
    mode: str = Field(description="delimiter | positional")
    delimiter: Optional[str] = None
    levels: list[HierarchySegmentLevelCreate] = Field(default_factory=list)
    segments: list[HierarchySegmentSliceCreate] = Field(default_factory=list)
    description: Optional[str] = None


class HierarchyGeneratedResponse(BaseModel):
    hierarchy: HierarchyDetailResponse
    generated_attributes: list[HierarchyAttributeRef] = Field(default_factory=list)


class UnassignedDateColumn(BaseModel):
    column_id: uuid.UUID
    column_name: str
    display_name: Optional[str] = None
    data_type: str
    table_id: uuid.UUID
    table_alias: str
    is_uda: bool = False


class HierarchyBatchDateRequest(BaseModel):
    grain: str
    calendar_table_id: uuid.UUID
    # F-016-10: ``measure_ids`` was a vestige of the abandoned
    # hierarchy-measure-link design (see F-016-21); the field was validated but
    # never used to create any link. Removed — measures bind to date hierarchies
    # at query time via ``Measure.hierarchy_id`` + the join graph, not here.
    column_ids: list[uuid.UUID] = Field(default_factory=list)


class HierarchyBatchDateSkipped(BaseModel):
    column_name: str
    reason: str


class HierarchyBatchDateResponse(BaseModel):
    created_hierarchies: int
    created_aliases: int
    skipped: list[HierarchyBatchDateSkipped]


class HierarchyPreviewWarning(BaseModel):
    level_name: str
    type: str
    message: str


class HierarchyPreviewLevelSummary(BaseModel):
    ordinal: int
    name: str
    estimated_members: Optional[int] = None


class HierarchyPreviewMember(BaseModel):
    level_ordinal: int
    level_name: str
    key_value: str
    # Bug-3617 (Phase 0.5a): the member's display caption, distinct from its
    # key. Resolved from the level's display-role HierarchyLevelAttribute when
    # one is configured; defaults to ``key_value`` when none. Lets the XMLA
    # member-identity layer separate MEMBER_KEY from MEMBER_CAPTION.
    caption: Optional[str] = None
    # Bug-3617 (Phase 0.5b): ancestor-first key path [root_key, ..., this_key]
    # for the member, when the discovery path can supply it (whole-level
    # enumeration of a single-table hierarchy). None on the drill path, where
    # the gateway reconstructs the path from the inbound canonical restriction.
    key_path: Optional[list[str]] = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    parent_key: Optional[str] = None
    children_loaded: bool = False
    child_count_estimate: Optional[int] = None


class HierarchyPreviewResponse(BaseModel):
    hierarchy_id: uuid.UUID
    hierarchy_name: str
    sample_size: int
    warnings: list[HierarchyPreviewWarning] = Field(default_factory=list)
    levels_summary: list[HierarchyPreviewLevelSummary] = Field(default_factory=list)
    members: list[HierarchyPreviewMember] = Field(default_factory=list)


