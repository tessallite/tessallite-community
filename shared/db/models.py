"""
SQLAlchemy ORM models for Tessallite.

Two-level database architecture:
  - SystemTenant lives in the global system DB (schema: tess_system)
  - All other models live in per-tenant DBs (schema: {tenant_slug}_meta)
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, Column, Date, ForeignKey, Index, Integer, Float, LargeBinary,
    Numeric, String, Table, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# TIMESTAMPTZ was removed from SQLAlchemy 2.x; use TIMESTAMP(timezone=True) instance
TIMESTAMPTZ = TIMESTAMP(timezone=True)


# ---------------------------------------------------------------------------
# Bases
# ---------------------------------------------------------------------------

class SystemBase(DeclarativeBase):
    """Base for system-DB tables (tess_system schema)."""
    pass


class TenantBase(DeclarativeBase):
    """Base for per-tenant tables ({slug}_meta schema)."""
    pass


# ---------------------------------------------------------------------------
# SYSTEM DB — tenant registry
# ---------------------------------------------------------------------------

class SystemTenant(SystemBase):
    __tablename__ = "tenants"
    __table_args__ = {"schema": "tess_system"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    encrypted_db_url: Mapped[bytes] = mapped_column(nullable=False)
    db_schema_prefix: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class SystemSetting(SystemBase):
    __tablename__ = "system_settings"
    __table_args__ = {"schema": "tess_system"}

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())
    updated_by: Mapped[Optional[str]] = mapped_column(String(255))


class SystemRestartPending(SystemBase):
    __tablename__ = "system_restart_pending"
    __table_args__ = {"schema": "tess_system"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    setting_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    written_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    written_by: Mapped[Optional[str]] = mapped_column(String(255))


class RevokedEmbedToken(SystemBase):
    __tablename__ = "revoked_embed_tokens"
    __table_args__ = {"schema": "tess_system"}

    jti: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    revoked_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    revoked_by: Mapped[Optional[str]] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)


class SsoState(SystemBase):
    """F-021-05: durable SSO authorization-flow state.

    The login leg (redirect to IdP) and the callback leg (ACS / OIDC callback)
    can land on different replicas, so per-process state breaks multi-replica
    deployments. This table holds the random ``state`` value, the originating
    tenant, the flow type, and a short ``expires_at`` so abandoned flows do not
    accumulate. ``browser_nonce`` binds the flow to the initiating browser via a
    short-lived cookie (login-CSRF defence, F-021-09)."""

    __tablename__ = "sso_states"
    __table_args__ = {"schema": "tess_system"}

    state: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    flow_type: Mapped[str] = mapped_column(String(16), nullable=False)
    browser_nonce: Mapped[Optional[str]] = mapped_column(String(128))
    # F-021-09: OIDC id_token replay defence — the nonce sent in the auth
    # request and verified against the id_token's ``nonce`` claim on callback.
    oidc_nonce: Mapped[Optional[str]] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, index=True)


# ---------------------------------------------------------------------------
# TENANT DB — all tables below live in {slug}_meta schema
# The schema name is injected dynamically at connection time.
# SQLAlchemy ORM classes are defined without a fixed schema; the search_path
# on the asyncpg connection is set to "{slug}_meta" before any query.
# ---------------------------------------------------------------------------

class Project(TenantBase):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    pocket_size_budget_bytes: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )

    connections: Mapped[list[ProjectConnection]] = relationship(back_populates="project", cascade="all, delete-orphan")
    models: Mapped[list[Model]] = relationship(back_populates="project", cascade="all, delete-orphan")


class ProjectConnection(TenantBase):
    __tablename__ = "project_connections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    connection_type: Mapped[str] = mapped_column(String(32), nullable=False)  # bigquery | postgresql | hadoop_spark (legacy 'jdbc' rows normalised on read)
    encrypted_credentials: Mapped[bytes] = mapped_column(nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    project: Mapped[Project] = relationship(back_populates="connections")
    data_sources: Mapped[list[DataSource]] = relationship(back_populates="project_connection")
    data_targets: Mapped[list[DataTarget]] = relationship(back_populates="project_connection")


class Model(TenantBase):
    __tablename__ = "models"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    target_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("data_targets.id", ondelete="SET NULL"), index=True)
    refresh_strategy: Mapped[str] = mapped_column(String(32), nullable=False, default="scheduled")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    aggregations_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    include_all_measures: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    seed: Mapped[str] = mapped_column(String(64), nullable=False)
    max_aggregates: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    miss_threshold_daily: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    miss_threshold_weekly: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    schema_drift_interval_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    llm_config_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("llm_provider_configs.id", ondelete="SET NULL"),
        index=True,
    )
    max_ai_recommendations: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    canvas_layout: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # Phase 9 / F9 — per-model predictive-aggregate eviction policy.
    # Values: predicted_first | lru | validated_survives | never_evict
    predictive_eviction_policy: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="predicted_first",
        server_default=text("'predicted_first'"),
    )
    # Phase 9 / F6 — dual-axis predictive storage budget. Either or both
    # may be NULL (no cap on that axis). The build orchestrator halts
    # when *either* would be exceeded.
    predictive_storage_budget_bytes: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    predictive_storage_budget_count: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    pocket_size_budget_bytes: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    # Phase 9 / F7 — Q9=A: default off (auto-approve). When toggled on,
    # the deploy hook drops candidates into a pending queue instead of
    # building them.
    predictive_requires_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    glossary_max_distinct: Mapped[int] = mapped_column(
        Integer, nullable=False, default=50, server_default=text("50")
    )
    # Phase 9 / F5 — last model_version_id we ran the predictive build
    # for. The optimizer's predictive-build sweep skips models where
    # ``deployed_version_id == predictive_built_for_version_id``.
    predictive_built_for_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    # FK enforced at the DB level by migration 0021; we don't repeat it
    # in the ORM because doing so makes SQLAlchemy try to wire an
    # implicit relationship to ModelVersion at mapper-configuration time,
    # which conflicts with Model.sources / Model.targets foreign-keys
    # disambiguation. Reads / writes treat this as a plain UUID column.
    deployed_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True)
    )
    last_deployed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    # KPI v2 model-level settings
    expose_kpis_inline: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fiscal_year_start_month: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    __table_args__ = (UniqueConstraint("project_id", "slug"),)

    project: Mapped[Project] = relationship(back_populates="models")
    sources: Mapped[list[DataSource]] = relationship(back_populates="model", cascade="all, delete-orphan", foreign_keys="DataSource.model_id")
    targets: Mapped[list[DataTarget]] = relationship(back_populates="model", cascade="all, delete-orphan", foreign_keys="DataTarget.model_id")
    tables: Mapped[list[ModelTable]] = relationship(back_populates="model", cascade="all, delete-orphan")
    user_defined_attributes: Mapped[list[UserDefinedAttribute]] = relationship(
        back_populates="model", cascade="all, delete-orphan"
    )
    hierarchies: Mapped[list[HierarchyDefinition]] = relationship(
        back_populates="model", cascade="all, delete-orphan"
    )
    dimensions: Mapped[list[Dimension]] = relationship(back_populates="model", cascade="all, delete-orphan")
    measures: Mapped[list[Measure]] = relationship(back_populates="model", cascade="all, delete-orphan")
    joins: Mapped[list[Join]] = relationship(back_populates="model", cascade="all, delete-orphan")
    aggregates: Mapped[list[AggregateDefinition]] = relationship(back_populates="model", cascade="all, delete-orphan")
    pockets: Mapped[list[PocketDefinition]] = relationship(back_populates="model", cascade="all, delete-orphan")
    schema_changes: Mapped[list[SchemaChangeEvent]] = relationship(back_populates="model", cascade="all, delete-orphan")
    alerts: Mapped[list["ModelAlert"]] = relationship(back_populates="model", cascade="all, delete-orphan")
    lineage_mappings: Mapped[list[LineageMapping]] = relationship(back_populates="model", cascade="all, delete-orphan")
    named_sets: Mapped[list["NamedSet"]] = relationship(back_populates="model", cascade="all, delete-orphan")
    kpis: Mapped[list["KPI"]] = relationship(back_populates="model", cascade="all, delete-orphan")


class ModelVersion(TenantBase):
    __tablename__ = "model_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)

    __table_args__ = (UniqueConstraint("model_id", "version_number"),)


class ModelParameter(TenantBase):
    __tablename__ = "model_parameters"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String(255))
    param_type: Mapped[str] = mapped_column(String(32), nullable=False)
    default_value: Mapped[Optional[dict]] = mapped_column(JSONB)
    allowed_values: Mapped[Optional[dict]] = mapped_column(JSONB)
    description: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    __table_args__ = (UniqueConstraint("model_id", "name", name="uq_model_parameters_model_name"),)


class DataSource(TenantBase):
    __tablename__ = "data_sources"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True)
    project_connection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("project_connections.id"), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)  # bigquery | jdbc | tessallite_passthrough
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    default_schema: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    model: Mapped[Model] = relationship(back_populates="sources", foreign_keys=[model_id])
    project_connection: Mapped[ProjectConnection] = relationship(back_populates="data_sources")
    tables: Mapped[list[ModelTable]] = relationship(back_populates="source", cascade="all, delete-orphan")
    calendars: Mapped[list["CalendarTable"]] = relationship(
        back_populates="data_source",
        cascade="all, delete-orphan",
        foreign_keys="CalendarTable.data_source_id",
    )


class DataTarget(TenantBase):
    __tablename__ = "data_targets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True)
    project_connection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("project_connections.id"), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)  # bigquery | postgresql
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    model: Mapped[Model] = relationship(back_populates="targets", foreign_keys=[model_id])
    project_connection: Mapped[ProjectConnection] = relationship(back_populates="data_targets")
    aggregate_definitions: Mapped[list[AggregateDefinition]] = relationship(back_populates="target")
    pocket_definitions: Mapped[list[PocketDefinition]] = relationship(back_populates="target")


class ModelTable(TenantBase):
    __tablename__ = "model_tables"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False, index=True)
    table_type: Mapped[str] = mapped_column(String(16), nullable=False)  # fact | dim_aggregate | dim_detail
    physical_name: Mapped[str] = mapped_column(String(512), nullable=False)
    alias: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    row_count_estimate: Mapped[Optional[int]] = mapped_column(BigInteger)
    last_stats_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    # Set when this ModelTable is a calendar alias. The linked CalendarTable
    # carries the date / year / quarter / month / week / day column meanings
    # used by the time-variant resolver.
    calendar_table_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("calendar_tables.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    # F-013-11: at most one fact table per model, enforced at the storage layer
    # so the check-then-act API guard cannot be raced into a two-fact model
    # (which the aggregate matcher/rewriter silently mis-handle). Migration
    # 0136 creates the matching DDL.
    #
    # Bug-3577: unique alias per model enforced at the DB layer so the
    # check-then-act API guard (_assert_alias_unique_in_model) cannot be
    # raced. The app-level IntegrityError retry (Bug-5246 in calendar.py)
    # handles collisions gracefully. Migration 0150 creates the matching DDL.
    __table_args__ = (
        Index(
            "uq_model_tables_one_fact_per_model",
            "model_id",
            unique=True,
            postgresql_where=text("table_type = 'fact'"),
        ),
        UniqueConstraint("model_id", "alias", name="uq_model_tables_model_id_alias"),
    )

    model: Mapped[Model] = relationship(back_populates="tables")
    source: Mapped[DataSource] = relationship(back_populates="tables")
    columns: Mapped[list[ModelColumn]] = relationship(back_populates="table", cascade="all, delete-orphan")
    user_defined_attributes: Mapped[list[UserDefinedAttribute]] = relationship(
        back_populates="table", cascade="all, delete-orphan"
    )
    calendar_table: Mapped[Optional["CalendarTable"]] = relationship(
        foreign_keys=[calendar_table_id]
    )


class ModelColumn(TenantBase):
    __tablename__ = "model_columns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_table_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("model_tables.id", ondelete="CASCADE"), nullable=False)
    column_name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text)
    is_hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    hidden_reason: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    is_primary_key: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    data_type: Mapped[str] = mapped_column(String(64), nullable=False)
    is_nullable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    cardinality_estimate: Mapped[Optional[int]] = mapped_column(BigInteger)
    last_stats_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    drift_removed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

    __table_args__ = (UniqueConstraint("model_table_id", "column_name"),)

    table: Mapped[ModelTable] = relationship(back_populates="columns")
    user_defined_attribute_refs: Mapped[list[UserDefinedAttributeColumnRef]] = relationship(
        back_populates="column", cascade="all, delete-orphan"
    )


class UserDefinedAttribute(TenantBase):
    __tablename__ = "user_defined_attributes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False)
    table_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("model_tables.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    expression: Mapped[str] = mapped_column(Text, nullable=False)
    output_data_type: Mapped[str] = mapped_column(String(20), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    validated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    validation_error: Mapped[Optional[str]] = mapped_column(Text)
    # True for UDAs the hierarchy/date-template generator produced. Their
    # expression vocabulary (EXTRACT/CASE) is system-authored and intentionally
    # wider than the user editor's function catalogue, so the validator must not
    # reject the generator's own output on a name/description-only edit (F-016-06).
    is_generated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    __table_args__ = (UniqueConstraint("model_id", "table_id", "name"),)

    model: Mapped[Model] = relationship(back_populates="user_defined_attributes")
    table: Mapped[ModelTable] = relationship(back_populates="user_defined_attributes")
    column_refs: Mapped[list[UserDefinedAttributeColumnRef]] = relationship(
        back_populates="attribute", cascade="all, delete-orphan"
    )


class UserDefinedAttributeColumnRef(TenantBase):
    __tablename__ = "user_defined_attribute_column_refs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    attribute_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_defined_attributes.id", ondelete="CASCADE"), nullable=False
    )
    column_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_columns.id"), nullable=False,
        index=True,
    )

    __table_args__ = (UniqueConstraint("attribute_id", "column_id"),)

    attribute: Mapped[UserDefinedAttribute] = relationship(back_populates="column_refs")
    column: Mapped[ModelColumn] = relationship(back_populates="user_defined_attribute_refs")


class HierarchyDefinition(TenantBase):
    __tablename__ = "hierarchy_definitions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # explicit | date_embedded | segment
    dimension_kind: Mapped[Optional[str]] = mapped_column(Text)  # time | geo | entity | None
    description: Mapped[Optional[str]] = mapped_column(Text)
    segment_config: Mapped[Optional[dict]] = mapped_column(JSONB)
    date_config: Mapped[Optional[dict]] = mapped_column(JSONB)
    calendar_type: Mapped[Optional[str]] = mapped_column(String(20))  # standard | fiscal | hijri | iso
    fiscal_year_start_month: Mapped[Optional[int]] = mapped_column(Integer)  # 1-12, only when calendar_type = "fiscal"
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    __table_args__ = (UniqueConstraint("model_id", "name"),)

    model: Mapped[Model] = relationship(back_populates="hierarchies")
    levels: Mapped[list[HierarchyLevel]] = relationship(
        back_populates="hierarchy", cascade="all, delete-orphan"
    )


class HierarchyLevel(TenantBase):
    __tablename__ = "hierarchy_levels"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hierarchy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hierarchy_definitions.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    key_attribute_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    key_attribute_source: Mapped[str] = mapped_column(String(30), nullable=False)  # physical_column | user_defined_attribute
    description: Mapped[Optional[str]] = mapped_column(Text)
    time_unit: Mapped[Optional[str]] = mapped_column(Text)  # year|half|quarter|month|week|day|hour|none
    allowed_time_calcs: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("hierarchy_id", "name"),
        UniqueConstraint("hierarchy_id", "ordinal"),
        UniqueConstraint("hierarchy_id", "key_attribute_id"),
    )

    hierarchy: Mapped[HierarchyDefinition] = relationship(back_populates="levels")
    attributes: Mapped[list[HierarchyLevelAttribute]] = relationship(
        back_populates="level", cascade="all, delete-orphan"
    )


class HierarchyLevelAttribute(TenantBase):
    __tablename__ = "hierarchy_level_attributes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    level_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hierarchy_levels.id", ondelete="CASCADE"), nullable=False
    )
    attribute_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    attribute_source: Mapped[str] = mapped_column(String(30), nullable=False)  # physical_column | user_defined_attribute
    role: Mapped[str] = mapped_column(String(10), nullable=False)  # display | filter

    __table_args__ = (UniqueConstraint("level_id", "attribute_id"),)

    level: Mapped[HierarchyLevel] = relationship(back_populates="attributes")


class HierarchyHealthIssue(TenantBase):
    __tablename__ = "hierarchy_health_issues"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hierarchy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hierarchy_definitions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    issue_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    detected_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    resolved_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, nullable=True)


class Dimension(TenantBase):
    __tablename__ = "dimensions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text)
    display_folder: Mapped[Optional[str]] = mapped_column(String(255))
    source_column_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("model_columns.id", ondelete="SET NULL"), index=True)
    user_defined_attribute_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_defined_attributes.id", ondelete="SET NULL"),
        index=True,
    )
    hierarchy: Mapped[Optional[dict]] = mapped_column(JSONB)
    is_time_dim: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    time_grain: Mapped[Optional[str]] = mapped_column(String(32))
    calc_expression: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    calc_expression_tables: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    is_invalid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    invalid_reason: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    __table_args__ = (UniqueConstraint("model_id", "name"),)

    model: Mapped[Model] = relationship(back_populates="dimensions")


class Measure(TenantBase):
    __tablename__ = "measures"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text)
    display_folder: Mapped[Optional[str]] = mapped_column(String(255))
    source_column_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("model_columns.id", ondelete="SET NULL"), index=True)
    user_defined_attribute_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_defined_attributes.id", ondelete="SET NULL"),
        index=True,
    )
    measure_type: Mapped[str] = mapped_column(String(32), nullable=False, default="standard")
    expression: Mapped[Optional[str]] = mapped_column(Text)
    calc_agg_mode: Mapped[Optional[str]] = mapped_column(String(32))
    data_type: Mapped[str] = mapped_column(String(32), nullable=False, default="numeric")
    default_agg: Mapped[str] = mapped_column(String(32), nullable=False, default="sum")
    format: Mapped[Optional[str]] = mapped_column(Text)
    variant_kind: Mapped[Optional[str]] = mapped_column(String(32))
    variant_of_measure_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("measures.id", ondelete="CASCADE"),
        index=True,
    )
    variant_n: Mapped[Optional[int]] = mapped_column(Integer)
    is_additive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    semi_additive_behavior: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    semi_additive_account_column_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_columns.id", ondelete="SET NULL"), nullable=True,
        index=True,
    )
    is_invalid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    invalid_reason: Mapped[Optional[str]] = mapped_column(Text)
    # When set, the time-variant resolver uses this ModelTable's linked
    # CalendarTable for period boundaries. Required when the measure has any
    # time-variant enabled; validated at the API layer, not in DB.
    calendar_model_table_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("model_tables.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    hierarchy_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hierarchy_definitions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    resolved_calendar_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("calendar_tables.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    resolved_date_col_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("model_columns.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    date_dimension_column_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("model_columns.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    cross_model_source_model_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    cross_model_source_measure_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    __table_args__ = (UniqueConstraint("model_id", "name"),)

    model: Mapped[Model] = relationship(back_populates="measures")
    variant_of: Mapped[Optional["Measure"]] = relationship(
        "Measure",
        remote_side=[id],
        foreign_keys=[variant_of_measure_id],
        backref="variants",
    )
    drill_through_set: Mapped[Optional["DrillThroughSet"]] = relationship(
        back_populates="measure",
        cascade="all, delete-orphan",
        uselist=False,
    )


class DrillThroughSet(TenantBase):
    """Drill-through configuration for a standard or variant measure.

    One row per measure; calculated measures have no row (no single
    source fact). Nullable fields carry implicit defaults resolved by
    the drill builder at query time.

    Phase 4C.1 of the drill-through + calculated-members plan.
    """

    __tablename__ = "drill_through_sets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    measure_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("measures.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    source_table_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("model_tables.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    detail_columns: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    joined_dimension_ids: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    row_limit_override: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_join_path: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now()
    )

    measure: Mapped[Measure] = relationship(back_populates="drill_through_set")


class NamedSet(TenantBase):
    __tablename__ = "named_sets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text)
    display_folder: Mapped[Optional[str]] = mapped_column(String(255))
    scope: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    expression: Mapped[str] = mapped_column(Text, nullable=False)
    dimensions: Mapped[Optional[str]] = mapped_column(Text)
    builder_definition: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    list_type: Mapped[Optional[str]] = mapped_column(String(32), default="advanced_mdx")
    certification_status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    replacement_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("named_sets.id", ondelete="SET NULL"), nullable=True)
    owner_user_id: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    __table_args__ = (UniqueConstraint("model_id", "name"),)

    model: Mapped["Model"] = relationship(back_populates="named_sets")


class KPI(TenantBase):
    __tablename__ = "kpis"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text)
    display_folder: Mapped[Optional[str]] = mapped_column(String(255))

    # Legacy v1 — retained for migration compatibility, not used by application code
    value_measure_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("measures.id", ondelete="SET NULL"), index=True)
    goal_measure_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("measures.id", ondelete="SET NULL"), index=True)
    status_expression: Mapped[Optional[str]] = mapped_column(Text)
    trend_expression: Mapped[Optional[str]] = mapped_column(Text)
    status_graphic: Mapped[str] = mapped_column(String(64), nullable=False, default="Traffic Light")
    trend_graphic: Mapped[str] = mapped_column(String(64), nullable=False, default="Standard Arrow")

    # v2 Expression DSL
    kpi_type: Mapped[Optional[str]] = mapped_column(String(32))  # simple_measure|ratio|variance|growth_rate|moving_window|composite
    expression: Mapped[Optional[str]] = mapped_column(Text)
    calc_agg_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="automatic")
    inner_agg: Mapped[Optional[str]] = mapped_column(String(32))
    inner_grain: Mapped[Optional[str]] = mapped_column(String(128))
    outer_agg: Mapped[Optional[str]] = mapped_column(String(32))

    # Semi-additive
    at_grain: Mapped[Optional[str]] = mapped_column(String(32))
    non_additive_agg: Mapped[Optional[str]] = mapped_column(String(16))
    carry_forward: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Target
    target_type: Mapped[Optional[str]] = mapped_column(String(32))  # static|measure|prior_period|expression|null
    target_value: Mapped[Optional[float]] = mapped_column(Numeric)
    target_measure_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    target_expression: Mapped[Optional[str]] = mapped_column(Text)
    target_period: Mapped[Optional[str]] = mapped_column(String(32))

    # Direction and thresholds
    direction: Mapped[str] = mapped_column(String(32), nullable=False, default="higher_is_better")
    presentation_type: Mapped[Optional[str]] = mapped_column(String(32))
    presentation_meta: Mapped[Optional[dict]] = mapped_column(JSONB)

    # Trend
    trend_period: Mapped[str] = mapped_column(String(16), nullable=False, default="month")
    trend_threshold: Mapped[float] = mapped_column(Numeric, nullable=False, default=0.01)
    trend_sparkline_periods: Mapped[int] = mapped_column(Integer, nullable=False, default=12)

    # Formatting
    format_token: Mapped[Optional[str]] = mapped_column(String(32))
    format_custom: Mapped[Optional[str]] = mapped_column(String(128))
    unit_label: Mapped[Optional[str]] = mapped_column(String(32))
    null_display_value: Mapped[str] = mapped_column(String(32), nullable=False, default="N/A")

    # Hierarchy and composition
    weight: Mapped[Optional[float]] = mapped_column(Float)
    parent_kpi_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("kpis.id", ondelete="SET NULL"), nullable=True)
    indicator_type: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    evaluation_order: Mapped[Optional[int]] = mapped_column(Integer)

    # Time dimension binding
    time_dimension_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))

    # Business builder definition (v3)
    business_definition: Mapped[Optional[dict]] = mapped_column(JSONB)

    # Governance
    certification_status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    replacement_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("kpis.id", ondelete="SET NULL"), nullable=True)
    owner_user_id: Mapped[Optional[str]] = mapped_column(String(255))

    # Deployment
    is_deployed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deployed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)

    # Snapshots
    snapshot_frequency: Mapped[Optional[str]] = mapped_column(String(128))
    snapshot_retention: Mapped[int] = mapped_column(Integer, nullable=False, default=90)

    # Lifecycle
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())
    created_by: Mapped[Optional[str]] = mapped_column(String(255))

    __table_args__ = (UniqueConstraint("model_id", "name"),)

    model: Mapped["Model"] = relationship(back_populates="kpis")
    snapshots: Mapped[list["KPISnapshot"]] = relationship(back_populates="kpi", cascade="all, delete-orphan")


class KPISnapshot(TenantBase):
    __tablename__ = "kpi_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kpi_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("kpis.id", ondelete="CASCADE"), nullable=False, index=True)
    snapshot_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())
    value: Mapped[Optional[float]] = mapped_column(Numeric)
    target: Mapped[Optional[float]] = mapped_column(Numeric)
    status: Mapped[Optional[int]] = mapped_column(Integer)
    # Widened to 255 (migration 0127): fail-loud KPI status labels embed the
    # composite-depth/TI-no-dimension sentences and arbitrary decomposition
    # detail, which overflow the original String(64) and would crash the upsert.
    status_label: Mapped[Optional[str]] = mapped_column(String(255))
    trend_pct: Mapped[Optional[float]] = mapped_column(Numeric)
    filters_applied: Mapped[Optional[dict]] = mapped_column(JSONB)
    evaluation_ms: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

    kpi: Mapped["KPI"] = relationship(back_populates="snapshots")


class KPILatest(TenantBase):
    """Materialised latest KPI evaluation results.

    Upserted by the scheduler snapshot sweep and evaluate-batch endpoint.
    Queried by the query-router when BI clients SELECT from the $KPIs virtual table.
    """
    __tablename__ = "kpi_latest"
    __table_args__ = (
        UniqueConstraint("model_id", "kpi_id", name="uq_kpi_latest_model_kpi"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True)
    kpi_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("kpis.id", ondelete="CASCADE"), nullable=False)
    kpi_name: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[Optional[float]] = mapped_column(Numeric)
    target: Mapped[Optional[float]] = mapped_column(Numeric)
    status: Mapped[Optional[int]] = mapped_column(Integer)
    # Widened to 255 (migration 0127): see KPISnapshot.status_label note —
    # fail-loud labels overflow the original String(64) and crash the batch upsert.
    status_label: Mapped[Optional[str]] = mapped_column(String(255))
    trend_pct: Mapped[Optional[float]] = mapped_column(Numeric)
    formatted_value: Mapped[Optional[str]] = mapped_column(String(128))
    evaluated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())


class NamedSetVersion(TenantBase):
    __tablename__ = "named_set_versions"
    # F-018-18: a unique (named_set_id, version_number) prevents two concurrent
    # updates from minting the same version number (which would break
    # revert-by-number). `_create_version` retries on collision.
    __table_args__ = (
        UniqueConstraint(
            "named_set_id", "version_number", name="uq_named_set_version_number"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    named_set_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("named_sets.id", ondelete="CASCADE"), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    changed_by: Mapped[Optional[str]] = mapped_column(String(255))
    changed_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    change_summary: Mapped[Optional[str]] = mapped_column(Text)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)


class KPIVersion(TenantBase):
    __tablename__ = "kpi_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kpi_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("kpis.id", ondelete="CASCADE"), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    changed_by: Mapped[Optional[str]] = mapped_column(String(255))
    changed_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    change_summary: Mapped[Optional[str]] = mapped_column(Text)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)


class NamedSetUsage(TenantBase):
    __tablename__ = "named_set_usage"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    named_set_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("named_sets.id", ondelete="CASCADE"), nullable=False, index=True)
    workbook_id: Mapped[Optional[str]] = mapped_column(String(255))
    worksheet: Mapped[Optional[str]] = mapped_column(String(255))
    cell_reference: Mapped[Optional[str]] = mapped_column(String(64))
    usage_type: Mapped[str] = mapped_column(String(32), nullable=False)
    reported_by: Mapped[Optional[str]] = mapped_column(String(255))
    reported_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())


class KPIUsage(TenantBase):
    __tablename__ = "kpi_usage"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kpi_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("kpis.id", ondelete="CASCADE"), nullable=False, index=True)
    workbook_id: Mapped[Optional[str]] = mapped_column(String(255))
    worksheet: Mapped[Optional[str]] = mapped_column(String(255))
    cell_reference: Mapped[Optional[str]] = mapped_column(String(64))
    usage_type: Mapped[str] = mapped_column(String(32), nullable=False)
    reported_by: Mapped[Optional[str]] = mapped_column(String(255))
    reported_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())


class UserEntityPreference(TenantBase):
    __tablename__ = "user_entity_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", "model_id", "entity_type", "entity_id", "preference_type", name="uq_user_entity_pref"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # FK with ON DELETE CASCADE so deleting a model purges its preference rows
    # rather than stranding them forever (F-029-15); migration 0139.
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("models.id", ondelete="CASCADE"),
        nullable=False,
    )
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    preference_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())


class CalendarTable(TenantBase):
    __tablename__ = "calendar_tables"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False
    )
    table_name: Mapped[str] = mapped_column(String(512), nullable=False)
    dialect: Mapped[str] = mapped_column(String(32), nullable=False)
    calendar_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="standard", server_default=text("'standard'")
    )
    date_column: Mapped[Optional[str]] = mapped_column(String(255))
    year_column: Mapped[Optional[str]] = mapped_column(String(255))
    half_column: Mapped[Optional[str]] = mapped_column(String(255))
    quarter_column: Mapped[Optional[str]] = mapped_column(String(255))
    month_column: Mapped[Optional[str]] = mapped_column(String(255))
    week_column: Mapped[Optional[str]] = mapped_column(String(255))
    day_column: Mapped[Optional[str]] = mapped_column(String(255))
    autocreated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fiscal_year_start_month: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    __table_args__ = (UniqueConstraint("data_source_id", "table_name", name="uq_calendar_data_source_table"),)

    data_source: Mapped[DataSource] = relationship(
        back_populates="calendars", foreign_keys=[data_source_id]
    )


class Join(TenantBase):
    __tablename__ = "joins"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True)
    left_table_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("model_tables.id", ondelete="CASCADE"), nullable=False, index=True)
    right_table_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("model_tables.id", ondelete="CASCADE"), nullable=False, index=True)
    join_type: Mapped[str] = mapped_column(String(32), nullable=False, default="many_to_one")
    left_column_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("model_columns.id", ondelete="CASCADE"), nullable=False, index=True)
    right_column_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("model_columns.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

    model: Mapped[Model] = relationship(back_populates="joins")


class AggregateDefinition(TenantBase):
    __tablename__ = "aggregate_definitions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("data_targets.id"), nullable=False, index=True)
    physical_table_name: Mapped[str] = mapped_column(String(512), nullable=False)
    target_schema: Mapped[Optional[str]] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    grain: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    grain_physical_cols: Mapped[Optional[list]] = mapped_column(JSONB)
    invalid_reason: Mapped[Optional[str]] = mapped_column(Text)
    source_row_count: Mapped[Optional[int]] = mapped_column(BigInteger)
    agg_row_count: Mapped[Optional[int]] = mapped_column(BigInteger)
    storage_bytes: Mapped[Optional[int]] = mapped_column(BigInteger)
    estimated_hit_rate: Mapped[Optional[float]] = mapped_column(Float)
    hit_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    # Phase 9 / F9 — taxonomy: predictive | demand | workload | manual.
    creation_reason: Mapped[str] = mapped_column(
        String(32), nullable=False, default="manual"
    )
    is_stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    include_quantiles: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Opt-in dispersion stats (STDDEV_POP/SAMP, VAR_POP/SAMP). Like quantiles
    # these are not re-aggregatable, so they route exact-grain only.
    include_stats: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Phase 8.C.2 — per-persona workload partition. NULL = unscoped
    # (global, usable under any persona). When populated, the router
    # only considers this aggregate for queries bound to the same
    # persona, and prefers it over a global candidate.
    persona_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("personas.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())
    last_refreshed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    retired_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    # F-009-08 — set when the retired-table purge sweep has dropped this
    # aggregate's physical target table to reclaim storage. Prevents the
    # daily sweep from re-attempting the DROP on every run.
    physical_table_purged_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    # Phase 9 / F10 — set by the predictive feedback sweep once a
    # predictive aggregate has earned enough real-query hits within the
    # evaluation window. The eviction policy ``validated_survives``
    # reads this column; the creation_reason label stays ``predictive``.
    predictive_validated_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)

    model: Mapped[Model] = relationship(back_populates="aggregates")
    target: Mapped[DataTarget] = relationship(back_populates="aggregate_definitions")
    columns: Mapped[list[AggregateColumn]] = relationship(back_populates="aggregate", cascade="all, delete-orphan")
    refresh_policy: Mapped[Optional[AggregateRefreshPolicy]] = relationship(back_populates="aggregate", uselist=False, cascade="all, delete-orphan")
    refresh_runs: Mapped[list[AggregateRefreshRun]] = relationship(back_populates="aggregate", cascade="all, delete-orphan")


class AggregateColumn(TenantBase):
    __tablename__ = "aggregate_columns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    aggregate_definition_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("aggregate_definitions.id", ondelete="CASCADE"), nullable=False, index=True)
    measure_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("measures.id", ondelete="SET NULL"), index=True)
    physical_col_name: Mapped[str] = mapped_column(String(255), nullable=False)
    stat_type: Mapped[str] = mapped_column(String(32), nullable=False)
    aggregation_function: Mapped[Optional[str]] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

    aggregate: Mapped[AggregateDefinition] = relationship(back_populates="columns")
    measure: Mapped[Optional["Measure"]] = relationship(foreign_keys=[measure_id])


class AggregateRefreshPolicy(TenantBase):
    __tablename__ = "aggregate_refresh_policies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    aggregate_definition_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("aggregate_definitions.id", ondelete="CASCADE"), nullable=False, unique=True)
    refresh_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="scheduled")
    cron_expression: Mapped[Optional[str]] = mapped_column(String(128))
    incremental_column: Mapped[Optional[str]] = mapped_column(String(255))
    incremental_lookback: Mapped[Optional[int]] = mapped_column(Integer)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    aggregate: Mapped[AggregateDefinition] = relationship(back_populates="refresh_policy")


class RefreshDependency(TenantBase):
    """Scheduler dependency chain: downstream refreshes after upstream completes."""
    __tablename__ = "refresh_dependencies"
    __table_args__ = (
        UniqueConstraint("upstream_aggregate_id", "downstream_aggregate_id", name="uq_refresh_dep"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    upstream_aggregate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("aggregate_definitions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    downstream_aggregate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("aggregate_definitions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())


class AggregateRefreshRun(TenantBase):
    __tablename__ = "aggregate_refresh_runs"

    # Canonical status vocabulary — the single source of truth for every
    # producer (full/incremental refresh, optimizer creator) and consumer
    # (SLA monitor, run-history API, metrics). Producers must write only
    # these values; consumers must filter with these sets (F-012-01: the
    # SLA monitor drifted to an imagined "done"/"success" vocabulary).
    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED})
    SUCCESS_STATUSES = frozenset({STATUS_COMPLETED})

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    aggregate_definition_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("aggregate_definitions.id", ondelete="CASCADE"), nullable=False, index=True)
    refresh_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=STATUS_RUNNING)
    started_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    rows_written: Mapped[Optional[int]] = mapped_column(BigInteger)
    bytes_processed: Mapped[Optional[int]] = mapped_column(BigInteger)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    triggered_by: Mapped[str] = mapped_column(String(32), nullable=False, default="scheduler")

    aggregate: Mapped[AggregateDefinition] = relationship(back_populates="refresh_runs")


class AggregateLifecycleEvent(TenantBase):
    """Phase 9 / F13 — per-model audit trail for every aggregate event.

    One row per create / approve / validate / refresh / refresh-fail /
    retire event. ``payload`` stores event-specific detail as JSONB so
    new event types don't require a schema change.
    """

    __tablename__ = "aggregate_lifecycle_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("models.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    aggregate_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("aggregate_definitions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), index=True
    )


class PocketDefinition(TenantBase):
    __tablename__ = "pocket_definitions"
    __table_args__ = (
        Index(
            "uq_pocket_global",
            "model_id", "query_fingerprint", "predicate_set_hash",
            unique=True,
            postgresql_where=text("persona_id IS NULL AND retired_at IS NULL"),
        ),
        Index(
            "uq_pocket_persona",
            "model_id", "query_fingerprint", "predicate_set_hash", "persona_id",
            unique=True,
            postgresql_where=text("persona_id IS NOT NULL AND retired_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("data_targets.id"), nullable=False, index=True)
    physical_table_name: Mapped[str] = mapped_column(String(512), nullable=False)
    target_schema: Mapped[Optional[str]] = mapped_column(String(255))
    defining_sql: Mapped[str] = mapped_column(Text, nullable=False)
    query_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    predicate_set_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    row_count: Mapped[Optional[int]] = mapped_column(BigInteger)
    storage_bytes: Mapped[Optional[int]] = mapped_column(BigInteger)
    refresh_policy: Mapped[str] = mapped_column(String(32), nullable=False, default="schedule")
    refresh_cron: Mapped[Optional[str]] = mapped_column(String(128))
    incremental_column: Mapped[Optional[str]] = mapped_column(String(255))
    incremental_lookback_hours: Mapped[Optional[int]] = mapped_column(Integer)
    ttl_days: Mapped[int] = mapped_column(Integer, nullable=False, default=14)
    # F-005-19 (Bug-2260): a freshly constructed pocket has NO materialised
    # table yet, so the ORM default must be "stale" (unmaterialised) not "fresh".
    # A "fresh" default was a footgun: any direct constructor that forgot to set
    # status would have entered the matcher pool claiming a cache that does not
    # exist. Both current writers set status="stale" explicitly; this aligns the
    # default with that contract.
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="stale")
    failure_reason: Mapped[Optional[str]] = mapped_column(Text)
    last_refresh_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    last_access_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    last_match_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    hit_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    time_saved_ms_total: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # Phase 8.C.2 — per-persona workload partition. NULL = unscoped.
    persona_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("personas.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())
    retired_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)

    model: Mapped[Model] = relationship(back_populates="pockets")
    target: Mapped[DataTarget] = relationship(back_populates="pocket_definitions")
    predicates: Mapped[list[PocketPredicate]] = relationship(back_populates="pocket", cascade="all, delete-orphan")
    refresh_runs: Mapped[list[PocketRefreshRun]] = relationship(back_populates="pocket", cascade="all, delete-orphan")
    refresh_policy_row: Mapped[Optional["PocketRefreshPolicy"]] = relationship(
        back_populates="pocket", cascade="all, delete-orphan", uselist=False
    )


class PocketPredicate(TenantBase):
    __tablename__ = "pocket_predicates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pocket_definition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pocket_definitions.id", ondelete="CASCADE"), nullable=False
    )
    column_name: Mapped[str] = mapped_column(String(255), nullable=False)
    operator: Mapped[str] = mapped_column(String(16), nullable=False)
    value_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("pocket_definition_id", "column_name", "operator", "value_json"),
    )

    pocket: Mapped[PocketDefinition] = relationship(back_populates="predicates")


class PocketRefreshRun(TenantBase):
    __tablename__ = "pocket_refresh_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pocket_definition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pocket_definitions.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    refresh_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    rows_written: Mapped[Optional[int]] = mapped_column(BigInteger)
    bytes_processed: Mapped[Optional[int]] = mapped_column(BigInteger)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    triggered_by: Mapped[str] = mapped_column(String(32), nullable=False, default="scheduler")

    pocket: Mapped[PocketDefinition] = relationship(back_populates="refresh_runs")


class PocketRefreshPolicy(TenantBase):
    """Schedule for refreshing a pocket table.

    Mirrors :class:`AggregateRefreshPolicy` but drops the incremental
    columns — pockets only support full refresh.  ``is_enabled`` lives
    here, not on :class:`PocketDefinition`, so "pocket active" and
    "refresh schedule active" stay independent.
    """

    __tablename__ = "pocket_refresh_policies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pocket_definition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pocket_definitions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    cron_expression: Mapped[Optional[str]] = mapped_column(String(128))
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    pocket: Mapped[PocketDefinition] = relationship(back_populates="refresh_policy_row")


# ---------------------------------------------------------------------------
# Row security (Phase 5.1)
# ---------------------------------------------------------------------------

class RowSecurityRule(TenantBase):
    """Per-model row-security rule, dual-shape.

    Two shapes discriminated by ``rule_type``:

    * ``role_predicate`` — sparse role-based filter. ``predicate_expression``
      is a restricted DSL expression (``dimension_equals(...)`` / ``in(...)``
      / boolean composition); ``applies_to_roles`` lists the roles for which
      the predicate is active.
    * ``user_mapping`` — dense per-user mapping. The predicate is
      ``dimension_col IN (SELECT value_col FROM mapping_table WHERE
      user_col = :user_id)``, compiled by the router.

    Enforcement is a subquery wrap applied as the final pass in
    ``query_rewriter.py``. Any active rule also disables aggregate + pocket
    matching for that execution.
    """

    __tablename__ = "row_security_rules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    dimension_path: Mapped[str] = mapped_column(String(255), nullable=False)
    rule_type: Mapped[str] = mapped_column(String(16), nullable=False)
    predicate_expression: Mapped[Optional[str]] = mapped_column(Text)
    applies_to_roles: Mapped[Optional[list[str]]] = mapped_column(JSONB)
    mapping_table_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_tables.id", ondelete="RESTRICT"),
        index=True,
    )
    mapping_user_column: Mapped[Optional[str]] = mapped_column(String(255))
    mapping_value_column: Mapped[Optional[str]] = mapped_column(String(255))
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Dynamic attribute mapping (Phase 4 Block C)
    # Values: jwt_role | idp_group | saml_claim | oidc_scope
    attribute_source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="jwt_role", server_default="jwt_role"
    )
    attribute_claim_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("model_id", "name"),)


class QueryLog(TenantBase):
    __tablename__ = "query_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    user_identity: Mapped[Optional[str]] = mapped_column(String(255))
    protocol: Mapped[str] = mapped_column(String(16), nullable=False)
    raw_query: Mapped[str] = mapped_column(Text, nullable=False)
    query_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    route_type: Mapped[str] = mapped_column(String(32), nullable=False)
    aggregate_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    pocket_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    security_rules_applied: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    persona_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
    )
    client_kind: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    rewritten_query: Mapped[Optional[str]] = mapped_column(Text)
    execution_ms: Mapped[Optional[int]] = mapped_column(Integer)
    rows_returned: Mapped[Optional[int]] = mapped_column(BigInteger)
    bytes_processed: Mapped[Optional[int]] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="success", server_default="success", index=True)
    error_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), index=True)


class QueryMissLog(TenantBase):
    __tablename__ = "query_miss_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    query_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # F-004-11: widened from 64 to 255 (migration 0135). The aggregate-skip
    # reason joins several skip tokens and exceeded 64 chars, truncating the
    # modeler-facing diagnostic mid-token.
    miss_reason: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_query: Mapped[Optional[str]] = mapped_column(Text)
    requested_dimensions: Mapped[Optional[list]] = mapped_column(JSONB)
    requested_measures: Mapped[Optional[list]] = mapped_column(JSONB)
    requested_grain: Mapped[Optional[list]] = mapped_column(JSONB)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    candidate_aggregate_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    persona_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
    )
    predicates_json: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    # F-005-14 (Bug-2256): a QueryMissLog row is keyed on the literal-FREE
    # fingerprint, so different literal variants of the same query shape
    # (country='GB' vs country='US') collapse into ONE row whose
    # `predicates_json` holds only the LATEST variant while `occurrence_count`
    # sums ALL variants. The pocket suggester would then justify a US-only
    # pocket with the combined GB+US hit count. `predicate_variants_json` keeps
    # a per-variant breakdown — a list of
    # ``{"predicate_set_hash", "predicates", "occurrence_count", "last_seen_at"}``
    # — so the pocket path can size and score each literal slice on its OWN hits.
    # Additive and pocket-specific: the aggregate optimizer (which groups by
    # grain/measures and intentionally sums across literals) ignores this column.
    predicate_variants_json: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    has_unresolvable_where: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    has_complex_sql: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    __table_args__ = (
        UniqueConstraint(
            "model_id",
            "query_fingerprint",
            "persona_id",
            name="uq_query_miss_logs_model_fingerprint_persona",
            postgresql_nulls_not_distinct=True,
        ),
    )


class RouteLog(TenantBase):
    __tablename__ = "route_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    query_log_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    route_stage: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[Optional[dict]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())


class LineageMapping(TenantBase):
    __tablename__ = "lineage_mappings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True)
    semantic_field_name: Mapped[str] = mapped_column(String(255), nullable=False)
    semantic_field_type: Mapped[str] = mapped_column(String(32), nullable=False)  # measure | dimension
    aggregate_col_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("aggregate_columns.id", ondelete="SET NULL"), index=True)
    source_column_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("model_columns.id", ondelete="SET NULL"), index=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

    model: Mapped[Model] = relationship(back_populates="lineage_mappings")


class SchemaChangeEvent(TenantBase):
    __tablename__ = "schema_change_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True)
    source_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("data_sources.id", ondelete="SET NULL"), index=True)
    table_name: Mapped[Optional[str]] = mapped_column(String(512))
    change_type: Mapped[str] = mapped_column(String(32), nullable=False)
    is_breaking: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    detected_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)

    model: Mapped[Model] = relationship(back_populates="schema_changes")


class ModelAlert(TenantBase):
    """Dedup-aware event stream for model health signals.

    Written by the model validator, scheduler, optimiser, and query
    router whenever a condition the modeler should know about is
    detected. Dedup enforced at the database level via a partial
    unique NULLS NOT DISTINCT index on (model_id, category,
    related_object_type, related_object_id) WHERE open — see migration 0145.
    """

    __tablename__ = "model_alerts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("models.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    severity: Mapped[str] = mapped_column(String(16), nullable=False)  # info | warning | error | critical
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    detail: Mapped[Optional[str]] = mapped_column(Text)
    related_object_type: Mapped[Optional[str]] = mapped_column(String(32))
    related_object_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    first_seen_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    dismissed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    dismissed_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))

    model: Mapped[Model] = relationship(back_populates="alerts")


class UserAccessBinding(TenantBase):
    __tablename__ = "user_access_bindings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)  # admin | modeler | viewer
    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    model_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

    # F-021-11: a user has at most one role per (project, model) scope. Role is
    # a mutable attribute of the binding, not part of its identity — including
    # it in the key allowed contradictory admin+viewer rows on the same scope
    # and broke the grant upsert (F-021-04). NULL model_id (project-level) is a
    # distinct scope from any concrete model id; Postgres treats NULLs as
    # distinct in a plain UNIQUE, so the project-level uniqueness is enforced by
    # the partial index added in migration 0142.
    __table_args__ = (
        UniqueConstraint(
            "user_identity", "project_id", "model_id",
            name="uq_access_binding_scope",
        ),
    )


class LocalUser(TenantBase):
    __tablename__ = "local_users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, server_default="member")
    auth_source: Mapped[str] = mapped_column(String(32), nullable=False, server_default="local")
    has_completed_onboarding: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())


# ---------------------------------------------------------------------------
# AI Optimiser tables (migration 0006)
# ---------------------------------------------------------------------------

class LLMProviderConfig(TenantBase):
    __tablename__ = "llm_provider_configs"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "display_name",
            name="uq_llm_config_project_display_name",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[Optional[str]] = mapped_column(Text)
    encrypted_api_key: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=4096)
    temperature: Mapped[float] = mapped_column(Float, nullable=False, default=0.2)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    config: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())


class ModelTelemetrySnapshot(TenantBase):
    __tablename__ = "model_telemetry_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True)
    snapshot_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    lookback_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    total_miss_patterns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_miss_occurrences: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    top_cost_score: Mapped[Optional[float]] = mapped_column(Float)
    triggered_by: Mapped[str] = mapped_column(String(32), nullable=False, default="scheduler")


class AIOptimizerRun(TenantBase):
    __tablename__ = "ai_optimizer_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True)
    triggered_by: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    is_dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    started_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    llm_provider: Mapped[Optional[str]] = mapped_column(String(64))
    llm_model: Mapped[Optional[str]] = mapped_column(String(128))
    telemetry_snapshot_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_telemetry_snapshots.id"),
        index=True,
    )
    recommendations_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    aggregates_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    aggregates_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    raw_llm_response: Mapped[Optional[str]] = mapped_column(Text)
    diagnostics_log: Mapped[Optional[list]] = mapped_column(JSONB)

    recommendations: Mapped[list[AIAggregateRecommendation]] = relationship(
        back_populates="optimizer_run", cascade="all, delete-orphan"
    )


class AIAggregateRecommendation(TenantBase):
    __tablename__ = "ai_aggregate_recommendations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True)
    optimizer_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_optimizer_runs.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    grain: Mapped[list] = mapped_column(JSONB, nullable=False)
    measures: Mapped[list] = mapped_column(JSONB, nullable=False)
    rationale: Mapped[Optional[str]] = mapped_column(Text)
    addresses_fingerprints: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    estimated_hit_rate: Mapped[Optional[float]] = mapped_column(Float)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="applied")
    aggregate_definition_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("aggregate_definitions.id", ondelete="SET NULL"),
        index=True,
    )
    queries_served: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    avg_latency_ms_improvement: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

    optimizer_run: Mapped[AIOptimizerRun] = relationship(back_populates="recommendations")


class ModelAISchedulerConfig(TenantBase):
    __tablename__ = "model_ai_scheduler_config"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    ai_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cron_expression: Mapped[str] = mapped_column(String(128), nullable=False, default="0 5 * * *")
    lookback_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=168)
    max_creates_per_run: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    min_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enable_ai_aggregation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    llm_config_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("llm_provider_configs.id", ondelete="SET NULL"),
        index=True,
    )
    # Per-model override for the glossary creator LLM. NULL = inherit the project
    # default (ProjectAgentConfig.glossary_llm_config_id). The aggregate-creator
    # override reuses llm_config_id above.
    glossary_llm_config_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("llm_provider_configs.id", ondelete="SET NULL"),
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())


# ---------------------------------------------------------------------------
# Glossary tables (migration 0009 — semantic-layer Phase 3)
# ---------------------------------------------------------------------------

class GlossaryEntry(TenantBase):
    __tablename__ = "glossary_entry"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    term: Mapped[str] = mapped_column(String(255), nullable=False)
    definition: Mapped[str] = mapped_column(Text, nullable=False)
    context_notes: Mapped[Optional[str]] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # llm | user | llm_approved
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending_review")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    superseded_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("glossary_entry.id", ondelete="SET NULL"),
        index=True,
    )
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    proposed_is_hidden: Mapped[Optional[bool]] = mapped_column(Boolean)
    visibility: Mapped[Optional[str]] = mapped_column(String(16))
    confidence: Mapped[Optional[str]] = mapped_column(String(16))
    sample_values: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    synonyms: Mapped[list["GlossarySynonym"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan",
        foreign_keys="GlossarySynonym.entry_id",
    )
    attachments: Mapped[list["GlossaryAttachment"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan",
        foreign_keys="GlossaryAttachment.entry_id",
    )


class GlossarySynonym(TenantBase):
    __tablename__ = "glossary_synonym"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("glossary_entry.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    synonym: Mapped[str] = mapped_column(String(255), nullable=False)

    entry: Mapped[GlossaryEntry] = relationship(back_populates="synonyms")


class GlossaryAttachment(TenantBase):
    __tablename__ = "glossary_attachment"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("glossary_entry.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)  # dimension | measure | column | concept
    target_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))

    entry: Mapped[GlossaryEntry] = relationship(back_populates="attachments")


class GlossaryShareToken(TenantBase):
    __tablename__ = "glossary_share_token"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    revoked_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)


# ---------------------------------------------------------------------------
# Configuration tables (migration 0017) — generic key/value JSONB stores
# validated by shared/config/registry.py and resolved by shared/config/resolver.py.
# ---------------------------------------------------------------------------

class TenantSetting(TenantBase):
    __tablename__ = "tenant_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())
    updated_by: Mapped[Optional[str]] = mapped_column(String(255))


class ProjectSetting(TenantBase):
    __tablename__ = "project_settings"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())
    updated_by: Mapped[Optional[str]] = mapped_column(String(255))


class ModelSetting(TenantBase):
    __tablename__ = "model_settings"

    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())
    updated_by: Mapped[Optional[str]] = mapped_column(String(255))


class Persona(TenantBase):
    """Phase 8.B — analyst-facing scope over a model, surfaced at the
    gateway as a sibling virtual catalog ``<model.slug>_<persona.slug>``.

    Empty include lists mean "no restriction on this object class".
    A populated list is an allow-list. ``bypass_row_security`` ships
    in 8.C.1 as a follow-on migration. ``includes_hidden_columns``
    replaces the old hardcoded ``_technical`` gateway variant — a
    seeded persona with ``slug='technical'`` and this flag set to
    true reproduces the previous technical catalog.
    """

    __tablename__ = "personas"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("models.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    included_measure_ids: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    included_dimension_ids: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    included_hierarchy_ids: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    audience_roles: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    default_filters: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # Phase 8.C.1 — X2: when true, the Query Router skips the Phase 5.1
    # row-security wrap for any execution bound to this persona.
    bypass_row_security: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Persona-as-catalog: when true, the gateway exposes ``is_hidden``
    # dimensions and measures through this persona's catalog. Replaces
    # the hardcoded ``_technical`` variant.
    includes_hidden_columns: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("model_id", "name", name="uq_persona_model_name"),
        UniqueConstraint("model_id", "slug", name="uq_persona_model_slug"),
    )


# ---------------------------------------------------------------------------
# PHASE 9 / F1 — Source statistics
# ---------------------------------------------------------------------------


class SourceStatistics(TenantBase):
    """Per-source-table statistics row. F2 records refresh cadence here."""

    __tablename__ = "source_statistics"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("data_sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    model_table_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("model_tables.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    row_count: Mapped[Optional[int]] = mapped_column(BigInteger)
    table_size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger)
    refresh_cadence: Mapped[str] = mapped_column(
        String(16), nullable=False, default="manual", server_default=text("'manual'")
    )  # manual | daily | weekly | monthly
    last_refreshed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    next_refresh_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "data_source_id",
            "model_table_id",
            name="uq_source_statistics_source_table",
        ),
    )

    columns: Mapped[list["SourceColumnStatistics"]] = relationship(
        back_populates="source_statistics", cascade="all, delete-orphan"
    )


class SourceColumnStatistics(TenantBase):
    __tablename__ = "source_column_statistics"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_statistics_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("source_statistics.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    model_column_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("model_columns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    distinct_count: Mapped[Optional[int]] = mapped_column(BigInteger)
    null_ratio: Mapped[Optional[float]] = mapped_column(Float)
    min_value: Mapped[Optional[str]] = mapped_column(Text)
    max_value: Mapped[Optional[str]] = mapped_column(Text)
    top_values: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    computed_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "source_statistics_id",
            "model_column_id",
            name="uq_source_column_statistics_table_column",
        ),
    )

    source_statistics: Mapped[SourceStatistics] = relationship(back_populates="columns")


class SourceJoinStatistics(TenantBase):
    __tablename__ = "source_join_statistics"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("data_sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    join_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("joins.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    selectivity: Mapped[Optional[float]] = mapped_column(Float)
    left_distinct_count: Mapped[Optional[int]] = mapped_column(BigInteger)
    right_distinct_count: Mapped[Optional[int]] = mapped_column(BigInteger)
    match_ratio: Mapped[Optional[float]] = mapped_column(Float)
    computed_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# Conversational agent — Phase Agent-A1 (migration 0049)
#
# Project-scoped agent: one config per project, allow-list of models,
# per-(project, model) prompt context, structured judge rubric, optional
# cross-model recipes, per-model alias map, conversation + turn log.
# All eight tables live in the per-tenant schema. The agent reuses the
# tenant-level llm_provider_configs for both answer + judge LLMs.
# ---------------------------------------------------------------------------


class ProjectAgentConfig(TenantBase):
    __tablename__ = "project_agent_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    display_name: Mapped[Optional[str]] = mapped_column(String(255))
    project_brief: Mapped[Optional[str]] = mapped_column(Text)
    agent_role: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="data analyst",
        server_default=text("'data analyst'"),
    )
    tone_preset: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="professional",
        server_default=text("'professional'"),
    )
    tone_overrides: Mapped[Optional[str]] = mapped_column(Text)
    brand_guidelines: Mapped[Optional[str]] = mapped_column(Text)
    safety_policy: Mapped[Optional[str]] = mapped_column(Text)
    content_rules: Mapped[Optional[str]] = mapped_column(Text)
    default_locale: Mapped[Optional[str]] = mapped_column(String(16))
    disclosure_text: Mapped[Optional[str]] = mapped_column(Text)
    webhook_url: Mapped[Optional[str]] = mapped_column(Text)
    webhook_signing_secret: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    primary_model_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="SET NULL"),
        index=True,
    )
    answer_llm_config_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("llm_provider_configs.id", ondelete="SET NULL"),
        index=True,
    )
    judge_llm_config_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("llm_provider_configs.id", ondelete="SET NULL"),
        index=True,
    )
    # Project-level default LLM for the aggregate creator (optimizer) and glossary
    # creator. NULL falls back to answer_llm_config_id. See
    # docs/architecture/architecture_llm-function-config.md.
    aggregate_llm_config_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("llm_provider_configs.id", ondelete="SET NULL"),
        index=True,
    )
    glossary_llm_config_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("llm_provider_configs.id", ondelete="SET NULL"),
        index=True,
    )
    judge_mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="async",
        server_default=text("'async'"),
    )
    judge_rubric_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_judge_rubrics.id", ondelete="SET NULL"),
        index=True,
    )
    judge_block_visibility: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="transparent",
        server_default=text("'transparent'"),
    )
    show_thought_process: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    show_semantic_query: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    show_physical_query: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    feedback_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    conversation_retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default=text("30")
    )
    enable_agent_log_screen: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    session_history_depth: Mapped[int] = mapped_column(
        Integer, nullable=False, default=20, server_default=text("20")
    )
    daily_token_budget: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    daily_cost_budget_usd: Mapped[float] = mapped_column(
        Numeric(10, 2), nullable=False, default=0, server_default=text("0")
    )
    max_query_complexity: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    agent_output_format: Mapped[str] = mapped_column(
        Text, nullable=False, default="json", server_default=text("'json'")
    )
    chart_type_selector: Mapped[str] = mapped_column(
        Text, nullable=False, default="auto", server_default=text("'auto'")
    )
    chart_renderer: Mapped[str] = mapped_column(
        Text, nullable=False, default="echarts", server_default=text("'echarts'")
    )
    chart_max_rows: Mapped[int] = mapped_column(
        Integer, nullable=False, default=500, server_default=text("500")
    )
    chart_color_palette: Mapped[str] = mapped_column(
        Text, nullable=False, default="default", server_default=text("'default'")
    )
    chart_size: Mapped[str] = mapped_column(
        Text, nullable=False, default="md", server_default=text("'md'")
    )
    include_data_table: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    max_compound_steps: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default=text("3")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now()
    )


class ProjectAgentModel(TenantBase):
    """Allow-list row: this project's agent may ground in this model."""

    __tablename__ = "project_agent_models"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("models.id", ondelete="CASCADE"),
        primary_key=True,
    )
    added_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now()
    )


class ProjectAgentModelContext(TenantBase):
    """Per (project, model) prompt context block. Hand-edited and
    auto-derived fields coexist; auto-derived ones regenerate on model
    publish."""

    __tablename__ = "project_agent_model_contexts"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("models.id", ondelete="CASCADE"),
        primary_key=True,
    )
    model_overview: Mapped[Optional[str]] = mapped_column(Text)
    analytical_capabilities: Mapped[Optional[str]] = mapped_column(Text)
    abbreviation_conflict_rules: Mapped[Optional[str]] = mapped_column(Text)
    example_questions: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    aggregates_summary: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    calendar_aliases: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    dimension_aliases: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    derived_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    published_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now()
    )


class AgentJudgeRubric(TenantBase):
    __tablename__ = "agent_judge_rubrics"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    sections: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now()
    )


class ProjectCrossModelRecipe(TenantBase):
    """Cross-model calc recipe: ordered query steps + sandboxed combine
    expression evaluated in agent-service.

    ROLLBACK CANDIDATE: this table can fold back into
    project_agent_configs.cross_model_calculations: jsonb if maintenance
    proves heavy — see D2 of the answered questions doc.
    """

    __tablename__ = "project_cross_model_recipes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    parameters: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    steps: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    # Semantic expression tree (Bug-5346) — combine step results as data, never
    # a formula string parsed as code. NULL when the recipe has no combine step.
    combine: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id", "name", name="uq_cross_model_recipe_project_name"
        ),
    )


class ModelAliasMap(TenantBase):
    """Per-model phrase -> canonical attribute jsonb. Single row per model."""

    __tablename__ = "model_alias_maps"

    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("models.id", ondelete="CASCADE"),
        primary_key=True,
    )
    alias_map: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now()
    )


class ProjectPersona(TenantBase):
    """Project-level persona: scopes the agent's view of models and
    attributes. The same persona name applies across all models in the
    project. Per-model attribute restrictions are in the scope table."""

    __tablename__ = "project_personas"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_project_persona_name"),
        UniqueConstraint("project_id", "slug", name="uq_project_persona_slug"),
    )

    model_scopes: Mapped[list["ProjectPersonaModelScope"]] = relationship(
        back_populates="persona", cascade="all, delete-orphan"
    )


class ProjectPersonaModelScope(TenantBase):
    """Per-model attribute scope for a project persona.

    A model with no scope row under a persona is excluded from the
    agent when that persona is active. Empty include lists mean
    full access to that model's attributes."""

    __tablename__ = "project_persona_model_scopes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_persona_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project_personas.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("models.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    included_measure_ids: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    included_dimension_ids: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )

    __table_args__ = (
        UniqueConstraint(
            "project_persona_id", "model_id",
            name="uq_project_persona_model_scope",
        ),
    )

    persona: Mapped[ProjectPersona] = relationship(back_populates="model_scopes")


class AgentConversation(TenantBase):
    __tablename__ = "agent_conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    caller_kind: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # 'tenant_user' | 'project_api_key'
    caller_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    persona_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project_personas.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # F-024 — per-conversation model pin. When set, the agent is restricted to
    # this single model for every turn (overriding the project allow-list and
    # primary_model_id). NULL = use project defaults. ON DELETE SET NULL so a
    # deleted model degrades the conversation gracefully to project default.
    pinned_model_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("models.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now()
    )
    last_active_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now()
    )
    title: Mapped[Optional[str]] = mapped_column(String(200))
    pinned_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)


class AgentTurn(TenantBase):
    """Single-row-per-turn log (F2 shape). One row contains everything an
    auditor needs to reconstruct a turn end-to-end."""

    __tablename__ = "agent_turns"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    user_message: Mapped[str] = mapped_column(Text, nullable=False)
    llm_plan: Mapped[Optional[dict]] = mapped_column(JSONB)
    thought_summary: Mapped[Optional[str]] = mapped_column(Text)
    semantic_query: Mapped[Optional[dict]] = mapped_column(JSONB)
    recipe_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project_cross_model_recipes.id", ondelete="SET NULL"),
        index=True,
    )
    recipe_steps_executed: Mapped[Optional[list]] = mapped_column(JSONB)
    routed_sql: Mapped[Optional[str]] = mapped_column(Text)
    route: Mapped[Optional[str]] = mapped_column(String(16))
    query_result_rows: Mapped[Optional[int]] = mapped_column(Integer)
    query_result_sample: Mapped[Optional[list]] = mapped_column(JSONB)
    answer_text: Mapped[Optional[str]] = mapped_column(Text)
    citations: Mapped[Optional[list]] = mapped_column(JSONB)
    guardrail_actions: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    judge_verdict: Mapped[Optional[str]] = mapped_column(String(32))
    judge_reasoning: Mapped[Optional[str]] = mapped_column(Text)
    judge_metrics: Mapped[Optional[dict]] = mapped_column(JSONB)
    user_feedback: Mapped[Optional[dict]] = mapped_column(JSONB)
    usage_input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    usage_output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    latency_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="ok", server_default=text("'ok'")
    )
    prompt_messages: Mapped[Optional[dict]] = mapped_column(JSONB)
    llm_raw_response: Mapped[Optional[str]] = mapped_column(Text)
    rendered_output: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    chart_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    calculation_steps: Mapped[Optional[list]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), index=True
    )

    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "turn_index",
            name="uq_agent_turns_conversation_turn_index",
        ),
    )


class AgentWebhookDlq(TenantBase):
    """One row per outbound webhook delivery that exhausted its retry
    budget. Operators retry / discard manually from the agent settings."""

    __tablename__ = "agent_webhook_dlq"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_conversations.id", ondelete="SET NULL"),
        index=True,
    )
    turn_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_turns.id", ondelete="SET NULL"),
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_status_code: Mapped[Optional[int]] = mapped_column(Integer)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    first_attempted_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now()
    )
    last_attempted_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now()
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)


# ---------------------------------------------------------------------------
# Audit logging (Phase 3 — Block A)
# ---------------------------------------------------------------------------

class IdpGroupRoleMapping(TenantBase):
    __tablename__ = "idp_group_role_mappings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    idp_group_name: Mapped[str] = mapped_column(Text, nullable=False)
    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())


class WebhookEndpoint(TenantBase):
    __tablename__ = "webhook_endpoints"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    signing_secret: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    event_filters: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[\"*\"]'::jsonb"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())


class WebhookDelivery(TenantBase):
    __tablename__ = "webhook_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("webhook_endpoints.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    # F-022-06: when a delivery fails it is left ``pending`` with the next
    # backoff time recorded here. A scheduler drain job picks up pending rows
    # whose ``next_attempt_at`` is due, so retries never sleep inside a request
    # or hold a DB session across the backoff schedule.
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, index=True)
    response_code: Mapped[Optional[int]] = mapped_column(Integer)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())


class AuditEvent(TenantBase):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    timestamp: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, index=True)
    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    actor_email: Mapped[Optional[str]] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[Optional[str]] = mapped_column(Text)
    target_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    target_name: Mapped[Optional[str]] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[Optional[dict]] = mapped_column(JSONB)
    ip_address: Mapped[Optional[str]] = mapped_column(Text)


class DataQualityRule(TenantBase):
    __tablename__ = "data_quality_rules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    rule_type: Mapped[str] = mapped_column(Text, nullable=False)
    rule_config: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    severity: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'warn'"))
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    block_on_failure: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, nullable=True)
    last_violation_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    __table_args__ = (UniqueConstraint("model_id", "name", name="uq_data_quality_rule_name"),)

    violations: Mapped[list["DataQualityViolation"]] = relationship(
        "DataQualityViolation",
        foreign_keys="DataQualityViolation.rule_id",
        cascade="all, delete-orphan",
    )


class DataQualityViolation(TenantBase):
    __tablename__ = "data_quality_violations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    rule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("data_quality_rules.id", ondelete="CASCADE"), nullable=False, index=True
    )
    detected_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    violation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    sample_values: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    aggregate_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    pocket_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pocket_definitions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )


# ---------------------------------------------------------------------------
# Impact analysis — downstream assets + gateway query references
# ---------------------------------------------------------------------------


downstream_asset_columns = Table(
    "downstream_asset_columns",
    TenantBase.metadata,
    Column("asset_id", UUID(as_uuid=True), ForeignKey("downstream_assets.id", ondelete="CASCADE"), primary_key=True),
    Column("model_column_id", UUID(as_uuid=True), ForeignKey("model_columns.id", ondelete="CASCADE"), primary_key=True),
)


class DownstreamAsset(TenantBase):
    __tablename__ = "downstream_assets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    asset_type: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_name: Mapped[str] = mapped_column(String(512), nullable=False)
    asset_url: Mapped[Optional[str]] = mapped_column(Text)
    owner: Mapped[Optional[str]] = mapped_column(String(255))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())
    columns: Mapped[list["ModelColumn"]] = relationship(secondary=downstream_asset_columns)


class GatewayQueryReference(TenantBase):
    __tablename__ = "gateway_query_references"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    queried_table: Mapped[str] = mapped_column(String(512), nullable=False)
    query_user: Mapped[Optional[str]] = mapped_column(String(255))
    query_text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))


# ---------------------------------------------------------------------------
# Data tagging + column-level persona security (Phase 9 Block H)
# ---------------------------------------------------------------------------

data_tag_columns = Table(
    "data_tag_columns",
    TenantBase.metadata,
    Column("tag_id", UUID(as_uuid=True), ForeignKey("data_tags.id", ondelete="CASCADE"), primary_key=True),
    Column("model_column_id", UUID(as_uuid=True), ForeignKey("model_columns.id", ondelete="CASCADE"), primary_key=True),
)


class DataTag(TenantBase):
    __tablename__ = "data_tags"
    __table_args__ = (
        UniqueConstraint("model_id", "tag_name", name="uq_data_tag_model_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    tag_name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    columns: Mapped[list["ModelColumn"]] = relationship(secondary=data_tag_columns)


class PersonaTagRestriction(TenantBase):
    __tablename__ = "persona_tag_restrictions"

    persona_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("personas.id", ondelete="CASCADE"), primary_key=True,
    )
    data_tag_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("data_tags.id", ondelete="CASCADE"), primary_key=True,
    )


# ---------------------------------------------------------------------------
# Agent cost ledger (Phase 4 — Block F)
# ---------------------------------------------------------------------------

class AgentCostEntry(TenantBase):
    """Per-turn cost record written after each LLM call completes."""

    __tablename__ = "agent_cost_ledger"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    turn_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_turns.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    llm_config_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("llm_provider_configs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # F-023-28 — the provider string the spend was costed against
    # (e.g. "anthropic", "openai", "google", or the judge provider). Lets the
    # /cost report split spend per provider from data rather than inferring
    # answer-vs-judge, and lets the F-023-04 budget fix be validated directly.
    # Indexed because the cost report groups by it. Nullable + indexed: rows
    # written before this column existed carry NULL and surface as "unknown".
    provider: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )
    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    estimated_cost_usd: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 6), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Scheduler SLA (migration 0070)
# ---------------------------------------------------------------------------


class RefreshSLAConfig(TenantBase):
    """Per-model SLA configuration for aggregate refresh operations."""

    __tablename__ = "refresh_sla_configs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    # Target completion time as HH:MM string in UTC, e.g. "07:00"
    target_completion_time: Mapped[str] = mapped_column(String(5), nullable=False)
    grace_period_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=15, server_default="15")
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    alert_on_breach: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    # Breach-episode tracking (B17 round 2, Finding 1): one alert per breach
    # episode (model + UTC day). ``last_breach_alerted_on`` is the day the
    # episode's single alert was emitted; ``last_breach_resolved_at`` is set
    # when every aggregate has a successful refresh for that day (recovery)
    # and cleared when a new episode alerts. Written only by the SLA monitor.
    last_breach_alerted_on: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    last_breach_resolved_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class NotificationRoute(TenantBase):
    __tablename__ = "notification_routes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    channel_type: Mapped[str] = mapped_column(String(16), nullable=False)
    channel_config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class NotificationDispatchDedup(TenantBase):
    """Durable, replica-shared dedup window for notification dispatches.

    Replaces the per-process in-memory dedup map in
    ``shared/alerting/dispatcher.py`` (F-022-04). One row per
    route-scoped ``dedup_key`` (``event_type:route_id:channel_type:target``)
    records the last time that exact alert was actually sent. The dispatcher performs an
    atomic ``INSERT ... ON CONFLICT DO UPDATE`` that only refreshes the
    timestamp when the previous send is older than the dedup window, so
    across N replicas exactly one of them wins the window and sends. This
    mirrors the DB-index dedup the model-alerts stream already uses.
    """

    __tablename__ = "notification_dispatch_dedup"

    dedup_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    last_dispatched_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), nullable=False
    )


class GlossaryBootstrapJob(TenantBase):
    """Durable, replica-shared status row for an async glossary bootstrap run.

    Replaces the per-process ``_GLOSSARY_BOOTSTRAP_JOBS`` module dict in
    ``model-service/src/api/glossary.py`` (F-018-03). That in-memory registry
    was multi-replica-unsafe (a poll could land on a replica that never saw
    the job and 404, wedging the UI spinner), unbounded (completed results
    lived forever), and lost on restart. One row per job records the project /
    model it belongs to, the phase status, a human message, and the serialized
    ``GlossaryBootstrapResponse`` once complete. Rows are bounded by a TTL
    sweep and a per-model retention cap so the table cannot grow without limit.
    """

    __tablename__ = "glossary_bootstrap_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("models.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    message: Mapped[Optional[str]] = mapped_column(Text)
    result: Mapped[Optional[dict]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SavedQuery(TenantBase):
    __tablename__ = "saved_queries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    query_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="sql", server_default=text("'sql'")
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now()
    )

    model: Mapped[Model] = relationship()


# ---------------------------------------------------------------------------
# Saved pivot views
# ---------------------------------------------------------------------------

class SavedPivotView(TenantBase):
    __tablename__ = "saved_pivot_views"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    measure_id: Mapped[str] = mapped_column(String(36), nullable=False)
    row_dim_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    col_dim_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    config_json: Mapped[Optional[str]] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    # When true the view is visible to the whole tenant, not just its owner
    # (F-029-22). Defaults to personal so existing views keep their semantics.
    is_shared: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now()
    )

    model: Mapped[Model] = relationship()


# ---------------------------------------------------------------------------
# Entity translations (i18n — glossary, measures, dimensions)
# ---------------------------------------------------------------------------

class EntityTranslation(TenantBase):
    """Stores translations for glossary entries, measures, dimensions, etc."""
    __tablename__ = "entity_translations"
    __table_args__ = (
        UniqueConstraint("model_id", "entity_type", "entity_id", "field_name", "locale", name="uq_entity_translation"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    locale: Mapped[str] = mapped_column(String(10), nullable=False)
    translated_text: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())


class ScratchpadMeasure(TenantBase):
    """Per-user ephemeral calculated expressions that don't pollute the model."""
    __tablename__ = "scratchpad_measures"
    __table_args__ = (
        UniqueConstraint("model_id", "created_by", "name", name="uq_scratchpad_measure"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String(255))
    expression: Mapped[str] = mapped_column(Text, nullable=False)
    data_type: Mapped[str] = mapped_column(String(32), nullable=False, default="numeric")
    format: Mapped[Optional[str]] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    model: Mapped[Model] = relationship()


# ---------------------------------------------------------------------------
# Solidatus integration (F-030-05)
# ---------------------------------------------------------------------------

class SolidatusConnection(TenantBase):
    __tablename__ = "solidatus_connections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True
    )
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    auth_type: Mapped[str] = mapped_column(String(32), nullable=False, default="bearer_token")
    encrypted_credentials: Mapped[bytes] = mapped_column(nullable=False)
    workspace_id: Mapped[Optional[str]] = mapped_column(Text)
    model_ref: Mapped[Optional[str]] = mapped_column(Text)
    sync_scope: Mapped[str] = mapped_column(String(32), nullable=False, default="model")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now()
    )

    project: Mapped[Project] = relationship()
    model: Mapped[Model] = relationship()


class SolidatusSyncRun(TenantBase):
    __tablename__ = "solidatus_sync_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("solidatus_connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    model_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=True
    )
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    finished_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    tessallite_snapshot_hash: Mapped[Optional[str]] = mapped_column(Text)
    solidatus_target_ref: Mapped[Optional[str]] = mapped_column(Text)
    nodes_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    edges_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    nodes_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    nodes_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    edges_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    edges_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    result_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    connection: Mapped[SolidatusConnection] = relationship()


class SolidatusObjectMapping(TenantBase):
    __tablename__ = "solidatus_object_mappings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("solidatus_connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tessallite_object_type: Mapped[str] = mapped_column(String(64), nullable=False)
    tessallite_object_id: Mapped[str] = mapped_column(Text, nullable=False)
    tessallite_stable_key: Mapped[str] = mapped_column(Text, nullable=False)
    solidatus_object_id: Mapped[Optional[str]] = mapped_column(Text)
    solidatus_object_ref: Mapped[Optional[str]] = mapped_column(Text)
    last_payload_hash: Mapped[str] = mapped_column(Text, nullable=False)
    last_synced_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    last_sync_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("solidatus_sync_runs.id", ondelete="SET NULL"), nullable=True
    )
    is_deprecated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    connection: Mapped[SolidatusConnection] = relationship()


# ---------------------------------------------------------------------------
# Collibra integration
# ---------------------------------------------------------------------------

class CollibraConnection(TenantBase):
    __tablename__ = "collibra_connections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True
    )
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    auth_type: Mapped[str] = mapped_column(String(32), nullable=False, default="bearer_token")
    encrypted_credentials: Mapped[bytes] = mapped_column(nullable=False)
    community_id: Mapped[Optional[str]] = mapped_column(Text)
    domain_id: Mapped[Optional[str]] = mapped_column(Text)
    asset_type_mapping: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    relation_type_mapping: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    responsibility_mapping: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    sync_scope: Mapped[str] = mapped_column(String(32), nullable=False, default="model")
    sync_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="rest_api")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now()
    )

    project: Mapped[Project] = relationship()
    model: Mapped[Model] = relationship()


class CollibraSyncRun(TenantBase):
    __tablename__ = "collibra_sync_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("collibra_connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    model_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=True
    )
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    finished_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    tessallite_snapshot_hash: Mapped[Optional[str]] = mapped_column(Text)
    collibra_import_job_id: Mapped[Optional[str]] = mapped_column(Text)
    assets_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    relations_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attributes_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    responsibilities_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    assets_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    assets_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    relations_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    relations_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warnings_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=list)
    result_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    connection: Mapped[CollibraConnection] = relationship()


class CollibraObjectMapping(TenantBase):
    __tablename__ = "collibra_object_mappings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("collibra_connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tessallite_object_type: Mapped[str] = mapped_column(String(64), nullable=False)
    tessallite_object_id: Mapped[str] = mapped_column(Text, nullable=False)
    tessallite_stable_key: Mapped[str] = mapped_column(Text, nullable=False)
    collibra_resource_type: Mapped[str] = mapped_column(String(32), nullable=False, default="asset")
    collibra_resource_id: Mapped[Optional[str]] = mapped_column(Text)
    collibra_full_name: Mapped[Optional[str]] = mapped_column(Text)
    last_payload_hash: Mapped[str] = mapped_column(Text, nullable=False)
    last_synced_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    last_sync_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("collibra_sync_runs.id", ondelete="SET NULL"), nullable=True
    )
    is_deprecated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
