"""Control-plane create-cap enforcement (open glue, edition-gated).

Default ON (``LICENSE_ENFORCEMENT_ENABLED=true``): create endpoints consult the
license manager and refuse over-cap creates with 403. The hatch
``LICENSE_ENFORCEMENT_ENABLED=false`` (or ``TESSALLITE_DEV_UNLIMITED``) is
internal-unlimited — never reported as Enterprise.

The policy lives in ``shared.licensing`` (the closed manager when present, else
the open stub). This module only wires it into model-service endpoints.
"""
from __future__ import annotations

import logging
import os
import time
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
    """Internal/dev unlimited manager. Never reports edition=enterprise (F-031-02)."""

    def status(self) -> dict[str, Any]:
        return {
            "edition": "internal-unlimited",
            "activated": False,
            "enforcement": False,
            "license_state": "dev_unlimited",
        }

    def entitlements(self) -> dict[str, Any]:
        return {"features": "all", "display_only": True}

    def classify_tenant(self, tenant_id: str) -> str:
        slug = str(tenant_id)
        if slug in ("demo", "acme-demo"):
            return "demo"
        return "own"

    def can_create(self, resource: str, current_count: int) -> Decision:
        return Decision(True, resource, current_count, None, "dev unlimited")


class _UnactivatedManager(LicenseManager):
    """Enforcement is ON but no valid license is installed: FAIL-CLOSED (Bug-5496).

    Without this, a missing license fell back to ``_UnlimitedManager`` (full product) even
    with enforcement on — so Community caps were trivially bypassed by simply NOT installing
    a license, rendering the licence model useless. An unactivated instance instead DENIES
    every capped create until a licence is activated. (Enforcement OFF still maps to the full
    product; this stub only applies when the operator turned enforcement ON.)
    """

    def status(self) -> dict[str, Any]:
        return {"edition": "community", "activated": False, "enforcement": True}

    def entitlements(self) -> dict[str, Any]:
        # Bug-6816: fail-closed managers must not report features: "all" while
        # denying creates. Report "none" so UI consumers that inspect entitlements
        # (e.g. feature-gate components) see a consistent deny signal.
        return {"features": "none", "activated": False}

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


class _InvalidLicenseManager(LicenseManager):
    """A licence document IS installed but could not be activated (Bug-6437).

    Distinguished from ``_UnactivatedManager`` (nothing installed): the operator
    uploaded a licence but it is expired, its signature is untrusted, or the
    closed verifier rejected it. FAIL-CLOSED like the unactivated case, but the
    reported state and reason say "installed-but-invalid" so the UI can prompt a
    renew/reinstall rather than mislead the operator into thinking no licence was
    ever provided.

    Bug-8164 / Bug-7466: the SPECIFIC failure signal from the underlying manager
    is preserved rather than flattened. ``license_state`` carries the distinct
    state ("invalid", "expired", or "manager_load_failed" for a present-but-broken
    closed build) and ``error_code`` carries the stable machine-readable taxonomy
    token of a rejected licence, so an operator/consumer sees WHY the licence is
    not active, not just that it isn't.
    """

    def __init__(
        self,
        *,
        license_state: str = "invalid",
        error_code: Optional[str] = None,
        load_error: Optional[str] = None,
    ) -> None:
        self._license_state = license_state or "invalid"
        self._error_code = error_code
        self._load_error = load_error

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "edition": "community",
            "activated": False,
            "enforcement": True,
            "license_state": self._license_state,
        }
        if self._error_code:
            out["error_code"] = self._error_code
        if self._load_error:
            out["load_error"] = self._load_error
        return out

    def entitlements(self) -> dict[str, Any]:
        # Bug-6816: same as _UnactivatedManager — fail-closed must not advertise
        # features: "all" when creates are denied.
        out: dict[str, Any] = {
            "features": "none",
            "activated": False,
            "license_state": self._license_state,
        }
        if self._error_code:
            out["error_code"] = self._error_code
        return out

    def classify_tenant(self, tenant_id: str) -> str:
        return "own"

    def can_create(self, resource: str, current_count: int) -> Decision:
        if self._license_state == "manager_load_failed":
            reason = (
                "The Tessallite license manager is installed in this build but "
                "could not be loaded, so no licence can be verified. This is a "
                "build/packaging fault — check the service logs and reinstall the "
                f"build before creating {resource}s."
            )
        else:
            reason = (
                "The installed Tessallite licence is expired or untrusted and "
                "could not be activated. Install a current, valid licence (System "
                f"Admin -> License & Edition) before creating {resource}s."
            )
        return Decision(False, resource, current_count, 0, reason)


# Cached manager, (re)built at startup and after every install. Sync callers read
# this; the DB read happens only on (re)load.
_MANAGER: Optional[LicenseManager] = None

# Bug-6436: the manager is cached per-process; on Cloud Run a licence installed
# (or changed) on replica A is invisible to replica B until B restarts. Bound the
# cross-replica staleness by re-reading the persisted licence when the cache is
# older than this TTL. ``_MANAGER_LOADED_AT`` is the monotonic-ish wall time of
# the last (re)load; the async enforcement path refreshes when stale.
_MANAGER_TTL_SECONDS = 60.0
_MANAGER_LOADED_AT: float = 0.0


def _dev_unlimited() -> bool:
    """Named hatch for internal/dev stacks (F-031-01).

    ``TESSALLITE_DEV_UNLIMITED`` is the explicit name. The legacy
    ``LICENSE_ENFORCEMENT_ENABLED=false`` flag remains a hatch so existing
    test helpers and hosted-demo compose keep working, but status never
    claims Enterprise.
    """
    s = get_settings()
    if bool(getattr(s, "TESSALLITE_DEV_UNLIMITED", False)):
        return True
    return not bool(s.LICENSE_ENFORCEMENT_ENABLED)


def _no_license_manager() -> LicenseManager:
    """The manager to use when no licence document is present: fail-CLOSED when
    enforcement is ON (unactivated -> deny), named unlimited hatch otherwise."""
    if _dev_unlimited():
        return _UnlimitedManager()
    return _UnactivatedManager()


def license_public_keys() -> str:
    """Verification key spec: vendor key always present; extras are additions.

    F-031-24: ``LICENSE_PUBLIC_KEYS`` cannot replace the built-in Tessallite
    release key. Extra ``key_id:base64`` fragments are appended; ``build_registry``
    keeps the first key_id (the vendor key).
    """
    extra = (get_settings().LICENSE_PUBLIC_KEYS or "").strip()
    if not extra:
        return BUILTIN_PUBLIC_KEYS
    return f"{BUILTIN_PUBLIC_KEYS},{extra}"


async def load_license_doc_from_db() -> Optional[dict]:
    """The active license document: system DB first (UI-fed), then a file (LICENSE_FILE).

    The system-DB lookup is best-effort: at first startup the system schema may not be
    migrated yet (on Kubernetes the migration runs as a post-install Job AFTER the pods
    start), so the ``system_settings`` query can fail. Swallow that and fall through to the
    file — otherwise a file-mounted licence (the Helm/Compose install path) never loads and
    the manager is stuck unactivated, denying every create even with a valid licence
    installed (Bug-5485)."""
    try:
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
    except Exception:  # noqa: BLE001 — schema not migrated yet / DB unavailable -> use the file
        logger.warning(
            "system-DB licence lookup failed (schema not ready?); falling back to LICENSE_FILE",
            exc_info=True,
        )
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
    Bug-5496). Called at startup, after an install, and on a stale-cache refresh
    (Bug-6436)."""
    global _MANAGER, _MANAGER_LOADED_AT
    doc = await load_license_doc_from_db()
    if doc is None:
        _MANAGER = _no_license_manager()
    else:
        mgr = load_manager(
            license_doc=doc, registry=build_registry(license_public_keys())
        )
        # Bug-6437: a licence document IS present but the manager reports it is
        # not activated -> the closed verifier rejected it (expired/untrusted).
        # Surface a distinct fail-closed "invalid" state instead of the generic
        # unactivated one, which reads as "no licence installed".
        status_doc = {}
        try:
            status_doc = mgr.status() or {}
        except Exception:  # noqa: BLE001 — a manager that cannot even report status is invalid
            logger.warning("licence manager status() raised; treating as invalid", exc_info=True)
        if (
            not status_doc.get("activated", False)
            and not _dev_unlimited()
        ):
            # Only reclassify as invalid when enforcement is ON. With enforcement
            # OFF (source-only dev), the create path no-ops regardless, so the
            # manager is never consulted and the dev state is left unchanged.
            #
            # Bug-8164 / Bug-7466: PRESERVE the specific failure signal instead of
            # flattening every inactive licence to a generic "invalid". A rejected
            # licence carries a machine-readable ``error_code`` (expired /
            # invalid_signature / unknown_key_id / ...), and a present-but-broken
            # closed build reports ``license_state: "manager_load_failed"`` with a
            # ``load_error`` — both must survive to the operator-facing status path.
            _MANAGER = _InvalidLicenseManager(
                license_state=status_doc.get("license_state") or "invalid",
                error_code=status_doc.get("error_code"),
                load_error=status_doc.get("load_error"),
            )
        else:
            _MANAGER = mgr
    _MANAGER_LOADED_AT = time.monotonic()
    return _MANAGER


def get_license_manager() -> LicenseManager:
    """Cached license manager (sync, no TTL refresh).

    Before the first load completes, fall back fail-closed (unactivated) when
    enforcement is ON so the pre-load window can't bypass caps either (Bug-5496).

    Bug-6815: this synchronous accessor returns the cached manager WITHOUT
    refreshing a stale cache. It is intentionally used by display/info callers
    (``edition.py``, ``admin.py``, ``beacon_runtime.py``, ``tenants.py``) and by
    ``enforce_demo_source_locked`` where the primary guard is the env-var tenant
    lock list (``DEMO_SOURCE_LOCKED_TENANTS``) — not the licence. The
    security-critical create-cap path (``enforce_create_cap``) calls the async
    ``_ensure_fresh_manager`` instead. Cross-replica staleness for the non-cap
    callers is bounded by ``_MANAGER_TTL_SECONDS`` (next ``enforce_create_cap``
    call on the same process refreshes the cache for everyone).
    """
    return _MANAGER if _MANAGER is not None else _no_license_manager()


async def store_license_doc(
    doc: dict, *, installed_by: Optional[str] = None, db=None
) -> None:
    """Persist an (already-verified) license document to the system DB.

    When ``db`` is supplied the upsert is executed on that session and is
    **not** committed or reloaded — the caller must emit ``system_audit`` on
    the same session, then ``commit``, then ``reload_license_manager``
    (Bug-9301: a failed audit must roll back the mutation).
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    async def _persist(session) -> None:
        stmt = (
            pg_insert(SystemSetting)
            .values(key=LICENSE_SETTING_KEY, value_json=doc, updated_by=installed_by)
            .on_conflict_do_update(
                index_elements=[SystemSetting.key],
                set_={"value_json": doc, "updated_by": installed_by},
            )
        )
        await session.execute(stmt)

    if db is not None:
        await _persist(db)
        return

    async for session in get_system_db():
        await _persist(session)
        await session.commit()
    await reload_license_manager()


async def clear_license_doc(*, db=None) -> bool:
    """Remove the installed license document.

    Bug-6476 (model-service side of operational revocation): once a license was
    installed there was no operable path to remove it — an operator who learns a
    license was revoked upstream could only overwrite it with another install.
    This deletes the persisted ``license.document`` system setting.

    When ``db`` is supplied the delete is executed on that session and is
    **not** committed or reloaded — the caller must emit ``system_audit`` on
    the same session, then ``commit``, then ``reload_license_manager``
    (Bug-9301). Returns True if a document was present and removed, False if
    there was nothing to remove.
    """
    from sqlalchemy import delete as sa_delete

    async def _delete(session) -> bool:
        result = await session.execute(
            sa_delete(SystemSetting).where(SystemSetting.key == LICENSE_SETTING_KEY)
        )
        return bool(result.rowcount)

    if db is not None:
        return await _delete(db)

    removed = False
    async for session in get_system_db():
        removed = await _delete(session)
        await session.commit()
    await reload_license_manager()
    return removed


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


async def _ensure_fresh_manager() -> LicenseManager:
    """Return the cached manager, reloading it first if the cache is stale or
    unset (Bug-6436).

    The manager is per-process, so a licence installed/changed on another Cloud
    Run replica is invisible until that replica reloads. Reloading when the cache
    is older than ``_MANAGER_TTL_SECONDS`` bounds cross-replica staleness for the
    enforcement decision (the security-relevant path) without a shared cache or a
    pub/sub broadcast. A reload failure keeps the last good manager rather than
    opening the caps.
    """
    if _MANAGER is None or (time.monotonic() - _MANAGER_LOADED_AT) > _MANAGER_TTL_SECONDS:
        try:
            return await reload_license_manager()
        except Exception:  # noqa: BLE001 — a transient DB error must not open the caps
            logger.warning(
                "stale-cache licence reload failed; using last cached manager",
                exc_info=True,
            )
    return get_license_manager()


async def enforce_import_model_cap(
    models_to_import: int, count_fn: Callable[[], Awaitable[int]],
    db=None,
) -> None:
    """Raise 403 if importing ``models_to_import`` models would exceed the cap.

    Bug-7468: all model-import paths (dbt, cube, atscale, catalog, snapshot,
    project-bundle) bypassed the licensed model cap. This helper reuses the
    same ``can_create`` decision as direct model creation but checks the cap
    against ``current_count + models_to_import - 1`` so the entire batch is
    rejected when the result would exceed the licence limit.

    No-op (and no count query) when enforcement is disabled — the full product.

    Bug-6567: when ``db`` (an async SQLAlchemy session) is provided, the
    count-then-decide check is serialised with a PostgreSQL advisory lock
    keyed on ``"model"`` — the SAME key space as ``enforce_create_cap``
    for models, so an import racing a direct create (or two concurrent
    imports) cannot both pass the check and exceed the licensed cap.
    """
    if _dev_unlimited():
        return
    if models_to_import < 1:
        return
    manager = await _ensure_fresh_manager()

    if db is not None:
        # Bug-6567: acquire the SAME advisory lock as enforce_create_cap for
        # "model" so imports and direct creates serialise against each other.
        import zlib
        from sqlalchemy import text
        lock_key = zlib.crc32("tessallite_cap_model".encode()) & 0x7FFFFFFF
        await db.execute(text(f"SELECT pg_advisory_xact_lock({lock_key})"))

    current = await count_fn()
    # can_create checks ``current_count < limit`` (one-more semantics).
    # Passing ``current + models_to_import - 1`` makes the check equivalent
    # to ``current + models_to_import <= limit``.
    decision = manager.can_create("model", current + models_to_import - 1)
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Importing {models_to_import} model(s) would exceed the "
                f"licensed model cap ({current} existing). {decision.reason}"
            ),
        )


async def enforce_create_cap(
    resource: str, count_fn: Callable[[], Awaitable[int]],
    db=None,
) -> None:
    """Raise 403 if creating one more ``resource`` would exceed the edition cap.

    No-op (and no count query) when enforcement is disabled — the full product.
    ``count_fn`` is awaited only when enforcement is on.

    Bug-6567: when ``db`` (an async SQLAlchemy session) is provided, the
    count-then-decide check is serialised with a PostgreSQL advisory lock
    keyed on the resource name hash. This prevents two concurrent creates
    at cap-1 from both passing the check and exceeding the licensed cap.
    Without ``db`` the behaviour is unchanged (backward compatible) for
    callers that cannot pass a session.
    """
    if _dev_unlimited():
        return
    # Bug-6436: refresh a stale per-process manager before deciding so a licence
    # installed on another replica is honoured within the TTL.
    manager = await _ensure_fresh_manager()

    if db is not None:
        # Bug-6567: acquire a transaction-scoped advisory lock so that
        # concurrent create requests serialise on the count check.
        # pg_advisory_xact_lock releases automatically at transaction end.
        import zlib
        lock_key = zlib.crc32(f"tessallite_cap_{resource}".encode()) & 0x7FFFFFFF
        from sqlalchemy import text
        await db.execute(text(f"SELECT pg_advisory_xact_lock({lock_key})"))

    current = await count_fn()
    decision = manager.can_create(resource, current)
    if not decision.allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=decision.reason)


def enforce_demo_source_locked(tenant_id: str) -> None:
    """Raise 403 if ``tenant_id`` is the demo tenant -- its data source is fixed.

    The Community demo tenant ships a seeded, read-only source; the user may edit the
    demo model and reseed, but not add/change its connection. Own tenants are
    unaffected.

    Bug-6529: the lock also fires when the deployment explicitly lists locked
    tenants in ``DEMO_SOURCE_LOCKED_TENANTS`` (comma-separated env var),
    regardless of whether license enforcement is enabled. This covers
    hosted-demo configs that run the full product (enforcement off) but still
    need the demo tenant's connections locked against mutation.
    """
    tid = str(tenant_id)

    # Bug-6529: explicit tenant lock list -- works even with enforcement off.
    # This is the primary guard for hosted-demo deployments that do not enable
    # Community license enforcement but still expose a demo tenant on the
    # public internet.
    locked_raw = os.environ.get("DEMO_SOURCE_LOCKED_TENANTS", "")
    if locked_raw:
        locked_ids = {t.strip() for t in locked_raw.split(",") if t.strip()}
        if tid in locked_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "The demo tenant's data source is fixed and read-only. "
                    "Use your own tenant to connect your own data."
                ),
            )

    # License-based demo classification (Community edition path).
    if _dev_unlimited():
        return
    if get_license_manager().classify_tenant(tid) == "demo":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "The demo tenant's data source is fixed and read-only. "
                "Use your own tenant to connect your own data."
            ),
        )
