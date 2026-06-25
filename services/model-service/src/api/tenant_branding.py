"""Tenant branding — custom theme per tenant (logo, colours, font)."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator

from shared.config.resolver import get_setting, set_setting
from shared.db.session import get_tenant_db
from src.auth.middleware import CurrentUser, get_current_user, require_tenant_admin

router = APIRouter(prefix="/tenants/{tenant_id}/branding", tags=["branding"])

_BRANDING_KEYS = [
    "branding.logo_url",
    "branding.primary_color",
    "branding.secondary_color",
    "branding.font_family",
    "branding.app_title",
]

# A 3- or 6-digit hex colour, e.g. ``#0B5FFF`` or ``#abc``.
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _validate_color(value: str | None, field: str) -> str | None:
    if value is None or value == "":
        return value
    if not _HEX_COLOR_RE.match(value):
        raise ValueError(f"{field} must be a hex colour like '#0B5FFF'")
    return value


def _validate_logo_url(value: str | None) -> str | None:
    """Accept an empty string (clears the logo), an absolute http(s) URL, or a
    same-origin path (``/...``). Reject anything else so a stored garbage value
    cannot silently render the default (F-029-13)."""
    if value is None or value == "":
        return value
    if value.startswith("/"):
        return value
    parsed = urlparse(value)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return value
    raise ValueError("logo_url must be an http(s) URL or an absolute path")


class BrandingConfig(BaseModel):
    logo_url: str | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    font_family: str | None = None
    app_title: str | None = None

    @field_validator("primary_color")
    @classmethod
    def _v_primary(cls, v: str | None) -> str | None:
        return _validate_color(v, "primary_color")

    @field_validator("secondary_color")
    @classmethod
    def _v_secondary(cls, v: str | None) -> str | None:
        return _validate_color(v, "secondary_color")

    @field_validator("logo_url")
    @classmethod
    def _v_logo(cls, v: str | None) -> str | None:
        return _validate_logo_url(v)


def _assert_own_tenant(tenant_id: str, current_user: CurrentUser) -> None:
    """The branding routes are keyed by a ``tenant_id`` path parameter, but the
    session always resolves to ``current_user.tenant_id``. Reject a mismatch so
    a request that names another tenant fails loud (403) rather than silently
    operating on the caller's own tenant (F-029-13)."""
    if tenant_id != current_user.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tenant_id does not match the authenticated tenant",
        )


@router.get("", response_model=BrandingConfig)
async def get_branding(
    tenant_id: str,
    current_user: CurrentUser = Depends(get_current_user),
) -> BrandingConfig:
    _assert_own_tenant(tenant_id, current_user)
    async for db in get_tenant_db(current_user.tenant_id):
        return BrandingConfig(
            logo_url=await get_setting("branding.logo_url", tenant_session=db),
            primary_color=await get_setting("branding.primary_color", tenant_session=db),
            secondary_color=await get_setting("branding.secondary_color", tenant_session=db),
            font_family=await get_setting("branding.font_family", tenant_session=db),
            app_title=await get_setting("branding.app_title", tenant_session=db),
        )
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.put("", response_model=BrandingConfig)
async def update_branding(
    tenant_id: str,
    body: BrandingConfig,
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> BrandingConfig:
    _assert_own_tenant(tenant_id, current_user)
    # Only fields the caller actually supplied are written; an explicit null or
    # empty string clears the stored setting (reverting to the theme default)
    # rather than being silently skipped (F-029-11).
    supplied = body.model_dump(exclude_unset=True)
    async for db in get_tenant_db(current_user.tenant_id):
        actor = current_user.email or "unknown"
        for field in (
            "logo_url",
            "primary_color",
            "secondary_color",
            "font_family",
            "app_title",
        ):
            if field in supplied:
                value = supplied[field] if supplied[field] is not None else ""
                await set_setting(
                    f"branding.{field}", value, actor=actor,
                    tenant_session=db, tenant_scope=True,
                )
        await db.commit()
        return body
    raise HTTPException(status_code=500, detail="DB session exhausted")
