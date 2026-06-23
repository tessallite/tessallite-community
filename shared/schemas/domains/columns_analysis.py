"""Auto-split from pydantic_models.py — Model Column, Table Auto-Analysis, User-defined Attributes"""
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
# Model Column
# ---------------------------------------------------------------------------

class ModelColumnResponse(OrmBase):
    id: uuid.UUID
    model_table_id: uuid.UUID
    column_name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    is_hidden: bool = False
    hidden_reason: Optional[str] = None
    is_primary_key: bool = False
    data_type: str
    is_nullable: bool
    cardinality_estimate: Optional[int]
    last_stats_at: Optional[datetime]
    created_at: datetime


class ModelColumnUpdate(BaseModel):
    """Modeller-editable fields on a physical column.

    `is_hidden` cascades through Phase 1's gateway visibility rules: hiding a
    physical column hides every Dimension/Measure that points at it.
    """
    display_name: Optional[str] = Field(default=None, max_length=255)
    description: Optional[str] = None
    is_hidden: Optional[bool] = None
    is_primary_key: Optional[bool] = None


# ---------------------------------------------------------------------------
# Table Auto-Analysis
# ---------------------------------------------------------------------------

class ColumnSuggestionResponse(BaseModel):
    column_id: str
    column_name: str
    suggested_role: str
    reason: str


class MeasureWarningResponse(BaseModel):
    column_id: str
    column_name: str
    current_role: str
    suggested_role: str
    severity: str
    reason: str


class TableAnalysisResponse(BaseModel):
    table_id: str
    suggested_table_type: str
    confidence: str
    reasoning: str
    column_suggestions: list[ColumnSuggestionResponse]
    date_columns: list[str]
    potential_calendar_column: Optional[str] = None
    measure_warnings: list[MeasureWarningResponse] = []


# ---------------------------------------------------------------------------
# User-defined Attributes
# ---------------------------------------------------------------------------

class UserDefinedAttributeCreate(BaseModel):
    name: str = Field(max_length=255)
    expression: str
    output_data_type: str = Field(description="varchar | integer | numeric | date")
    description: Optional[str] = None


class UserDefinedAttributeUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=255)
    expression: Optional[str] = None
    output_data_type: Optional[str] = Field(default=None, description="varchar | integer | numeric | date")
    description: Optional[str] = None


class UserDefinedAttributeResponse(OrmBase):
    id: uuid.UUID
    table_id: uuid.UUID
    model_id: uuid.UUID
    name: str
    expression: str
    output_data_type: str
    description: Optional[str]
    validated: bool
    validation_error: Optional[str]
    is_generated: bool = False
    referenced_columns: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class UserDefinedAttributeValidateRequest(BaseModel):
    expression: str
    output_data_type: str = Field(description="varchar | integer | numeric | date")


class UserDefinedAttributeLiveValidationResult(BaseModel):
    executed: bool
    success: bool
    error: Optional[str] = None
    sample_value: Optional[str] = None


class UserDefinedAttributeValidateResponse(BaseModel):
    parse_valid: bool
    columns_resolved: bool
    referenced_columns: list[str] = Field(default_factory=list)
    unsupported_functions: list[str] = Field(default_factory=list)
    live_validation: UserDefinedAttributeLiveValidationResult


class UserDefinedAttributeFunctionOption(BaseModel):
    name: str
    signature: str
    template: str
    description: str


class TableAttributeResponse(BaseModel):
    kind: str = Field(description="physical | user_defined")
    id: uuid.UUID
    table_id: uuid.UUID
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    is_hidden: bool = False
    hidden_reason: Optional[str] = None
    is_primary_key: bool = False
    data_type: str
    is_user_defined: bool
    is_generated: bool = False
    expression: Optional[str] = None
    validated: Optional[bool] = None
    validation_error: Optional[str] = None


