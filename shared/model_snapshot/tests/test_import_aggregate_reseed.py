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


def test_reseed_replaces_unexpected_shapes_with_new_namespaced_name():
    """Bug-7298: non-standard shapes (wrong prefix, 1-part, 4+-part, bare
    hex) must NOT pass through unchanged — they still point at the SOURCE
    model's physical table.  The reseed now generates a fresh
    ``<prefix>_<new_seed>_<suffix>`` name for any non-matching shape."""
    # Wrong prefix: rewritten under the REQUESTED prefix, not left as-is
    result = _reseed_physical_table_name("agg_x_y", "pocket", "NEW")
    assert result.startswith("pocket_NEW_"), f"expected pocket_NEW_*, got {result}"
    assert result != "agg_x_y"

    # 1-part name: rewritten
    result = _reseed_physical_table_name("agg_only", "agg", "NEW")
    assert result.startswith("agg_NEW_"), f"expected agg_NEW_*, got {result}"
    assert result != "agg_only"

    # 4-part name: rewritten
    result = _reseed_physical_table_name("agg_a_b_c", "agg", "NEW")
    assert result.startswith("agg_NEW_"), f"expected agg_NEW_*, got {result}"
    assert result != "agg_a_b_c"


def test_reseed_replaces_bare_hex_optimizer_name():
    """Bug-7298 regression: optimizer-created aggregates use bare hex names
    from ``secrets.token_hex(6)`` — these have no ``agg_`` prefix and no
    3-part structure.  They MUST be replaced on import to prevent the
    imported clone from sharing the source model's physical table."""
    bare_hex = "a1b2c3d4e5f6"
    result = _reseed_physical_table_name(bare_hex, "agg", "newseed")
    assert result.startswith("agg_newseed_"), f"expected agg_newseed_*, got {result}"
    assert result != bare_hex


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
                # Live physical-build metadata from the source database must
                # never survive import, even in a hand-edited/legacy bundle.
                "built_for_storage_binding": {
                    "target_id": "source-target",
                    "project_connection_id": "source-connection",
                    "routing_fingerprint": "source-fingerprint",
                },
                # Bug-8602: the SOURCE identity is the same class of live
                # metadata — an imported aggregate holds no rows read from the
                # exporting tenant's database either.
                "built_for_source_binding": {
                    "model_id": "source-model",
                    "source_connection_id": "source-connection",
                    "routing_fingerprint": "source-fingerprint",
                },
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
    assert row["built_for_storage_binding"] is None
    assert row["built_for_source_binding"] is None


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
async def test_legacy_policy_import_defaults_append_only_authority_fail_closed():
    model_id = uuid.uuid4()
    agg_id = uuid.uuid4()
    snap = {
        "aggregates": [{
            "id": str(agg_id),
            "physical_table_name": "agg_keepme_contract",
            "status": "active",
            "grain": [],
            "columns": [],
            "refresh_policy": {
                "id": str(uuid.uuid4()),
                "aggregate_definition_id": str(agg_id),
                "refresh_mode": "incremental",
                "cron_expression": "0 2 * * *",
                "incremental_column": "business_date",
                "incremental_lookback": 7,
                "is_enabled": True,
            },
        }],
    }
    db = _capture_db()

    await _insert_aggregates(model_id, snap, db)

    policy_rows = [r for r in db._captured if "incremental_column" in r]
    assert len(policy_rows) == 1
    assert policy_rows[0]["incremental_append_only"] is False
    assert policy_rows[0]["full_rebuild_interval_days"] is None


@pytest.mark.asyncio
async def test_version_restore_preserves_explicit_append_only_authority_and_cadence():
    """A same-model version restore/revert (``force_pending`` False) points at the
    SAME data source, so the append-only declaration it made about that source is
    still valid and is preserved. This is the counterpart to the import/clone
    reset below (Bug-8768, Adjustment 4)."""
    model_id = uuid.uuid4()
    agg_id = uuid.uuid4()
    snap = {
        "aggregates": [{
            "id": str(agg_id),
            "physical_table_name": "agg_keepme_declared",
            "status": "active",
            "grain": [],
            "columns": [],
            "refresh_policy": {
                "id": str(uuid.uuid4()),
                "aggregate_definition_id": str(agg_id),
                "refresh_mode": "incremental",
                "cron_expression": "0 2 * * *",
                "incremental_column": "business_date",
                "incremental_lookback": 7,
                "incremental_append_only": True,
                "full_rebuild_interval_days": 30,
                "is_enabled": True,
            },
        }],
    }
    db = _capture_db()

    # force_pending=False -> version restore / revert, not an import/clone.
    await _insert_aggregates(model_id, snap, db, force_pending=False)

    policy_rows = [r for r in db._captured if "incremental_column" in r]
    assert len(policy_rows) == 1
    assert policy_rows[0]["incremental_append_only"] is True
    assert policy_rows[0]["full_rebuild_interval_days"] == 30


@pytest.mark.asyncio
async def test_import_resets_append_only_declaration_and_reports_it():
    """Bug-8768 (Adjustment 4): import/clone (``force_pending`` True) RESETS an
    imported ``incremental_append_only=true`` to false — the assertion is about
    one specific source and the imported model may point at data that behaves
    differently — and raises an explanatory alert so the disabling is visible.
    The periodic cadence is left intact for when the user re-enables incremental.

    Fails before the fix: the rehydrator used ``setdefault``, which preserved the
    imported ``true`` and silently authorised a windowed patch against an unvetted
    source.
    """
    from unittest.mock import MagicMock

    from shared.db.models import ModelAlert

    model_id = uuid.uuid4()
    agg_id = uuid.uuid4()
    snap = {
        "aggregates": [{
            "id": str(agg_id),
            "physical_table_name": "agg_src_declared",
            "status": "active",
            "grain": [],
            "columns": [],
            "refresh_policy": {
                "id": str(uuid.uuid4()),
                "aggregate_definition_id": str(agg_id),
                "refresh_mode": "incremental",
                "cron_expression": "0 2 * * *",
                "incremental_column": "business_date",
                "incremental_lookback": 7,
                "incremental_append_only": True,
                "full_rebuild_interval_days": 30,
                "is_enabled": True,
            },
        }],
    }
    db = _capture_db()
    # Session.add is synchronous on a real AsyncSession; use a sync mock so the
    # explanatory-alert add is a plain recorded call, not an un-awaited coroutine.
    db.add = MagicMock()

    await _insert_aggregates(model_id, snap, db, force_pending=True, reseed="z", reset_incremental_authority=True)

    policy_rows = [r for r in db._captured if "incremental_column" in r]
    assert len(policy_rows) == 1
    # The append-only assertion is dropped; the cadence setting is left in place.
    assert policy_rows[0]["incremental_append_only"] is False
    assert policy_rows[0]["full_rebuild_interval_days"] == 30

    # Exactly one explanatory ModelAlert was added, and it names the disabling.
    added_alerts = [
        c.args[0] for c in db.add.call_args_list
        if c.args and isinstance(c.args[0], ModelAlert)
    ]
    assert len(added_alerts) == 1
    alert = added_alerts[0]
    assert alert.model_id == model_id
    assert alert.category == "aggregate_lifecycle"
    assert "disabled" in alert.detail.lower()
    assert "append-only" in alert.detail.lower()


@pytest.mark.asyncio
async def test_import_does_not_report_when_no_append_only_to_reset():
    """No reset alert when the imported policy never declared append-only."""
    from unittest.mock import MagicMock

    from shared.db.models import ModelAlert

    model_id = uuid.uuid4()
    agg_id = uuid.uuid4()
    snap = {
        "aggregates": [{
            "id": str(agg_id),
            "physical_table_name": "agg_src_plain",
            "status": "active",
            "grain": [],
            "columns": [],
            "refresh_policy": {
                "id": str(uuid.uuid4()),
                "aggregate_definition_id": str(agg_id),
                "refresh_mode": "scheduled",
                "cron_expression": "0 2 * * *",
                "incremental_column": None,
                "incremental_lookback": None,
                "incremental_append_only": False,
                "full_rebuild_interval_days": None,
                "is_enabled": True,
            },
        }],
    }
    db = _capture_db()
    db.add = MagicMock()

    await _insert_aggregates(model_id, snap, db, force_pending=True, reseed="z", reset_incremental_authority=True)

    added_alerts = [
        c.args[0] for c in db.add.call_args_list
        if c.args and isinstance(c.args[0], ModelAlert)
    ]
    assert added_alerts == []


@pytest.mark.asyncio
async def test_insert_aggregates_reports_forced_pending_for_observability():
    """Bug-5346 + Bug-7903: importing an ACTIVE aggregate forces it to pending
    (its materialised table does not exist post-import); the helper reports
    (id, prior_status) so the caller can emit a lifecycle event + alert instead of
    a silent transition.

    Bug-7903 (Fable MED #3/#5): a DISABLED aggregate is NON-SERVING regardless of a
    physical table, so it is kept DISABLED — NOT forced to pending. Forcing it to
    pending left it stuck "pending" forever when its imported policy was disabled
    (never swept — a UI lie), and risked an export→import chain laundering it to
    active. So a disabled aggregate is no longer reported as "forced pending" and is
    written with status "disabled"."""
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
    forced = await _insert_aggregates(model_id, snap, db, force_pending=True, reseed="z", reset_incremental_authority=True)

    forced_ids = {fid: prior for fid, prior in forced}
    # Only ACTIVE is forced from a healthy servable state -> reported.
    assert forced_ids == {active_id: "active"}
    # disabled stays disabled (not forced pending, not reported); retired unchanged.
    assert disabled_id not in forced_ids
    assert retired_id not in forced_ids

    # Verify the written statuses: active -> pending (+durable prior), disabled ->
    # disabled, retired -> pending (F-013-05, non-serving until rebuilt).
    status_by_name = {
        r.get("physical_table_name"): r for r in db._captured
        if "physical_table_name" in r and "status" in r
    }
    assert status_by_name["agg_z_a"]["status"] == "pending"
    assert status_by_name["agg_z_a"].get("refresh_prior_status") == "active"
    assert status_by_name["agg_z_b"]["status"] == "disabled"
    assert "refresh_prior_status" not in status_by_name["agg_z_b"] or \
        status_by_name["agg_z_b"].get("refresh_prior_status") is None


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
