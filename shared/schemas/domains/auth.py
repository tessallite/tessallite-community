"""Auto-split from pydantic_models.py — Auth"""
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
# Auth
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    tenant_id: str = Field(description="Tenant slug -- routes to the correct tenant DB")
    email: str
    password: str


class SystemLoginRequest(BaseModel):
    email: str
    password: str


EMBED_CAPABILITIES = ("query", "chat", "explore")


class EmbedTokenRequest(BaseModel):
    tenant_id: str = Field(description="Target tenant slug")
    user_identity: str = Field(
        min_length=1, max_length=255,
        description="Display name for audit trail (e.g. end-user email or name)",
    )
    persona_id: str | None = Field(
        default=None, description="Lock session to this persona's permissions",
    )
    project_ids: list[str] | None = Field(
        default=None, description="Restrict access to these project IDs only",
    )
    model_ids: list[str] | None = Field(
        default=None, description="Restrict visible models to this list",
    )
    capabilities: list[str] | None = Field(
        default=None,
        description="Allowed capabilities: query, chat, explore. Default: all",
    )
    expiry_minutes: int = Field(
        default=180, ge=5, le=1440,
        description="Token lifetime in minutes (default 180, max 1440)",
    )

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        invalid = [c for c in v if c not in EMBED_CAPABILITIES]
        if invalid:
            raise ValueError(
                f"Invalid capabilities: {invalid}. "
                f"Allowed: {list(EMBED_CAPABILITIES)}"
            )
        return v

    @field_validator("project_ids", "model_ids", "capabilities")
    @classmethod
    def reject_empty_scope_lists(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and len(v) == 0:
            raise ValueError(
                "Pass null to leave unrestricted, or a non-empty list to restrict scope"
            )
        return v


class EmbedTokenScope(BaseModel):
    tenant_id: str
    user_identity: str
    persona_id: str | None = None
    project_ids: list[str] | None = None
    model_ids: list[str] | None = None
    capabilities: list[str]
    expiry_minutes: int


class EmbedTokenResponse(BaseModel):
    token: str
    expires_at: str
    scope: EmbedTokenScope


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str | None = None


# Single source of truth for the grantable role vocabulary (H-1: the
# model_technical audience role must be grantable through this API).
from shared.auth.roles import ALLOWED_LOCAL_USER_ROLES as _ALLOWED_LOCAL_USER_ROLES


def _validate_local_user_role(value: str | None) -> str | None:
    if value is None:
        return value
    if value not in _ALLOWED_LOCAL_USER_ROLES:
        raise ValueError(
            f"role must be one of {_ALLOWED_LOCAL_USER_ROLES}, got {value!r}"
        )
    return value


class UserCreate(BaseModel):
    username: str
    email: str
    password: str
    role: Optional[str] = None

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: str | None) -> str | None:
        return _validate_local_user_role(v)


class UserUpdate(BaseModel):
    username: str | None = None
    email: str | None = None
    is_active: bool | None = None
    role: Optional[str] = None

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: str | None) -> str | None:
        return _validate_local_user_role(v)


class UserPasswordReset(BaseModel):
    password: str


class UserResponse(OrmBase):
    id: uuid.UUID
    username: str
    email: str
    is_active: bool
    role: str
    auth_source: str = "local"
    has_completed_onboarding: bool = False
    created_at: datetime


