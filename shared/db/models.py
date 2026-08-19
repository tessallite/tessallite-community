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
    BigInteger, Boolean, CheckConstraint, Column, Date, ForeignKey, Index, Integer,
    Float, LargeBinary, Numeric, String, Table, Text, UniqueConstraint, func, text,
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


class SystemAuditEvent(SystemBase):
    """Platform-plane audit (licence, tenant lifecycle, system-admin session).

    CP-08 / G-022-01: tenant ``audit_events`` cannot survive tenant-schema drop
    and cannot record system-admin actions that have no tenant session.
    """

    __tablename__ = "system_audit_events"
    __table_args__ = {"schema": "tess_system"}

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
    tenant_slug: Mapped[Optional[str]] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[Optional[dict]] = mapped_column(JSONB)
    ip_address: Mapped[Optional[str]] = mapped_column(Text)


class LoginLockout(SystemBase):
    """Per-account login lockout (G-021-04). System-schema so discover can lock
    unknown-email probes without a tenant session.
    """

    __tablename__ = "login_lockouts"
    __table_args__ = (
        UniqueConstraint("scope_key", "email_canonical", name="uq_login_lockout_scope_email"),
        {"schema": "tess_system"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    scope_key: Mapped[str] = mapped_column(String(128), nullable=False)
    email_canonical: Mapped[str] = mapped_column(String(255), nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), onupdate=func.now()
    )


class RevokedEmbedToken(SystemBase):
    """One revocation record per (token, revoking tenant).

    Bug-6306 / Bug-6352 R2: ``jti`` alone was the primary key, which made the
    row a shared slot two tenants could fight over. A jti is plaintext in any
    embed JWT that has ever leaked, so a stranger could overwrite the owning
    tenant's revocation and bring a killed token back to life (last-writer-wins
    upsert), or pre-claim the jti so the owner's own revoke collided forever
    (plain insert). The composite key gives each tenant its own row: revocations
    are independent and no tenant can observe or overwrite another's. See
    migration 0189.
    """

    __tablename__ = "revoked_embed_tokens"
    __table_args__ = {"schema": "tess_system"}

    jti: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, nullable=False, index=True,
    )
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
    # F-021-03 / Bug-7993: the SAML AuthnRequest ID issued when this flow was
    # started. The ACS callback supplies it to python3-saml as the expected
    # ``request_id`` so the IdP's ``InResponseTo`` is validated (request-binding),
    # rejecting an assertion that was not produced for this exact login attempt.
    request_id: Mapped[Optional[str]] = mapped_column(String(128))
    # Bug-8142: PKCE (RFC 7636) code_verifier for an in-flight OIDC
    # authorization-code flow. The S256 challenge derived from it is sent on the
    # authorization request; the verifier itself is replayed on the back-channel
    # token exchange, so an intercepted authorization code cannot be redeemed
    # without it. Nullable — SAML flows and pre-existing rows carry no verifier.
    code_verifier: Mapped[Optional[str]] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, index=True)


class SamlAssertionReplay(SystemBase):
    """F-021-03 / Bug-7993: durable SAML assertion-ID replay ledger.

    A signed, still-valid SAML assertion could otherwise be captured and
    replayed against the ACS (paired with a freshly minted RelayState) to forge
    a second session for the victim. Each processed assertion's unique ID is
    recorded here atomically; a second ACS POST carrying the same assertion ID
    is rejected. Rows are reaped after ``not_on_or_after`` (the assertion's own
    validity horizon) — once an assertion is expired the library rejects it on
    time grounds, so the ledger only needs to cover the live window.
    """

    __tablename__ = "saml_assertion_replay"
    __table_args__ = {"schema": "tess_system"}

    assertion_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    not_on_or_after: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now()
    )


class SchedulerJobExecution(SystemBase):
    """Durable scheduler job-execution ledger (F-012-05 / Bug-8132).

    A platform-wide OPERATOR ledger, not tenant-scoped: every registered
    APScheduler job runs system-wide (the sweeps iterate all tenants inside one
    fire), so its execution history belongs in the system DB alongside
    ``SystemTenant`` / ``SystemSetting``. It therefore inherits ``SystemBase``
    and lives in ``tess_system`` — never ``TenantBase``. (The v5 predecessor
    inherited ``TenantBase`` while writing to the system DB and shipped NO
    migration, so the table never existed and every write was swallowed into a
    debug log — a dead ledger that read as populated. This model exists in a
    real migration on the SYSTEM branch and its writers never swallow.)

    One row per job execution, keyed for correlation by ``job_id`` +
    ``scheduled_fire_time``:

    - A scheduled run writes ``started`` on APScheduler ``EVENT_JOB_SUBMITTED``
      and is updated to ``success`` / ``error`` on ``EVENT_JOB_EXECUTED`` /
      ``EVENT_JOB_ERROR``; a dropped tick writes ``misfire`` on
      ``EVENT_JOB_MISSED``.
    - A manual trigger (``POST /scheduler/trigger/{job_id}``, Bug-8133) writes
      its own ``started`` -> ``success`` / ``error`` pair with
      ``trigger_source='manual'`` and ``scheduled_fire_time`` = the trigger
      time, so operator-initiated runs share one durable, queryable ledger with
      the scheduled ones.
    """

    __tablename__ = "scheduler_job_executions"
    __table_args__ = (
        # B02: the correlation key is UNIQUE, so the start (SUBMITTED) and the
        # terminal (EXECUTED/ERROR/MISSED) events for ONE run collapse to ONE
        # row via an atomic upsert (execution_ledger.record_*), even when the
        # async event writes reorder — a fast/lock-busy job can no longer leave
        # two rows and make /scheduler/jobs show a completed job as running.
        UniqueConstraint(
            "job_id",
            "scheduled_fire_time",
            "trigger_source",
            name="uq_scheduler_job_executions_correlation",
        ),
        # Powers the per-job last-run lookup that GET /scheduler/jobs reads back.
        Index(
            "ix_scheduler_job_executions_job_started",
            "job_id",
            "started_at",
        ),
        {"schema": "tess_system"},
    )

    # Durable execution id returned to a manual trigger caller.
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # The scheduled fire time this row records. NULL is not used in practice —
    # scheduled runs carry the APScheduler fire time and manual runs carry the
    # trigger instant — but kept nullable so a future non-scheduled writer is
    # not forced to invent one.
    scheduled_fire_time: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    # 'scheduled' (APScheduler listener) or 'manual' (trigger endpoint).
    trigger_source: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'scheduled'")
    )
    # 'started' | 'success' | 'error' | 'misfire'.
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    # Short human outcome summary for a success/misfire ('completed', etc.).
    outcome: Mapped[Optional[str]] = mapped_column(Text)
    # Failure message for an error row (bounded by the writer).
    error_text: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now()
    )


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
    # Bug-8395 [FIXED]: the deploy_epoch the predictive stamp was made under.
    # A revert may target the version that is ALREADY deployed
    # ("revert-to-same-version", Bug-7140): it retires + drops every predictive
    # aggregate absent from the reverted-to snapshot (rehydrator
    # `drop_orphan_aggregates`) and bumps deploy_epoch while leaving
    # deployed_version_id unchanged. A version-id-only comparison therefore
    # reported "already built" for a model that had just lost every predictive
    # aggregate, and never rebuilt it.
    # Both columns are now written together and compared together through the
    # single shared rule in
    # `optimizer/src/lifecycle/predictive_build.py`
    # (`predictive_build_is_current` / `stamp_predictive_build`), used by the
    # auto-sweep (`lifecycle/predictive_sweep.py`), the cold-start kickoff
    # (`api/cold_start_routes.py`) and the manual build route
    # (`api/predictive_routes.py`). A NULL epoch (rows stamped before migration
    # 0204 added the column) counts as NOT built — one cheap, self-terminating
    # rebuild, because the planner already excludes materialised
    # (grain, measure-set) pairs.
    predictive_built_for_epoch: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
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
    # Bug-7140: monotonically increasing counter bumped on every deploy,
    # undeploy, and revert. The query-router cache uses (model_id,
    # deployed_version_id, deploy_epoch) as its cache key so that
    # undeploy (pointer -> NULL) and revert-to-same-version (pointer
    # unchanged but content changed) invalidate stale entries across all
    # replicas without relying on a fan-out eviction HTTP call.
    deploy_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # F-017-03 (Bug-7989): monotonically increasing counter bumped on every
    # successful DATA refresh (aggregate full/incremental refresh, pocket
    # refresh, manual source refresh). Distinct from deploy_epoch (which tracks
    # DEFINITION deploy/undeploy/revert). The KPI evaluation cache folds
    # data_epoch into its key so a scorecard is never stale past the DB read
    # after a refresh, on every model-service replica, without a cross-process
    # event bus. Bumped via shared.model_refresh_epoch.bump_data_epoch.
    data_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # Bug-7787: monotonically increasing counter bumped by the shared
    # ``dependency_mutation`` helper on every mutation that can add, remove,
    # rename, or rebind a model dependency edge. The impact-analysis preview
    # returns it as an optimistic token; the destructive request sends the
    # expected value and the server recomputes under lock, returning
    # IMPACT_REVISION_STALE on mismatch. Draft control metadata — distinct from
    # ``deployed_version_id``/``deploy_epoch`` (the deployed runtime pointer).
    dependency_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
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
    named_queries: Mapped[list["NamedQuery"]] = relationship(back_populates="model", cascade="all, delete-orphan")


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
    # Bug-6295: an imported version row carries history metadata (number,
    # summary) but NOT a faithful historical snapshot — the export bundle
    # deliberately omits each version's snapshot_json (Bug-7623), so the
    # source-tenant shapes are genuinely unrecoverable on import. When True,
    # ``snapshot_json`` is a placeholder ({}) and MUST NOT be treated as the
    # version's real shape: reverting to it would rehydrate an empty/wrong
    # definition, silently serving today's shape (or nothing) under an old
    # label. False/NULL means the snapshot is authentic (native Save path).
    snapshot_unavailable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

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
    # Bug-7195: the ``type`` domain is enforced application-side by
    # ``hierarchies.py::_normalize_hierarchy_type`` (ALLOWED_HIERARCHY_TYPES).
    # A DB-level CHECK is the fail-closed backstop so an out-of-band writer
    # (import/rehydrate, a script, a future endpoint that forgets the helper)
    # can never persist an unroutable type. Kept in lock-step with
    # ALLOWED_HIERARCHY_TYPES in the API layer.
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # explicit | date_embedded | segment
    dimension_kind: Mapped[Optional[str]] = mapped_column(Text)  # time | geo | entity | None
    description: Mapped[Optional[str]] = mapped_column(Text)
    segment_config: Mapped[Optional[dict]] = mapped_column(JSONB)
    date_config: Mapped[Optional[dict]] = mapped_column(JSONB)
    calendar_type: Mapped[Optional[str]] = mapped_column(String(20))  # standard | fiscal | iso_week | retail_445 | hijri | thai_buddhist
    fiscal_year_start_month: Mapped[Optional[int]] = mapped_column(Integer)  # 1-12, only when calendar_type = "fiscal"
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("model_id", "name"),
        # Bug-7195: fail-closed backstop mirroring ALLOWED_HIERARCHY_TYPES.
        CheckConstraint(
            "type IN ('explicit', 'date_embedded', 'segment')",
            name="ck_hierarchy_definitions_type",
        ),
    )

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
    # Bug-5434: optional DISPLAY column for a flat (single-level) dimension, distinct
    # from the KEY column (``source_column_id``). When set, member discovery surfaces
    # this column's value as the member CAPTION (MEMBER_NAME) while the key stays the
    # member identity (MEMBER_KEY). NULL = caption equals key (legacy behaviour).
    display_column_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_columns.id", ondelete="SET NULL"),
        index=True,
    )
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
    # Provenance: when a dimension was auto-added as a detail of another
    # dimension's bijection relationship, these two nullable FKs record which
    # relationship and which owning dimension created it. A non-null value
    # means the dimension is a provenance-linked "detail of [X]" entry in the
    # dimension list, managed through the attribute-relationships section and
    # referentially locked (cannot be deleted independently while active).
    detail_of_relationship_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dimension_attribute_relationships.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    detail_of_dimension_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dimensions.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    __table_args__ = (UniqueConstraint("model_id", "name"),)

    model: Mapped[Model] = relationship(back_populates="dimensions")


class DimensionAttributeRelationship(TenantBase):
    """A modeller-declared key-to-detail relationship on a dimension.

    Spec: architecture_derived-grain-aggregate-routing.md §5.3. This is EXPLICIT,
    model-managed declaration content and is multi-row because one dimension key
    may govern several details (name, ISO code, phone key). It is distinct from
    ``Dimension.display_column_id`` (a caption choice) — the presence of a display
    column never declares 1:1 and never creates one of these rows (§2.5).

    Phase 1b persists and round-trips the declaration only. No serving, no
    verification: those are Phase 2+. Runtime verification evidence lives in a
    SEPARATE table (``DimensionAttributeVerification``, Phase 2) because it is
    live operational state, not pinned model content.

    ``key_column_id`` is pinned even though it normally equals the owning
    dimension's current ``source_column_id``: a key rebind changes the
    declaration hash and stales evidence instead of silently retargeting a
    trusted edge. V1 accepts physical columns in the same governed model relation;
    calculated/UDA details are source-only (a future expression-plus-data proof).
    """

    __tablename__ = "dimension_attribute_relationships"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    dimension_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dimensions.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    # Pinned key column (see class docstring). SET NULL on physical column delete
    # so the declaration survives as a broken/stale edge rather than vanishing.
    key_column_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_columns.id", ondelete="SET NULL"), index=True,
    )
    detail_column_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_columns.id", ondelete="SET NULL"), index=True,
    )
    # BIJECTION | FUNCTIONAL_N_TO_1 (spec §5.3). Determines the safe measure set
    # once serving lands (I14): a bijection is an exact partition relabel; an N:1
    # edge is a real coarsening. Never opportunistically upgraded N:1 -> exact.
    cardinality: Mapped[str] = mapped_column(String(32), nullable=False)
    # REJECT_NULL is fixed in v1 (spec §5.3): NULL key/detail endpoints are
    # excluded from any relationship proof (I16). Stored so a future policy can
    # widen it without a migration.
    null_policy: Mapped[str] = mapped_column(String(32), nullable=False, default="REJECT_NULL")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Stable hash over the declaration's meaning (key/detail column ids,
    # cardinality, null policy). Any edit that changes meaning changes the hash,
    # which stales all Phase-2 verification evidence for this row.
    declaration_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    # One declaration per (dimension, detail, cardinality) pairing — the same
    # detail may be declared once per dimension. Different details on the same
    # dimension are distinct rows.
    __table_args__ = (
        UniqueConstraint(
            "dimension_id", "detail_column_id", "cardinality",
            name="uq_dim_attr_rel_dimension_detail_cardinality",
        ),
    )

    model: Mapped[Model] = relationship()
    dimension: Mapped[Dimension] = relationship(foreign_keys=[dimension_id])


class DimensionAttributeVerification(TenantBase):
    """Mostly-append-only complete-data verification evidence for a declared
    attribute relationship (spec §5.3 / §7.6; see the upsert carve-out below).

    This is LIVE OPERATIONAL STATE, not model content: it is tied to an immutable
    deployed version and (for artifact checks) a physical refresh run. It is
    therefore deliberately EXCLUDED from model snapshots — a rehydrated model must
    be re-verified against its reverted/imported deployed version before any edge
    can serve. The current status is the newest row per relationship (the API
    projects a denormalised current-status view); rows are append-only except for
    the one deliberate in-place upsert path documented below.

    Phase 2 writes these rows but authorises NO serving route — the router trust
    predicate (§7.6.4) is a later phase. A row is written ``VERIFIED`` only when
    every directional + NULL check passed for the SAME declaration hash, deployed
    version, and (for artifacts) active refresh run; any failure/timeout/
    counterexample/unsupported-type writes ``BROKEN`` or ``ERROR`` and never
    ``VERIFIED``.

    Mostly append-only, with ONE deliberate in-place update path: the periodic
    relationship health sweep (scheduler ``derived_relationship_sweep``) re-proves
    each edge over the served data on a cadence and UPSERTS its artifact-local
    evidence — it UPDATES the existing row for the SAME idempotency key
    (relationship_id, artifact_refresh_run_id, declaration_hash, verifier_version),
    refreshing ``status`` / ``violation_count`` / ``error_code`` / ``checked_at``
    rather than inserting a duplicate (the unique constraint below forbids the
    duplicate). Newest-wins semantics are preserved (``checked_at`` is bumped), so
    the trust predicate still reads the current verdict; the trade-off is that the
    build-time row's prior status for the active run is overwritten by the latest
    re-check.
    """

    __tablename__ = "dimension_attribute_verifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    relationship_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dimension_attribute_relationships.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # VERIFIED | BROKEN | STALE | ERROR | PENDING (spec §5.3; PENDING = Bug-7894:
    # a text BIJECTION relabel checked 1:1 at source but awaiting artifact-build
    # serve-collation certification — non-serving, clears to VERIFIED on build).
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    verifier_version: Mapped[str] = mapped_column(String(64), nullable=False)
    # The declaration hash this evidence was produced for. The router trust
    # predicate (§7.6.4) requires it to equal the deployed declaration's hash;
    # an edit that changes the hash strands this evidence as non-matching (stale).
    declaration_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    deployed_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_versions.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    # The deploy epoch (Model.deploy_epoch) at verification time. A deploy/revert
    # advances the epoch, so losing/older evidence cannot satisfy the predicate.
    deploy_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # TENANT_GLOBAL (mandatory deploy check) | PERSONA_ARTIFACT (additional).
    scope_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="TENANT_GLOBAL")
    scope_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Immutable source version/watermark when the connector exposes one; else the
    # successful refresh run is the artifact data version (§7.6.4 rule 6).
    source_data_version: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # DEPLOY_CHECK | AGGREGATE | POCKET (spec §5.3). Deploy-check evidence is
    # model-health only; artifact evidence names the exact physical run/manifest.
    artifact_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="DEPLOY_CHECK")
    artifact_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    artifact_refresh_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    artifact_manifest_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    checked_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    violation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # A failed-direction / unsupported-type / timeout code. Counterexample VALUES
    # are never persisted (they may be sensitive) — only counts and a code.
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        # Idempotency key for artifact evidence (§7.6.3): relationship + artifact
        # + refresh run + declaration hash + verifier version uniquely identify a
        # verification attempt, so retries are idempotent.
        UniqueConstraint(
            "relationship_id", "artifact_refresh_run_id", "declaration_hash", "verifier_version",
            name="uq_dim_attr_verif_run_decl_verifier",
        ),
        Index(
            "ix_dim_attr_verif_relationship_checked",
            "relationship_id", "checked_at",
        ),
    )

    relationship: Mapped[DimensionAttributeRelationship] = relationship()


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
    # Bug-6673: FK + ondelete=SET NULL so deleting the referenced measure
    # NULLs this column instead of leaving a dangling UUID.
    target_measure_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("measures.id", ondelete="SET NULL"),
        nullable=True,
    )
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
    # Bug-6673: FK + ondelete=SET NULL so deleting the referenced dimension
    # NULLs this column instead of leaving a dangling UUID.
    time_dimension_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dimensions.id", ondelete="SET NULL"),
        nullable=True,
    )

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
    # Bug-7982 (Codex re-gate residual 2): the deployed-version + deploy-epoch the
    # cached value was evaluated AGAINST. $KPIs must serve a row only when these
    # match the model's CURRENT deployed pointer/epoch — otherwise a
    # definition-changing revert (which bumps deploy_epoch) would keep serving the
    # stale value computed under the OLD definition (mixed-version wrong number).
    # NULL means "epoch unknown" and is treated as incompatible (fail-closed).
    evaluated_for_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    evaluated_for_epoch: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Bug-7982 (Codex re-gate R6, finding 1): within-epoch write ordering. The
    # epoch guard (evaluated_for_epoch) only orders writes ACROSS epochs; two
    # writers evaluating the SAME epoch race, and whichever COMMITS last wins
    # regardless of which read fresher source data. ``eval_started_at`` is
    # captured ONCE when an evaluation begins (before any source read), so the
    # upsert guard can order by the tuple (evaluated_for_epoch, eval_started_at):
    # a later-STARTING evaluation is never overwritten by an earlier-starting one
    # that merely commits after it. NULL = legacy/unstamped row, treated as the
    # oldest (any stamped write may overwrite it within the same epoch).
    eval_started_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMPTZ, nullable=True
    )
    # Bug-7982 (Codex re-gate R7, finding 2): ``eval_started_at`` came from
    # ``clock_timestamp()``, which is NOT unique (82,242 duplicates in 100k live
    # samples) — an exact tie let the ``<=`` ordering comparison admit both
    # writers, so last-commit-wins resurfaced. ``eval_generation`` is a strictly
    # increasing, never-repeating value from the tenant-schema sequence
    # ``kpi_eval_generation_seq``, allocated ONCE at evaluation start: a TOTAL
    # order over evaluations. Equal generations mean the SAME logical evaluation
    # (the sweep threads its token into evaluate-batch so both writes share one),
    # which is exactly the case the ordering guard must admit. NULL = legacy row
    # written before migration 0183, treated as the oldest. ``eval_started_at``
    # remains as metadata and as the documented fallback order.
    eval_generation: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )


class PendingKpiReeval(TenantBase):
    """Durable outbox for post-deploy/revert KPI re-evaluation (Bug-7982 finding 6).

    A deploy/revert bumps ``deploy_epoch``; the ``$KPIs`` serve predicate then
    withholds every ``kpi_latest`` row stamped with the OLD epoch until it is
    re-evaluated under the new epoch. The in-process fire-and-forget re-eval
    trigger closes that gap in seconds on the happy path, but it is NON-DURABLE:
    if the process exits between the deploy/revert commit and the background task
    actually running, the trigger is silently lost and ``$KPIs`` stays withheld
    until the next hourly sweep, with no operator-visible signal.

    This row is written INSIDE the deploy/revert transaction (so it commits
    atomically with the epoch bump). On the happy path the trigger deletes it
    after a successful re-eval. If it survives (process died / trigger failed),
    the scheduler sweep drains it: it logs a WARNING that a re-eval is overdue,
    fires the re-eval, and deletes the row.
    """
    __tablename__ = "pending_kpi_reeval"
    __table_args__ = (
        UniqueConstraint("model_id", name="uq_pending_kpi_reeval_model"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # The deploy epoch this re-eval was requested for. Lets the sweep skip a row
    # that a later deploy has already superseded (a newer epoch is pending).
    requested_for_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )


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
    # Join ORIENTATION — which rows survive: inner / left / right / full.
    #
    # This field used to default to ``many_to_one``, which is a CARDINALITY
    # label, not a join type. Rendering an undeclared token as an un-flipped
    # LEFT JOIN makes the preserved relation depend on the compiler's base
    # table rather than on the model, so no route can prove two plans over the
    # same model hold the same rows. The default is now ``inner`` — the value
    # ``JoinCreate`` has always applied on the write path and the value the
    # JoinsPanel preselects — so the two orthogonal properties can no longer be
    # conflated by any new row. Existing rows may still carry a legacy token;
    # ``shared.semantic.join_keyword.join_keyword`` keeps coercing those to
    # their historical rendering (contract invariant 4) rather than raising.
    # See docs/architecture/architecture_join-orientation-and-cardinality.md.
    join_type: Mapped[str] = mapped_column(String(32), nullable=False, default="inner")
    # Join CARDINALITY — how many rows on each side match:
    # one_to_one / one_to_many / many_to_one / many_to_many. NULL means the
    # modeller has not declared it. Orthogonal to ``join_type`` (invariant 3):
    # cardinality NEVER changes the rendered SQL keyword. It is fan-out
    # metadata. Consumers today, all through
    # ``shared.semantic.join_keyword.edge_cardinality``: the many-to-many
    # compatibility guard (``semantic/field_compatibility.py``), the two
    # drill-through join-path classifiers (``model-service api/measures.py``,
    # ``query-router drill/semantic_builder.py``), and the LookML export's
    # ``relationship`` derivation (``scripts/lookml_export/model.py``, which
    # also inverts it when the traversal reaches this edge from its declared
    # RIGHT endpoint — Bug-8654/Bug-8641). It also round-trips through the
    # YAML snapshot as its own ``cardinality`` key. No FROM-clause builder
    # reads this field.
    cardinality: Mapped[Optional[str]] = mapped_column(String(32))
    left_column_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("model_columns.id", ondelete="CASCADE"), nullable=False, index=True)
    right_column_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("model_columns.id", ondelete="CASCADE"), nullable=False, index=True)
    # Join POPULATION PARTICIPATION — Bug-8615, governance phase G1. The
    # modeller-declared answer to "is this join's row-filtering / row-
    # multiplying effect part of what this model MEANS?". Orthogonal to both
    # ``join_type`` (which rows survive the join) and ``cardinality`` (how many
    # rows match): those describe the join, this declares the modeller's
    # INTENT about the model's row population. Contract:
    # docs/architecture/architecture_join-population-governance.md (contract 2).
    #
    #   preserve_base_rows  (DEFAULT) may still be elided — historical behaviour
    #   population_defining           must never be elided (wired in phase G3)
    #   enrichment_only               may be elided; fan-out accepted
    #   undeclared                    no modeller decision; the deploy-time
    #                                 validator reports a non-neutral one
    #
    # Vocabulary lives in ``shared.schemas.domains.aggregates_security``
    # (``POPULATION_PARTICIPATION_VALUES``); it is deliberately NOT imported
    # here so this module stays dependency-free, exactly like ``join_type``.
    #
    # The default is what every pre-existing row gets (migration 0190 applies
    # the same server default), so adding this column changes NO served
    # numbers. ``server_default`` is permanent on purpose: the snapshot
    # rehydrate path issues a raw ``insert(Join).values(**row)`` whose row
    # omits this key for any snapshot saved before the column existed.
    population_participation: Mapped[str] = mapped_column(
        String(32), nullable=False,
        default="preserve_base_rows",
        server_default=text("'preserve_base_rows'"),
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())

    model: Mapped[Model] = relationship(back_populates="joins")


class JoinPopulationCheck(TenantBase):
    """Deploy-time row-loss / row-multiplication evidence for ONE join.

    Bug-8615 phase G1. LIVE OPERATIONAL STATE, not model content: it describes
    the SOURCE data's current shape under a specific deployed version + deploy
    epoch, so it is deliberately EXCLUDED from model snapshots (registered as
    such in ``model_snapshot/tests/test_snapshot_coverage_guard.py``) and is
    re-established by the next deploy after a revert/import.

    Exactly one CURRENT row per join (``join_id`` unique — the same shape
    ``source_join_statistics`` uses). The deploy hook replaces the whole
    model's set inside the deploy transaction, so "no row for a join" honestly
    means "not evaluated at the last deploy" rather than "evaluated clean".

    Rows cascade-delete with their ``Join`` (which the canonical model delete
    removes explicitly before ``models``) and with their ``Model``, so no extra
    step is required in ``shared/model_snapshot/cascade_delete.py``.

    WARN-ONLY in this phase: a ``BLOCKED`` row is computed and surfaced but
    never prevents a deploy (governance plan phase G5 owns block mode).
    """

    __tablename__ = "join_population_checks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    join_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("joins.id", ondelete="CASCADE"),
        nullable=False, unique=True,
    )
    # Denormalised so the health read and the deploy-time replace need no
    # sub-select through ``joins``. CASCADE from both sides keeps it consistent.
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    deployed_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    deploy_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # neutral | filtering | multiplying (shared.semantic.join_population_validator)
    classification: Mapped[str] = mapped_column(String(16), nullable=False)
    # The join's declared population_participation AT CHECK TIME, so a reader
    # can see what the verdict was computed against.
    population_participation: Mapped[str] = mapped_column(String(32), nullable=False)
    # OK | WARNING | BLOCKED — this join's contribution to the model rollup.
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # True only when the source probe actually returned numbers. False means the
    # ratios below are NULL and the verdict was reached conservatively.
    measured: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    row_loss_ratio: Mapped[Optional[float]] = mapped_column(Float)
    row_mult_ratio: Mapped[Optional[float]] = mapped_column(Float)
    # max(row_loss_ratio, row_mult_ratio) — the value compared to the threshold.
    row_effect_ratio: Mapped[Optional[float]] = mapped_column(Float)
    # Stable machine reason code (the validator's REASON_* constants).
    reason: Mapped[Optional[str]] = mapped_column(String(64))
    # Hash of EVERY join attribute the classification was computed from
    # (``join_population_validator.join_definition_fingerprint``). A verdict is
    # only current while the join it measured is unchanged, and the deploy
    # epoch alone does not say that: ``PATCH /joins/{id}`` can change
    # ``join_type`` or a join column — direct classifier inputs — without
    # bumping the epoch. Comparing one hand-picked field is how that gap first
    # appeared, so the health surface compares this instead. NULL only for a
    # row written before this column existed.
    inputs_fingerprint: Mapped[Optional[str]] = mapped_column(String(64))
    checked_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())


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
    # --- Derived-grain routing manifests (Bug-7359, spec §5.2/§5.3, Phase 3) ---
    # These are IMMUTABLE build metadata describing what was actually materialised
    # (I8): they are written in the SAME lifecycle transaction as the physical
    # artifact and its verified evidence. Descriptive only in Phase 3 — no route
    # reads them yet (serving is shadow-only through Phase 4).
    #   grain_keys        ordered list of MaterializedGrainKey dicts (spec §5.2).
    #   attribute_edges   list of MaterializedAttributeEdge dicts naming each
    #                     carried key->detail relationship, evidence + run + hash.
    #   passenger_columns list of passenger column descriptors (detail carried
    #                     beside its key; NOT an independent grain key).
    grain_keys: Mapped[Optional[list]] = mapped_column(JSONB)
    attribute_edges: Mapped[Optional[list]] = mapped_column(JSONB)
    passenger_columns: Mapped[Optional[list]] = mapped_column(JSONB)
    # LIVE pointer to the refresh run whose built rows the current manifest +
    # verified edges describe (spec §5.3). Set ATOMICALLY only after the
    # artifact-local check passes over the built artifact. Deliberately EXCLUDED
    # from the snapshot serialiser and CLEARED on import/clone/rehydrate — a
    # rehydrated definition has no physical run and must re-earn trust (§5.3, I8).
    active_refresh_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("aggregate_refresh_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    # F-013-02 (Bug-8250): IMMUTABLE artifact-to-version binding. The exact
    # deployed model version + epoch this physical artifact was BUILT FOR,
    # written atomically at successful refresh from the model's then-current
    # deploy pointer. The runtime matcher REQUIRES an exact match against the
    # model's current (deployed_version_id, deploy_epoch); a fresh artifact
    # built under a previous definition must NOT serve after a deploy/revert.
    # Freshness is not compatibility. Like active_refresh_run_id these are LIVE
    # build metadata: snapshot-EXCLUDED and CLEARED on import/clone/rehydrate,
    # so a rehydrated artifact has no build for the current version and stays
    # non-servable until a rebuild re-earns the binding. NULL = never built for
    # any deployed version (unmaterialised, or built while undeployed).
    built_for_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    built_for_epoch: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Bug-8481: LIVE physical-build storage identity. Records the exact
    # ``{target_id, project_connection_id, routing_fingerprint}`` captured before
    # CTAS/DELETE+INSERT began. A completion re-proves the live target and
    # connection under row locks before it may restore ACTIVE; mismatch leaves
    # the aggregate stale and non-serving. Snapshot-EXCLUDED/import-CLEARED like
    # the version binding because an exported definition carries no physical
    # table at the recorded database.
    built_for_storage_binding: Mapped[Optional[dict]] = mapped_column(JSONB)
    # Bug-8602: LIVE physical-build SOURCE identity — the sibling of
    # ``built_for_storage_binding`` for the other side of the build. Records the
    # ``{model_id, source_connection_id, source_connection_project_id,
    # routing_fingerprint}`` captured before the CTAS read its first row, i.e.
    # WHICH database these rows came FROM. The project id carries the Bug-5325
    # cross-project refusal through to serve time, where the source connection
    # is never dialled and nothing else would re-check it.
    # A cross-database aggregate (source connection A, target connection B) has
    # no DataTarget on A, so the target binding cannot speak for it at all: an
    # admin editing A's host/database would otherwise leave the aggregate
    # serving rows materialised from the OLD database while the source-route
    # fallback for the same query reads the NEW one. Snapshot-EXCLUDED and
    # import-CLEARED like the other build bindings.
    built_for_source_binding: Mapped[Optional[dict]] = mapped_column(JSONB)
    # Bug-7903: DURABLE pre-refresh status snapshot. The uniform refresh
    # pending-guard flips a servable aggregate to "pending" (committed,
    # non-servable) BEFORE any physical change and restores it AFTER the new
    # run + manifest + VERIFIED evidence commit. Because a process crash loses
    # any in-memory snapshot, the prior status is persisted here in the SAME
    # committed transaction as the pending flip, so recovery (the sweep re-running
    # a stuck-"pending" aggregate) restores it to EXACTLY its prior state — never
    # re-activating one that was "disabled"/"retired". Also set by the rehydrator
    # when it forces active/disabled aggregates to pending on import, so the first
    # rebuild restores the imported state faithfully. NULL when no refresh is in
    # flight; cleared on the terminal restore.
    refresh_prior_status: Mapped[Optional[str]] = mapped_column(String(32))
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
    # Bug-7359: active_refresh_run_id adds a SECOND FK path between
    # aggregate_definitions and aggregate_refresh_runs, so the historical
    # runs relationship must name its FK explicitly to stay unambiguous.
    refresh_runs: Mapped[list[AggregateRefreshRun]] = relationship(
        back_populates="aggregate", cascade="all, delete-orphan",
        foreign_keys="AggregateRefreshRun.aggregate_definition_id",
    )


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
    quantile_coverage: Mapped[Optional["QuantileCoverage"]] = relationship(
        back_populates="aggregate_column",
        uselist=False,
        cascade="all, delete-orphan",
    )


class QuantileCoverage(TenantBase):
    """Versioned semantic identity of one materialised pNN aggregate column.

    Spec §4.2 (Bug-6969/5891). A ``pNN`` physical-name suffix proves only a
    conventional fraction — NOT continuous vs discrete method, ASC vs DESC order,
    null policy, input expression, value type, or build exactness (Gap D). This
    row is the AUTHORITATIVE proof input the router's ``QuantileServeProof``
    consumes; a pNN ``AggregateColumn`` WITHOUT a coverage row is treated as
    ``exactness='unknown'`` and is never served in exact mode (I8).

    One-to-one with the pNN ``AggregateColumn`` it describes. Written in the same
    lifecycle transaction as the physical column (I8: build evidence is recorded
    at materialisation, never inferred later from the current connection).
    Fractions are stored as exact decimal TEXT (never a float) so
    ``PERCENTILE_CONT(0.3333333333)`` can never alias a column through binary
    rounding (§16.14).
    """
    __tablename__ = "quantile_coverage"
    __table_args__ = (
        UniqueConstraint(
            "aggregate_column_id", name="uq_quantile_coverage_column"
        ),
        CheckConstraint(
            "method IN ('continuous', 'discrete')",
            name="ck_quantile_coverage_method",
        ),
        CheckConstraint(
            "order_direction IN ('asc', 'desc')",
            name="ck_quantile_coverage_direction",
        ),
        CheckConstraint(
            "exactness IN ('exact', 'bounded_approximate', 'unknown')",
            name="ck_quantile_coverage_exactness",
        ),
        CheckConstraint(
            "null_policy IN ('ignore_nulls', 'respect_nulls')",
            name="ck_quantile_coverage_null_policy",
        ),
        # Fraction is exact decimal TEXT in [0,1]; a simple format guard so a
        # malformed producer write cannot persist a non-numeric fraction that the
        # loader would then have to reject at query time.
        CheckConstraint(
            r"fraction ~ '^[0-9]+(\.[0-9]+)?$'",
            name="ck_quantile_coverage_fraction_format",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    aggregate_column_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("aggregate_columns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalised aggregate FK so the router can load all coverage for a
    # candidate in one query without joining through columns.
    aggregate_definition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("aggregate_definitions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    measure_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("measures.id", ondelete="SET NULL"), index=True
    )
    semantic_measure_name: Mapped[str] = mapped_column(String(255), nullable=False)
    input_expression_fingerprint: Mapped[str] = mapped_column(String(512), nullable=False)
    # Exact decimal TEXT, e.g. '0.5', '0.95', '0.3333333333'. NEVER a float.
    fraction: Mapped[str] = mapped_column(String(64), nullable=False)
    method: Mapped[str] = mapped_column(String(16), nullable=False)  # continuous | discrete
    order_direction: Mapped[str] = mapped_column(String(4), nullable=False, default="asc")
    null_policy: Mapped[str] = mapped_column(String(16), nullable=False, default="ignore_nulls")
    value_type: Mapped[Optional[str]] = mapped_column(String(64))
    # Physical column is "value_collation" (bare "collation" is a reserved
    # keyword in PostgreSQL and breaks CREATE TABLE — Bug-7858); the Python
    # attribute stays `collation` so contract/producer/consumer code is unchanged.
    collation: Mapped[Optional[str]] = mapped_column("value_collation", String(64))
    timezone: Mapped[Optional[str]] = mapped_column(String(64))
    # exact | bounded_approximate | unknown. Legacy/backfilled rows are 'unknown'
    # and never served in exact mode until rebuilt with certified evidence (I8).
    exactness: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    algorithm: Mapped[Optional[str]] = mapped_column(String(32))
    algorithm_version: Mapped[Optional[str]] = mapped_column(String(32))
    build_source_dialect: Mapped[Optional[str]] = mapped_column(String(32))
    build_model_version: Mapped[Optional[str]] = mapped_column(String(64))
    refresh_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("aggregate_refresh_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    coverage_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    aggregate_column: Mapped[AggregateColumn] = relationship(
        back_populates="quantile_coverage",
        foreign_keys=[aggregate_column_id],
    )


class AggregateRefreshPolicy(TenantBase):
    __tablename__ = "aggregate_refresh_policies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    aggregate_definition_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("aggregate_definitions.id", ondelete="CASCADE"), nullable=False, unique=True)
    refresh_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="scheduled")
    cron_expression: Mapped[Optional[str]] = mapped_column(String(128))
    incremental_column: Mapped[Optional[str]] = mapped_column(String(255))
    incremental_lookback: Mapped[Optional[int]] = mapped_column(Integer)
    incremental_append_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    full_rebuild_interval_days: Mapped[Optional[int]] = mapped_column(Integer)
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

    aggregate: Mapped[AggregateDefinition] = relationship(
        back_populates="refresh_runs",
        foreign_keys=[aggregate_definition_id],
    )


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


class PhysicalCleanupTask(TenantBase):
    """Detached durable outbox for aggregate/pocket target-table cleanup.

    The row is created in the same tenant-metadata transaction that deletes a
    model or replaces a project, but target DDL runs only after that transaction
    commits.  Every correlation identifier is deliberately a plain UUID rather
    than a foreign key: model/project/connection/definition rows may all be
    deleted by the owning transaction, while this retry and audit evidence must
    survive.  ``encrypted_credentials`` remains inside the platform's existing
    Fernet credential envelope; ``connection_config`` is the validated
    non-secret ProjectConnection config snapshot.
    """

    __tablename__ = "physical_cleanup_tasks"
    __table_args__ = (
        CheckConstraint(
            # F-013-07: named_query is the third materialised family whose
            # physical table is dropped through this outbox (migration 0214).
            "artifact_kind IN ('aggregate', 'pocket', 'named_query')",
            name="ck_physical_cleanup_tasks_artifact_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'failed', 'succeeded')",
            name="ck_physical_cleanup_tasks_status",
        ),
        Index(
            "ix_physical_cleanup_tasks_due",
            "status", "next_attempt_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    artifact_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    artifact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    connection_type: Mapped[str] = mapped_column(String(32), nullable=False)
    connection_display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    encrypted_credentials: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    connection_config: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    target_schema: Mapped[str] = mapped_column(String(512), nullable=False)
    qualified_table_name: Mapped[str] = mapped_column(String(1024), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default=text("'pending'")
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    requested_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now()
    )
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMPTZ, nullable=True, index=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    error_message: Mapped[Optional[str]] = mapped_column(Text)


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
    # Bug-8719: when True the fact table primary key is included in the pocket's
    # materialised output even though it is hidden in the model. Required for
    # incremental refresh (the row-key DELETE needs the PK to match rows). Auto-set
    # when incremental_column + incremental_lookback_hours are both configured and
    # the fact PK is hidden. Not user-facing — the Pocket drawer shows an
    # informational message instead of a checkbox.
    include_fact_key: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    ttl_days: Mapped[int] = mapped_column(Integer, nullable=False, default=14)
    # F-005-19 (Bug-2260): a freshly constructed pocket has NO materialised
    # table yet, so the ORM default must be "stale" (unmaterialised) not "fresh".
    # A "fresh" default was a footgun: any direct constructor that forgot to set
    # status would have entered the matcher pool claiming a cache that does not
    # exist. Both current writers set status="stale" explicitly; this aligns the
    # default with that contract.
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="stale")
    # --- Derived-grain routing row manifest (Bug-7359, spec §5.2/§5.3, Phase 3) ---
    # Row-preserving pockets get a SEPARATE versioned manifest rather than reusing
    # aggregate grain_keys (spec §5.2): deployed model/version, exact row-population
    # definition + fingerprint, ordered materialised column IDs with physical
    # names/types/nullability, source semantic context, build/refresh id + edge
    # descriptors, and manifest hash. A pocket without this manifest keeps its
    # legacy routes but cannot accept a derived-expression route.
    #
    # SECURITY-LOAD-BEARING since Bug-8018/Bug-8393 — no longer descriptive, and
    # pockets ARE routable under active row-level security when it proves them
    # safe. ``row_manifest["columns"]`` is the authoritative record of the output
    # columns the built pocket table exposes; the query-router serves a pocket
    # under RLS ONLY when every security dimension column appears there (matched
    # EXACTLY, case-sensitive, as ``logical_name or physical_column``). Written by
    # ``shared/pocket/row_manifest.write_pocket_row_manifest`` on every completed
    # pocket refresh, in the same transaction as ``active_refresh_run_id``.
    row_manifest: Mapped[Optional[dict]] = mapped_column(JSONB)
    # LIVE pointer to the pocket refresh run whose rows the manifest describes.
    # Snapshot-EXCLUDED + import-CLEARED, same contract as the aggregate pointer.
    # Unlike the aggregate pointer this is NOT an attribute-edge trust pointer: it
    # is advanced on EVERY completed pocket refresh and cleared on a failed one
    # (Bug-8393), which is what makes ``(status, active_refresh_run_id)`` a sound
    # generation stamp for the pocket's physical table (Bug-8392).
    active_refresh_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pocket_refresh_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    # F-013-03 / F-005-01 (Bug-8250): IMMUTABLE artifact-to-version binding, same
    # contract as the aggregate columns above. A fresh pocket built under a
    # previous model definition must NOT serve after a deploy/revert — the
    # matcher requires an exact match against the model's current
    # (deployed_version_id, deploy_epoch). Snapshot-EXCLUDED + import-CLEARED, so
    # a rehydrated pocket must rebuild before re-entry. NULL = never built for a
    # deployed version. Distinct from row_manifest: that one proves what the
    # build MATERIALISED (and gates row-security serving — see above); these
    # columns are the mandatory, always-written, indexed VERSION gate.
    built_for_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    built_for_epoch: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
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
    # Bug-7359: active_refresh_run_id adds a SECOND FK path between
    # pocket_definitions and pocket_refresh_runs; name the historical-runs FK.
    refresh_runs: Mapped[list[PocketRefreshRun]] = relationship(
        back_populates="pocket", cascade="all, delete-orphan",
        foreign_keys="PocketRefreshRun.pocket_definition_id",
    )
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

    pocket: Mapped[PocketDefinition] = relationship(
        back_populates="refresh_runs",
        foreign_keys=[pocket_definition_id],
    )


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
# Named Queries (governed modeler-authored semantic queries)
# ---------------------------------------------------------------------------

class NamedQuery(TenantBase):
    """A governed, named semantic query against a base model.

    The DEFINITION half of a Named Query: model-bound logical SQL
    (``definition_sql``, never raw dialect SQL), a derived output-column
    schema, a serving shape, caps, and ownership. Serialised into the
    deployed snapshot (invariant 7). The MATERIALISATION half lives on
    :class:`NamedQueryArtifact`, on the shared artifact substrate — it is NOT
    a pocket or aggregate subtype.

    ``shape`` is derived at validate time: ``projection`` (row-slice; no
    GROUP BY, no aggregate function in the projection) or ``aggregated``.
    It drives which existing security proof is reused at serve time.
    """

    __tablename__ = "named_queries"
    __table_args__ = (
        Index(
            "uq_named_queries_model_lower_name",
            "model_id",
            func.lower(text("name")),
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text)
    display_folder: Mapped[Optional[str]] = mapped_column(String(255))
    definition_sql: Mapped[str] = mapped_column(Text, nullable=False)
    # [{"name": str, "type": "string|number|boolean|date|timestamp"}] —
    # derived at validate time from the bound select list. The authoritative
    # physical column types are recorded in the artifact's row_manifest on
    # every completed refresh.
    output_columns: Mapped[Optional[list]] = mapped_column(JSONB)
    shape: Mapped[str] = mapped_column(String(16), nullable=False, default="projection")
    row_cap: Mapped[Optional[int]] = mapped_column(Integer)
    column_cap: Mapped[Optional[int]] = mapped_column(Integer)
    certification_status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    created_by: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    model: Mapped["Model"] = relationship(back_populates="named_queries")
    artifact: Mapped[Optional["NamedQueryArtifact"]] = relationship(
        back_populates="named_query", cascade="all, delete-orphan", uselist=False
    )
    refresh_policy_row: Mapped[Optional["NamedQueryRefreshPolicy"]] = relationship(
        back_populates="named_query", cascade="all, delete-orphan", uselist=False
    )
    refresh_runs: Mapped[list["NamedQueryRefreshRun"]] = relationship(
        back_populates="named_query", cascade="all, delete-orphan",
        foreign_keys="NamedQueryRefreshRun.named_query_id",
    )


class NamedQueryArtifact(TenantBase):
    """The materialisation half of a Named Query (shared artifact substrate).

    Mirrors the :class:`PocketDefinition` artifact columns: physical result
    table on the target, an output-column manifest, target/build/version
    bindings, lifecycle status and refresh state. Lifecycle CHECK is the same
    ``fresh|stale|invalidating|failed`` set as pockets.
    """

    __tablename__ = "named_query_artifacts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('fresh', 'stale', 'invalidating', 'failed')",
            name="ck_named_query_artifacts_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    named_query_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("named_queries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("data_targets.id"), nullable=False, index=True)
    physical_table_name: Mapped[str] = mapped_column(String(512), nullable=False)
    target_schema: Mapped[Optional[str]] = mapped_column(String(255))
    # Output-column manifest, same shape as the pocket row manifest
    # (``shared/semantic/artifact_manifest.RowManifest``). SECURITY-LOAD-BEARING:
    # the query-router serves a projection-shaped Named Query to an RLS
    # principal ONLY when every security dimension column appears in
    # ``row_manifest.columns`` (matched case-sensitively).
    row_manifest: Mapped[Optional[dict]] = mapped_column(JSONB)
    row_count: Mapped[Optional[int]] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="stale")
    failure_reason: Mapped[Optional[str]] = mapped_column(Text)
    active_refresh_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("named_query_refresh_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_refresh_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    retired_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ)
    # Immutable artifact-to-version binding — same contract as pocket/aggregate
    # columns. A fresh artifact built under a previous model definition must
    # not serve after a deploy/revert. Snapshot-EXCLUDED as a live pointer;
    # the snapshot carries only the artifact identity pointer.
    built_for_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    built_for_epoch: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    named_query: Mapped[NamedQuery] = relationship(back_populates="artifact")
    target: Mapped[DataTarget] = relationship()


class NamedQueryRefreshPolicy(TenantBase):
    """1:1 schedule policy for a Named Query (mirror PocketRefreshPolicy).

    v1 policy types: ``schedule`` (cron) and ``manual``. ``is_enabled`` lives
    here, not on the definition, so "query exists" and "schedule active" stay
    independent.
    """

    __tablename__ = "named_query_refresh_policies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    named_query_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("named_queries.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    cron_expression: Mapped[Optional[str]] = mapped_column(String(128))
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())

    named_query: Mapped[NamedQuery] = relationship(back_populates="refresh_policy_row")


class NamedQueryRefreshRun(TenantBase):
    """Run history for a Named Query refresh (mirror PocketRefreshRun)."""

    __tablename__ = "named_query_refresh_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    named_query_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("named_queries.id", ondelete="CASCADE"), nullable=False,
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

    named_query: Mapped[NamedQuery] = relationship(
        back_populates="refresh_runs",
        foreign_keys=[named_query_id],
    )


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

    Enforcement (F-007-01) injects the compiled predicate into the ``WHERE`` of
    EVERY ``SELECT`` that scans a physical table -- each UNION branch, scalar
    subquery, subquery-first FROM and CTE body -- so it always applies before
    any ``LIMIT``. It is NOT the outer ``SELECT * FROM (<planned>) AS __ts_sec``
    subquery wrap this docstring used to describe; two query-router tests assert
    that alias is absent from the rewritten query.

    An active rule does NOT disable the aggregate and pocket matchers
    (Bug-7033 / Bug-8018, corrected here by Bug-8397). Each candidate must
    instead PROVE it can carry the same predicate -- an aggregate needs every
    security dimension column in its grain
    (``router._aggregate_is_rls_safe``), a pocket needs a row-preserving
    ``SELECT *`` whose ``row_manifest`` records every security column, matched
    case-sensitively (``router._pocket_is_rls_safe``) -- and anything unproven
    routes to source with the predicate injected there. A shape that cannot be
    proved fully constrained is rejected, never run unfiltered.
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
    # Bug-6426: how this row's result was served.
    #   "live"      — the route (source/aggregate/pocket) actually executed and
    #                 its measured execution_ms / bytes_processed are real.
    #   "cache_hit" — the result was re-served from the in-TTL result cache; no
    #                 route executed, so execution_ms / bytes_processed are 0 and
    #                 are NOT real measurements.
    # ``route_type`` still records the ORIGINAL route the cached value took (so a
    # cached aggregate hit keeps route_type="aggregate" for volume/top-user
    # analytics), but acceleration-rate and cost-savings rollups MUST separate
    # cache_hit rows from live acceleration — a cache re-serve is not a new
    # acceleration event and its zeros must never be averaged into savings.
    # NULL is treated as "live" for backfilled historical rows.
    cache_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, index=True)
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
    # Bug-8071: this row is a ROLLUP keyed on the literal-free fingerprint, and
    # the conflict-update OVERWRITES ``miss_reason`` on every repeat. One row was
    # therefore serving as both cumulative candidate state and event history,
    # and only the last event survived: a pattern that missed 400 times for a
    # missing grain and 3 times for staleness read as "stale". A modeller could
    # not prove why a query kept missing, and the optimizer could not tell an
    # ABSENT aggregate (build one) from an existing-but-unservable one (a new
    # aggregate fixes nothing).
    #
    # Bounded per-reason history: reason entries plus an optional
    # ``{"class_totals": {"build": n, "repair": n, "ineligible": n}, ...}``
    # summary preserving remediation counts for evicted or legacy events.
    # ``miss_reason`` KEEPS its meaning (the most recent reason) so existing
    # readers are unaffected. NULL means "no history recorded yet"; consumers
    # fall back to ``miss_reason`` (see shared/miss_reason_taxonomy.py).
    miss_reason_counts_json: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
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
    # Bug-7453: when both related_object_type and related_object_id are NULL
    # (model-wide alerts), the NULLS NOT DISTINCT dedup index collapses
    # distinct alerts of the same category. detail_hash discriminates by
    # content so e.g. two different "refresh_failure" reasons stay separate.
    detail_hash: Mapped[Optional[str]] = mapped_column(String(64))
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
    role: Mapped[str] = mapped_column(String(32), nullable=False)  # admin | modeler | viewer | model_viewer
    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    model_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), index=True)
    # Bug-6303: provenance of the binding, used to make SSO group grants
    # revocable without ever disturbing manual grants.
    #   "manual"    (default) — granted via the access API, project import, or
    #                any pre-existing row (server_default). NEVER touched by the
    #                SSO group sync.
    #   "sso_group"           — materialised from an IdP group-role mapping on
    #                SSO login. Reconciled on every login: revoked when the user
    #                is de-provisioned from the mapped IdP group.
    # New/legacy rows default to "manual" so no historical grant is ever
    # auto-revoked when this column is introduced (fail-closed).
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="manual", server_default="manual"
    )
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
    # Bug-6597: provenance of the CURRENT ``role`` value.
    #   "manual" — set by an operator (user-management API) or a pre-existing
    #              row via the server_default. NEVER auto-downgraded by SSO.
    #   "sso"    — assigned by the JIT/SSO group-mapping machinery. An SSO-
    #              elevated ``tenant_admin`` carrying this source is reconciled
    #              DOWN when its IdP admin group disappears (jit_adopt_user).
    # Fail-closed default is "manual" so a manually-promoted admin is never
    # silently demoted, and legacy rows are treated as operator intent.
    role_source: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="manual"
    )
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    has_completed_onboarding: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now(), onupdate=func.now())


class PersonalAccessToken(TenantBase):
    """Personal Access Token (PAT) for BI-client authentication (Bug-7314).

    SSO (SAML/OIDC) users have no password to present to a JDBC/XMLA client.
    A PAT is a long-lived bearer secret the user mints in the web UI and pastes
    as the PASSWORD in Excel (XMLA Basic) / Power BI (PostgreSQL :5433). The
    plaintext token is shown ONCE at creation; only ``token_hash`` (a bcrypt
    hash — never the plaintext) and a short ``token_prefix`` for lookup/display
    are stored. Validation resolves the token to its owning ``local_users`` row;
    tenant + role are taken LIVE from that row at each use, never stamped onto
    the token, so a role change or deactivation takes effect immediately
    (subject to the gateway's short session-validation TTL).
    """
    __tablename__ = "personal_access_tokens"
    __table_args__ = (
        Index("ix_personal_access_tokens_user_id", "user_id"),
        Index("ix_personal_access_tokens_token_prefix", "token_prefix"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("local_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # bcrypt hash of the full plaintext token. Never store the plaintext.
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # Public, non-secret lookup key: the fixed scheme prefix plus a short random
    # public id (e.g. "tesspat_ab12cd34"). Indexed so verification narrows to a
    # small candidate set before the constant-time bcrypt compare. NOT a secret
    # on its own — it never authenticates without the full token's hash match.
    token_prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False, server_default="")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    expires_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, nullable=True)


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
    # Bug-8034: durable worker claim fields — which scheduler process claimed
    # this run out of the queue, and when. NULL while the run is ``queued``;
    # set by the dispatcher's committed ``queued -> running`` flip, and left in
    # place after the run terminates as the audit record of who dispatched it.
    # Cleared only when a failed hand-off returns the run to the queue.
    # Spec: docs/architecture/architecture_ai-advisor-durable-dispatch.md.
    claimed_by: Mapped[Optional[str]] = mapped_column(String(256), nullable=True, default=None)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, nullable=True, default=None)
    recommendations_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    aggregates_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    aggregates_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # F-011-04: provider-reported usage for per-run spend attribution. Populated
    # from the adapter's last_usage after the provider call; NULL when the run
    # failed before the call or the provider reported no usage.
    input_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer)
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
    # Phase 8.C.1 — X2: when true, the Query Router skips row-security
    # filtering entirely for any execution bound to this persona -- no rule is
    # compiled and no predicate is injected, so the aggregate and pocket fast
    # paths become available unconditionally rather than only to the candidates
    # that can prove they carry the predicate. (Bug-8397: there is no outer
    # subquery to skip -- enforcement is per-scan WHERE injection.)
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
    __table_args__ = (
        # judge_mode is a two-value enum: "sync" (validated-first, the default —
        # the verdict resolves before the answer is exposed) or "async" (answer
        # shown, then validated). The API already constrains it
        # (agent_config.py, pattern ^(async|sync)$), but the column is the
        # producer/consumer boundary for the snapshot serialiser, the rehydrator
        # and guardrails/block._should_block, so a DB-level guard stops any
        # out-of-band writer persisting a value those consumers cannot interpret.
        # See migration 0208.
        CheckConstraint(
            "judge_mode IN ('sync', 'async')",
            name="ck_project_agent_configs_judge_mode",
        ),
    )

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
    # Bug-8411 — which agent events this project's webhook subscribes to.
    # Values are validated against shared/webhooks/agent_event_types.py;
    # ``["*"]`` (the default, and the behaviour every project had before
    # filters existed) means every event. Nullable on purpose: a row written
    # before this column existed reads as NULL and
    # ``agent_event_subscribed`` treats NULL as "deliver everything", so no
    # existing receiver silently stops getting events on upgrade.
    webhook_event_filters: Mapped[Optional[list]] = mapped_column(
        JSONB, server_default=text("'[\"*\"]'::jsonb")
    )
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
        # F-023-29 / Bug-8148 — the DEFAULT is validated-first ("sync"): the
        # judge verdict is resolved BEFORE the answer is exposed, so an
        # unvetted answer is never shown by default. "async" (answer shown,
        # then validated) remains an explicit per-project lower-assurance
        # override. See migration 0178 and
        # docs/questions/questions_f023-29-default-judge-mode.md.
        default="sync",
        server_default=text("'sync'"),
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
    # Bug-6521 — per-send idempotency key. Set on the reserved placeholder so a
    # retried stream/sync POST carrying the same key dedupes to the existing
    # turn instead of minting a duplicate. NULL for keyless callers (multiple
    # NULLs allowed via the partial unique index below).
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), index=True
    )

    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "turn_index",
            name="uq_agent_turns_conversation_turn_index",
        ),
        # Bug-6521 — atomic dedup anchor: one turn per (conversation, key).
        # Partial so NULL keys (keyless callers) are exempt and never collide.
        Index(
            "uq_agent_turns_conversation_idempotency_key",
            "conversation_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
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
    # Bug-8350 — the raw destination URL is never persisted: it commonly
    # embeds bearer tokens / API keys in userinfo, query params, OR path
    # segments (the reported repro used a path segment), and this table is
    # copied into backups and returned verbatim by GET /dlq. `target_host` is
    # a sanitised `scheme://host[:port]` hint only -- path is dropped too
    # (see shared.webhooks.redact.redact_url_for_display). A manual retry
    # reloads the live URL from ProjectAgentConfig, so the plaintext is never
    # needed again once a row exists here.
    #
    # Bug-8407 -- there used to be a `target_url_hash` column here too,
    # described as "a one-way fingerprint of the full URL for
    # dedup/correlation". It was first an unsalted SHA-256 (offline-brute-
    # forceable: 42,001 candidates in 0.07s), then hardened to bcrypt. Both
    # versions shared the real defect: NOTHING ever read the column. Salted
    # bcrypt cannot correlate rows, so it could not serve the stated purpose
    # either. It was a persisted, per-row derivative of a secret-bearing URL
    # that bought the product nothing and cost a ~0.3s bcrypt call on every
    # DLQ write. Dropped in migration 0188; coarse correlation uses
    # `target_host`. Do not reintroduce a hash here without a reader.
    target_host: Mapped[Optional[str]] = mapped_column(Text)
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
    # F-022-06: encrypted snapshot of the endpoint signing secret captured when
    # this delivery was enqueued. Retries sign with THIS pinned secret, not the
    # endpoint's current one, so rotating the endpoint secret never invalidates
    # a queued (in-flight) delivery's signature. NULL only for legacy rows
    # created before this column existed — those fall back to the endpoint's
    # current secret exactly as before.
    signing_secret_snapshot: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    # Bug-8557: the destination URL snapshotted at enqueue time (migration
    # 0202). The dispatcher reads THIS frozen URL, never the live
    # `endpoint.url`: `shared/webhooks/dispatcher.py::rebuild_signed_body`
    # returns it, and every dispatch site
    # (`_dispatch_queued_deliveries`, `drain_pending_deliveries`,
    # `model-service/src/api/webhooks.py::retry_dlq`) sends to it. Pinning the
    # secret without the destination was incoherent: an admin repointing an
    # endpoint at receiver B sent rows queued for A to B, carrying A's payload
    # under A's pinned secret — and, since a URL change also rotates the
    # secret, handing B an HMAC computed under a key B does not hold.
    # NULL only for rows enqueued before this column existed. Those are NOT
    # sent to a guessed URL: they fail to the DLQ on dispatch with
    # `INCOHERENT_DELIVERY_ROW_REASON` (reason token
    # `incoherent_delivery_row`). A manual DLQ retry re-pins the destination
    # from the endpoint and is the recovery path for them, because a manual
    # retry IS an explicit operator decision about where to send.
    destination_url_snapshot: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
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


class EmbedTokenMint(TenantBase):
    """Ledger of issued embed tokens (F-021-08). Revocation still uses
    ``tess_system.revoked_embed_tokens``; this row is the tenant inventory.
    """

    __tablename__ = "embed_token_mints"

    jti: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    actor_email: Mapped[str] = mapped_column(String(255), nullable=False)
    user_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    persona_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    project_ids: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    model_ids: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    capabilities: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMPTZ, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())


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
    # Breach-episode tracking (B17 round 2, Finding 1): one episode per model
    # per UTC day. Bug-8146 split the two markers below because they answer
    # different questions and one of them used to answer both wrongly.
    #
    # ``last_breach_alerted_on`` is DELIVERY EVIDENCE: the day at least one
    # notification channel confirmed it accepted this model's breach alert.
    # It used to be stamped unconditionally on the first breach observation,
    # so a dispatch in which every channel failed still recorded the breach as
    # alerted and nobody was ever told. It now gates re-attempt: the SLA
    # monitor re-dispatches on each sweep of the open episode until this is
    # set, and stops once it is.
    last_breach_alerted_on: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    # ``last_breach_episode_opened_on`` (migration 0203) is EPISODE STATE: the
    # day the episode opened, stamped on the first breach observation whatever
    # the alert flag and whatever the delivery outcome. It is what dedups the
    # episode, so one breach never opens a second episode on the same day.
    # Written and read by `services/scheduler/src/jobs/sla_monitor.py`; rows
    # written before 0203 carry NULL and fall back to
    # ``last_breach_alerted_on`` there so an episode in flight across an
    # upgrade is not re-alerted.
    last_breach_episode_opened_on: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
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


class NotificationDelivery(TenantBase):
    """Durable, operator-visible record of one email/Slack notification attempt.

    Bug-8053 (F-022-04): before this table the alerting dispatcher persisted
    routes and dedup claims but NO record of whether a notification actually
    reached its destination. An email or Slack send that failed left only an
    application-log line — invisible in the product, impossible for an operator
    to see, retry, or prove. A broken SMTP/Slack configuration could fail
    indefinitely with nothing surfaced.

    One row is written per delivery attempt with a terminal outcome:
    ``status='sent'`` for a genuine successful send, ``status='failed'`` for a
    send that raised or a channel that was skipped because it was misconfigured
    (SMTP unset, no recipients, no webhook URL). ``target`` is a NON-SECRET
    destination hint — joined recipients for email, a non-reversible hash of the
    webhook URL for Slack (mirroring the dedup key, never the plaintext secret).
    Exposed to operators through the tenant-scoped notification-deliveries API.
    """

    __tablename__ = "notification_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Keep the record even if the route is later deleted (SET NULL): a failed
    # delivery is evidence that must outlive the route configuration.
    route_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("notification_routes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    channel_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # Non-secret destination hint (email recipients / hashed Slack URL).
    target: Mapped[Optional[str]] = mapped_column(Text)
    # 'sent' | 'failed' — 'sent' ONLY on a genuine successful send.
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, server_default=func.now(), nullable=False, index=True
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
    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "tessallite_object_type",
            "tessallite_object_id",
        ),
    )

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
    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "tessallite_object_type",
            "tessallite_object_id",
            "collibra_resource_type",
        ),
    )

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

    # Bug-7515: match SolidatusObjectMapping's connection relationship.
    connection: Mapped[CollibraConnection] = relationship()
