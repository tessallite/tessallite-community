"""Edition + limits read endpoints (open). UI consumes these for the badge and
the current/max count displays. Read-only; no enforcement happens here.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from shared.db.models import LocalUser, Model
from shared.db.session import get_tenant_db
from src.auth.middleware import CurrentUser, get_current_user
from src.licensing_guard import get_license_manager

logger = logging.getLogger(__name__)
router = APIRouter(tags=["edition"])


@router.get("/edition")
async def get_edition(_: CurrentUser = Depends(get_current_user)) -> dict:
    """Edition + activation summary (no secrets)."""
    return get_license_manager().status()


async def _tenant_usage(tenant_id) -> dict:
    """Current counts for the capped resources (simple select count). Returns
    empty on any failure (e.g. non-tenant context) so the read-only badge never
    breaks the UI."""
    try:
        async for db in get_tenant_db(tenant_id):
            models = int((await db.execute(select(func.count()).select_from(Model))).scalar() or 0)
            users = int((await db.execute(select(func.count()).select_from(LocalUser))).scalar() or 0)
            return {"models": models, "users": users}
    except Exception:  # noqa: BLE001 — display-only; degrade rather than 500
        logger.debug("usage count unavailable for tenant %s", tenant_id, exc_info=True)
    return {}


@router.get("/limits")
async def get_limits(current_user: CurrentUser = Depends(get_current_user)) -> dict:
    """Entitlement limits + current usage for current/max display in the UI."""
    manager = get_license_manager()
    return {
        "edition": manager.status().get("edition"),
        "entitlements": manager.entitlements(),
        "usage": await _tenant_usage(current_user.tenant_id),
    }
