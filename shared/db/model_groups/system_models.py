"""System-level ORM models (tess_system schema).

Grouped from ``shared/db/models.py`` for organizational clarity.
All models are still fully defined in ``models.py`` for backwards compatibility.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, String, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TIMESTAMPTZ = TIMESTAMP(timezone=True)


class SystemBase(DeclarativeBase):
    """Base for system-DB tables (tess_system schema)."""
    pass


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
