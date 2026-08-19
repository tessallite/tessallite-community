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
from sqlalchemy.pool import NullPool

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
# Bug-6674: cache the (engine, factory) pair so eviction can dispose() the
# engine, closing its connection pool instead of stranding connections until
# GC.  The public API continues to return only the factory; the engine is
# kept internally for lifecycle management.
_tenant_engines: dict[str, tuple[object, async_sessionmaker]] = {}
# Bug-7980 follow-up: a SEPARATE NullPool-backed engine per tenant used ONLY for
# the short-lived REPEATABLE READ Save snapshot session, so that second
# connection never draws from / blocks on the bounded request pool while many
# Saves are paused on the per-model advisory lock (which would starve/deadlock
# the request pool). NullPool opens and closes a dedicated connection each use.
# INVARIANT: this cache is disposed only via ``evict_tenant_engine``; that is safe
# ONLY because ``poolclass=NullPool`` holds no idle connections. If the pool class
# ever changes, add an explicit shutdown-disposal hook for both caches.
_tenant_snapshot_engines: dict[str, tuple[object, async_sessionmaker]] = {}
_tenant_engines_lock = asyncio.Lock()  # M-04 fix: protect concurrent access


def _decrypt_db_url(encrypted: bytes) -> str:
    from shared.security.credential_crypto import decrypt_str
    return decrypt_str(encrypted)


def normalize_tenant_db_url(db_url: str, _tenant_slug: str | None = None) -> str:
    """Rewrite a tenant DB URL to use the SYSTEM database connection details.

    Bug-6675: the stored tenant URL is NOT used as-is. The function
    force-rewrites host, port, username, password, and database from
    ``SYSTEM_DATABASE_URL`` so that all tenant schemas route through the
    current environment's active database server. This is correct for the
    single-database deployment model where every tenant schema lives inside
    the same PostgreSQL instance as the system schema.

    ``_tenant_slug`` is accepted for backward compatibility but unused.

    On parse failure the original ``db_url`` is returned unchanged so the
    caller can still attempt a connection (best-effort).
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


async def _resolve_tenant_dsn(tenant_id: str) -> tuple[str, str]:
    """Look up and normalise a tenant's DB URL + its quoted ``_meta`` schema.

    Double-quotes the schema identifier so slugs with hyphens (allowed by
    TenantCreate's ``^[a-z0-9_-]+$`` pattern) survive Postgres parsing of the
    ``search_path`` startup option.
    """
    async with SystemSessionLocal() as sys_db:
        result = await sys_db.execute(
            select(SystemTenant).where(SystemTenant.slug == tenant_id)
        )
        tenant = result.scalar_one_or_none()
        if tenant is None:
            raise ValueError(f"Tenant {tenant_id} not found in system DB")
        db_url = normalize_tenant_db_url(
            _decrypt_db_url(tenant.encrypted_db_url),
        )
        schema = f"{tenant.db_schema_prefix}_meta"
        quoted_schema = '"' + schema.replace('"', '""') + '"'
        return db_url, quoted_schema


async def get_tenant_session_factory(tenant_id: str) -> async_sessionmaker:
    """Returns (and caches) a session factory for the given tenant's DB."""
    # M-04 fix: atomic check-then-create with lock to prevent duplicate engines
    async with _tenant_engines_lock:
        if tenant_id in _tenant_engines:
            _engine, factory = _tenant_engines[tenant_id]
            return factory

        db_url, quoted_schema = await _resolve_tenant_dsn(tenant_id)
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
        _tenant_engines[tenant_id] = (engine, factory)
        return factory


async def get_tenant_snapshot_session_factory(tenant_id: str) -> async_sessionmaker:
    """Returns (and caches) a NullPool session factory for the tenant's DB.

    Bug-7980 follow-up: the Save snapshot runs in its own short-lived
    REPEATABLE READ transaction on a SECOND connection while the request's main
    connection AND the per-model advisory lock are still held. Backing that
    second connection with a NullPool engine means each snapshot opens its own
    dedicated connection (closed immediately after) instead of competing for a
    slot in the bounded request pool — eliminating the starvation/deadlock a
    burst of lock-paused Saves would otherwise cause.
    """
    async with _tenant_engines_lock:
        if tenant_id in _tenant_snapshot_engines:
            _engine, factory = _tenant_snapshot_engines[tenant_id]
            return factory

        db_url, quoted_schema = await _resolve_tenant_dsn(tenant_id)
        engine = create_async_engine(
            db_url,
            poolclass=NullPool,
            echo=False,
            connect_args={
                "server_settings": {"search_path": f"{quoted_schema},public"}
            },
        )
        factory = async_sessionmaker(
            engine, expire_on_commit=False,
            info={"tenant_id": tenant_id},
        )
        _tenant_snapshot_engines[tenant_id] = (engine, factory)
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
    """Evict a tenant's cached engine (use after tenant DB URL changes).

    Bug-6674: dispose() the evicted engine so its connection pool is closed
    immediately rather than leaking up to pool_size server connections
    until GC collects the orphaned engine.
    """
    # M-04 fix: acquire lock before modifying _tenant_engines
    async with _tenant_engines_lock:
        entry = _tenant_engines.pop(tenant_id, None)
        if entry is not None:
            engine, _factory = entry
            await engine.dispose()
        # Bug-7980 follow-up: evict the parallel NullPool snapshot engine too so a
        # tenant DB URL change does not leave a stale snapshot engine behind.
        snap_entry = _tenant_snapshot_engines.pop(tenant_id, None)
        if snap_entry is not None:
            snap_engine, _snap_factory = snap_entry
            await snap_engine.dispose()
