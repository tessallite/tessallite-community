"""Embed token revocation — in-memory cached blocklist backed by PostgreSQL."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import delete, select

from shared.db.models import RevokedEmbedToken
from shared.db.session import get_system_db

_cache: set[str] = set()
_cache_loaded_at: float = 0.0
_CACHE_TTL_SECONDS = 60
_cache_lock = asyncio.Lock()  # H-05 fix: protect concurrent cache access


async def _refresh_cache() -> None:
    """Refresh the cache from database. Caller must hold _cache_lock."""
    global _cache, _cache_loaded_at
    now = datetime.now(timezone.utc)
    async for db in get_system_db():
        result = await db.execute(
            select(RevokedEmbedToken.jti).where(
                RevokedEmbedToken.expires_at > now
            )
        )
        _cache = {str(row[0]) for row in result.all()}
        _cache_loaded_at = time.monotonic()


async def _is_revoked_in_db(jti: str) -> bool:
    """Authoritative single-row check against the shared revocation table."""
    now = datetime.now(timezone.utc)
    async for db in get_system_db():
        try:
            uuid_jti = UUID(jti)
        except (ValueError, TypeError):
            return False
        row = await db.get(RevokedEmbedToken, uuid_jti)
        return row is not None and row.expires_at > now
    return False


async def is_embed_token_revoked(jti: str) -> bool:
    # H-05 fix: atomic check-then-refresh to prevent duplicate refreshes.
    async with _cache_lock:
        if time.monotonic() - _cache_loaded_at > _CACHE_TTL_SECONDS:
            await _refresh_cache()
        if jti in _cache:
            return True
    # F-021-08: a cache MISS on one replica can be stale — a token revoked on
    # another replica is not in this process's set until the next refresh (up
    # to 60s). Confirm a miss with an authoritative indexed PK lookup so cross-
    # replica revocation takes effect immediately. The positive (already-known
    # revoked) case above never reaches the DB; only the comparatively rare
    # "looks valid" case pays one cheap point lookup. Cache the confirmed
    # revocation so subsequent requests short-circuit.
    if await _is_revoked_in_db(jti):
        async with _cache_lock:
            _cache.add(jti)
        return True
    return False


async def revoke_embed_token(
    jti: UUID,
    tenant_id: str,
    revoked_by: str,
    expires_at: datetime,
) -> None:
    async for db in get_system_db():
        db.add(RevokedEmbedToken(
            jti=jti,
            tenant_id=tenant_id,
            revoked_by=revoked_by,
            expires_at=expires_at,
        ))
        await db.commit()
    # H-05 fix: acquire lock before adding to cache
    async with _cache_lock:
        _cache.add(str(jti))


async def prune_expired_tokens() -> int:
    async for db in get_system_db():
        now = datetime.now(timezone.utc)
        result = await db.execute(
            delete(RevokedEmbedToken).where(
                RevokedEmbedToken.expires_at <= now
            )
        )
        await db.commit()
        return result.rowcount
    return 0
