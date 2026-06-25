"""Edition + limits read endpoints (open). UI consumes these for the badge and
the current/max count displays. Read-only; no enforcement happens here.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from shared.db.models import LocalUser, Model, Project, SystemTenant
from shared.db.session import get_system_db, get_tenant_db
from src.auth.middleware import CurrentUser, get_current_user
from src.licensing_guard import get_license_manager

logger = logging.getLogger(__name__)
router = APIRouter(tags=["edition"])


@router.get("/edition")
async def get_edition(_: CurrentUser = Depends(get_current_user)) -> dict:
    """Edition + activation summary (no secrets)."""
    return get_license_manager().status()


async def _own_tenant_count() -> int | None:
    """Platform-level count of OWN (non-demo) tenants, matching the ``own_tenants``
    entitlement and the create-cap counter in ``api/tenants.py`` (demo excluded via
    ``classify_tenant``). Lives in the system DB, not the tenant DB. Returns ``None``
    on failure so the caller can omit it (degrade) rather than report a wrong 0."""
    try:
        manager = get_license_manager()
        async for sys_db in get_system_db():
            slugs = (await sys_db.execute(select(SystemTenant.slug))).scalars().all()
            return sum(
                1 for s in slugs if manager.classify_tenant(str(s)) != "demo"
            )
    except Exception:  # noqa: BLE001 — display-only; degrade rather than 500
        logger.debug("own-tenant count unavailable", exc_info=True)
    return None


async def _tenant_usage(tenant_id) -> dict:
    """Current counts for the capped resources (simple select count). Returns
    empty on any failure (e.g. non-tenant context) so the read-only badge never
    breaks the UI.

    Counts are scoped to match the entitlement each is compared against:
    - ``models`` / ``users`` / ``projects`` are per-tenant (tenant DB), matching the
      ``models`` / ``users`` / ``projects_per_own_tenant`` caps.
    - ``tenants`` is platform-level (system DB), matching the ``own_tenants`` cap."""
    usage: dict = {}
    try:
        async for db in get_tenant_db(tenant_id):
            usage["models"] = int(
                (await db.execute(select(func.count()).select_from(Model))).scalar() or 0
            )
            usage["users"] = int(
                (await db.execute(select(func.count()).select_from(LocalUser))).scalar() or 0
            )
            usage["projects"] = int(
                (await db.execute(select(func.count()).select_from(Project))).scalar() or 0
            )
            break
    except Exception:  # noqa: BLE001 — display-only; degrade rather than 500
        logger.debug("usage count unavailable for tenant %s", tenant_id, exc_info=True)

    own_tenants = await _own_tenant_count()
    if own_tenants is not None:
        usage["tenants"] = own_tenants
    return usage


@router.get("/limits")
async def get_limits(current_user: CurrentUser = Depends(get_current_user)) -> dict:
    """Entitlement limits + current usage for current/max display in the UI."""
    manager = get_license_manager()
    return {
        "edition": manager.status().get("edition"),
        "entitlements": manager.entitlements(),
        "usage": await _tenant_usage(current_user.tenant_id),
    }
