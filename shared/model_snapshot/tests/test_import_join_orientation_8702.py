"""Bug-8702: pre-0194 imports cannot resurrect undeclared join orientation.

Migration 0194 repairs rows and deployed snapshots that already exist when the
migration runs.  A project bundle imported later bypasses that one-time repair,
so the import boundary must apply the same policy to both the live shape and
every restorable historical version.  Ordinary version revert remains verbatim.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.model_snapshot.importer import prepare_snapshot_for_import
from shared.model_snapshot.rehydrator import _insert_joins, insert_model_versions


def _capture_db() -> AsyncMock:
    db = AsyncMock()
    captured: list[dict] = []

    async def _execute(statement):
        captured.append(dict(statement.compile().params))
        return MagicMock()

    db.execute = AsyncMock(side_effect=_execute)
    db.captured = captured
    return db


def _legacy_snapshot(*, join_id: str | None = None) -> dict:
    return {
        "schema_version": 4,
        "model": {"id": str(uuid.uuid4()), "slug": "legacy"},
        "joins": [
            {
                "id": join_id or str(uuid.uuid4()),
                "model_id": str(uuid.uuid4()),
                "join_type": "many_to_one",
            }
        ],
        "data_sources": [],
        "data_targets": [],
    }


def test_bug_8702_pre_0194_live_bundle_declares_orientation_and_cardinality():
    """The imported live row gets 0194's exact known declaration.

    ``many_to_one`` historically occupied the orientation field.  The repaired
    representation is an explicit LEFT orientation plus the preserved fan-out
    in the separate cardinality field.
    """
    rewritten, missing = prepare_snapshot_for_import(
        _legacy_snapshot(), new_model_id=uuid.uuid4(), connection_mapping={}
    )

    assert missing == []
    assert rewritten["joins"][0]["join_type"] == "left"
    assert rewritten["joins"][0]["cardinality"] == "many_to_one"


def test_bug_8702_invalid_stored_cardinality_does_not_erase_legacy_fan_out():
    """0194's semantic selector replaces non-null but unusable cardinality.

    Bundle rows bypass the API enum. If an invalid separate value survived
    while ``join_type`` was rewritten, consumers could no longer recover the
    legacy many-to-one fan-out and the compatibility guard could fail open.
    """
    snapshot = _legacy_snapshot()
    snapshot["joins"][0]["cardinality"] = "not_a_cardinality"

    rewritten, missing = prepare_snapshot_for_import(
        snapshot, new_model_id=uuid.uuid4(), connection_mapping={}
    )

    assert missing == []
    assert rewritten["joins"][0]["join_type"] == "left"
    assert rewritten["joins"][0]["cardinality"] == "many_to_one"


@pytest.mark.asyncio
async def test_bug_8702_pre_0194_version_snapshot_gets_the_same_declaration():
    """A later revert of imported history cannot restore the legacy token."""
    model_id = uuid.uuid4()
    db = _capture_db()
    await insert_model_versions(
        model_id,
        [
            {
                "id": str(uuid.uuid4()),
                "version_number": 1,
                "snapshot_json": _legacy_snapshot(),
            }
        ],
        db,
        bundle_carries_version_snapshots=True,
        connection_mapping={},
        model_slug="imported",
    )

    rows = [row for row in db.captured if "snapshot_json" in row]
    assert len(rows) == 1
    stored_join = rows[0]["snapshot_json"]["joins"][0]
    assert rows[0]["snapshot_unavailable"] is False
    assert stored_join["join_type"] == "left"
    assert stored_join["cardinality"] == "many_to_one"


@pytest.mark.asyncio
async def test_bug_8702_direct_revert_rehydration_remains_verbatim():
    """Import repair must not silently rewrite an existing saved version.

    ``_insert_joins`` is the shared insert path used by ordinary reverts.  Its
    contract remains verbatim; only ``prepare_snapshot_for_import`` normalises.
    """
    model_id = uuid.uuid4()
    join_id = str(uuid.uuid4())
    db = _capture_db()

    await _insert_joins(model_id, _legacy_snapshot(join_id=join_id), db)

    assert len(db.captured) == 1
    inserted = db.captured[0]
    assert inserted["join_type"] == "many_to_one"
    assert "cardinality" not in inserted
