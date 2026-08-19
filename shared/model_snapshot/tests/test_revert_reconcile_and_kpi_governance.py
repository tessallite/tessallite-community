"""Bug-7147 — revert correctness for the snapshot rehydrator (source/target
reconcile).

Bug-7147 [CORRECTNESS]: a revert must reconcile the live data_sources /
data_targets set to EXACTLY the reverted-to snapshot's set. The old upsert path
inserted/updated the snapshot's rows but never deleted live sources/targets the
snapshot did not contain, so a model that gained a source/target after the
reverted-to version kept those "ghost" rows on revert. The reconcile must delete
ghosts FK-safely: a target still referenced by a PRESERVED aggregate/pocket
(RESTRICT FK) is retained (in use, would 500 the revert), not deleted.

Bug-7148 note: this file previously also covered ``_restore_kpi_governance``
(a capture-before-truncate / reinsert-and-reapply helper for KPI governance
across a revert). Bug-7982 R3 replaced that whole capture/reinsert design with
an IN-PLACE upsert (``_upsert_definition_rows`` via ``_insert_kpis``) that
never truncates KPI rows in the first place — CASCADE children (KPIVersion,
KPIUsage, KPISnapshot, KPILatest) are never dropped, so there is nothing to
"restore" governance onto after a truncate that no longer happens.
``_restore_kpi_governance`` became dead code (no call site) and has been
removed along with its tests here; governance-preservation across a revert is
now covered by ``shared/model_snapshot/tests/test_revert_cascade_child_preservation.py``
and the model-service live-DB integration suite.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.db.models import (
    AggregateDefinition,
    DataSource,
    DataTarget,
    PocketDefinition,
)
from shared.model_snapshot.rehydrator import _reconcile_sources_and_targets

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fake AsyncSession that routes SELECTs to canned rows by selected entity and
# records DELETE / UPDATE statements executed against it.
# ---------------------------------------------------------------------------

def _result(rows):
    res = MagicMock()
    res.all.return_value = rows
    return res


class _FakeDB:
    def __init__(self, *, select_map):
        # select_map: entity-name -> list[tuple]
        self._select_map = select_map
        self.deletes: list[tuple[str, object]] = []   # (table, whereclause repr)
        self.updates: list[tuple[str, dict]] = []
        self.execute = AsyncMock(side_effect=self._execute)

    async def _execute(self, stmt):
        # SELECT
        if getattr(stmt, "is_select", False):
            try:
                ent = stmt.column_descriptions[0].get("entity")
                name = getattr(ent, "__name__", None)
            except Exception:
                name = None
            return _result(self._select_map.get(name, []))
        # DELETE
        table = getattr(getattr(stmt, "table", None), "name", None)
        if stmt.__class__.__name__ == "Delete":
            self.deletes.append((table, stmt))
            return MagicMock()
        # UPDATE
        if stmt.__class__.__name__ == "Update":
            try:
                params = dict(stmt.compile().params)
            except Exception:
                params = {}
            self.updates.append((table, params))
            return MagicMock()
        return MagicMock()


def _deleted_ids(db, table_name):
    """Return the id-set a DELETE ... WHERE id IN (...) targeted for a table."""
    out: set = set()
    for table, stmt in db.deletes:
        if table != table_name:
            continue
        try:
            params = stmt.compile().params
        except Exception:
            params = {}
        for v in params.values():
            candidates = v if isinstance(v, (list, tuple)) else [v]
            for c in candidates:
                if isinstance(c, uuid.UUID):
                    out.add(c)
    return out


# ---------------------------------------------------------------------------
# Bug-7147 — source/target reconcile
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reconcile_deletes_ghost_source_and_target():
    """A live source/target absent from the snapshot is a ghost and is deleted."""
    snap_src, ghost_src = uuid.uuid4(), uuid.uuid4()
    snap_tgt, ghost_tgt = uuid.uuid4(), uuid.uuid4()
    model_id = uuid.uuid4()

    db = _FakeDB(select_map={
        "DataTarget": [(snap_tgt,), (ghost_tgt,)],
        "DataSource": [(snap_src,), (ghost_src,)],
        # ghost target referenced by nothing
        "AggregateDefinition": [],
        "PocketDefinition": [],
    })

    await _reconcile_sources_and_targets(
        model_id, db,
        snapshot_source_ids={snap_src},
        snapshot_target_ids={snap_tgt},
    )

    assert _deleted_ids(db, "data_targets") == {ghost_tgt}
    assert _deleted_ids(db, "data_sources") == {ghost_src}


@pytest.mark.asyncio
async def test_reconcile_retains_ghost_target_referenced_by_preserved_aggregate():
    """A ghost target still referenced by a preserved aggregate is retained
    (RESTRICT FK; deleting it would 500 the revert)."""
    snap_tgt = uuid.uuid4()
    ghost_tgt_in_use = uuid.uuid4()
    model_id = uuid.uuid4()

    db = _FakeDB(select_map={
        "DataTarget": [(snap_tgt,), (ghost_tgt_in_use,)],
        "DataSource": [],
        # preserved aggregate still points at the ghost target
        "AggregateDefinition": [(ghost_tgt_in_use,)],
        "PocketDefinition": [],
    })

    await _reconcile_sources_and_targets(
        model_id, db,
        snapshot_source_ids=set(),
        snapshot_target_ids={snap_tgt},
    )

    # The in-use ghost target was NOT deleted.
    assert ghost_tgt_in_use not in _deleted_ids(db, "data_targets")


@pytest.mark.asyncio
async def test_reconcile_retains_ghost_target_referenced_by_preserved_pocket():
    snap_tgt = uuid.uuid4()
    ghost_tgt_in_use = uuid.uuid4()
    model_id = uuid.uuid4()

    db = _FakeDB(select_map={
        "DataTarget": [(snap_tgt,), (ghost_tgt_in_use,)],
        "DataSource": [],
        "AggregateDefinition": [],
        "PocketDefinition": [(ghost_tgt_in_use,)],
    })

    await _reconcile_sources_and_targets(
        model_id, db,
        snapshot_source_ids=set(),
        snapshot_target_ids={snap_tgt},
    )

    assert ghost_tgt_in_use not in _deleted_ids(db, "data_targets")


@pytest.mark.asyncio
async def test_reconcile_no_ghosts_deletes_nothing():
    """When the live set already equals the snapshot set, nothing is deleted."""
    src, tgt = uuid.uuid4(), uuid.uuid4()
    model_id = uuid.uuid4()

    db = _FakeDB(select_map={
        "DataTarget": [(tgt,)],
        "DataSource": [(src,)],
        "AggregateDefinition": [],
        "PocketDefinition": [],
    })

    await _reconcile_sources_and_targets(
        model_id, db,
        snapshot_source_ids={src},
        snapshot_target_ids={tgt},
    )

    assert _deleted_ids(db, "data_targets") == set()
    assert _deleted_ids(db, "data_sources") == set()
