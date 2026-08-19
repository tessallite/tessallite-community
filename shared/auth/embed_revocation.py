"""Embed token revocation — in-memory cached blocklist backed by PostgreSQL.

Bug-6306 / Bug-6352: revocation is TENANT-SCOPED, in both the check and the
storage key.

The revoke endpoint takes a bare ``jti`` and the platform keeps no register of
issued embed tokens, so it cannot prove at revoke time that the caller owns the
token being named. A jti is not a secret either — it is plaintext in every
embed JWT, and those end up in ISV pages, referrer headers and access logs. The
authorization decision is therefore made where the owning tenant IS known: a
revocation only silences a token presented by the SAME tenant that recorded it.

Storage is keyed on ``(jti, tenant_id)``, not ``jti``. With one row per jti the
two tenants shared a slot, and both orderings were exploitable:

* stranger writes SECOND, last-writer-wins upsert → the owner's revocation is
  overwritten and the killed token is live again (access-control bypass);
* stranger writes FIRST, plain insert → the owner's own revoke collides on the
  primary key and the token can never be killed (denial of revocation).

Per-tenant rows remove the shared slot, so neither ordering is reachable. See
migration 0189.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from shared.db.models import RevokedEmbedToken
from shared.db.session import get_system_db

# jti -> the set of tenants that have a live revocation for it. A set, not a
# single value: two tenants may legitimately hold a record for the same jti
# (one of them is wrong about owning it, and it does not matter which).
_cache: dict[str, set[str]] = {}
_cache_loaded_at: float = 0.0
_CACHE_TTL_SECONDS = 60
_cache_lock = asyncio.Lock()  # H-05 fix: protect concurrent cache access


async def _refresh_cache() -> None:
    """Refresh the cache from database. Caller must hold _cache_lock."""
    global _cache, _cache_loaded_at
    now = datetime.now(timezone.utc)
    async for db in get_system_db():
        result = await db.execute(
            select(RevokedEmbedToken.jti, RevokedEmbedToken.tenant_id).where(
                RevokedEmbedToken.expires_at > now
            )
        )
        fresh: dict[str, set[str]] = {}
        for jti, tenant_id in result.all():
            fresh.setdefault(str(jti), set()).add(tenant_id)
        _cache = fresh
        _cache_loaded_at = time.monotonic()


async def _is_revoked_for_tenant_in_db(jti: str, tenant_id: str) -> bool:
    """Authoritative single-row check against the shared revocation table."""
    now = datetime.now(timezone.utc)
    async for db in get_system_db():
        try:
            uuid_jti = UUID(jti)
        except (ValueError, TypeError):
            return False
        row = await db.execute(
            select(RevokedEmbedToken.jti).where(
                RevokedEmbedToken.jti == uuid_jti,
                RevokedEmbedToken.tenant_id == tenant_id,
                RevokedEmbedToken.expires_at > now,
            )
        )
        return row.first() is not None
    return False


async def is_embed_token_revoked(jti: str, tenant_id: str) -> bool:
    """Return True if *jti* was revoked BY THE TENANT that owns the token.

    ``tenant_id`` is the tenant claim of the token being validated. A
    revocation recorded by a different tenant is not this tenant's business
    and is ignored (Bug-6306 / Bug-6352).
    """
    # H-05 fix: atomic check-then-refresh to prevent duplicate refreshes.
    async with _cache_lock:
        if time.monotonic() - _cache_loaded_at > _CACHE_TTL_SECONDS:
            await _refresh_cache()
        if tenant_id in _cache.get(jti, ()):
            return True
    # F-021-08: a cache MISS on one replica can be stale — a token revoked on
    # another replica is not in this process's map until the next refresh (up
    # to 60s). Confirm the miss with an authoritative indexed lookup so
    # cross-replica revocation takes effect immediately. The positive case
    # above never reaches the DB; only the comparatively rare "looks valid"
    # case pays one cheap point lookup. Cache the confirmed revocation so
    # subsequent requests short-circuit.
    if await _is_revoked_for_tenant_in_db(jti, tenant_id):
        async with _cache_lock:
            _cache.setdefault(jti, set()).add(tenant_id)
        return True
    return False


async def revoke_embed_token(
    jti: UUID,
    tenant_id: str,
    revoked_by: str,
    expires_at: datetime,
) -> None:
    """Record a revocation for *jti* under *tenant_id*.

    The upsert conflicts on the full ``(jti, tenant_id)`` key, so it only ever
    refreshes THIS tenant's own record — re-revoking is idempotent, and no
    tenant's write can touch another tenant's row.
    """
    async for db in get_system_db():
        stmt = (
            pg_insert(RevokedEmbedToken)
            .values(
                jti=jti,
                tenant_id=tenant_id,
                revoked_by=revoked_by,
                expires_at=expires_at,
            )
            .on_conflict_do_update(
                index_elements=[
                    RevokedEmbedToken.jti, RevokedEmbedToken.tenant_id,
                ],
                set_={
                    "revoked_by": revoked_by,
                    "expires_at": expires_at,
                    "revoked_at": datetime.now(timezone.utc),
                },
            )
        )
        await db.execute(stmt)
        await db.commit()
    # H-05 fix: acquire lock before touching the cache
    async with _cache_lock:
        _cache.setdefault(str(jti), set()).add(tenant_id)


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
