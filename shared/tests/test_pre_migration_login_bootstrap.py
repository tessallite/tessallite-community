"""Bug-9552 — login-lockout and system-audit helpers must tolerate a schema
behind migration 0212 so the login-gated migration path can bootstrap.

The migrate-then-serve deploy gate authenticates via the system-login
endpoint before migrations run. ``assert_not_locked`` / ``system_audit``
touched tables created by migration 0212 with no tolerance for an undefined
table, so every login 500'd and migrations could never run on a schema-behind
system (demonstrated live on GCP 2026-08-23).

Contract under test:
- Lockout helpers fail OPEN (skip, with a session rollback) only for
  undefined-table errors; every other database error still raises.
- ``system_audit`` skips the event when the audit table is absent, using a
  probe that never touches the caller's transaction; it writes normally when
  the table exists.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import ProgrammingError

from shared.audit.system import system_audit
from shared.auth.lockout import (
    assert_not_locked,
    record_login_failure,
    record_login_success,
)


class _FakeUndefinedTable:
    """Mimics asyncpg.exceptions.UndefinedTableError (SQLSTATE 42P01)."""

    sqlstate = "42P01"

    def __str__(self) -> str:
        return 'relation "tess_system.login_lockouts" does not exist'


class _FakeProgrammingError(Exception):
    sqlstate = "XX000"

    def __str__(self) -> str:
        return "some other database error"


def _undefined_table_error() -> ProgrammingError:
    return ProgrammingError("SELECT ...", {}, _FakeUndefinedTable())


def _other_programming_error() -> ProgrammingError:
    return ProgrammingError("SELECT ...", {}, _FakeProgrammingError())


def _db_raising(exc: Exception) -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=exc)
    db.rollback = AsyncMock()
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_bug9552_assert_not_locked_fails_open_on_missing_table():
    db = _db_raising(_undefined_table_error())
    await assert_not_locked(db, "__system__", "admin@example.com")
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_bug9552_record_login_failure_fails_open_on_missing_table():
    db = _db_raising(_undefined_table_error())
    await record_login_failure(db, "__system__", "admin@example.com")
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_bug9552_record_login_success_fails_open_on_missing_table():
    db = _db_raising(_undefined_table_error())
    await record_login_success(db, "__system__", "admin@example.com")
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_bug9552_lockout_helpers_still_raise_on_other_errors():
    for helper in (
        assert_not_locked,
        record_login_failure,
        record_login_success,
    ):
        db = _db_raising(_other_programming_error())
        with pytest.raises(ProgrammingError):
            await helper(db, "__system__", "admin@example.com")
        db.rollback.assert_not_awaited()


def _audit_db(probe_result):
    db = AsyncMock()
    result = MagicMock()
    result.scalar.return_value = probe_result
    db.execute = AsyncMock(return_value=result)
    db.add = MagicMock()
    db.flush = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_bug9552_system_audit_skips_event_when_table_missing():
    db = _audit_db(None)
    event = await system_audit(db, action="a", severity="warn")
    assert event is None
    db.add.assert_not_called()
    db.flush.assert_not_awaited()
    # The probe must be a read, and must not disturb the caller transaction.
    probe_stmt = str(db.execute.await_args.args[0])
    assert "to_regclass" in probe_stmt
    assert "audit_events" in probe_stmt


@pytest.mark.asyncio
async def test_bug9552_system_audit_writes_when_table_present():
    db = _audit_db("tess_system.audit_events")
    event = await system_audit(db, action="a", severity="warn")
    assert event is not None
    db.add.assert_called_once()
    db.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_bug9552_system_audit_probe_failure_fails_closed():
    # A probe that errors must NOT silently disable auditing: it reports
    # present and the normal insert path runs as before.
    db = AsyncMock()
    db.add = MagicMock()  # plain mock: system_audit never awaits db.add
    db.execute = AsyncMock(side_effect=ProgrammingError("SELECT", {}, Exception("x")))
    db.flush = AsyncMock()
    event = await system_audit(db, action="a", severity="warn")
    assert event is not None
    db.add.assert_called_once()
    db.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_bug9552_token_version_defaults_to_zero_on_missing_table():
    from unittest.mock import patch

    from shared.auth.middleware import get_system_admin_token_version

    async def _gen(db):
        yield db

    db = _db_raising(_undefined_table_error())
    with patch("shared.db.session.get_system_db", lambda: _gen(db)):
        version = await get_system_admin_token_version()
    assert version == 0
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_bug9552_token_version_still_raises_on_other_errors():
    from unittest.mock import patch

    from shared.auth.middleware import get_system_admin_token_version

    async def _gen(db):
        yield db

    db = _db_raising(_other_programming_error())
    with patch("shared.db.session.get_system_db", lambda: _gen(db)):
        with pytest.raises(ProgrammingError):
            await get_system_admin_token_version()
    db.rollback.assert_not_awaited()
