"""Control-plane create-cap enforcement (open glue, edition-gated).

Default OFF (``LICENSE_ENFORCEMENT_ENABLED=false``): a true no-op — the full
product keeps its current unlimited behaviour. ON (Community): create endpoints
consult the license manager and refuse over-cap creates with 403.

The policy lives in ``shared.licensing`` (the closed manager when present, else
the open stub). This module only wires it into model-service endpoints.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from fastapi import HTTPException, status
from sqlalchemy import select

from shared.config.settings import get_settings
from shared.db.models import SystemSetting
from shared.db.session import get_system_db
from shared.licensing import LicenseManager
from shared.licensing.loader import build_registry, load_license_doc
from shared.licensing.manager import Decision, load_manager

logger = logging.getLogger(__name__)

# The license is fed from the UI and persisted in the system DB
# (``system_settings``), so it survives restarts and works on read-only
# filesystems (e.g. Cloud Run) with no env/file configuration.
LICENSE_SETTING_KEY = "license.document"

# Built-in Tessallite release verification key (PUBLIC — also shipped in
# ``.env.example`` and the community bundle). Used when ``LICENSE_PUBLIC_KEYS``
# is unset so an uploaded license can be verified with zero configuration.
# Override via the env var only for a non-standard signing key.
BUILTIN_PUBLIC_KEYS = "tessallite-prod-2026:/by3PGaeLD425bBkMrAbD2NeHROHyjLHVA37pS5w3+0="


class _UnlimitedManager(LicenseManager):
    """Full-product manager: everything allowed, no caps. Used when no license is
    installed."""

    def status(self) -> dict[str, Any]:
        return {"edition": "enterprise", "activated": True, "enforcement": False}

    def entitlements(self) -> dict[str, Any]:
        return {"features": "all"}

    def classify_tenant(self, tenant_id: str) -> str:
        return "own"

    def can_create(self, resource: str, current_count: int) -> Decision:
        return Decision(True, resource, current_count, None, "enforcement disabled")


class _UnactivatedManager(LicenseManager):
    """Enforcement is ON but no valid license is installed: FAIL-CLOSED (Bug-5474).

    Without this, a missing license fell back to ``_UnlimitedManager`` (full product) even
    with enforcement on — so Community caps were trivially bypassed by simply NOT installing
    a license, rendering the licence model useless. An unactivated instance instead DENIES
    every capped create until a licence is activated. (Enforcement OFF still maps to the full
    product; this stub only applies when the operator turned enforcement ON.)
    """

    def status(self) -> dict[str, Any]:
        return {"edition": "community", "activated": False, "enforcement": True}

    def entitlements(self) -> dict[str, Any]:
        return {"features": "all", "activated": False}

    def classify_tenant(self, tenant_id: str) -> str:
        return "own"

    def can_create(self, resource: str, current_count: int) -> Decision:
        return Decision(
            False,
            resource,
            current_count,
            0,
            "License not activated. Install a valid Tessallite licence "
            f"(System Admin -> License & Edition) before creating {resource}s.",
        )


# Cached manager, (re)built at startup and after every install. Sync callers read
# this; the DB read happens only on (re)load.
_MANAGER: Optional[LicenseManager] = None


def _no_license_manager() -> LicenseManager:
    """The manager to use when no licence document is present: fail-CLOSED when enforcement
    is ON (unactivated -> deny), full product when enforcement is OFF (dev/internal)."""
    if get_settings().LICENSE_ENFORCEMENT_ENABLED:
        return _UnactivatedManager()
    return _UnlimitedManager()


def license_public_keys() -> str:
    """Verification key spec: the env override if set, else the built-in key."""
    return get_settings().LICENSE_PUBLIC_KEYS or BUILTIN_PUBLIC_KEYS


async def load_license_doc_from_db() -> Optional[dict]:
    """The active license document: system DB first (UI-fed), then a legacy file."""
    async for db in get_system_db():
        row = (
            await db.execute(
                select(SystemSetting.value_json).where(
                    SystemSetting.key == LICENSE_SETTING_KEY
                )
            )
        ).scalar_one_or_none()
        if row:
            return row
    s = get_settings()
    if s.LICENSE_FILE:
        try:
            return load_license_doc(s.LICENSE_FILE)
        except Exception:  # noqa: BLE001 — missing/unreadable file degrades to none
            logger.warning("Could not read LICENSE_FILE %s", s.LICENSE_FILE, exc_info=True)
    return None


async def reload_license_manager() -> LicenseManager:
    """(Re)build the cached manager from the persisted license. No license -> unactivated
    (fail-closed) when enforcement is ON, full product when OFF (see _no_license_manager;
    Bug-5474). Called at startup and after an install."""
    global _MANAGER
    doc = await load_license_doc_from_db()
    if doc is None:
        _MANAGER = _no_license_manager()
    else:
        _MANAGER = load_manager(
            license_doc=doc, registry=build_registry(license_public_keys())
        )
    return _MANAGER


def get_license_manager() -> LicenseManager:
    """Cached license manager. Before the first load completes, fall back fail-closed
    (unactivated) when enforcement is ON so the pre-load window can't bypass caps either
    (Bug-5474)."""
    return _MANAGER if _MANAGER is not None else _no_license_manager()


async def store_license_doc(doc: dict, *, installed_by: Optional[str] = None) -> None:
    """Persist an (already-verified) license document to the system DB and reload
    the manager so it applies immediately — no restart, works on read-only hosts."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    async for db in get_system_db():
        stmt = (
            pg_insert(SystemSetting)
            .values(key=LICENSE_SETTING_KEY, value_json=doc, updated_by=installed_by)
            .on_conflict_do_update(
                index_elements=[SystemSetting.key],
                set_={"value_json": doc, "updated_by": installed_by},
            )
        )
        await db.execute(stmt)
        await db.commit()
    await reload_license_manager()


async def has_installed_license() -> bool:
    """Whether a license document is persisted in the system DB."""
    async for db in get_system_db():
        row = (
            await db.execute(
                select(SystemSetting.key).where(
                    SystemSetting.key == LICENSE_SETTING_KEY
                )
            )
        ).first()
        return row is not None
    return False


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
