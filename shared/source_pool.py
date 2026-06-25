"""Connection pool manager for source PostgreSQL databases.

Pools are keyed by ``(host, port, database, user, password-hash)`` and created
lazily on first use.  BigQuery and Spark connections are not pooled — they use
HTTP/Thrift transports with their own session management.

Credential lifecycle (F-014-06): asyncpg authenticates at connect time, so a
pool created with an old password keeps working until the source revokes that
password.  Because pools live in per-replica process memory, an API-triggered
purge cannot reach every replica.  Including a short hash of the password in
the key makes a credential change route to a *new* pool automatically; the
stale pool then simply ages out via the idle reaper below.  This is the only
replica-safe invalidation strategy (no cross-process purge needed).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

import asyncpg

from shared.config.bootstrap import system_snapshot_get

logger = logging.getLogger(__name__)

# value: (pool, last_used_monotonic)
_pools: dict[str, tuple[asyncpg.Pool, float]] = {}
_lock = asyncio.Lock()

MIN_POOL_SIZE = 2
MAX_POOL_SIZE = 10
# F-014-06: a pool untouched for this long is closed by the reaper so a stale
# (rotated-credential or changed-host) pool does not hold idle connections
# forever. 30 minutes balances connection reuse against credential freshness.
POOL_IDLE_TTL_SECONDS = 1800


def _password_fingerprint(password: str) -> str:
    """Short, non-reversible fingerprint of the password for the pool key.

    Never logs or stores the password itself — only a truncated SHA-256 so a
    credential change produces a different key without exposing the secret."""
    return hashlib.sha256((password or "").encode()).hexdigest()[:12]


def _pool_key(host: str, port: int, database: str, user: str, password: str) -> str:
    # F-014-06: the password fingerprint is part of the key so rotating the
    # source password routes acquires to a fresh pool instead of reusing one
    # authenticated with the old (now-revoked) password.
    return f"{user}@{host}:{port}/{database}#{_password_fingerprint(password)}"


async def _get_or_create_pool(
    host: str, port: int, database: str, user: str, password: str,
) -> asyncpg.Pool:
    key = _pool_key(host, port, database, user, password)
    # C-01/C-05 fix: all _pools dict access must be inside the lock to prevent
    # race conditions with concurrent remove_pool() or close_all_pools() calls.
    async with _lock:
        await _reap_idle_pools_locked()
        if key in _pools:
            pool, _ = _pools[key]
            if not pool._closed:
                _pools[key] = (pool, time.monotonic())
                return pool
            # Pool is closed — remove stale entry before creating a new one
            del _pools[key]

        command_timeout = float(system_snapshot_get("query.statement_timeout_seconds"))
        connect_timeout = float(system_snapshot_get("query.connect_timeout_seconds"))
        pool = await asyncpg.create_pool(
            host=host, port=port, database=database,
            user=user, password=password,
            min_size=MIN_POOL_SIZE,
            max_size=MAX_POOL_SIZE,
            command_timeout=command_timeout,
            timeout=connect_timeout,
        )
        _pools[key] = (pool, time.monotonic())
        logger.debug("Created source pool %s (min=%d, max=%d)", key, MIN_POOL_SIZE, MAX_POOL_SIZE)
        return pool


async def _reap_idle_pools_locked() -> None:
    """Close and drop pools untouched for longer than the idle TTL.

    Caller must already hold ``_lock``. Pools are closed *outside* the lock to
    avoid holding it during I/O, so this collects victims first."""
    now = time.monotonic()
    expired = [
        key for key, (_, last_used) in _pools.items()
        if now - last_used > POOL_IDLE_TTL_SECONDS
    ]
    for key in expired:
        pool, _ = _pools.pop(key)
        # Schedule close without awaiting under the lock.
        asyncio.create_task(_close_pool(pool, key))


@asynccontextmanager
async def acquire_source_connection(
    host: str, port: int, database: str, user: str, password: str,
) -> AsyncIterator[asyncpg.Connection]:
    pool = await _get_or_create_pool(host, port, database, user, password)
    async with pool.acquire() as conn:
        yield conn


async def remove_pool(
    host: str, port: int, database: str, user: str, password: str | None = None,
) -> None:
    """Remove and close pool(s) for an endpoint. Must be called from an async context.

    F-014-06: with ``password`` omitted, every pool for the
    ``user@host:port/database`` endpoint (regardless of credential fingerprint)
    is removed — useful for an explicit invalidation after a credential change
    within a single process."""
    # C-01 fix: acquire lock before modifying _pools dict
    if password is not None:
        keys = [_pool_key(host, port, database, user, password)]
    else:
        prefix = f"{user}@{host}:{port}/{database}#"
        keys = [k for k in _pools if k.startswith(prefix)]
    victims = []
    async with _lock:
        for key in keys:
            entry = _pools.pop(key, None)
            if entry is not None:
                victims.append((key, entry[0]))
    # Close the pools outside the lock to avoid holding it during I/O
    for key, pool in victims:
        if pool and not pool._closed:
            await _close_pool(pool, key)


async def _close_pool(pool: asyncpg.Pool, key: str) -> None:
    try:
        await pool.close()
        logger.debug("Closed source pool %s", key)
    except Exception:
        logger.warning("Error closing source pool %s", key, exc_info=True)


async def close_all_pools() -> None:
    """Close all pools. Acquires lock to safely iterate and clear the dict."""
    # C-01 fix: acquire lock before accessing _pools dict
    async with _lock:
        pools_to_close = [(key, entry[0]) for key, entry in _pools.items()]
        _pools.clear()
    # Close pools outside the lock to avoid holding it during I/O
    for key, pool in pools_to_close:
        try:
            await pool.close()
            logger.debug("Closed source pool %s", key)
        except Exception:
            logger.warning("Error closing source pool %s", key, exc_info=True)
