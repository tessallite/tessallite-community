"""Bug-7982: _insert_named_sets must preserve valid cyclic/self replacement
graphs (A->B,B->A or A->A) instead of erasing them, and detach only truly
dangling/cross-model references.

A single-pass topological insert could never resolve a cycle (no row is ever
"ready"), so the old fallback stripped every replacement_id — silently losing a
valid governance graph. The fix inserts all rows with replacement_id NULL, then
wires the links in a second pass once every id exists.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.sql.dml import Insert, Update

from shared.model_snapshot.rehydrator import _insert_named_sets


def _recording_db():
    db = AsyncMock()
    inserts: list[dict] = []
    updates: list[dict] = []

    async def _execute(stmt, *a, **k):
        if isinstance(stmt, Insert):
            inserts.append(dict(stmt.compile().params))
        elif isinstance(stmt, Update):
            updates.append(dict(stmt.compile().params))
        # The in-place upsert first SELECTs live parent ids; answer empty so the
        # (empty live) path is all-inserts + link-wiring updates.
        res = MagicMock()
        res.all.return_value = []
        return res

    db.execute = AsyncMock(side_effect=_execute)
    db._inserts = inserts
    db._updates = updates
    return db


def _ns(id_, name, replacement_id=None):
    row = {"id": str(id_), "name": name, "expression": "{[X]}", "scope": 1}
    if replacement_id is not None:
        row["replacement_id"] = str(replacement_id)
    return row


@pytest.mark.asyncio
async def test_two_cycle_replacement_survives():
    a, b = uuid.uuid4(), uuid.uuid4()
    snap = {"named_sets": [_ns(a, "A", b), _ns(b, "B", a)]}
    db = _recording_db()

    await _insert_named_sets(uuid.uuid4(), snap, db)

    # Pass 1: both inserted with NO replacement_id.
    assert len(db._inserts) == 2
    assert all("replacement_id" not in ins for ins in db._inserts)
    # Pass 2: both links wired via UPDATE.
    wired = {str(u.get("replacement_id")) for u in db._updates}
    assert wired == {str(a), str(b)}
    assert len(db._updates) == 2


@pytest.mark.asyncio
async def test_self_replacement_survives():
    a = uuid.uuid4()
    snap = {"named_sets": [_ns(a, "A", a)]}
    db = _recording_db()

    await _insert_named_sets(uuid.uuid4(), snap, db)

    assert len(db._inserts) == 1
    assert "replacement_id" not in db._inserts[0]
    assert len(db._updates) == 1
    assert str(db._updates[0].get("replacement_id")) == str(a)


@pytest.mark.asyncio
async def test_dangling_replacement_is_detached_not_erased_for_others():
    a, b, ghost = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    # A -> ghost (dangling), B -> A (valid)
    snap = {"named_sets": [_ns(a, "A", ghost), _ns(b, "B", a)]}
    db = _recording_db()

    await _insert_named_sets(uuid.uuid4(), snap, db)

    assert len(db._inserts) == 2
    # Only the valid B->A link is wired; the dangling A->ghost is left NULL.
    assert len(db._updates) == 1
    assert str(db._updates[0].get("replacement_id")) == str(a)
