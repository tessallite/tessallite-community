"""Control-plane create-cap enforcement (open glue, edition-gated).

Default OFF (``LICENSE_ENFORCEMENT_ENABLED=false``): a true no-op — the full
product keeps its current unlimited behaviour. ON (Community): create endpoints
consult the license manager and refuse over-cap creates with 403.

The policy lives in ``shared.licensing`` (the closed manager when present, else
the open stub). This module only wires it into model-service endpoints.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Awaitable, Callable

from fastapi import HTTPException, status

from shared.config.settings import get_settings
from shared.licensing import LicenseManager
from shared.licensing.loader import manager_from
from shared.licensing.manager import Decision


class _UnlimitedManager(LicenseManager):
    """Full-product manager: everything allowed, no caps. Used when enforcement is off."""

    def status(self) -> dict[str, Any]:
        return {"edition": "enterprise", "activated": True, "enforcement": False}

    def entitlements(self) -> dict[str, Any]:
        return {"features": "all"}

    def classify_tenant(self, tenant_id: str) -> str:
        return "own"

    def can_create(self, resource: str, current_count: int) -> Decision:
        return Decision(True, resource, current_count, None, "enforcement disabled")


@lru_cache
def get_license_manager() -> LicenseManager:
    """Cached manager. Off -> unlimited; on -> closed manager from configured license."""
    s = get_settings()
    if not s.LICENSE_ENFORCEMENT_ENABLED:
        return _UnlimitedManager()
    return manager_from(s.LICENSE_FILE, s.LICENSE_PUBLIC_KEYS)


async def enforce_create_cap(
    resource: str, count_fn: Callable[[], Awaitable[int]]
) -> None:
    """Raise 403 if creating one more ``resource`` would exceed the edition cap.

    No-op (and no count query) when enforcement is disabled — the full product.
    ``count_fn`` is awaited only when enforcement is on.
    """
    if not get_settings().LICENSE_ENFORCEMENT_ENABLED:
        return
    current = await count_fn()
    decision = get_license_manager().can_create(resource, current)
    if not decision.allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=decision.reason)


def enforce_demo_source_locked(tenant_id: str) -> None:
    """Raise 403 if ``tenant_id`` is the demo tenant — its data source is fixed.

    The Community demo tenant ships a seeded, read-only source; the user may edit the
    demo model and reseed, but not add/change its connection. Own tenants are
    unaffected. No-op when enforcement is disabled (the full product).
    """
    if not get_settings().LICENSE_ENFORCEMENT_ENABLED:
        return
    if get_license_manager().classify_tenant(str(tenant_id)) == "demo":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "The demo tenant's data source is fixed and read-only. "
                "Use your own tenant to connect your own data."
            ),
        )
