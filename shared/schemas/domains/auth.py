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


class EmbedRlsSubject(BaseModel):
    """Admin-authored row-security subject carried in a signed embed token.

    Bug-7995 / F-024-01: embed tokens previously carried no RLS role/group/claim,
    so attribute-based (``idp_group`` / ``saml_claim`` / ``oidc_scope``) and
    role-based (``jwt_role``) row-security rules could never fire for an embedded
    subject. The tenant admin who mints the token sets this subject deliberately;
    the embedded end user never supplies or widens it (it travels inside the JWT
    signature). It only ever NARROWS the rows returned — it feeds row-security rule
    matching, which can add restricting predicates or deny rows, never grant access
    outside the token's model/project/persona scope.

    Fields mirror the interactive access-token claim names (``role`` / ``groups`` /
    ``claims``) so a single decode path serves both. All fields are optional; an
    omitted subject on a role-governed model fails closed (deny-all) downstream.
    """
    role: str | None = Field(
        default=None, max_length=64,
        description="Effective row-security role for jwt_role rules",
    )

    @field_validator("role")
    @classmethod
    def reject_sentinel_role(cls, v: str | None) -> str | None:
        # Opus review hardening: the literal string "embed" is the
        # EMBED_ROLE_SENTINEL used internally when no RLS role is set. A token
        # carrying rls.role="embed" would be indistinguishable from the sentinel
        # and silently dropped — fail loud at mint so the admin fixes the typo.
        if v is not None and v.strip().lower() == "embed":
            raise ValueError(
                'role "embed" is reserved; use the actual IdP/JWT role name '
                "(e.g. the role string configured in the model's row-security rules)"
            )
        return v
    groups: list[str] | None = Field(
        default=None,
        description="IdP group names for idp_group row-security rules",
    )
    claims: dict[str, Any] | None = Field(
        default=None,
        description=(
            "SAML attributes / OIDC claims+scopes for saml_claim / oidc_scope "
            "row-security rules (claim_name -> string or list of strings)"
        ),
    )

    @field_validator("groups")
    @classmethod
    def reject_empty_groups(cls, v: list[str] | None) -> list[str] | None:
        # Bug-7995: an empty list must not be read as "unrestricted" — reject the
        # ambiguous [] the same way scope lists are rejected. Pass null to omit.
        if v is not None and len(v) == 0:
            raise ValueError(
                "Pass null to omit groups, or a non-empty list of group names"
            )
        return v

    @field_validator("claims")
    @classmethod
    def reject_empty_claims(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is not None and len(v) == 0:
            raise ValueError(
                "Pass null to omit claims, or a non-empty claim map"
            )
        return v

    @model_validator(mode="after")
    def reject_all_empty(self):
        # A subject object that carries no role, no groups, and no claims is a
        # mint mistake — reject it rather than sign a meaningless subject.
        if self.role is None and self.groups is None and self.claims is None:
            raise ValueError(
                "rls subject must set at least one of role, groups, or claims"
            )
        return self


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
        description=(
            "Allowed capabilities: query, chat, explore. Omit or null is deny-all "
            "(no capabilities). Pass an explicit list to grant; [] is also deny-all."
        ),
    )
    rls: EmbedRlsSubject | None = Field(
        default=None,
        description=(
            "Row-security subject (role/groups/claims) applied to this embedded "
            "session so attribute/role RLS rules fire. Omit to leave the session "
            "without an RLS subject (a role-governed model then denies all rows)."
        ),
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

    @field_validator("project_ids", "model_ids")
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
    rls: EmbedRlsSubject | None = None
    expiry_minutes: int


class EmbedTokenResponse(BaseModel):
    token: str
    expires_at: str
    scope: EmbedTokenScope


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str | None = None


# ---------------------------------------------------------------------------
# Personal Access Tokens (Bug-7314) — BI-client auth for SSO users
# ---------------------------------------------------------------------------

# Cap the caller-supplied lifetime. 0/omitted => non-expiring (until revoked).
_PAT_MAX_EXPIRES_DAYS = 365


class PersonalAccessTokenCreate(BaseModel):
    """Request to mint a new PAT for the authenticated user."""
    label: str = Field(
        default="", max_length=255,
        description="Human label to identify this token (e.g. 'Excel laptop').",
    )
    expires_in_days: int | None = Field(
        default=None, ge=1, le=_PAT_MAX_EXPIRES_DAYS,
        description=(
            "Optional lifetime in days (1..365). Omit for a non-expiring token "
            "(revocable at any time)."
        ),
    )

    @field_validator("label")
    @classmethod
    def _strip_label(cls, v: str) -> str:
        return (v or "").strip()


class PersonalAccessTokenResponse(OrmBase):
    """PAT metadata — never carries the plaintext token or its hash."""
    id: uuid.UUID
    label: str
    token_prefix: str
    created_at: datetime
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class PersonalAccessTokenCreateResponse(BaseModel):
    """Returned once at creation. ``token`` is the ONLY time the plaintext is
    ever available; it is not retrievable afterwards."""
    token: str = Field(description="The plaintext PAT — shown once, store it now.")
    pat: PersonalAccessTokenResponse


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


import re as _re

_PASSWORD_MIN_LENGTH = 12
_PASSWORD_PATTERN_UPPER = _re.compile(r"[A-Z]")
_PASSWORD_PATTERN_LOWER = _re.compile(r"[a-z]")
_PASSWORD_PATTERN_DIGIT = _re.compile(r"[0-9]")


def _validate_password(value: str) -> str:
    """Bug-5733: enforce server-side password complexity.

    Minimum 12 characters, at least one uppercase letter, one lowercase
    letter, and one digit.
    """
    if len(value) < _PASSWORD_MIN_LENGTH:
        raise ValueError(
            f"Password must be at least {_PASSWORD_MIN_LENGTH} characters long"
        )
    if not _PASSWORD_PATTERN_UPPER.search(value):
        raise ValueError("Password must contain at least one uppercase letter")
    if not _PASSWORD_PATTERN_LOWER.search(value):
        raise ValueError("Password must contain at least one lowercase letter")
    if not _PASSWORD_PATTERN_DIGIT.search(value):
        raise ValueError("Password must contain at least one digit")
    return value


class UserCreate(BaseModel):
    username: str
    email: str
    password: str
    role: Optional[str] = None

    @field_validator("password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        return _validate_password(v)

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

    # Bug-6667: reject explicit null for NOT NULL columns. Without these,
    # a PATCH body like {"role": null} passes schema validation and the
    # handler writes NULL into the NOT NULL local_users column, causing a
    # 500 (IntegrityError) instead of a clean 422.
    @model_validator(mode="after")
    def _reject_null_not_null_fields(self):
        """Reject explicit null for fields that map to NOT NULL DB columns."""
        raw = self.__pydantic_fields_set__
        if "role" in raw and self.role is None:
            raise ValueError("role cannot be null; omit the field to leave it unchanged")
        if "is_active" in raw and self.is_active is None:
            raise ValueError("is_active cannot be null; omit the field to leave it unchanged")
        if "username" in raw and self.username is None:
            raise ValueError("username cannot be null; omit the field to leave it unchanged")
        if "email" in raw and self.email is None:
            raise ValueError("email cannot be null; omit the field to leave it unchanged")
        return self


class UserPasswordReset(BaseModel):
    password: str

    @field_validator("password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        return _validate_password(v)


class UserResponse(OrmBase):
    id: uuid.UUID
    username: str
    email: str
    is_active: bool
    role: str
    auth_source: str = "local"
    # Bug-6597: provenance of ``role`` ("manual" operator-set / "sso" IdP-derived).
    # Read-only; surfaced so an admin UI can distinguish an SSO-elevated admin
    # (auto-revocable) from a manually-promoted one.
    role_source: str = "manual"
    token_version: int = 0
    has_completed_onboarding: bool = False
    created_at: datetime
