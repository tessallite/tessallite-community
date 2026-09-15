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
from sqlalchemy import case, func, null, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.identity import canonical_email
from shared.config.settings import get_settings
from shared.db.models import LoginLockout
from shared.db.pre_migration import is_missing_table_error

SYSTEM_SCOPE = "__system__"
DISCOVER_SCOPE = "__discover__"

_MAX_FAILURES = get_settings().TESSALLITE_LOGIN_LOCKOUT_MAX_FAILURES
_LOCKOUT = timedelta(minutes=15)


async def _get(
    db: AsyncSession,
    scope_key: str,
    email: str,
    *,
    for_update: bool = False,
) -> LoginLockout | None:
    statement = select(LoginLockout).where(
        LoginLockout.scope_key == scope_key,
        LoginLockout.email_canonical == canonical_email(email),
    )
    if for_update:
        statement = statement.with_for_update()
    result = await db.execute(statement)
    return result.scalar_one_or_none()


async def assert_not_locked(
    db: AsyncSession,
    scope_key: str,
    email: str,
    *,
    lock_row: bool = False,
) -> None:
    # 0 = account locking disabled (the default). Return before touching the
    # database so a disabled lockout costs nothing and takes no row lock.
    if _MAX_FAILURES <= 0:
        return
    try:
        row = await _get(db, scope_key, email, for_update=lock_row)
    except ProgrammingError as exc:
        if is_missing_table_error(exc):
            await db.rollback()
            return  # pre-migration: lockout unavailable; fail open for bootstrap
        raise
    if row is None or row.locked_until is None:
        return
    # Bug-9833: raising the threshold for an isolated demo profile must also
    # make a row locked under the lower default immediately usable. A
    # successful login then deletes that stale counter through the normal path.
    if row.failed_count < _MAX_FAILURES:
        return
    until = row.locked_until
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    if until > datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Try again later.",
        )


async def record_login_failure(db: AsyncSession, scope_key: str, email: str) -> None:
    try:
        email_c = canonical_email(email)
        now = datetime.now(timezone.utc)
        table = LoginLockout.__table__
        locked_until = table.c.locked_until
        expired = locked_until.is_not(None) & (locked_until <= now)
        next_count = case(
            (expired, 1),
            else_=func.coalesce(table.c.failed_count, 0) + 1,
        )
        statement = (
            pg_insert(LoginLockout)
            .values(
                scope_key=scope_key,
                email_canonical=email_c,
                failed_count=1,
                locked_until=None,
            )
            .on_conflict_do_update(
                constraint="uq_login_lockout_scope_email",
                set_={
                    "failed_count": next_count,
                    # Bug-10060: with the account lock disabled (_MAX_FAILURES 0,
                    # the default) the count is still recorded as a signal, but
                    # locked_until is never set. Without this the threshold test
                    # `next_count >= 0` is true on the FIRST failure, so a
                    # disabled lock would still stamp a lock date nothing honours.
                    "locked_until": (
                        case(
                            (next_count >= _MAX_FAILURES, now + _LOCKOUT),
                            (expired, null()),
                            else_=locked_until,
                        )
                        if _MAX_FAILURES > 0
                        else null()
                    ),
                    # ``onupdate`` is not applied to a Core upsert automatically.
                    "updated_at": func.now(),
                },
            )
        )
        # The unique-key conflict path takes the row lock inside PostgreSQL,
        # closing the absent-row gap between SELECT and INSERT.  It also
        # naturally waits for a concurrent reset/delete and then retries the
        # insert against the post-reset state.
        await db.execute(statement)
        await db.commit()
    except ProgrammingError as exc:
        if is_missing_table_error(exc):
            await db.rollback()
            return  # pre-migration: failure counter unavailable; fail open
        raise


async def record_login_success(db: AsyncSession, scope_key: str, email: str) -> None:
    try:
        row = await _get(db, scope_key, email, for_update=True)
        if row is not None:
            await db.delete(row)
            await db.flush()
    except ProgrammingError as exc:
        if is_missing_table_error(exc):
            await db.rollback()
            return  # pre-migration: nothing to clear; fail open
        raise
