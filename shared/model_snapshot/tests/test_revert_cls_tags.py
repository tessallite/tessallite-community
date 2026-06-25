"""F-013-03 (closed by B15, regression-locked here): revert must NOT silently
strip column-level-security tag assignments.

Before B15, ``data_tags`` / ``data_tag_columns`` / ``persona_tag_restrictions``
were not in the snapshot. The revert truncate deleted ModelColumn rows; the
``ondelete=CASCADE`` on ``data_tag_columns.model_column_id`` wiped every
tag-to-column link, and nothing restored them — a persona restricted from
``pii`` columns immediately saw them (fail-open).

The serialiser now emits these families and the rehydrator's
``_insert_data_tags`` restores them: tags re-created, column links re-attached
to the rebuilt columns, persona restrictions re-attached. This test locks that
restoration so the security regression cannot return.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.model_snapshot.rehydrator import _insert_data_tags


def _capture_db(live_column_ids, live_persona_ids):
    """Mock AsyncSession: column/persona existence queries return the supplied
    live ids; insert() statements are recorded by table name."""
    db = AsyncMock()
    inserts: list[tuple[str, dict]] = []
    calls = {"n": 0}

    col_result = MagicMock()
    col_result.all.return_value = [(c,) for c in live_column_ids]
    persona_result = MagicMock()
    persona_result.all.return_value = [(p,) for p in live_persona_ids]

    async def _execute(stmt):
        # SELECTs come first (columns, then personas); inserts in between.
        table = getattr(getattr(stmt, "table", None), "name", None)
        if table is None:
            # SELECT — alternate columns then personas by call order.
            calls["n"] += 1
            return col_result if calls["n"] == 1 else persona_result
        try:
            params = dict(stmt.compile().params)
        except Exception:
            params = {}
        inserts.append((table, params))
        return MagicMock()

    db.execute = AsyncMock(side_effect=_execute)
    db._inserts = inserts
    return db


@pytest.mark.asyncio
async def test_revert_restores_tag_column_links_and_persona_restrictions():
    model_id = uuid.uuid4()
    tag_id = uuid.uuid4()
    persona_id = uuid.uuid4()
    col_a = uuid.uuid4()
    col_b = uuid.uuid4()
    col_gone = uuid.uuid4()  # a column dropped in the reverted-to version

    snapshot = {
        "data_tags": [
            {
                "id": str(tag_id),
                "tag_name": "pii",
                "column_ids": [str(col_a), str(col_b), str(col_gone)],
            }
        ],
        "persona_tag_restrictions": [
            {"persona_id": str(persona_id), "data_tag_id": str(tag_id)}
        ],
    }

    # col_gone no longer exists post-rehydrate; persona survives.
    db = _capture_db(live_column_ids={col_a, col_b}, live_persona_ids={persona_id})
    await _insert_data_tags(model_id, snapshot, db)

    tables = [t for t, _ in db._inserts]
    # The tag is re-created.
    assert "data_tags" in tables
    # Both surviving column links are re-attached; the dropped column is skipped.
    link_rows = [p for t, p in db._inserts if t == "data_tag_columns"]
    linked_cols = {r["model_column_id"] for r in link_rows}
    assert linked_cols == {col_a, col_b}
    assert col_gone not in linked_cols
    # The persona restriction is re-attached (CLS is restored, not fail-open).
    restr_rows = [p for t, p in db._inserts if t == "persona_tag_restrictions"]
    assert len(restr_rows) == 1
    assert restr_rows[0]["persona_id"] == persona_id
    assert restr_rows[0]["data_tag_id"] == tag_id


@pytest.mark.asyncio
async def test_restriction_dropped_when_persona_absent():
    """If the persona was removed in the reverted-to version, its restriction
    is not re-created (no dangling FK), but the tag + column links still are."""
    model_id = uuid.uuid4()
    tag_id = uuid.uuid4()
    persona_id = uuid.uuid4()
    col_a = uuid.uuid4()

    snapshot = {
        "data_tags": [
            {"id": str(tag_id), "tag_name": "pii", "column_ids": [str(col_a)]}
        ],
        "persona_tag_restrictions": [
            {"persona_id": str(persona_id), "data_tag_id": str(tag_id)}
        ],
    }
    db = _capture_db(live_column_ids={col_a}, live_persona_ids=set())  # persona gone
    await _insert_data_tags(model_id, snapshot, db)

    assert any(t == "data_tag_columns" for t, _ in db._inserts)
    assert not any(t == "persona_tag_restrictions" for t, _ in db._inserts)
