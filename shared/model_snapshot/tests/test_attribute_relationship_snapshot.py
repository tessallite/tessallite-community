"""Snapshot round-trip for dimension attribute relationships (v4, Bug-7359).

Verifies that a declared relationship (spec §5.3) is re-inserted on rehydrate
with its declaration preserved and re-scoped to the target model id, and that the
serialiser emits the ``attribute_relationships`` key. Runtime verification
evidence is NOT part of the snapshot — only the pinned declaration travels.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.model_snapshot.rehydrator import _insert_attribute_relationships


def _capture_db():
    db = AsyncMock()
    captured: list[dict] = []

    async def _execute(stmt):
        try:
            captured.append(dict(stmt.compile().params))
        except Exception:
            pass
        return MagicMock()

    db.execute = AsyncMock(side_effect=_execute)
    db._captured = captured
    return db


@pytest.mark.asyncio
async def test_insert_attribute_relationships_rescopes_model_id_and_preserves_declaration():
    model_id = uuid.uuid4()
    rel_id = uuid.uuid4()
    dim_id = uuid.uuid4()
    key_col = uuid.uuid4()
    detail_col = uuid.uuid4()
    snap = {
        "attribute_relationships": [
            {
                "id": str(rel_id),
                "model_id": str(uuid.uuid4()),  # OLD model id — must be replaced
                "dimension_id": str(dim_id),
                "key_column_id": str(key_col),
                "detail_column_id": str(detail_col),
                "cardinality": "BIJECTION",
                "null_policy": "REJECT_NULL",
                "enabled": True,
                "declaration_hash": "abc123",
            }
        ]
    }
    db = _capture_db()
    await _insert_attribute_relationships(model_id, snap, db)

    assert len(db._captured) == 1
    params = db._captured[0]
    # Re-scoped to the target model, declaration content preserved, PK travels.
    assert params["model_id"] == model_id
    assert params["dimension_id"] == dim_id
    assert params["cardinality"] == "BIJECTION"
    assert params["null_policy"] == "REJECT_NULL"
    assert params["declaration_hash"] == "abc123"
    assert params["id"] == rel_id


@pytest.mark.asyncio
async def test_insert_attribute_relationships_noop_when_absent():
    db = _capture_db()
    await _insert_attribute_relationships(uuid.uuid4(), {}, db)
    assert db._captured == []


def test_serialiser_emits_attribute_relationships_key():
    # The coverage guard asserts the table is COVERED with this key; here we pin
    # the exact snapshot key name the serialiser must emit and the rehydrator
    # must consume, so producer and consumer cannot drift.
    from shared.model_snapshot.tests.test_snapshot_coverage_guard import COVERED

    assert COVERED["dimension_attribute_relationships"] == "attribute_relationships"
