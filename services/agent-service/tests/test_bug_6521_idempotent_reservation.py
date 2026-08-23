"""Bug-6521 — streaming-retry idempotency: turn-reservation dedup.

A retried stream/sync POST carrying the same Idempotency-Key must NOT reserve
(and therefore must not execute) a second turn. These tests exercise
``_reserve_turn`` directly against a controllable fake session.

Wave-C decision #13 (in-flight turn dedup — Option A): the accepted final
contract is ONE durable turn row per ``(conversation_id, idempotency_key)``,
accepting a rare bounded double-COMPUTE during a genuine concurrent same-key
race but never a duplicate user-visible answer. The concurrent-race test
``test_concurrent_same_key_race_reuses_single_durable_row`` pins that
one-durable-row guarantee against the partial-unique-index race-loser path.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from src.api.conversations import _reserve_turn


def _result_returning(scalar_value):
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar_value
    return result


@pytest.mark.asyncio
async def test_repeated_key_with_completed_turn_does_not_reserve_second_turn():
    """A key whose turn already ran to completion -> duplicate; NO new turn."""
    conv_id = uuid.uuid4()
    existing = SimpleNamespace(
        id=uuid.uuid4(), turn_index=3, status="ok",
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result_returning(existing))
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.scalar = AsyncMock()

    res = await _reserve_turn(
        db, conv_id, user_message="q", idempotency_key="key-1",
    )

    assert res.is_duplicate is True
    assert res.existing_turn_id == existing.id
    assert res.turn_index == 3
    # The critical guarantee: no second placeholder row is inserted.
    db.add.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_repeated_key_with_inflight_reservation_reuses_same_row():
    """A key whose reservation is still ``streaming`` -> reuse the same row,
    never a second turn (fail-safe: an orphaned reservation is not withheld)."""
    conv_id = uuid.uuid4()
    existing = SimpleNamespace(
        id=uuid.uuid4(), turn_index=2, status="streaming",
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result_returning(existing))
    db.add = MagicMock()
    db.commit = AsyncMock()

    res = await _reserve_turn(
        db, conv_id, user_message="q", idempotency_key="key-2",
    )

    assert res.is_duplicate is False
    assert res.turn_index == 2
    assert res.existing_turn_id == existing.id
    db.add.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_fresh_key_reserves_a_new_placeholder():
    """First send for a key -> reserve a fresh placeholder stamped with the key."""
    conv_id = uuid.uuid4()
    db = AsyncMock()
    # Key lookup returns None, then the FOR UPDATE lock select returns a
    # (never-dereferenced) result.
    db.execute = AsyncMock(return_value=_result_returning(None))
    db.scalar = AsyncMock(return_value=4)  # max existing turn_index
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    res = await _reserve_turn(
        db, conv_id, user_message="hello", idempotency_key="key-3",
    )

    assert res.is_duplicate is False
    assert res.turn_index == 5
    db.add.assert_called_once()
    placeholder = db.add.call_args[0][0]
    assert placeholder.idempotency_key == "key-3"
    assert placeholder.status == "streaming"
    assert placeholder.turn_index == 5


@pytest.mark.asyncio
async def test_no_key_reserves_without_dedup():
    """Keyless callers are never deduped — a fresh turn always reserves."""
    conv_id = uuid.uuid4()
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result_returning(None))
    db.scalar = AsyncMock(return_value=-1)
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    res = await _reserve_turn(
        db, conv_id, user_message="hello", idempotency_key=None,
    )

    assert res.is_duplicate is False
    assert res.turn_index == 0
    db.add.assert_called_once()
    placeholder = db.add.call_args[0][0]
    assert placeholder.idempotency_key is None


@pytest.mark.asyncio
async def test_concurrent_same_key_race_reuses_single_durable_row():
    """Wave-C decision #13 — the concurrent same-key race yields ONE durable row.

    Two first-attempts for the same ``(conversation_id, idempotency_key)`` both
    observe no existing turn and both try to INSERT a placeholder. The winner
    commits first; the loser's INSERT collides on the partial-unique
    ``uq_agent_turns_conversation_idempotency_key`` index and the DB raises
    ``IntegrityError`` on commit. ``_reserve_turn`` must then roll back the
    loser's colliding insert and REUSE the winner's already-committed row —
    never mint a second turn. This is the code-evidence for the decision's
    accepted contract: a genuine race may double-COMPUTE (re-run into the same
    row, last-writer-wins) but can NEVER produce a second durable turn row and
    therefore never a duplicate user-visible answer.
    """
    conv_id = uuid.uuid4()
    key = "race-key"
    # The winner committed its placeholder at turn_index 7, still in flight.
    winner = SimpleNamespace(id=uuid.uuid4(), turn_index=7, status="streaming")

    db = AsyncMock()
    # execute() is called three times on the race-loser path:
    #  1. initial key lookup -> None (the loser saw no row before the winner
    #     committed);
    #  2. the FOR UPDATE conversation-lock select (result unused);
    #  3. after the IntegrityError, reload the winner's placeholder by key.
    db.execute = AsyncMock(side_effect=[
        _result_returning(None),
        _result_returning(None),
        _result_returning(winner),
    ])
    db.scalar = AsyncMock(return_value=6)  # max existing turn_index seen by loser
    db.add = MagicMock()
    # The loser's INSERT violates the partial-unique (conversation_id,
    # idempotency_key) index -> the DB raises IntegrityError on commit.
    db.commit = AsyncMock(
        side_effect=IntegrityError("INSERT", {}, Exception("duplicate key")),
    )
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()

    res = await _reserve_turn(
        db, conv_id, user_message="q", idempotency_key=key,
    )

    # One durable row: the loser reuses the WINNER's row and index, never a
    # freshly minted second turn for the key.
    assert res.turn_index == winner.turn_index
    assert res.existing_turn_id == winner.id
    # Winner still streaming -> the loser reuses it (bounded double compute is
    # accepted), writing the SINGLE row last-writer-wins, not a duplicate.
    assert res.is_duplicate is False
    # The loser's colliding INSERT was rolled back — no second durable row
    # escaped the race.
    db.rollback.assert_awaited_once()
