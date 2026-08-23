"""
Admin routes — database migrations and credential rotation.

POST /admin/migrate/system          — run system schema migrations (Alembic)
POST /admin/migrate/tenant/{slug}   — run tenant schema migrations (Alembic)
POST /admin/rotate-credentials      — re-encrypt all stored credentials with the current key
"""
from __future__ import annotations

import logging
import os
import subprocess

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.audit.system import system_audit
from shared.config.bootstrap import system_snapshot_get
from shared.config.settings import get_settings
from shared.db.models import (
    CollibraConnection,
    LLMProviderConfig,
    ProjectConnection,
    SolidatusConnection,
    SystemTenant,
    WebhookEndpoint,
)
from shared.db.session import get_system_db, get_tenant_db, normalize_tenant_db_url
from shared.licensing.errors import LicenseError
from shared.licensing.loader import build_registry
from shared.licensing.verify import verify_license
from shared.security.credential_crypto import decrypt_str, re_encrypt_blob
from shared.webhooks.dispatcher import emit_webhook_logged as emit_webhook
from src.auth.middleware import CurrentUser, require_system_admin
from src.licensing_guard import (
    clear_license_doc,
    get_license_manager,
    has_installed_license,
    license_public_keys,
    load_license_doc_from_db,
    reload_license_manager,
    store_license_doc,
)

settings = get_settings()
router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_system_admin)])

# Cloud Run uses tessallite/shared/, local dev uses shared/
ALEMBIC_INI_CANDIDATES = [
    "tessallite/shared/db/migrations/alembic.ini",  # Cloud Run
    "shared/db/migrations/alembic.ini",              # Local dev
]


def _find_alembic_ini() -> str:
    """Find the alembic.ini file, checking multiple possible locations."""
    for candidate in ALEMBIC_INI_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    raise FileNotFoundError(
        f"alembic.ini not found in any of: {ALEMBIC_INI_CANDIDATES}"
    )


def _run_alembic(mode: str, tenant_slug: str = "", database_url: str = "") -> dict:
    """Run Alembic upgrade for the correct branch in a subprocess."""
    env = {**os.environ, "MIGRATE_MODE": mode}
    if tenant_slug:
        env["TENANT_SLUG"] = tenant_slug
    if database_url:
        env["DATABASE_URL"] = database_url

    # Target the correct branch head so system and tenant migrations
    # are independent (0001 is system branch, 0002 is tenant branch).
    target = f"{mode}@head"

    alembic_ini = _find_alembic_ini()
    result = subprocess.run(
        ["python", "-m", "alembic", "-c", alembic_ini, "upgrade", target],
        capture_output=True,
        text=True,
        env=env,
        timeout=int(system_snapshot_get("control.admin_timeout")),
    )

    if result.returncode != 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Migration failed: {result.stderr.strip()}",
        )

    return {"status": "ok", "mode": mode, "output": result.stdout.strip()}


@router.post("/migrate/system")
async def migrate_system() -> dict:
    """Run Alembic migrations for the system schema (tess_system)."""
    return _run_alembic("system")


@router.post("/migrate/tenant/{tenant_slug}")
async def migrate_tenant(
    tenant_slug: str,
    sys_db: AsyncSession = Depends(get_system_db),
) -> dict:
    """Run Alembic migrations for a tenant schema ({slug}_meta)."""
    result = await sys_db.execute(
        select(SystemTenant).where(SystemTenant.slug == tenant_slug)
    )
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404, detail=f"Tenant '{tenant_slug}' not found")

    db_url = normalize_tenant_db_url(
        decrypt_str(tenant.encrypted_db_url),
        tenant.slug,
    )

    return _run_alembic("tenant", tenant_slug, database_url=db_url)


logger = logging.getLogger(__name__)


async def _rotate_model_blobs(
    db: AsyncSession,
    model_cls,
    attr_name: str,
    counter_key: str,
    rotated: dict[str, int],
) -> None:
    rows = (await db.execute(select(model_cls))).scalars().all()
    for row in rows:
        blob = getattr(row, attr_name, None)
        if not blob:
            continue
        new_blob, changed = re_encrypt_blob(blob)
        if changed:
            setattr(row, attr_name, new_blob)
            rotated[counter_key] += 1


@router.post("/rotate-credentials")
async def rotate_credentials(
    sys_db: AsyncSession = Depends(get_system_db),
) -> dict:
    """Re-encrypt all stored credentials from the previous key to the current key.

    Workflow:
      1. Generate a new Fernet key.
      2. Set CREDENTIAL_ENCRYPTION_KEY=<new> and CREDENTIAL_ENCRYPTION_KEY_PREVIOUS=<old>.
      3. Restart services (dual-key window is now active — reads succeed with either key).
      4. Call this endpoint to re-encrypt everything under the new key.
      5. Remove CREDENTIAL_ENCRYPTION_KEY_PREVIOUS and restart.
    """
    rotated = {
        "tenants": 0,
        "connections": 0,
        "llm_keys": 0,
        "webhooks": 0,
        "solidatus_connections": 0,
        "collibra_connections": 0,
    }
    failed_tenants: list[str] = []

    tenants = (await sys_db.execute(select(SystemTenant))).scalars().all()

    for t in tenants:
        try:
            async for tenant_db in get_tenant_db(t.slug):
                await _rotate_model_blobs(
                    tenant_db,
                    ProjectConnection,
                    "encrypted_credentials",
                    "connections",
                    rotated,
                )
                await _rotate_model_blobs(
                    tenant_db,
                    LLMProviderConfig,
                    "encrypted_api_key",
                    "llm_keys",
                    rotated,
                )
                await _rotate_model_blobs(
                    tenant_db,
                    WebhookEndpoint,
                    "signing_secret",
                    "webhooks",
                    rotated,
                )
                await _rotate_model_blobs(
                    tenant_db,
                    SolidatusConnection,
                    "encrypted_credentials",
                    "solidatus_connections",
                    rotated,
                )
                await _rotate_model_blobs(
                    tenant_db,
                    CollibraConnection,
                    "encrypted_credentials",
                    "collibra_connections",
                    rotated,
                )

                await tenant_db.commit()
        except Exception:
            logger.exception("Failed to rotate credentials for tenant %s", t.slug)
            failed_tenants.append(t.slug)

    for t in tenants:
        new_blob, changed = re_encrypt_blob(t.encrypted_db_url)
        if changed:
            t.encrypted_db_url = new_blob
            rotated["tenants"] += 1

    await sys_db.commit()

    if failed_tenants:
        raise HTTPException(
            status_code=500,
            detail=f"Rotation failed for tenants: {', '.join(failed_tenants)}. "
                   "Dual-key window is still active — retry is safe.",
        )

    return {"status": "ok", "rotated": rotated}


# ---------------------------------------------------------------------------
# License manager (system-admin). Upload / replace the signed license from the
# UI (Bug-5466). The license is fed from the frontend and persisted in the
# SYSTEM DB (not a file/env), so it survives restart and works on read-only
# hosts (e.g. Cloud Run). Verified with the built-in public key before it is
# stored; applied immediately via a manager reload. Closed engine untouched.
# ---------------------------------------------------------------------------

async def _license_status() -> dict:
    """Non-secret license/edition status for the admin UI."""
    s = get_settings()
    mgr = get_license_manager()
    return {
        "edition": mgr.status().get("edition"),
        "status": mgr.status(),
        "entitlements": mgr.entitlements(),
        "enforcement_enabled": bool(s.LICENSE_ENFORCEMENT_ENABLED),
        "has_license": await has_installed_license(),
    }


@router.get("/license")
async def get_license_status() -> dict:
    """Current edition/entitlements + whether a license is installed."""
    return await _license_status()


@router.post("/license")
async def install_license(
    body: dict = Body(..., description="The signed license JSON document"),
    current_user: CurrentUser = Depends(require_system_admin),
) -> dict:
    """Verify the uploaded license and persist it to the system DB, applying it
    immediately (no restart, no file, works on read-only hosts)."""
    # Verify signature + expiry BEFORE persisting (offline, pure). The built-in
    # public key is used unless LICENSE_PUBLIC_KEYS overrides it.
    try:
        lic = verify_license(body, build_registry(license_public_keys()))
    except LicenseError as exc:
        # Bug-8164: return the STRUCTURED failure taxonomy, not opaque prose, so a
        # consumer (the admin UI, an automation client) can branch on a stable
        # ``error_code`` token instead of parsing the human message.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error_code": exc.error_code, "message": f"License rejected: {exc}"},
        )

    async for sys_db in get_system_db():
        await store_license_doc(
            body, installed_by=getattr(current_user, "email", None), db=sys_db
        )
        await system_audit(
            sys_db,
            action="license.install",
            severity="critical",
            actor_email=getattr(current_user, "email", None),
            target_type="license",
            target_name=str(getattr(lic, "license_id", "")),
            detail={"edition": getattr(lic, "edition", None)},
        )
        await sys_db.commit()
        break
    await reload_license_manager()
    await emit_webhook("__system__", "license.installed", {
        "license_id": str(getattr(lic, "license_id", "")),
    })

    logger.warning(
        "[LICENSE] installed license_id=%s edition=%s by=%s",
        getattr(lic, "license_id", "?"),
        get_license_manager().status().get("edition"),
        getattr(current_user, "email", "?"),
    )
    return {"status": "installed", "license": await _license_status()}


@router.delete("/license")
async def uninstall_license(
    current_user: CurrentUser = Depends(require_system_admin),
) -> dict:
    """Remove the installed license (operational revocation, model-service side).

    Bug-6476: gives a system admin an operable path to deactivate a license that
    was revoked or superseded upstream. Removing it reverts entitlements
    immediately — to the full product when enforcement is off, or fail-closed
    unactivated (Community caps deny) when enforcement is on. Upstream/automatic
    propagation of an issuer-side revocation is a separate control-plane concern.
    """
    removed = False
    async for sys_db in get_system_db():
        removed = await clear_license_doc(db=sys_db)
        await system_audit(
            sys_db,
            action="license.uninstall",
            severity="critical",
            actor_email=getattr(current_user, "email", None),
            target_type="license",
            detail={"removed": removed},
        )
        await sys_db.commit()
        break
    await reload_license_manager()
    await emit_webhook("__system__", "license.uninstalled", {"removed": removed})

    # A licence mounted via LICENSE_FILE (the Helm/Compose install path,
    # Bug-5485) is reloaded on the manager reload even after the DB document is
    # deleted. Detect that so the response does not misleadingly report the
    # licence as gone. load_license_doc_from_db checks the DB first (now empty)
    # then falls through to the file. Note this reports the file is PRESENT, not
    # that it is valid — an expired/untrusted file reloads fail-closed, and the
    # nested ``license.status`` carries the true activated flag.
    file_license_present = (await load_license_doc_from_db()) is not None

    if file_license_present:
        result_status = "file_license_present"
    elif removed:
        result_status = "removed"
    else:
        result_status = "no_license"

    logger.warning(
        "[LICENSE] uninstalled license (removed=%s, file_license_present=%s) by=%s",
        removed,
        file_license_present,
        getattr(current_user, "email", "?"),
    )
    response = {
        "status": result_status,
        "license": await _license_status(),
    }
    if file_license_present:
        response["message"] = (
            "A licence file mounted via LICENSE_FILE is still present and was "
            "reloaded after the database licence was removed. Check "
            "license.status.activated for whether it currently governs "
            "entitlements, and remove or replace the mounted file at the "
            "deployment level to fully revoke."
        )
    return response
