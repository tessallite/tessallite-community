"""Auto-split from pydantic_models.py — Model, Data Source, Calendar table (Phase 2), Data Target, Model Table"""
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
# Model
# ---------------------------------------------------------------------------

class ModelCreate(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9_-]+$", max_length=64)
    display_name: Optional[str] = Field(default=None, max_length=255)
    description: Optional[str] = None
    refresh_strategy: str = "scheduled"
    aggregations_enabled: bool = True
    include_all_measures: bool = True
    max_aggregates: int = 50
    miss_threshold_daily: int = 3
    miss_threshold_weekly: int = 5
    schema_drift_interval_hours: int = 24
    pocket_size_budget_bytes: Optional[int] = Field(
        default=None, ge=0,
        description="Per-model byte ceiling for pocket tables. NULL = inherit from project.",
    )


EvictionPolicy = Literal[
    "predicted_first", "lru", "validated_survives", "never_evict"
]


class ModelUpdate(BaseModel):
    slug: Optional[str] = Field(None, pattern=r"^[a-z0-9_-]+$", max_length=64)
    display_name: Optional[str] = None
    description: Optional[str] = None
    target_id: Optional[uuid.UUID] = None
    refresh_strategy: Optional[str] = None
    status: Optional[str] = None
    aggregations_enabled: Optional[bool] = None
    include_all_measures: Optional[bool] = None
    max_aggregates: Optional[int] = None
    miss_threshold_daily: Optional[int] = None
    miss_threshold_weekly: Optional[int] = None
    schema_drift_interval_hours: Optional[int] = None
    canvas_layout: Optional[dict[str, Any]] = None
    # Phase 9 — predictive aggregate controls. All four are optional and
    # default-bearing on the DB side; PATCH only mutates when a key is sent.
    predictive_storage_budget_bytes: Optional[int] = Field(
        default=None, ge=0,
        description="Per-model byte ceiling for predictive aggregates. NULL = no cap on this axis.",
    )
    predictive_storage_budget_count: Optional[int] = Field(
        default=None, ge=0,
        description="Per-model count ceiling for predictive aggregates. NULL = no cap on this axis.",
    )
    predictive_eviction_policy: Optional[EvictionPolicy] = Field(
        default=None,
        description="Eviction policy when max_aggregates is exceeded.",
    )
    predictive_requires_approval: Optional[bool] = Field(
        default=None,
        description="When true, deploy-time predictive build waits for explicit approval.",
    )
    pocket_size_budget_bytes: Optional[int] = Field(
        default=None, ge=0,
        description="Per-model byte ceiling for pocket tables. NULL = inherit from project.",
    )
    glossary_max_distinct: Optional[int] = Field(
        default=None, ge=1, le=500,
        description="Max distinct values to probe per dimension during glossary bootstrap.",
    )


class ModelResponse(OrmBase):
    id: uuid.UUID
    project_id: uuid.UUID
    slug: str
    display_name: str
    description: Optional[str]
    target_id: Optional[uuid.UUID]
    refresh_strategy: str
    status: str
    aggregations_enabled: bool
    include_all_measures: Optional[bool] = None
    seed: str
    max_aggregates: int
    miss_threshold_daily: int
    miss_threshold_weekly: int
    schema_drift_interval_hours: int
    # Optional in the pydantic shape so create-then-return (before the
    # row is refreshed and picks up the JSONB server_default) doesn't
    # 422; reads from a refreshed row produce {} via default_factory.
    canvas_layout: Optional[dict[str, Any]] = Field(default_factory=dict)
    # Phase 9 — predictive aggregate controls (read side).
    predictive_storage_budget_bytes: Optional[int] = None
    predictive_storage_budget_count: Optional[int] = None
    predictive_eviction_policy: Optional[EvictionPolicy] = None
    # Optional in the pydantic shape — same reason as `canvas_layout`
    # above: the Boolean's server_default fires at flush, so a Model
    # serialized before refresh would otherwise 422 on None.
    predictive_requires_approval: Optional[bool] = None
    pocket_size_budget_bytes: Optional[int] = None
    glossary_max_distinct: Optional[int] = None
    deployed_version_id: Optional[uuid.UUID] = None
    last_deployed_at: Optional[datetime] = None
    # Populated by the models API after a join against model_versions. The
    # numbers are what the Model Builder toolbar chip displays (`Saved v5`,
    # `Deployed v3`); carrying them on ModelResponse saves the frontend a
    # second round-trip.
    deployed_version_number: Optional[int] = None
    last_saved_version_number: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    # Phase 5 of the semantic-layer plan: trust signals the gateway uses
    # to render freshness/source/owner footers in Excel column tooltips
    # and synthetic XMLA info measures.
    trust_meta: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Data Source
# ---------------------------------------------------------------------------

class DataSourceCreate(BaseModel):
    project_connection_id: uuid.UUID
    source_type: str = Field(description="bigquery | jdbc | tessallite_passthrough | hadoop_spark")
    display_name: str = Field(max_length=255)
    default_schema: Optional[str] = None
    config: dict[str, Any] = Field(default_factory=dict)


class DataSourceUpdate(BaseModel):
    project_connection_id: Optional[uuid.UUID] = None
    display_name: Optional[str] = None
    default_schema: Optional[str] = None
    config: Optional[dict[str, Any]] = None


class DataSourceResponse(OrmBase):
    id: uuid.UUID
    model_id: uuid.UUID
    project_connection_id: uuid.UUID
    source_type: str
    display_name: str
    default_schema: Optional[str] = None
    config: dict[str, Any]
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Calendar table (Phase 2)
# ---------------------------------------------------------------------------

CALENDAR_TYPES = frozenset({
    "standard", "fiscal", "iso_week", "retail_445",
    "hijri", "thai_buddhist",
})


class CalendarTableCreate(BaseModel):
    data_source_id: uuid.UUID
    table_name: str = Field(max_length=512, description="Fully-qualified physical table name including schema/dataset where applicable")
    dialect: str = Field(description="postgresql | bigquery | hadoop_spark")
    calendar_type: str = Field(default="standard", description="Calendar type: standard, fiscal, iso_week, retail_445, hijri, thai_buddhist")
    date_column: Optional[str] = None
    year_column: Optional[str] = None
    half_column: Optional[str] = None
    quarter_column: Optional[str] = None
    month_column: Optional[str] = None
    week_column: Optional[str] = None
    day_column: Optional[str] = None
    autocreated: bool = False

    @field_validator("calendar_type")
    @classmethod
    def _validate_calendar_type(cls, v: str) -> str:
        if v not in CALENDAR_TYPES:
            raise ValueError(f"calendar_type must be one of {sorted(CALENDAR_TYPES)}")
        return v


class CalendarTableUpdate(BaseModel):
    table_name: Optional[str] = None
    dialect: Optional[str] = None
    calendar_type: Optional[str] = None
    date_column: Optional[str] = None
    year_column: Optional[str] = None

    @field_validator("calendar_type")
    @classmethod
    def _validate_calendar_type(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in CALENDAR_TYPES:
            raise ValueError(f"calendar_type must be one of {sorted(CALENDAR_TYPES)}")
        return v
    half_column: Optional[str] = None
    quarter_column: Optional[str] = None
    month_column: Optional[str] = None
    week_column: Optional[str] = None
    day_column: Optional[str] = None


class CalendarTableResponse(OrmBase):
    id: uuid.UUID
    data_source_id: uuid.UUID
    table_name: str
    dialect: str
    calendar_type: str = "standard"
    date_column: Optional[str] = None
    year_column: Optional[str] = None
    half_column: Optional[str] = None
    quarter_column: Optional[str] = None
    month_column: Optional[str] = None
    week_column: Optional[str] = None
    day_column: Optional[str] = None
    autocreated: bool
    fiscal_year_start_month: int = 1
    created_at: datetime
    updated_at: datetime
    auto_created_aliases: list[str] = []


# ---------------------------------------------------------------------------
# Data Target
# ---------------------------------------------------------------------------

class DataTargetCreate(BaseModel):
    project_connection_id: uuid.UUID
    target_type: str = Field(description="bigquery | postgresql | hadoop_spark")
    display_name: str = Field(max_length=255)
    config: dict[str, Any] = Field(default_factory=dict)


class DataTargetUpdate(BaseModel):
    project_connection_id: Optional[uuid.UUID] = None
    target_type: Optional[str] = None
    display_name: Optional[str] = None
    config: Optional[dict[str, Any]] = None


class DataTargetResponse(OrmBase):
    id: uuid.UUID
    model_id: uuid.UUID
    project_connection_id: uuid.UUID
    target_type: str
    display_name: str
    config: dict[str, Any]
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Model Table
# ---------------------------------------------------------------------------

class ModelTableCreate(BaseModel):
    source_id: uuid.UUID
    table_type: str = Field(description="fact | dim_aggregate | dim_detail")
    physical_name: str = Field(max_length=512)
    alias: Optional[str] = Field(default=None, max_length=255, description="Unique alias within the model; auto-generated if omitted. Must match ^[a-z][a-z0-9_]*$ when provided.")
    display_name: str = Field(max_length=255)
    description: Optional[str] = None
    calendar_table_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Set when this ModelTable is a calendar alias. Links to the CalendarTable that carries column meanings used by time-variant measures.",
    )


class ModelTableUpdate(BaseModel):
    table_type: Optional[str] = None
    alias: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    calendar_table_id: Optional[uuid.UUID] = None


class ModelTableResponse(OrmBase):
    id: uuid.UUID
    model_id: uuid.UUID
    source_id: uuid.UUID
    table_type: str
    physical_name: str
    alias: str
    display_name: str
    description: Optional[str] = None
    row_count_estimate: Optional[int]
    last_stats_at: Optional[datetime]
    calendar_table_id: Optional[uuid.UUID] = None
    created_at: datetime
    updated_at: datetime


