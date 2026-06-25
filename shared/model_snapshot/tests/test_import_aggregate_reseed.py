"""F-013-05: snapshot import must not land aggregates/pockets active and
pointing at the SOURCE model's physical tables.

The fix has two halves, both proven here:

1. ``import_export.py`` passes ``force_aggregate_pending`` / ``force_pocket_stale``
   so imported materializations arrive not-yet-built (status pending / stale)
   rather than ``active`` — the router will not route onto a foreign table.
2. The rehydrator rebinds the seed segment of each ``physical_table_name`` to
   the destination model's fresh seed, so when the materialization is later
   refreshed it builds a table distinct from the source's
   (``agg_<source_seed>_<suffix>`` → ``agg_<new_seed>_<suffix>``).
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.model_snapshot.rehydrator import (
    _insert_aggregates,
    _insert_pockets,
    _reseed_physical_table_name,
)


# ---------------------------------------------------------------------------
# Pure helper
# ---------------------------------------------------------------------------

def test_reseed_replaces_only_the_seed_segment():
    assert _reseed_physical_table_name("agg_OLD_abcd", "agg", "NEW") == "agg_NEW_abcd"
    assert _reseed_physical_table_name("pocket_OLD_ff01", "pocket", "NEW") == "pocket_NEW_ff01"


def test_reseed_handles_uuid_seeds_without_underscores():
    src = "123e4567-e89b-12d3-a456-426614174000"
    name = f"agg_{src}_aa11"
    assert _reseed_physical_table_name(name, "agg", "fresh") == "agg_fresh_aa11"


def test_reseed_leaves_unexpected_shapes_untouched():
    assert _reseed_physical_table_name("agg_x_y", "pocket", "NEW") == "agg_x_y"  # wrong prefix
    assert _reseed_physical_table_name("agg_only", "agg", "NEW") == "agg_only"   # 1 part
    assert _reseed_physical_table_name("agg_a_b_c", "agg", "NEW") == "agg_a_b_c"  # 4 parts


def test_reseed_no_op_when_seed_absent_or_non_string():
    assert _reseed_physical_table_name("agg_a_b", "agg", None) == "agg_a_b"
    assert _reseed_physical_table_name(None, "agg", "NEW") is None


# ---------------------------------------------------------------------------
# Insert helpers: capture the rows handed to db.execute()
# ---------------------------------------------------------------------------

def _capture_db():
    """Mock AsyncSession that records the values dict of every insert()."""
    db = AsyncMock()
    captured: list[dict] = []

    async def _execute(stmt):
        # SQLAlchemy Insert: compile() exposes the bound params.
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
async def test_insert_aggregates_reseeds_physical_name_on_import():
    model_id = uuid.uuid4()
    src_seed = "11112222"
    snap = {
        "aggregates": [
            {
                "id": str(uuid.uuid4()),
                "physical_table_name": f"agg_{src_seed}_abcd",
                "status": "active",
                "grain": [],
                "columns": [],
            }
        ]
    }
    db = _capture_db()
    await _insert_aggregates(model_id, snap, db, force_pending=True, reseed="newseed99")

    agg_rows = [r for r in db._captured if "physical_table_name" in r]
    assert agg_rows, "no aggregate row captured"
    row = agg_rows[0]
    assert row["physical_table_name"] == "agg_newseed99_abcd"
    assert row["status"] == "pending"  # force_pending overrides active


@pytest.mark.asyncio
async def test_insert_aggregates_no_reseed_when_not_importing():
    """Revert / non-import paths must NOT rewrite the physical name."""
    model_id = uuid.uuid4()
    snap = {
        "aggregates": [
            {
                "id": str(uuid.uuid4()),
                "physical_table_name": "agg_keepme_abcd",
                "status": "active",
                "grain": [],
                "columns": [],
            }
        ]
    }
    db = _capture_db()
    await _insert_aggregates(model_id, snap, db, force_pending=False, reseed=None)
    agg_rows = [r for r in db._captured if "physical_table_name" in r]
    assert agg_rows[0]["physical_table_name"] == "agg_keepme_abcd"


@pytest.mark.asyncio
async def test_insert_aggregates_reports_forced_pending_for_observability():
    """Bug-5346: importing a HEALTHY aggregate forces it to pending; the
    helper must report (id, prior_status) so the caller can emit a lifecycle
    event + alert instead of the transition being silent."""
    model_id = uuid.uuid4()
    active_id = uuid.uuid4()
    disabled_id = uuid.uuid4()
    retired_id = uuid.uuid4()
    snap = {
        "aggregates": [
            {"id": str(active_id), "physical_table_name": "agg_s_a",
             "status": "active", "grain": [], "columns": []},
            {"id": str(disabled_id), "physical_table_name": "agg_s_b",
             "status": "disabled", "grain": [], "columns": []},
            {"id": str(retired_id), "physical_table_name": "agg_s_c",
             "status": "retired", "grain": [], "columns": []},
        ]
    }
    db = _capture_db()
    forced = await _insert_aggregates(model_id, snap, db, force_pending=True, reseed="z")

    forced_ids = {fid: prior for fid, prior in forced}
    # active + disabled were forced from a healthy state -> reported.
    assert forced_ids == {active_id: "active", disabled_id: "disabled"}
    # retired was never healthy -> not reported as a "disappeared" active.
    assert retired_id not in forced_ids


@pytest.mark.asyncio
async def test_insert_aggregates_reports_nothing_when_not_forcing():
    model_id = uuid.uuid4()
    snap = {
        "aggregates": [
            {"id": str(uuid.uuid4()), "physical_table_name": "agg_s_a",
             "status": "active", "grain": [], "columns": []},
        ]
    }
    db = _capture_db()
    forced = await _insert_aggregates(model_id, snap, db, force_pending=False, reseed=None)
    assert forced == []


@pytest.mark.asyncio
async def test_insert_pockets_reseeds_physical_name_on_import():
    model_id = uuid.uuid4()
    snap = {
        "pockets": [
            {
                "id": str(uuid.uuid4()),
                "physical_table_name": "pocket_sourceseed_ff01",
                "status": "active",
                "predicates": [],
            }
        ]
    }
    db = _capture_db()
    await _insert_pockets(model_id, snap, db, force_stale=True, reseed="destseed00")
    pocket_rows = [r for r in db._captured if "physical_table_name" in r]
    assert pocket_rows[0]["physical_table_name"] == "pocket_destseed00_ff01"
    assert pocket_rows[0]["status"] == "stale"
