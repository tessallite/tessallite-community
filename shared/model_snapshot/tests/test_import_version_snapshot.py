"""Bug-5354 — on project import, a ModelVersion's snapshot_json must be
regenerated from the freshly-rehydrated LIVE model state, NOT copied verbatim
from the bundle.

The bundle's per-version snapshot_json carries the source tenant's column /
dimension / measure ids. Import creates new live rows with new ids, so the
bundle snapshot's ids are foreign in the destination tenant. Because the
query-router binds dimensions/measures from the deployed snapshot but resolves
columns/tables from LIVE meta (B15 design), a foreign-id snapshot can never map
a field to a physical table (HTTP 422). insert_model_versions therefore replaces
snapshot_json with snapshot_model(live).
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.model_snapshot.rehydrator import insert_model_versions


def _capture_db():
    """Mock AsyncSession that records the values dict of every insert()."""
    db = AsyncMock()
    captured: list[dict] = []

    async def _execute(stmt):
        try:
            compiled = stmt.compile()
            captured.append(dict(compiled.params))
        except Exception:
            pass
        return MagicMock()

    db.execute = AsyncMock(side_effect=_execute)
    db._captured = captured
    return db


@pytest.mark.asyncio
async def test_insert_model_versions_regenerates_snapshot_from_live():
    model_id = uuid.uuid4()
    # The bundle version carries a FOREIGN-id snapshot (ids from another tenant).
    foreign_snapshot = {
        "columns": [{"id": "FOREIGN-COL-1"}],
        "measures": [{"id": "FOREIGN-MEAS-1", "source_column_id": "FOREIGN-COL-1"}],
    }
    bundle_versions = [{
        "id": str(uuid.uuid4()),
        "version_number": 3,
        "summary": "imported",
        "snapshot_json": foreign_snapshot,
    }]
    # The live model serialises to a snapshot whose ids match live meta.
    live_snapshot = {
        "columns": [{"id": "LIVE-COL-1"}],
        "measures": [{"id": "LIVE-MEAS-1", "source_column_id": "LIVE-COL-1"}],
    }
    db = _capture_db()
    with patch(
        "shared.model_snapshot.serialiser.snapshot_model",
        new=AsyncMock(return_value=live_snapshot),
    ) as mock_snap:
        remap = await insert_model_versions(model_id, bundle_versions, db)

    mock_snap.assert_awaited_once_with(model_id, db)
    rows = [r for r in db._captured if "snapshot_json" in r]
    assert rows, "no model_version row captured"
    stored = rows[0]["snapshot_json"]
    # The stored snapshot is the LIVE one, never the bundle's foreign-id copy.
    assert stored == live_snapshot
    assert stored != foreign_snapshot
    # version metadata (number/summary) is preserved; only snapshot_json swapped.
    assert rows[0]["version_number"] == 3
    assert rows[0]["summary"] == "imported"
    # id remap returned for the inserted version.
    assert remap[bundle_versions[0]["id"]] == rows[0]["id"]


@pytest.mark.asyncio
async def test_insert_model_versions_serialises_live_once_for_many_versions():
    model_id = uuid.uuid4()
    live_snapshot = {"columns": [{"id": "LIVE-COL"}], "measures": []}
    versions = [
        {"id": str(uuid.uuid4()), "version_number": 1, "snapshot_json": {"columns": [{"id": "OLD1"}]}},
        {"id": str(uuid.uuid4()), "version_number": 2, "snapshot_json": {"columns": [{"id": "OLD2"}]}},
    ]
    db = _capture_db()
    with patch(
        "shared.model_snapshot.serialiser.snapshot_model",
        new=AsyncMock(return_value=live_snapshot),
    ) as mock_snap:
        await insert_model_versions(model_id, versions, db)

    # Live state does not change across one model's versions — serialise once.
    assert mock_snap.await_count == 1
    rows = [r for r in db._captured if "snapshot_json" in r]
    assert len(rows) == 2
    for r in rows:
        assert r["snapshot_json"] == live_snapshot


@pytest.mark.asyncio
async def test_insert_model_versions_empty_list_does_not_serialise():
    model_id = uuid.uuid4()
    db = _capture_db()
    with patch(
        "shared.model_snapshot.serialiser.snapshot_model",
        new=AsyncMock(),
    ) as mock_snap:
        remap = await insert_model_versions(model_id, [], db)
    assert remap == {}
    mock_snap.assert_not_awaited()
