"""Auto-split from pydantic_models.py — Tenant (system DB), Project, Project Connection (credential pool)"""
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
# Tenant (system DB)
# ---------------------------------------------------------------------------

class TenantCreate(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9_-]+$", max_length=64)
    display_name: str = Field(max_length=255)
    database_url: Optional[str] = Field(None, description="Optional PG URL; auto-derived from system DB if omitted.")


class TenantUpdate(BaseModel):
    display_name: Optional[str] = Field(None, max_length=255)
    is_active: Optional[bool] = None


class TenantResponse(OrmBase):
    id: uuid.UUID
    slug: str
    display_name: str
    db_schema_prefix: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------

class ProjectCreate(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9_-]+$", max_length=64)
    display_name: Optional[str] = Field(default=None, max_length=255)
    pocket_size_budget_bytes: Optional[int] = Field(
        default=None, ge=0,
        description="Project-level byte ceiling for pocket tables. NULL = unlimited.",
    )


class ProjectUpdate(BaseModel):
    slug: Optional[str] = Field(None, pattern=r"^[a-z0-9_-]+$", max_length=64)
    display_name: Optional[str] = Field(None, max_length=255)
    is_active: Optional[bool] = None
    pocket_size_budget_bytes: Optional[int] = Field(
        default=None, ge=0,
        description="Project-level byte ceiling for pocket tables. NULL = unlimited.",
    )


class ProjectResponse(OrmBase):
    id: uuid.UUID
    slug: str
    display_name: str
    is_active: bool
    pocket_size_budget_bytes: Optional[int] = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Project Connection (credential pool)
# ---------------------------------------------------------------------------

_ALLOWED_CONNECTION_TYPES = ("bigquery", "postgresql", "hadoop_spark", "redshift", "snowflake", "sqlserver")


def _validate_connection_type(value: str) -> str:
    """Phase C of the code-review remediation: writes must use the canonical
    ``hadoop_spark`` value; the legacy ``jdbc`` label was historically used
    for Spark/Hive but collided with PostgreSQL-over-JDBC, so it is now
    rejected on create/update paths. Existing rows with ``jdbc`` keep working
    because the read path normalises them — see ``_run_connection_test`` and
    ``query-router/_execute_sql``.
    """
    if value == "jdbc":
        raise ValueError(
            "connection_type 'jdbc' is no longer supported. Use 'hadoop_spark' "
            "for Spark/Hive Thrift connections, or 'postgresql' for PostgreSQL."
        )
    if value not in _ALLOWED_CONNECTION_TYPES:
        raise ValueError(
            f"connection_type must be one of {_ALLOWED_CONNECTION_TYPES}, got {value!r}."
        )
    return value


class ConnectionCreate(BaseModel):
    display_name: str = Field(max_length=255)
    connection_type: str = Field(description="bigquery | postgresql | hadoop_spark | redshift | snowflake | sqlserver")
    credentials: dict[str, Any] = Field(description="Raw credentials -- encrypted at rest, never returned")
    config: dict[str, Any] = Field(default_factory=dict, description="Non-sensitive config (host, dataset, etc.)")

    @field_validator("connection_type")
    @classmethod
    def _check_connection_type(cls, v: str) -> str:
        return _validate_connection_type(v)


class ConnectionUpdate(BaseModel):
    display_name: Optional[str] = None
    connection_type: Optional[str] = None
    credentials: Optional[dict[str, Any]] = None
    config: Optional[dict[str, Any]] = None

    @field_validator("connection_type")
    @classmethod
    def _check_connection_type(cls, v: Optional[str]) -> Optional[str]:
        # F-014-10: PATCH previously skipped validation, so a client could
        # set connection_type to ``jdbc``/``oracle``/any string and the
        # connection then failed at every dispatch site with "Unsupported
        # connector type". Apply the same gate as ConnectionCreate; ``None``
        # means "field not being changed" and is left untouched.
        if v is None:
            return v
        return _validate_connection_type(v)


class ConnectionResponse(OrmBase):
    id: uuid.UUID
    project_id: uuid.UUID
    display_name: str
    connection_type: str
    config: dict[str, Any]
    # Non-sensitive preview of the stored credentials so the edit dialog can
    # pre-fill host/port/database/username. Sensitive keys (password,
    # service_account_json, secret, token, api_key) are stripped server-side.
    # Populated by the API layer; the ORM object does not carry it.
    credentials_preview: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


# Legacy aliases
ProjectConnectionCreate = ConnectionCreate
ProjectConnectionUpdate = ConnectionUpdate
ProjectConnectionResponse = ConnectionResponse


class ProfileTableRef(BaseModel):
    """A single table to profile. ``schema`` defaults to ``public`` (PG)."""
    schema_: str = Field("public", alias="schema")
    table: str = Field(min_length=1)

    model_config = {"populate_by_name": True}


class ProfileTablesRequest(BaseModel):
    """F-014-14: typed body for the profile endpoint. Previously the route
    took a raw ``dict`` and ``tbl["table"]`` raised a bare ``KeyError`` that
    surfaced as a misleading 502 ``"'table'"`` instead of a 422 validation
    error."""
    tables: list[ProfileTableRef] = Field(default_factory=list)


class ConnectionTestResponse(BaseModel):
    ok: bool
    detail: Optional[str] = None


class ConnectionTestRequest(BaseModel):
    connection_type: str = Field(description="bigquery | postgresql | hadoop_spark | redshift | snowflake | sqlserver")
    credentials: dict[str, Any] = Field(description="Raw credentials to validate")

    @field_validator("connection_type")
    @classmethod
    def _check_connection_type(cls, v: str) -> str:
        return _validate_connection_type(v)
    config: dict[str, Any] = Field(default_factory=dict, description="Non-sensitive config")


