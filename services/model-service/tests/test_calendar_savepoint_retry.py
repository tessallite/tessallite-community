"""Tests for Bug-5246: calendar alias creation savepoint-retry.

Verifies that _create_calendar_alias wraps the alias INSERT in a
SAVEPOINT (begin_nested) so an IntegrityError rolls back only the
nested transaction, leaving the parent transaction's prior work
(e.g. a flushed CalendarTable) intact.
"""
from __future__ import annotations

import contextlib
import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from src.api.calendar import _create_calendar_alias

pytestmark = pytest.mark.unit


def _make_calendar(cal_id=None, table_name="public.dim_date"):
    return types.SimpleNamespace(
        id=cal_id or uuid.uuid4(),
        table_name=table_name,
        date_column="date_key",
        year_column="cal_year",
        half_column=None,
        quarter_column="cal_quarter",
        month_column="cal_month",
        week_column=None,
        day_column=None,
    )


def _make_db(*, fail_flush_count=0):
    """Build a mock DB session with begin_nested() as an async context manager.

    ``fail_flush_count`` controls how many consecutive flushes inside the
    savepoint raise IntegrityError before succeeding.
    """
    db = AsyncMock()
    db.add = MagicMock()

    flush_calls = {"n": 0}

    async def _flush():
        flush_calls["n"] += 1
        if flush_calls["n"] <= fail_flush_count:
            raise IntegrityError("INSERT", {}, Exception("duplicate alias"))

    db.flush = AsyncMock(side_effect=_flush)
    db.rollback = AsyncMock()

    @contextlib.asynccontextmanager
    async def _begin_nested():
        yield None

    db.begin_nested = MagicMock(side_effect=lambda: _begin_nested())

    # _create_calendar_alias issues multiple db.execute calls:
    #   1) Check for existing ModelTable with matching physical_name
    #      → scalar_one_or_none() should return None (no pre-existing alias)
    #   2) _next_alias: select existing aliases
    #      → scalars().all() should return []
    #   On retry, _next_alias is called again (same shape).
    default_result = MagicMock()
    default_result.scalar_one_or_none.return_value = None
    default_result.scalars.return_value.all.return_value = []
    default_result.scalar.return_value = 0  # for alias count check
    db.execute = AsyncMock(return_value=default_result)

    return db


# ---------------------------------------------------------------------------
# Happy path: no collision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alias_created_without_collision():
    db = _make_db(fail_flush_count=0)
    cal = _make_calendar()
    model_id = uuid.uuid4()
    source_id = uuid.uuid4()

    table = await _create_calendar_alias(
        db, model_id=model_id, source_id=source_id,
        calendar=cal, alias=None, display_name=None,
    )

    assert table is not None
    assert table.calendar_table_id == cal.id
    # begin_nested was called once (the successful attempt)
    assert db.begin_nested.call_count == 1
    # No rollback needed
    db.rollback.assert_not_awaited()


# ---------------------------------------------------------------------------
# Retry path: auto-generated alias collides then succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_alias_retries_on_integrity_error():
    """When no user-supplied alias is given and the first attempt hits an
    IntegrityError, the function retries with a new alias rather than
    rolling back the entire session."""
    db = _make_db(fail_flush_count=1)
    cal = _make_calendar()
    model_id = uuid.uuid4()
    source_id = uuid.uuid4()

    table = await _create_calendar_alias(
        db, model_id=model_id, source_id=source_id,
        calendar=cal, alias=None, display_name=None,
    )

    assert table is not None
    # Two begin_nested calls: first failed, second succeeded
    assert db.begin_nested.call_count == 2
    # Crucially, db.rollback() must NOT have been called — the savepoint
    # handles the rollback internally, not via an explicit session rollback.
    db.rollback.assert_not_awaited()


# ---------------------------------------------------------------------------
# User-supplied alias collision: no retry, immediate 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_alias_collision_raises_409_no_retry():
    """When a user-supplied alias collides, the function must NOT retry
    with a different alias — it raises 409 immediately."""
    from fastapi import HTTPException

    db = _make_db(fail_flush_count=1)
    cal = _make_calendar()
    model_id = uuid.uuid4()
    source_id = uuid.uuid4()

    with pytest.raises(HTTPException) as exc_info:
        await _create_calendar_alias(
            db, model_id=model_id, source_id=source_id,
            calendar=cal, alias="my_custom_alias", display_name=None,
        )

    assert exc_info.value.status_code == 409
    assert "my_custom_alias" in exc_info.value.detail
    # Only one attempt — no retry
    assert db.begin_nested.call_count == 1
    db.rollback.assert_not_awaited()


# ---------------------------------------------------------------------------
# Exhausted retries: 409 after max attempts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exhausted_retries_raises_409():
    """When all retry attempts fail, a 409 is raised."""
    from fastapi import HTTPException

    db = _make_db(fail_flush_count=10)  # more than _MAX_ALIAS_RETRIES
    cal = _make_calendar()
    model_id = uuid.uuid4()
    source_id = uuid.uuid4()

    with pytest.raises(HTTPException) as exc_info:
        await _create_calendar_alias(
            db, model_id=model_id, source_id=source_id,
            calendar=cal, alias=None, display_name=None,
        )

    assert exc_info.value.status_code == 409
    assert "retries" in exc_info.value.detail.lower()
    db.rollback.assert_not_awaited()


# ---------------------------------------------------------------------------
# Parent transaction integrity: prior flushed work survives a retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_savepoint_does_not_discard_prior_work():
    """The key regression test for Bug-5246: a CalendarTable flushed before
    _create_calendar_alias must remain in the session after an alias
    collision + retry. With the old db.rollback() approach, the parent
    transaction's flushed CalendarTable would be discarded."""
    db = _make_db(fail_flush_count=1)
    cal = _make_calendar()
    model_id = uuid.uuid4()
    source_id = uuid.uuid4()

    # Simulate that a CalendarTable was flushed earlier in the same
    # transaction (as auto_create_calendar does at ~line 784).
    # After _create_calendar_alias completes, db.rollback must NOT
    # have been called — only the savepoint rolls back.
    table = await _create_calendar_alias(
        db, model_id=model_id, source_id=source_id,
        calendar=cal, alias=None, display_name=None,
    )

    assert table is not None
    # The critical assertion: no session-level rollback occurred.
    db.rollback.assert_not_awaited()
    # begin_nested was used (savepoints), not bare rollback.
    assert db.begin_nested.call_count >= 2
