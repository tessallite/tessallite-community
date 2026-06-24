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

from shared.config.bootstrap import system_snapshot_get
from shared.config.settings import get_settings
from shared.db.models import (
    LLMProviderConfig,
    ProjectConnection,
    SystemTenant,
    WebhookEndpoint,
)
from shared.db.session import get_system_db, get_tenant_db, normalize_tenant_db_url
from shared.licensing.errors import LicenseError
from shared.licensing.loader import build_registry
from shared.licensing.verify import verify_license
from shared.security.credential_crypto import decrypt_str, re_encrypt_blob
from src.auth.middleware import CurrentUser, require_system_admin
from src.licensing_guard import (
    get_license_manager,
    has_installed_license,
    license_public_keys,
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
    rotated = {"tenants": 0, "connections": 0, "llm_keys": 0, "webhooks": 0}
    failed_tenants: list[str] = []

    tenants = (await sys_db.execute(select(SystemTenant))).scalars().all()

    for t in tenants:
        try:
            async for tenant_db in get_tenant_db(t.slug):
                conns = (await tenant_db.execute(select(ProjectConnection))).scalars().all()
                for c in conns:
                    new_blob, changed = re_encrypt_blob(c.encrypted_credentials)
                    if changed:
                        c.encrypted_credentials = new_blob
                        rotated["connections"] += 1

                llm_cfgs = (await tenant_db.execute(select(LLMProviderConfig))).scalars().all()
                for lc in llm_cfgs:
                    if lc.encrypted_api_key:
                        new_blob, changed = re_encrypt_blob(lc.encrypted_api_key)
                        if changed:
                            lc.encrypted_api_key = new_blob
                            rotated["llm_keys"] += 1

                hooks = (await tenant_db.execute(select(WebhookEndpoint))).scalars().all()
                for h in hooks:
                    if h.signing_secret:
                        new_blob, changed = re_encrypt_blob(h.signing_secret)
                        if changed:
                            h.signing_secret = new_blob
                            rotated["webhooks"] += 1

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
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"License rejected: {exc}",
        )

    await store_license_doc(body, installed_by=getattr(current_user, "email", None))

    logger.warning(
        "[LICENSE] installed license_id=%s edition=%s by=%s",
        getattr(lic, "license_id", "?"),
        get_license_manager().status().get("edition"),
        getattr(current_user, "email", "?"),
    )
    return {"status": "installed", "license": await _license_status()}
