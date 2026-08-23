"""Per-account login lockout (G-021-04).

Stored in ``tess_system.login_lockouts`` so discover can lock unknown-email
probes without a tenant session. Failure increments commit immediately so a
subsequent 401 still persists the counter. Success clears the row on the
caller session (committed with the login audit).

Pre-migration bootstrap: the table is created by migration 0212, so a system
whose schema is behind that migration cannot serve lockout queries. The
login-gated migration path (deploy migrate-then-serve, first-install
post-deploy) must still be able to authenticate, so every public helper here
fails OPEN (no lockout) when the table does not exist, with a rollback so the
session is left usable. Any other database error still raises.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.identity import canonical_email
from shared.db.models import LoginLockout
from shared.db.pre_migration import is_missing_table_error

SYSTEM_SCOPE = "__system__"
DISCOVER_SCOPE = "__discover__"

_MAX_FAILURES = 5
_LOCKOUT = timedelta(minutes=15)


async def _get(
    db: AsyncSession, scope_key: str, email: str
) -> LoginLockout | None:
    result = await db.execute(
        select(LoginLockout).where(
            LoginLockout.scope_key == scope_key,
            LoginLockout.email_canonical == canonical_email(email),
        )
    )
    return result.scalar_one_or_none()


async def assert_not_locked(db: AsyncSession, scope_key: str, email: str) -> None:
    try:
        row = await _get(db, scope_key, email)
    except ProgrammingError as exc:
        if is_missing_table_error(exc):
            await db.rollback()
            return  # pre-migration: lockout unavailable; fail open for bootstrap
        raise
    if row is None or row.locked_until is None:
        return
    until = row.locked_until
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    if until > datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Try again later.",
        )


async def record_login_failure(
    db: AsyncSession, scope_key: str, email: str
) -> None:
    try:
        email_c = canonical_email(email)
        now = datetime.now(timezone.utc)
        row = await _get(db, scope_key, email_c)
        if row is None:
            row = LoginLockout(
                scope_key=scope_key,
                email_canonical=email_c,
                failed_count=1,
                locked_until=None,
            )
            db.add(row)
        else:
            if row.locked_until is not None:
                until = row.locked_until
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
                if until <= now:
                    row.failed_count = 0
                    row.locked_until = None
            row.failed_count = int(row.failed_count or 0) + 1
            if row.failed_count >= _MAX_FAILURES:
                row.locked_until = now + _LOCKOUT
        await db.flush()
        await db.commit()
    except ProgrammingError as exc:
        if is_missing_table_error(exc):
            await db.rollback()
            return  # pre-migration: failure counter unavailable; fail open
        raise


async def record_login_success(
    db: AsyncSession, scope_key: str, email: str
) -> None:
    try:
        row = await _get(db, scope_key, email)
        if row is not None:
            await db.delete(row)
            await db.flush()
    except ProgrammingError as exc:
        if is_missing_table_error(exc):
            await db.rollback()
            return  # pre-migration: nothing to clear; fail open
        raise
