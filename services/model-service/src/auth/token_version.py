"""Atomic LocalUser token-version invalidation helpers."""
from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from shared.db.models import LocalUser


async def bump_local_user_token_version(
    db: AsyncSession,
    user: LocalUser,
) -> int:
    """Atomically increment ``LocalUser.token_version`` and align ORM state.

    The helper intentionally does not commit. Existing route/JIT transactions
    decide when the security mutation and its audit record become durable.
    """
    result = await db.execute(
        update(LocalUser)
        .where(LocalUser.id == user.id)
        .values(token_version=LocalUser.token_version + 1)
        .returning(LocalUser.token_version)
    )
    new_version = result.scalar_one_or_none()
    if new_version is None:
        raise ValueError("LocalUser row not found while bumping token_version")
    try:
        set_committed_value(user, "token_version", int(new_version))
    except AttributeError:
        # Unit tests use lightweight stand-ins; real route/JIT paths pass ORM
        # instances where set_committed_value protects the identity map.
        user.token_version = int(new_version)
    return int(new_version)
