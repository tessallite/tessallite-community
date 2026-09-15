"""
Two-level database session management.

- System DB: global PostgreSQL for tenant registry
- Tenant DB: tenant metadata lives in per-tenant schemas within the shared DB
"""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import asynccontextmanager
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
from shared.db.tenant_readiness import (
    TenantReadinessError,
    ensure_tenant_schema_ready,
    raise_tenant_readiness_error,
)

logger = logging.getLogger(__name__)
settings = get_settings()

# ---------------------------------------------------------------------------
# System DB engine (single global engine)
# ---------------------------------------------------------------------------
_system_engine = create_async_engine(
    settings.SYSTEM_DATABASE_URL,
    # SQLAlchemy pool_size=0 disables the size limit (including overflow).
    # Positive sizes retain an explicit cap with no overflow.
    pool_size=settings.SYSTEM_DB_POOL_SIZE,
    max_overflow=0,
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
#
# Bug-9192 / RFGPT-002: OrderedDict + TENANT_ENGINE_CACHE_MAX so the number of
# retained engines (and therefore idle connections) is bounded per process.
# Access refreshes LRU order; overflow disposes the least-recently-used engine.
_tenant_engines: OrderedDict[str, tuple[object, async_sessionmaker]] = OrderedDict()
# Bug-7980 follow-up: a SEPARATE NullPool-backed engine per tenant used ONLY for
# the short-lived REPEATABLE READ Save snapshot session, so that second
# connection never draws from / blocks on the bounded request pool while many
# Saves are paused on the per-model advisory lock (which would starve/deadlock
# the request pool). NullPool opens and closes a dedicated connection each use.
# INVARIANT: this cache is disposed only via ``evict_tenant_engine`` / LRU
# eviction; that is safe ONLY because ``poolclass=NullPool`` holds no idle
# connections. If the pool class ever changes, add an explicit shutdown-disposal
# hook for both caches.
_tenant_snapshot_engines: OrderedDict[str, tuple[object, async_sessionmaker]] = OrderedDict()
_tenant_engines_lock = asyncio.Lock()  # protect cache mutation/read ordering
# Serialize cold creation and cache publication for one tenant while leaving
# different tenants free to resolve and probe independently. The cache lock
# below remains a short mutation/read lock. Neither lock may span readiness,
# connection-pool, or disposal I/O (Bug-9990).
# Each entry is retained only while at least one getter/eviction caller owns a
# lease.  A strong, explicitly reference-counted registry is needed here:
# cancelled coroutines can retain their local lock in a traceback after the
# caller has returned, which makes weak-value collection an unreliable bound.
class _TenantCreationState:
    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0


_tenant_creation_locks: dict[str, _TenantCreationState] = {}


def _tenant_engine_cache_max() -> int:
    return int(settings.TENANT_ENGINE_CACHE_MAX)


async def _dispose_engine_entry(entry: tuple[object, async_sessionmaker] | None) -> None:
    if entry is None:
        return
    engine, _factory = entry
    await engine.dispose()


async def _dispose_engine_entries(
    entries: list[tuple[object, async_sessionmaker]],
) -> None:
    """Dispose entries after the cache lock has been released."""
    for entry in entries:
        await _dispose_engine_entry(entry)


async def _cached_engine_entry(
    cache: OrderedDict[str, tuple[object, async_sessionmaker]],
    tenant_id: str,
) -> tuple[object, async_sessionmaker] | None:
    """Read and refresh one cache entry without doing any I/O under the lock."""
    async with _tenant_engines_lock:
        entry = cache.get(tenant_id)
        if entry is not None:
            cache.move_to_end(tenant_id)
        return entry


async def _release_tenant_creation_lease(
    tenant_id: str,
    state: _TenantCreationState,
) -> None:
    """Drop one active lease and remove an idle tenant lock."""
    async with _tenant_engines_lock:
        if _tenant_creation_locks.get(tenant_id) is not state:
            return
        state.users -= 1
        if state.users == 0:
            _tenant_creation_locks.pop(tenant_id, None)


@asynccontextmanager
async def _tenant_creation_lock(tenant_id: str):
    """Serialize one tenant's engine creation/publication with a bounded lease.

    Registry accounting is kept under the short cache lock. Database authority
    lookup, readiness checks, pool checkout, and engine disposal all run
    outside this lock. Cancellation before acquisition still releases the
    registry lease, so unknown-slug churn cannot accumulate state.
    """
    async with _tenant_engines_lock:
        state = _tenant_creation_locks.get(tenant_id)
        if state is None:
            state = _TenantCreationState()
            _tenant_creation_locks[tenant_id] = state
        state.users += 1

    acquired = False
    try:
        try:
            await state.lock.acquire()
            acquired = True
        except BaseException:
            raise
        yield state.lock
    finally:
        if acquired:
            state.lock.release()
        await _release_tenant_creation_lease(tenant_id, state)


async def _remove_cached_engine_entry(
    cache: OrderedDict[str, tuple[object, async_sessionmaker]],
    tenant_id: str,
    expected: tuple[object, async_sessionmaker],
) -> list[tuple[object, async_sessionmaker]]:
    """Remove a still-current entry and its sibling without awaiting in lock."""
    removed: list[tuple[object, async_sessionmaker]] = []
    async with _tenant_engines_lock:
        if cache.get(tenant_id) is not expected:
            return removed
        cache.pop(tenant_id, None)
        removed.append(expected)
        sibling_cache = (
            _tenant_snapshot_engines
            if cache is _tenant_engines
            else _tenant_engines
        )
        sibling = sibling_cache.pop(tenant_id, None)
        if sibling is not None:
            removed.append(sibling)
    return removed


async def _publish_request_engine(
    tenant_id: str,
    entry: tuple[object, async_sessionmaker],
    *,
    db_url: str,
    quoted_schema: str,
) -> tuple[tuple[object, async_sessionmaker], bool, list[tuple[object, async_sessionmaker]]]:
    """Publish a request entry while keeping all waits outside the lock.

    Returns the winning entry, whether the candidate won, and entries that the
    caller must dispose after this function releases the lock.
    """
    dispose: list[tuple[object, async_sessionmaker]] = []
    async with _tenant_engines_lock:
        current = _tenant_engines.get(tenant_id)
        if current is not None and _entry_matches(current, db_url, quoted_schema):
            _tenant_engines.move_to_end(tenant_id)
            dispose.append(entry)
            return current, False, dispose

        if current is not None:
            _tenant_engines.pop(tenant_id, None)
            dispose.append(current)
            snap_entry = _tenant_snapshot_engines.pop(tenant_id, None)
            if snap_entry is not None:
                dispose.append(snap_entry)

        _tenant_engines[tenant_id] = entry
        _tenant_engines.move_to_end(tenant_id)
        limit = _tenant_engine_cache_max()
        while len(_tenant_engines) > limit:
            old_id, old_entry = _tenant_engines.popitem(last=False)
            logger.info(
                "Bug-9192: disposing LRU tenant engine for %s (cache_max=%d)",
                old_id,
                limit,
            )
            dispose.append(old_entry)
            # Keep the parallel NullPool snapshot engine in sync so a URL-stale
            # snapshot cannot outlive its request-engine sibling.
            snap_entry = _tenant_snapshot_engines.pop(old_id, None)
            if snap_entry is not None:
                dispose.append(snap_entry)
        return entry, True, dispose


async def _publish_snapshot_engine(
    tenant_id: str,
    entry: tuple[object, async_sessionmaker],
    *,
    db_url: str,
    quoted_schema: str,
) -> tuple[tuple[object, async_sessionmaker], bool, list[tuple[object, async_sessionmaker]]]:
    """Publish a snapshot entry without awaiting while the cache is locked."""
    dispose: list[tuple[object, async_sessionmaker]] = []
    async with _tenant_engines_lock:
        current = _tenant_snapshot_engines.get(tenant_id)
        if current is not None and _entry_matches(current, db_url, quoted_schema):
            _tenant_snapshot_engines.move_to_end(tenant_id)
            dispose.append(entry)
            return current, False, dispose

        if current is not None:
            _tenant_snapshot_engines.pop(tenant_id, None)
            dispose.append(current)
            request_entry = _tenant_engines.pop(tenant_id, None)
            if request_entry is not None:
                dispose.append(request_entry)

        _tenant_snapshot_engines[tenant_id] = entry
        _tenant_snapshot_engines.move_to_end(tenant_id)
        # Snapshot engines hold no idle connections; still bound the dict so a
        # long-lived process cannot accumulate unbounded engine objects.
        limit = _tenant_engine_cache_max()
        while len(_tenant_snapshot_engines) > limit:
            old_id, old_entry = _tenant_snapshot_engines.popitem(last=False)
            dispose.append(old_entry)
        return entry, True, dispose


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
        # A parser exception can include the complete URL. Keep the failure
        # useful without placing a credential or whole DSN in service logs.
        logger.error("Error normalizing tenant database URL (%s)", type(e).__name__)
        return db_url


async def _resolve_tenant_dsn(tenant_id: str) -> tuple[str, str]:
    """Look up and normalise a tenant's DB URL + its quoted ``_meta`` schema.

    Double-quotes the schema identifier so slugs with hyphens (allowed by
    TenantCreate's ``^[a-z0-9_-]+$`` pattern) survive Postgres parsing of the
    ``search_path`` startup option.
    """
    try:
        async with SystemSessionLocal() as sys_db:
            result = await sys_db.execute(
                select(SystemTenant).where(SystemTenant.slug == tenant_id)
            )
            tenant = result.scalar_one_or_none()
            if tenant is None:
                raise ValueError(f"Tenant {tenant_id} not found in system DB")
            try:
                stored_url = _decrypt_db_url(tenant.encrypted_db_url)
            except Exception as exc:  # noqa: BLE001 - tenant access must fail closed
                raise_tenant_readiness_error(
                    tenant_slug=tenant_id,
                    operation="tenant database session acquisition",
                    cause=(
                        "stored DB credentials cannot be decrypted with configured keys"
                    ),
                    original=exc,
                )
            db_url = normalize_tenant_db_url(stored_url)
            schema = f"{tenant.db_schema_prefix}_meta"
            quoted_schema = '"' + schema.replace('"', '""') + '"'
            return db_url, quoted_schema
    except TenantReadinessError:
        raise
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - unreadable authority must fail closed
        raise_tenant_readiness_error(
            tenant_slug=tenant_id,
            operation="tenant database session acquisition",
            cause="tenant registry could not be read",
            original=exc,
        )


def _engine_matches_url(engine: object, db_url: str) -> bool:
    """Return whether a cached SQLAlchemy engine still uses the resolved URL."""
    try:
        actual = engine.url.render_as_string(hide_password=False)
    except Exception:
        # Test doubles and non-SQLAlchemy adapters do not expose ``url``. The
        # real engines created in this module always do; readiness still runs
        # for every such cached entry.
        return True
    return actual == db_url


def _factory_schema(factory: object) -> str | None:
    """Read the schema captured when a tenant factory was created."""
    kw = getattr(factory, "kw", None)
    info = kw.get("info") if isinstance(kw, Mapping) else None
    if isinstance(info, Mapping):
        schema = info.get("tenant_schema")
        return str(schema) if schema is not None else None
    return None


def _entry_matches(
    entry: tuple[object, async_sessionmaker],
    db_url: str,
    quoted_schema: str,
) -> bool:
    """Match both connection authority and startup search path."""
    engine, factory = entry
    if not _engine_matches_url(engine, db_url):
        return False
    # Test doubles from older focused guards may not expose factory.kw. Real
    # tenant factories always capture the schema, so a missing value is only
    # tolerated for those doubles.
    cached_schema = _factory_schema(factory)
    return cached_schema is None or cached_schema == quoted_schema


_TENANT_ENGINE_INFO = "_tenant_readiness_engine"
_TENANT_OPERATION_INFO = "_tenant_readiness_operation"


class _TenantReadinessSession(AsyncSession):
    """Recheck authority and schema readiness when a factory is actually used.

    ``async_sessionmaker.__call__`` is synchronous, so a retained factory cannot
    await the system authority lookup there.  The production callers enumerate
    as ``async with factory()``; checking at context entry closes that reuse
    gap without recursively acquiring the factory or holding the cache lock
    during readiness I/O.
    """

    async def __aenter__(self) -> _TenantReadinessSession:
        info = self.info
        engine = info.get(_TENANT_ENGINE_INFO)
        tenant_id = info.get("tenant_id")
        quoted_schema = info.get("tenant_schema")
        operation = info.get(
            _TENANT_OPERATION_INFO,
            "tenant database session use",
        )
        if engine is not None and tenant_id and quoted_schema:
            current_url, current_schema = await _resolve_tenant_dsn(str(tenant_id))
            if (
                not _engine_matches_url(engine, current_url)
                or current_schema != quoted_schema
            ):
                raise_tenant_readiness_error(
                    tenant_slug=str(tenant_id),
                    operation=str(operation),
                    cause=(
                        "tenant database authority changed; reacquire the tenant "
                        "session"
                    ),
                )
            await ensure_tenant_schema_ready(
                engine,
                tenant_slug=str(tenant_id),
                quoted_schema=str(quoted_schema),
                operation=str(operation),
            )
        return await super().__aenter__()


def _tenant_session_factory(
    engine: object,
    *,
    tenant_id: str,
    quoted_schema: str,
    operation: str,
) -> async_sessionmaker:
    """Create a factory that retains the serving-boundary readiness guard."""
    return async_sessionmaker(
        engine,
        class_=_TenantReadinessSession,
        expire_on_commit=False,
        info={
            "tenant_id": tenant_id,
            "tenant_schema": quoted_schema,
            _TENANT_ENGINE_INFO: engine,
            _TENANT_OPERATION_INFO: operation,
        },
    )


async def get_tenant_session_factory(tenant_id: str) -> async_sessionmaker:
    """Returns (and caches) a session factory for the given tenant's DB."""
    # A cache-hit readiness check may wait for a bounded request-pool slot. It
    # must run before taking the creation lock: a Save can hold that pool slot
    # while it needs the same lock to acquire its independent snapshot factory.
    db_url, quoted_schema = await _resolve_tenant_dsn(tenant_id)
    cached = await _cached_engine_entry(_tenant_engines, tenant_id)
    if cached is not None and _entry_matches(cached, db_url, quoted_schema):
        engine, factory = cached
        await ensure_tenant_schema_ready(
            engine,
            tenant_slug=tenant_id,
            quoted_schema=quoted_schema,
            operation="tenant database session acquisition",
        )
        return factory

    dispose: list[tuple[object, async_sessionmaker]] = []
    candidate: tuple[object, async_sessionmaker] | None = None
    async with _tenant_creation_lock(tenant_id):
        # Recheck after acquiring the narrow creation/publication lock. No
        # database connection or engine disposal is awaited while it is held.
        current = await _cached_engine_entry(_tenant_engines, tenant_id)
        if current is not None and _entry_matches(current, db_url, quoted_schema):
            candidate = current
        else:
            try:
                engine = create_async_engine(
                    db_url,
                    # Bug-9192/Bug-9613: bound each cached tenant pool. The aggregate
                    # retained connection product is validated by Settings before the
                    # engine can be created.
                    pool_size=settings.TENANT_DB_POOL_SIZE,
                    max_overflow=0,
                    pool_pre_ping=True,
                    pool_recycle=1800,
                    echo=False,
                    connect_args={
                        "server_settings": {"search_path": f"{quoted_schema},public"}
                    },
                )
            except Exception as exc:  # noqa: BLE001 - malformed credentials fail closed
                raise_tenant_readiness_error(
                    tenant_slug=tenant_id,
                    operation="tenant database session acquisition",
                    cause="tenant database connection could not be opened",
                    original=exc,
                )
            factory = _tenant_session_factory(
                engine,
                tenant_id=tenant_id,
                quoted_schema=quoted_schema,
                operation="tenant database session use",
            )
            if current is not None:
                dispose.extend(
                    await _remove_cached_engine_entry(
                        _tenant_engines,
                        tenant_id,
                        current,
                    )
                )
            candidate, _candidate_won, publish_dispose = await _publish_request_engine(
                tenant_id,
                (engine, factory),
                db_url=db_url,
                quoted_schema=quoted_schema,
            )
            dispose.extend(publish_dispose)

    await _dispose_engine_entries(dispose)
    assert candidate is not None
    engine, factory = candidate
    try:
        await ensure_tenant_schema_ready(
            engine,
            tenant_slug=tenant_id,
            quoted_schema=quoted_schema,
            operation="tenant database session acquisition",
        )
    except Exception:
        await _dispose_engine_entries(
            await _remove_cached_engine_entry(
                _tenant_engines,
                tenant_id,
                candidate,
            )
        )
        raise
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
    db_url, quoted_schema = await _resolve_tenant_dsn(tenant_id)
    cached = await _cached_engine_entry(_tenant_snapshot_engines, tenant_id)
    if cached is not None and _entry_matches(cached, db_url, quoted_schema):
        engine, factory = cached
        await ensure_tenant_schema_ready(
            engine,
            tenant_slug=tenant_id,
            quoted_schema=quoted_schema,
            operation="tenant snapshot session acquisition",
        )
        return factory

    dispose: list[tuple[object, async_sessionmaker]] = []
    candidate: tuple[object, async_sessionmaker] | None = None
    async with _tenant_creation_lock(tenant_id):
        current = await _cached_engine_entry(_tenant_snapshot_engines, tenant_id)
        if current is not None and _entry_matches(current, db_url, quoted_schema):
            candidate = current
        else:
            # Bug-9192: NullPool retains no connections and accepts no
            # pool_size/max_overflow knobs, so this short-lived snapshot path is
            # already bounded to one connection per active snapshot operation.
            try:
                engine = create_async_engine(
                    db_url,
                    poolclass=NullPool,
                    echo=False,
                    connect_args={
                        "server_settings": {"search_path": f"{quoted_schema},public"}
                    },
                )
            except Exception as exc:  # noqa: BLE001 - malformed credentials fail closed
                raise_tenant_readiness_error(
                    tenant_slug=tenant_id,
                    operation="tenant snapshot session acquisition",
                    cause="tenant database connection could not be opened",
                    original=exc,
                )
            factory = _tenant_session_factory(
                engine,
                tenant_id=tenant_id,
                quoted_schema=quoted_schema,
                operation="tenant snapshot session use",
            )
            if current is not None:
                dispose.extend(
                    await _remove_cached_engine_entry(
                        _tenant_snapshot_engines,
                        tenant_id,
                        current,
                    )
                )
            candidate, _candidate_won, publish_dispose = await _publish_snapshot_engine(
                tenant_id,
                (engine, factory),
                db_url=db_url,
                quoted_schema=quoted_schema,
            )
            dispose.extend(publish_dispose)

    await _dispose_engine_entries(dispose)
    assert candidate is not None
    engine, factory = candidate
    try:
        await ensure_tenant_schema_ready(
            engine,
            tenant_slug=tenant_id,
            quoted_schema=quoted_schema,
            operation="tenant snapshot session acquisition",
        )
    except Exception:
        await _dispose_engine_entries(
            await _remove_cached_engine_entry(
                _tenant_snapshot_engines,
                tenant_id,
                candidate,
            )
        )
        raise
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
    # Serialize the cache mutation with cold publication, then release the
    # creation lock before disposal. Pool cleanup must never delay snapshot
    # factory publication for this tenant.
    entries: list[tuple[object, async_sessionmaker]] = []
    async with _tenant_creation_lock(tenant_id):
        async with _tenant_engines_lock:
            entry = _tenant_engines.pop(tenant_id, None)
            if entry is not None:
                entries.append(entry)
            # Bug-7980 follow-up: evict the parallel NullPool snapshot engine too so a
            # tenant DB URL change does not leave a stale snapshot engine behind.
            snap_entry = _tenant_snapshot_engines.pop(tenant_id, None)
            if snap_entry is not None:
                entries.append(snap_entry)
    await _dispose_engine_entries(entries)
