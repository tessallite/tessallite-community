"""Bug-7982 completion round — bounded lock-wait timeout on the per-model
definition/governance advisory lock (item 4: calendar.py holds the lock across
a source DDL call).

``calendar.py``'s ``auto_create_calendar`` / ``bind_calendar`` hold the
per-model advisory lock across network I/O to the SOURCE database (a
destructive DDL call, by design — F-016-01 requires it to run LAST, after all
metadata work has succeeded, so a late-acquire like ``refresh_named_list``'s is
not safe there). Without a bound, a slow or hung source call could pin the
model's lock and starve every other definition writer indefinitely.

``acquire_model_definition_lock`` now applies a PostgreSQL ``lock_timeout``
(``settings.MODEL_DEFINITION_LOCK_TIMEOUT_SECONDS``) scoped to the CALLER's own
transaction, combined into the SAME SQL statement as the lock acquisition
(``set_config('lock_timeout', ..., true)`` alongside
``pg_advisory_xact_lock(...)``) so the call-count contract (exactly one
``db.execute()``) that several ordered-mock unit tests across the 44+ locked
endpoints depend on is preserved. A lock wait that exceeds the timeout raises
PostgreSQL SQLSTATE 55P03, which this helper translates into a clear,
retryable HTTP 503 rather than letting a raw DB error or an indefinite hang
reach the caller.

These guards lock (pure-mock unit coverage only — see the live-DB companion
test below):
  1. the acquisition remains exactly ONE ``db.execute()`` call (mutation-visible
     via call count) carrying BOTH the lock_timeout config and the advisory
     lock acquisition in one statement;
  2. a lock-timeout error (SQLSTATE 55P03) is translated to HTTP 503 with an
     actionable, retryable message;
  3. any OTHER DBAPIError (a different SQLSTATE) is NOT swallowed — it
     propagates unchanged, so this fix does not mask unrelated DB failures.

A 4th guard — a second transaction that cannot acquire an already-held lock
within a short configured timeout fails fast with the 503 instead of hanging
until the holder's transaction ends — requires a REAL Postgres connection
(two genuinely concurrent transactions) and lives in
``tests/integration/test_versioning_consistency_db.py::test_second_writer_fails_fast_instead_of_hanging``
(opus5 finding 4.7: a live-DB test does not belong under this file's blanket
``pytest.mark.unit``, per the CLAUDE.md test-strategy taxonomy — moved there).
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Call-count + SQL-shape contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_lock_is_exactly_one_execute_call():
    """The call-count contract several ordered-mock unit tests across the
    locked endpoints depend on: adding the lock_timeout must not turn this
    into two db.execute() calls."""
    from src.api._model_lock import acquire_model_definition_lock

    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock())

    await acquire_model_definition_lock(db, uuid.uuid4())

    assert db.execute.call_count == 1


@pytest.mark.asyncio
async def test_acquire_lock_statement_bounds_then_restores_timeout_in_order():
    """R6 finding 5: the single statement must (1) set the SHORT lock_timeout,
    (2) acquire the advisory lock under it, then (3) RESTORE the prior
    lock_timeout — in that order, so only the acquisition itself is bounded and a
    later statement in the same transaction is not silently governed by the short
    timeout (which would surface as an unhandled 500).

    The ordering is forced by a chain of MATERIALIZED CTEs; this test asserts the
    load-bearing STRUCTURE (not merely that the function names appear somewhere):
      * the SHORT ``set_config('lock_timeout', :timeout, ...)`` comes BEFORE
        ``pg_advisory_xact_lock`` — bound established before the wait;
      * a RESTORE ``set_config('lock_timeout', ...)`` referencing the captured
        prior value comes AFTER ``pg_advisory_xact_lock``;
      * ``current_setting('lock_timeout')`` captures the prior value;
      * MATERIALIZED prevents the planner inlining/reordering the CTEs.
    (The real runtime restore is proven end-to-end against Postgres in
    tests/integration/test_bug7982_r6_db.py::test_lock_timeout_is_restored_after_acquisition.)"""
    from src.api._model_lock import acquire_model_definition_lock, model_advisory_lock_key

    captured = {}

    async def _execute(stmt, params=None):
        captured["sql"] = str(stmt)
        captured["params"] = params
        return MagicMock()

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)

    model_id = uuid.uuid4()
    await acquire_model_definition_lock(db, model_id)

    sql = captured["sql"]
    sql_upper = sql.upper()
    assert "MATERIALIZED" in sql_upper, "CTEs must be MATERIALIZED to block inlining"
    assert "CURRENT_SETTING" in sql_upper, "prior lock_timeout must be captured"
    # The parameterised (short) set_config binds :timeout; the restore uses the
    # captured prior value. Both appear, around the lock acquisition.
    lock_idx = sql_upper.index("PG_ADVISORY_XACT_LOCK")
    short_idx = sql.index(":timeout")
    restore_idx = sql_upper.rindex("SET_CONFIG")
    assert short_idx < lock_idx, (
        f"the short lock_timeout must be SET before the lock is acquired: {sql!r}"
    )
    assert restore_idx > lock_idx, (
        f"lock_timeout must be RESTORED after the lock is acquired: {sql!r}"
    )
    assert captured["params"]["key"] == model_advisory_lock_key(model_id)

    from shared.config.settings import get_settings
    settings = get_settings()
    assert captured["params"]["timeout"] == (
        f"{settings.MODEL_DEFINITION_LOCK_TIMEOUT_SECONDS}s"
    )


# ---------------------------------------------------------------------------
# Lock-timeout error translation
# ---------------------------------------------------------------------------


class _FakeAsyncpgLockTimeout(Exception):
    """Stands in for asyncpg.exceptions.LockNotAvailableError (SQLSTATE 55P03)."""
    sqlstate = "55P03"


class _FakeAsyncpgOtherError(Exception):
    """A DIFFERENT DB error — must NOT be swallowed/translated."""
    sqlstate = "40001"  # serialization_failure, unrelated to lock waiting


def _dbapi_error(orig: Exception) -> DBAPIError:
    return DBAPIError.instance(
        statement="SELECT set_config(...), pg_advisory_xact_lock(...)",
        params={},
        orig=orig,
        dbapi_base_err=Exception,
    )


@pytest.mark.asyncio
async def test_lock_timeout_translates_to_503():
    from src.api._model_lock import acquire_model_definition_lock

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=_dbapi_error(_FakeAsyncpgLockTimeout("lock timeout"))
    )

    with pytest.raises(HTTPException) as exc_info:
        await acquire_model_definition_lock(db, uuid.uuid4())

    assert exc_info.value.status_code == 503
    # Actionable and retryable, not a raw DB error leaking to the caller.
    assert "retry" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_unrelated_dbapi_error_is_not_swallowed():
    """Mutation check: only SQLSTATE 55P03 is translated. Any other DB error
    (e.g. a serialization failure) must propagate unchanged — this fix must
    never mask an unrelated DB failure as 'model locked, retry'."""
    from src.api._model_lock import acquire_model_definition_lock

    db = AsyncMock()
    boom = _dbapi_error(_FakeAsyncpgOtherError("serialization failure"))
    db.execute = AsyncMock(side_effect=boom)

    with pytest.raises(DBAPIError):
        await acquire_model_definition_lock(db, uuid.uuid4())
