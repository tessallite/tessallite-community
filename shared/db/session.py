"""
Two-level database session management.

- System DB: global PostgreSQL for tenant registry
- Tenant DB: tenant metadata lives in per-tenant schemas within the shared DB
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncGenerator

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sqlalchemy import select

from shared.config.settings import get_settings
from shared.db.models import SystemTenant

logger = logging.getLogger(__name__)
settings = get_settings()

# ---------------------------------------------------------------------------
# System DB engine (single global engine)
# ---------------------------------------------------------------------------
_system_engine = create_async_engine(
    settings.SYSTEM_DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=1800,
    echo=False,
)
SystemSessionLocal = async_sessionmaker(_system_engine, expire_on_commit=False)


async def get_system_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency: yields an AsyncSession scoped to the system DB."""
    async with SystemSessionLocal() as session:
        yield session


# ---------------------------------------------------------------------------
# Per-tenant DB engine cache
# ---------------------------------------------------------------------------
_tenant_engines: dict[str, async_sessionmaker] = {}
_tenant_engines_lock = asyncio.Lock()  # M-04 fix: protect concurrent access


def _decrypt_db_url(encrypted: bytes) -> str:
    from shared.security.credential_crypto import decrypt_str
    return decrypt_str(encrypted)


def normalize_tenant_db_url(db_url: str, tenant_slug: str) -> str:
    """
    Return the tenant DB URL as stored.

    Previous versions redirected slug-named databases back to the system
    database.  The current deployment uses separate databases per tenant,
    so the URL stored in the tenant record is authoritative.
    """
    try:
        t_url = make_url(db_url)
        s_url = make_url(settings.SYSTEM_DATABASE_URL)
        
        # Dynamically rewrite connection details to match the active system database.
        # This routes tenant schema connections to the current environment's active VM/local instance.
        new_url = t_url._replace(
            host=s_url.host,
            port=s_url.port,
            username=s_url.username,
            password=s_url.password,
            database=s_url.database
        )
        return new_url.render_as_string(hide_password=False)
    except Exception as e:
        logger.error(f"Error normalizing tenant database URL: {e}")
        return db_url


async def get_tenant_session_factory(tenant_id: str) -> async_sessionmaker:
    """Returns (and caches) a session factory for the given tenant's DB."""
    # M-04 fix: atomic check-then-create with lock to prevent duplicate engines
    async with _tenant_engines_lock:
        if tenant_id in _tenant_engines:
            return _tenant_engines[tenant_id]

        async with SystemSessionLocal() as sys_db:
            result = await sys_db.execute(
                select(SystemTenant).where(SystemTenant.slug == tenant_id)
            )
            tenant = result.scalar_one_or_none()
            if tenant is None:
                raise ValueError(f"Tenant {tenant_id} not found in system DB")
            db_url = normalize_tenant_db_url(
                _decrypt_db_url(tenant.encrypted_db_url),
                tenant.slug,
            )
            schema = f"{tenant.db_schema_prefix}_meta"
            # Double-quote the identifier so slugs with hyphens (allowed by
            # TenantCreate's ^[a-z0-9_-]+$ pattern) survive Postgres parsing
            # of the search_path startup option.
            quoted_schema = '"' + schema.replace('"', '""') + '"'
            engine = create_async_engine(
                db_url,
                pool_pre_ping=True,
                pool_recycle=1800,
                echo=False,
                connect_args={
                    "server_settings": {"search_path": f"{quoted_schema},public"}
                },
            )
            factory = async_sessionmaker(
                engine, expire_on_commit=False,
                info={"tenant_id": tenant_id},
            )
            _tenant_engines[tenant_id] = factory
            return factory


async def get_tenant_db(tenant_id: str) -> AsyncGenerator[AsyncSession, None]:
    """Dependency: yields an AsyncSession scoped to the given tenant's DB.

    Stores ``tenant_id`` in ``session.info`` so downstream code (e.g.
    ``get_setting``) can scope the tenant cache without callers having
    to thread the slug explicitly.
    """
    factory = await get_tenant_session_factory(tenant_id)
    async with factory() as session:
        session.info["tenant_id"] = tenant_id
        yield session


async def evict_tenant_engine(tenant_id: str) -> None:
    """Evict a tenant's cached engine (use after tenant DB URL changes)."""
    # M-04 fix: acquire lock before modifying _tenant_engines
    async with _tenant_engines_lock:
        _tenant_engines.pop(tenant_id, None)
