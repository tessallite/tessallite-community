"""System-level configuration API.

Three endpoints, all guarded by ``require_system_admin``:

  GET  /system/settings              — registry + current effective values
  PUT  /system/settings/{key}        — write a single value (validated)
  GET  /system/settings/restart-pending — list outstanding restart-required writes
  POST /system/settings/restart-pending/clear — drop pending entries (used after restart)
  GET  /system/settings/bootstrap    — read-only mirror of bootstrap env values

The GET endpoint returns one JSON object per key with the registry
metadata (type, default, description, restart_required, section) plus the
currently stored value, so the UI can render forms purely off the
response payload.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config.bootstrap import refresh_system_snapshot, update_snapshot
from shared.config.registry import get_def, surfaced_for_level
from shared.config.resolver import (
    clear_pending_restart,
    get_setting,
    list_pending_restart,
    set_setting,
)
from shared.config.settings import get_settings
from shared.db.session import get_system_db
from src.auth.middleware import CurrentUser, require_system_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/system/settings", tags=["system-settings"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class SettingItem(BaseModel):
    key: str
    section: str
    type: str
    description: str
    restart_required: bool
    default: Any
    value: Any
    env_var: Optional[str] = None
    sensitive_display: bool = False
    label: Optional[str] = None
    ui_help: Optional[str] = None
    ui_group: Optional[str] = None
    ui_control: Optional[str] = None
    ui_choices: Optional[list] = None
    unit: Optional[str] = None


class SettingsListResponse(BaseModel):
    items: list[SettingItem]


class SettingWrite(BaseModel):
    value: Any


class RestartPendingItem(BaseModel):
    setting_key: str
    written_at: Optional[str]
    written_by: Optional[str]


class BootstrapItem(BaseModel):
    name: str
    value: str
    sensitive: bool
    description: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("", response_model=SettingsListResponse)
async def list_system_settings(
    sys_db: AsyncSession = Depends(get_system_db),
    _admin: CurrentUser = Depends(require_system_admin),
) -> SettingsListResponse:
    """Return every surfaced system-level setting with its registry metadata and effective value.

    Legacy keys flagged ``surfaced=False`` (xmla.*, llm.* fallbacks, pocket/predictive
    system fallbacks, source_db.* / agg_target.* / spark.thrift_*) are intentionally
    excluded from the public listing — they live in the registry only so existing
    call-sites still resolve via fallback.
    """
    items: list[SettingItem] = []
    for d in surfaced_for_level("system"):
        try:
            value = await get_setting(d.key, system_session=sys_db)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("settings list: failed to resolve %s: %s", d.key, exc)
            value = d.default
        items.append(
            SettingItem(
                key=d.key,
                section=d.section,
                type=d.type,
                description=d.description,
                restart_required=d.restart_required,
                default=d.default,
                value=value,
                env_var=d.env_var,
                sensitive_display=d.sensitive_display,
                label=d.label,
                ui_help=d.ui_help,
                ui_group=d.ui_group,
                ui_control=d.ui_control,
                ui_choices=d.ui_choices,
                unit=d.unit,
            )
        )
    items.sort(key=lambda it: (it.ui_group or it.section, it.label or it.key))
    return SettingsListResponse(items=items)


@router.put("/{key}")
async def write_system_setting(
    key: str,
    body: SettingWrite = Body(...),
    sys_db: AsyncSession = Depends(get_system_db),
    admin: CurrentUser = Depends(require_system_admin),
) -> dict:
    """Write one system-level setting. Validates against the registry."""
    try:
        d = get_def(key, "system")
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown system setting: {key!r}",
        )
    if d.env_var:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{key!r} is a read-only bootstrap setting (sourced from env)",
        )
    if not d.surfaced:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{key!r} is not editable through the System Configuration UI",
        )
    try:
        await set_setting(
            key, body.value,
            actor=admin.email or admin.user_id,
            system_session=sys_db,
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    # Update in-process snapshot so the new value takes effect immediately
    # for non-restart-required keys. Restart-required keys still need a
    # service restart to bind into APScheduler / middleware / port listeners.
    update_snapshot(key, body.value)

    return {"status": "ok", "key": key, "restart_required": d.restart_required}


@router.get("/restart-pending", response_model=list[RestartPendingItem])
async def get_restart_pending(
    sys_db: AsyncSession = Depends(get_system_db),
    _admin: CurrentUser = Depends(require_system_admin),
) -> list[RestartPendingItem]:
    rows = await list_pending_restart(sys_db)
    return [RestartPendingItem(**r) for r in rows]


@router.post("/restart-pending/clear")
async def clear_restart_pending_endpoint(
    sys_db: AsyncSession = Depends(get_system_db),
    _admin: CurrentUser = Depends(require_system_admin),
) -> dict:
    """Drop all pending-restart entries — call after a service restart."""
    await clear_pending_restart(sys_db)
    await refresh_system_snapshot()
    return {"status": "ok"}


@router.get("/bootstrap", response_model=list[BootstrapItem])
async def get_bootstrap_view(
    _admin: CurrentUser = Depends(require_system_admin),
) -> list[BootstrapItem]:
    """Return the read-only bootstrap fields sourced from .env.

    Secrets are masked; non-secret fields are displayed verbatim so the
    operator can verify what's in effect without SSHing into the host.
    """
    s = get_settings()

    def mask(v: str) -> str:
        if not v:
            return "(empty)"
        return f"***** ({len(v)} chars)"

    items = [
        BootstrapItem(
            name="SYSTEM_DATABASE_URL",
            value=_redact_dsn(s.SYSTEM_DATABASE_URL),
            sensitive=True,
            description="System DB DSN. Stored credentials are masked in this view.",
        ),
        BootstrapItem(
            name="CREDENTIAL_ENCRYPTION_KEY",
            value=mask(s.CREDENTIAL_ENCRYPTION_KEY),
            sensitive=True,
            description="Fernet key used to encrypt source DB credentials at rest.",
        ),
        BootstrapItem(
            name="JWT_SECRET_KEY",
            value=mask(s.JWT_SECRET_KEY),
            sensitive=True,
            description="HMAC signing key for issued JWTs.",
        ),
        BootstrapItem(
            name="JWT_ALGORITHM",
            value=s.JWT_ALGORITHM,
            sensitive=False,
            description="JWT signing algorithm.",
        ),
        BootstrapItem(
            name="SYSTEM_ADMIN_EMAIL",
            value=s.SYSTEM_ADMIN_EMAIL,
            sensitive=False,
            description="Login email of the bootstrap system administrator.",
        ),
        BootstrapItem(
            name="SYSTEM_ADMIN_PASSWORD",
            value=mask(s.SYSTEM_ADMIN_PASSWORD),
            sensitive=True,
            description="Bootstrap system admin password (rotate via .env).",
        ),
        BootstrapItem(
            name="JDBC_PORT",
            value=str(s.JDBC_PORT),
            sensitive=False,
            description="PostgreSQL wire-protocol port for the gateway.",
        ),
        BootstrapItem(
            name="XMLA_PORT",
            value=str(s.XMLA_PORT),
            sensitive=False,
            description="XMLA/HTTP port for the gateway.",
        ),
        BootstrapItem(
            name="QUERY_ROUTER_URL",
            value=s.QUERY_ROUTER_URL,
            sensitive=False,
            description="Internal URL the gateway uses to reach the query router.",
        ),
        BootstrapItem(
            name="MODEL_SERVICE_URL",
            value=s.MODEL_SERVICE_URL,
            sensitive=False,
            description="Internal URL services use to reach model-service.",
        ),
        BootstrapItem(
            name="OPTIMIZER_URL",
            value=s.OPTIMIZER_URL,
            sensitive=False,
            description="Internal URL services use to reach the optimizer.",
        ),
        BootstrapItem(
            name="CORS_ORIGINS",
            value=s.CORS_ORIGINS,
            sensitive=False,
            description="Comma-separated CORS allow-list (read at startup).",
        ),
    ]
    return items


def _redact_dsn(dsn: str) -> str:
    """Replace the password section of a DSN with stars."""
    if "://" not in dsn or "@" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    if "@" not in rest:
        return dsn
    creds, host_part = rest.rsplit("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{scheme}://{user}:*****@{host_part}"
    return dsn
