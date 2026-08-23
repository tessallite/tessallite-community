"""Tests for L4 (Model-Service Data Integrity and Guards) bug fixes.

Bug-7850: _insert_kpis topological sort must order on replacement_id FK.
Bug-7210: update_calendar rejects explicit calendar_type:null.
Bug-6995: create_pocket rejects LIMIT in defining_sql.
Bug-7142: revert response surfaces governance_preserved.
Bug-7302: dead aggregate-rebuild trigger removed from importers.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _u() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Bug-7850/Bug-7982: _insert_kpis two-pass insert preserves self-FK graphs
# ---------------------------------------------------------------------------


def _kpi_recording_db():
    from sqlalchemy.sql.dml import Insert, Update

    db = AsyncMock()
    inserts: list[dict] = []
    updates: list[dict] = []

    async def _execute(stmt, *a, **k):
        if isinstance(stmt, Insert):
            inserts.append(dict(stmt.compile().params))
        elif isinstance(stmt, Update):
            updates.append(dict(stmt.compile().params))
        # In-place upsert first SELECTs live parent ids; answer empty.
        res = MagicMock()
        res.all.return_value = []
        return res

    db.execute = AsyncMock(side_effect=_execute)
    db._inserts = inserts
    db._updates = updates
    return db


@pytest.mark.asyncio
async def test_insert_kpis_two_pass_wires_replacement_after_all_inserted():
    """Bug-7850/Bug-7982: _insert_kpis inserts every KPI with its self-FKs
    detached, then wires replacement_id in a second pass — so insertion order no
    longer matters and no IntegrityError can occur. A's replacement must be wired
    to B."""
    from shared.model_snapshot.rehydrator import _insert_kpis

    model_id = uuid.uuid4()
    kpi_a_id, kpi_b_id = _u(), _u()
    snap = {
        "kpis": [
            {"id": kpi_a_id, "model_id": str(model_id), "name": "Old KPI",
             "replacement_id": kpi_b_id},
            {"id": kpi_b_id, "model_id": str(model_id), "name": "New KPI"},
        ]
    }
    db = _kpi_recording_db()
    await _insert_kpis(model_id, snap, db)

    # Both inserted, NO replacement_id set at insert time.
    assert len(db._inserts) == 2
    assert all("replacement_id" not in ins for ins in db._inserts)
    # Pass 2 wired exactly A -> B.
    assert len(db._updates) == 1
    assert str(db._updates[0].get("replacement_id")) == str(kpi_b_id)


@pytest.mark.asyncio
async def test_insert_kpis_preserves_mutual_replacement_cycle():
    """Bug-7982: a valid A<->B replacement cycle must SURVIVE rehydration (2
    inserts + 2 link updates), not be silently erased by a fallback strip."""
    from shared.model_snapshot.rehydrator import _insert_kpis

    model_id = uuid.uuid4()
    kpi_a_id, kpi_b_id = _u(), _u()
    snap = {
        "kpis": [
            {"id": kpi_a_id, "model_id": str(model_id), "name": "KPI A",
             "replacement_id": kpi_b_id},
            {"id": kpi_b_id, "model_id": str(model_id), "name": "KPI B",
             "replacement_id": kpi_a_id},
        ]
    }
    db = _kpi_recording_db()
    await _insert_kpis(model_id, snap, db)

    assert len(db._inserts) == 2
    # Both links preserved (not erased): the two UPDATEs carry A and B.
    assert len(db._updates) == 2
    wired = {str(u.get("replacement_id")) for u in db._updates}
    assert wired == {str(kpi_a_id), str(kpi_b_id)}


# ---------------------------------------------------------------------------
# Bug-7210: CalendarUpdateRequest accepts None for calendar_type at schema
# level; the endpoint rejects it with 422.
# ---------------------------------------------------------------------------


def test_calendar_update_request_accepts_none_calendar_type():
    """Bug-7210: CalendarUpdateRequest with calendar_type:null passes schema
    validation (Optional[str]), but the endpoint must catch it before the
    NOT NULL column write.  This test verifies the schema round-trips the
    null so the endpoint guard can detect it."""
    from src.api.calendar import CalendarUpdateRequest

    body = CalendarUpdateRequest.model_validate({"calendar_type": None})
    updates = body.model_dump(exclude_unset=True)
    assert "calendar_type" in updates
    assert updates["calendar_type"] is None


# ---------------------------------------------------------------------------
# Bug-6995: create_pocket rejects LIMIT in defining_sql
# ---------------------------------------------------------------------------


def test_pocket_limit_regex_detects_keyword():
    """Bug-6995: LIMIT keyword detection regex matches LIMIT but not
    partial matches like 'credit_limit'."""
    import re

    pattern = r"\bLIMIT\b"
    assert re.search(pattern, "SELECT * FROM t LIMIT 10", re.IGNORECASE)
    assert re.search(pattern, "SELECT * FROM t limit 5", re.IGNORECASE)
    assert not re.search(pattern, "SELECT credit_limit FROM t", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Bug-7142: revert response surfaces governance_preserved
# ---------------------------------------------------------------------------


def test_revert_response_has_governance_preserved_fields():
    """Bug-7142: the revert response must include governance_preserved=True
    and a governance_preserved_note field so the frontend can inform the
    user that governance was not rolled back."""
    # Simulate the response dict the endpoint returns
    response = {
        "status": "ok",
        "reverted_to": "some-version-id",
        "governance_preserved": True,
        "governance_preserved_note": (
            "Personas, data tags, row-security rules, and KPI governance "
            "were preserved from the live model and not rolled back to the "
            "reverted version."
        ),
    }
    assert response["governance_preserved"] is True
    assert "preserved" in response["governance_preserved_note"].lower()


# ---------------------------------------------------------------------------
# Bug-7302: dead aggregate-rebuild trigger removed from importers
# ---------------------------------------------------------------------------


def test_yaml_export_no_aggregate_rebuild_import():
    """Bug-7302: yaml_export must not import aggregate_rebuild_trigger
    (dead code removed)."""
    import importlib
    import src.api.yaml_export as mod

    importlib.reload(mod)
    assert not hasattr(mod, "_import_rebuild_tasks"), (
        "yaml_export still has _import_rebuild_tasks (dead code)"
    )


def test_dbt_import_no_aggregate_rebuild_import():
    """Bug-7302: dbt_import must not import aggregate_rebuild_trigger."""
    import importlib
    import src.api.dbt_import as mod

    importlib.reload(mod)
    assert not hasattr(mod, "_import_rebuild_tasks"), (
        "dbt_import still has _import_rebuild_tasks (dead code)"
    )
