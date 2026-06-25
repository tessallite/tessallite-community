"""
Tenant provisioning and management routes.

POST /tenants   — system admin only: creates a SystemTenant + provisions schemas.
GET  /tenants   — system admin: list all; tenant user: see own tenant only.
GET  /tenants/me — tenant user: get own tenant info from JWT.
"""
from __future__ import annotations

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from shared.config.settings import get_settings
from shared.db.models import SystemTenant
from shared.db.session import get_system_db, normalize_tenant_db_url, evict_tenant_engine
from shared.schemas.pydantic_models import TenantCreate, TenantResponse, TenantUpdate
from shared.security.credential_crypto import decrypt_str, encrypt_str
from src.auth.middleware import CurrentUser, forbid_embed_user, get_current_user, require_system_admin
from src.licensing_guard import enforce_create_cap, get_license_manager

settings = get_settings()
router = APIRouter(prefix="/tenants", tags=["tenants"])


def _encrypt_db_url(url: str) -> bytes:
    # Rotation-aware: encrypts under the current key (the first rotation key).
    return encrypt_str(url)


@router.post("", response_model=TenantResponse, status_code=status.HTTP_201_CREATED)
async def create_tenant(
    body: TenantCreate,
    sys_db: AsyncSession = Depends(get_system_db),
    _admin: CurrentUser = Depends(require_system_admin),
) -> TenantResponse:
    """
    Provisions a new tenant inside the shared database:
    1. Validates slug uniqueness.
    2. Chooses the shared system DB (or uses body.database_url if explicitly provided).
    3. Creates {slug}_meta and {slug}_aggregates schemas in that DB.
    4. Stores encrypted DB URL in system DB.
    """
    # Edition cap: count existing OWN (non-demo) tenants; demo does not count.
    async def _count_own_tenants() -> int:
        rows = await sys_db.execute(select(SystemTenant.slug))
        mgr = get_license_manager()
        return sum(
            1 for s in rows.scalars().all() if mgr.classify_tenant(str(s)) != "demo"
        )

    await enforce_create_cap("tenant", _count_own_tenants)

    # Validate slug uniqueness
    result = await sys_db.execute(
        select(SystemTenant).where(SystemTenant.slug == body.slug)
    )
    existing = result.scalar_one_or_none()
    if existing:
        # If the tenant's encrypted URL cannot be read under any configured
        # key (current or previous), re-encrypt with the current key so
        # downstream operations don't fail with InvalidToken. The dual-key
        # helper already tolerates a previous-key blob during a rotation
        # window, so this only triggers when no configured key works.
        try:
            decrypt_str(existing.encrypted_db_url)
        except (InvalidToken, Exception):
            # Unreadable encryption — rebuild and update the DB URL
            db_url = settings.SYSTEM_DATABASE_URL
            existing.encrypted_db_url = _encrypt_db_url(db_url)
            await sys_db.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tenant with slug '{body.slug}' already exists",
        )

    # Default to the shared database; only use a custom URL when explicitly requested.
    db_url = normalize_tenant_db_url(
        body.database_url or settings.SYSTEM_DATABASE_URL,
        body.slug,
    )

    # Create schemas in the target database
    engine = create_async_engine(db_url, echo=False)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{body.slug}_meta"'))
            await conn.execute(
                text(f'CREATE SCHEMA IF NOT EXISTS "{body.slug}_aggregates"')
            )
    except Exception as exc:
        await engine.dispose()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to provision tenant schemas: {exc}",
        )
    finally:
        await engine.dispose()

    encrypted = _encrypt_db_url(db_url)
    tenant = SystemTenant(
        slug=body.slug,
        display_name=body.display_name,
        encrypted_db_url=encrypted,
        db_schema_prefix=body.slug,
        is_active=True,
    )
    sys_db.add(tenant)
    try:
        await sys_db.commit()
    except IntegrityError:
        await sys_db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tenant with slug '{body.slug}' already exists",
        )
    await sys_db.refresh(tenant)
    return TenantResponse.model_validate(tenant)


@router.get("", response_model=list[TenantResponse])
async def list_tenants(
    sys_db: AsyncSession = Depends(get_system_db),
    _admin: CurrentUser = Depends(require_system_admin),
) -> list[TenantResponse]:
    """System admin only: list all tenants."""
    from sqlalchemy import select

    result = await sys_db.execute(
        select(SystemTenant).order_by(SystemTenant.slug)
    )
    tenants = result.scalars().all()
    return [TenantResponse.model_validate(t) for t in tenants]


@router.get("/me", response_model=TenantResponse)
async def get_my_tenant(
    sys_db: AsyncSession = Depends(get_system_db),
    current_user: CurrentUser = Depends(get_current_user),
    _: None = Depends(forbid_embed_user),
) -> TenantResponse:
    """Tenant user: get info about own tenant (from JWT tenant_id)."""
    from sqlalchemy import select
    result = await sys_db.execute(
        select(SystemTenant).where(SystemTenant.slug == current_user.tenant_id)
    )
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return TenantResponse.model_validate(tenant)


@router.get("/{tenant_id}", response_model=TenantResponse)
async def get_tenant(
    tenant_id: str,
    sys_db: AsyncSession = Depends(get_system_db),
    _admin: CurrentUser = Depends(require_system_admin),
) -> TenantResponse:
    """System admin only: get any tenant by slug."""
    from sqlalchemy import select
    result = await sys_db.execute(
        select(SystemTenant).where(SystemTenant.slug == tenant_id)
    )
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return TenantResponse.model_validate(tenant)


@router.patch("/{tenant_id}", response_model=TenantResponse)
async def update_tenant(
    tenant_id: str,
    body: TenantUpdate,
    sys_db: AsyncSession = Depends(get_system_db),
    _admin: CurrentUser = Depends(require_system_admin),
) -> TenantResponse:
    """System admin only: update tenant display name and active flag.

    The slug is intentionally immutable — it is the schema name and is
    referenced by every downstream tenant table, so renaming would require
    a schema rename plus a re-encryption of the stored DB URL. Out of
    scope for an admin edit dialog.
    """
    from sqlalchemy import select

    result = await sys_db.execute(
        select(SystemTenant).where(SystemTenant.slug == tenant_id)
    )
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")

    updates = body.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(tenant, key, value)
    await sys_db.commit()
    await sys_db.refresh(tenant)
    return TenantResponse.model_validate(tenant)


@router.delete("/{tenant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tenant(
    tenant_id: str,
    sys_db: AsyncSession = Depends(get_system_db),
    _admin: CurrentUser = Depends(require_system_admin),
) -> None:
    from sqlalchemy import select

    result = await sys_db.execute(
        select(SystemTenant).where(SystemTenant.slug == tenant_id)
    )
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")

    db_url = normalize_tenant_db_url(decrypt_str(tenant.encrypted_db_url), tenant.slug)

    engine = create_async_engine(db_url, echo=False)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{tenant.slug}_meta" CASCADE'))
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{tenant.slug}_aggregates" CASCADE'))
    except Exception as exc:
        await engine.dispose()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to delete tenant schemas: {exc}",
        )
    finally:
        await engine.dispose()

    await sys_db.delete(tenant)
    await sys_db.commit()
    await evict_tenant_engine(tenant_id)
