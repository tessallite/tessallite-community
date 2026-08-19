"""Rehydrator certification_status boundary clamp (Bug-6622).

The model-service Create/Update API gates ``certification_status`` to the enum
["draft","shared","certified","deprecated"] (Bug-6264 / SH-1). The rehydrator,
however, pours import-bundle rows straight into the live tables and bypasses
that sanctioned entry point. A hand-authored or tampered bundle could therefore
carry an out-of-enum status straight into the DB.

These tests prove the light defense-in-depth clamp: any object that carries
certification_status (KPI + Named Set) is coerced to the safe default "draft"
on an unknown value (non-blocking — the import still completes) and the coercion
is recorded, while every valid value passes through unchanged.
"""
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.model_snapshot.rehydrator import (
    _DEFAULT_CERTIFICATION_STATUS,
    _VALID_CERTIFICATION_STATUSES,
    _clamp_certification_status,
    _insert_kpis,
    _insert_named_sets,
)


# ---------------------------------------------------------------------------
# The valid set must stay in lock-step with the single-source schema Literal.
# ---------------------------------------------------------------------------

def test_valid_set_matches_schema_literal():
    from typing import get_args

    from shared.schemas.domains.governance_advanced import CertificationStatus

    assert _VALID_CERTIFICATION_STATUSES == frozenset(get_args(CertificationStatus))
    assert _DEFAULT_CERTIFICATION_STATUS in _VALID_CERTIFICATION_STATUSES
    assert _VALID_CERTIFICATION_STATUSES == {
        "draft", "shared", "certified", "deprecated"
    }


# ---------------------------------------------------------------------------
# Unit gate: _clamp_certification_status
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("valid", ["draft", "shared", "certified", "deprecated"])
def test_clamp_passes_valid_values_unchanged(valid):
    row = {"name": "x", "certification_status": valid}
    _clamp_certification_status(row, object_kind="kpi", object_id="id-1")
    assert row["certification_status"] == valid


@pytest.mark.parametrize("bogus", ["bogus", "CERTIFIED", "", "approved", "trusted"])
def test_clamp_coerces_unknown_to_draft(bogus, caplog):
    row = {"name": "x", "certification_status": bogus}
    with caplog.at_level(logging.WARNING):
        _clamp_certification_status(row, object_kind="kpi", object_id="id-1")
    assert row["certification_status"] == "draft"
    # The coercion is recorded so it is visible to operators.
    assert any("Bug-6622" in r.message for r in caplog.records)
    assert any("id-1" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "malformed",
    [
        ["certified"],           # unhashable list — would raise TypeError un-guarded
        {"status": "certified"}, # unhashable dict
        7,                       # non-string scalar
        True,                    # bool
    ],
)
def test_clamp_coerces_non_string_shapes_without_raising(malformed, caplog):
    """A tampered bundle can smuggle a non-string shape into the field. The
    clamp must coerce it to "draft" (non-blocking), never raise TypeError on the
    membership test — the whole point of Bug-6622 is that a bad bundle imports
    safely rather than crashing the request."""
    row = {"name": "x", "certification_status": malformed}
    with caplog.at_level(logging.WARNING):
        _clamp_certification_status(row, object_kind="named_set", object_id="id-9")
    assert row["certification_status"] == "draft"
    assert any("Bug-6622" in r.message for r in caplog.records)


def test_clamp_absent_key_is_untouched():
    row = {"name": "x"}
    _clamp_certification_status(row, object_kind="named_set", object_id="id-2")
    assert "certification_status" not in row


def test_clamp_does_not_warn_on_valid_value(caplog):
    row = {"name": "x", "certification_status": "certified"}
    with caplog.at_level(logging.WARNING):
        _clamp_certification_status(row, object_kind="kpi", object_id="id-3")
    assert not any("Bug-6622" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Integration: the whole insert path must clamp, not just the unit gate.
# ---------------------------------------------------------------------------

def _capture_inserts():
    """Mock AsyncSession that records the params of every insert."""
    db = AsyncMock()
    inserts: list[dict] = []

    from sqlalchemy.sql.dml import Insert

    async def _execute(stmt):
        # Record only INSERTs — the in-place upsert also SELECTs live ids and may
        # UPDATE self-FK links; those are not the rows under test.
        if isinstance(stmt, Insert):
            try:
                inserts.append(dict(stmt.compile().params))
            except Exception:
                inserts.append({})
        res = MagicMock()
        res.all.return_value = []
        return res

    db.execute = AsyncMock(side_effect=_execute)
    db._inserts = inserts
    return db


@pytest.mark.asyncio
async def test_insert_named_sets_clamps_bogus_to_draft(caplog):
    model_id = uuid.uuid4()
    ns_id = str(uuid.uuid4())
    snap = {
        "named_sets": [
            {
                "id": ns_id,
                "name": "top_accounts",
                "expression": "{[x]}",
                "certification_status": "bogus",
            }
        ]
    }
    db = _capture_inserts()
    with caplog.at_level(logging.WARNING):
        await _insert_named_sets(model_id, snap, db)  # must NOT raise
    assert db._inserts[0]["certification_status"] == "draft"
    assert any("Bug-6622" in r.message for r in caplog.records)
    assert any(ns_id in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_insert_named_sets_valid_passes_through():
    model_id = uuid.uuid4()
    snap = {
        "named_sets": [
            {
                "id": str(uuid.uuid4()),
                "name": "certified_set",
                "expression": "{[x]}",
                "certification_status": "certified",
            }
        ]
    }
    db = _capture_inserts()
    await _insert_named_sets(model_id, snap, db)
    assert db._inserts[0]["certification_status"] == "certified"


@pytest.mark.asyncio
async def test_insert_kpis_clamps_bogus_to_draft(caplog):
    model_id = uuid.uuid4()
    kpi_id = str(uuid.uuid4())
    snap = {
        "kpis": [
            {
                "id": kpi_id,
                "name": "revenue_kpi",
                "certification_status": "tampered",
            }
        ]
    }
    db = _capture_inserts()
    with caplog.at_level(logging.WARNING):
        await _insert_kpis(model_id, snap, db)  # must NOT raise
    assert db._inserts[0]["certification_status"] == "draft"
    assert any("Bug-6622" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_insert_kpis_valid_passes_through():
    model_id = uuid.uuid4()
    snap = {
        "kpis": [
            {
                "id": str(uuid.uuid4()),
                "name": "shared_kpi",
                "certification_status": "shared",
            }
        ]
    }
    db = _capture_inserts()
    await _insert_kpis(model_id, snap, db)
    assert db._inserts[0]["certification_status"] == "shared"


@pytest.mark.asyncio
async def test_insert_kpis_clamps_bogus_in_orphan_fallback_round(caplog):
    """The KPI inserter has a second insert path: the orphan/cycle fallback
    round that fires when no KPI is topologically ready (e.g. a parent_kpi_id
    that points outside the snapshot). The clamp must cover that path too."""
    model_id = uuid.uuid4()
    kpi_id = str(uuid.uuid4())
    # parent points at a non-existent KPI -> never "ready" -> fallback round.
    snap = {
        "kpis": [
            {
                "id": kpi_id,
                "name": "orphan_kpi",
                "parent_kpi_id": str(uuid.uuid4()),
                "certification_status": "bogus",
            }
        ]
    }
    db = _capture_inserts()
    with caplog.at_level(logging.WARNING):
        await _insert_kpis(model_id, snap, db)  # must NOT raise
    assert db._inserts[0]["certification_status"] == "draft"
    assert any("Bug-6622" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_insert_kpis_absent_status_falls_to_db_default():
    """A KPI with no certification_status key is left alone; the NOT-NULL DB
    default ("draft") applies. The clamp must not inject the column."""
    model_id = uuid.uuid4()
    snap = {"kpis": [{"id": str(uuid.uuid4()), "name": "plain_kpi"}]}
    db = _capture_inserts()
    await _insert_kpis(model_id, snap, db)
    # The clamp must not inject a value; the column falls to its NOT-NULL DB
    # default ("draft") at execution. In the unbound compile the param shows as
    # None (no explicit value sent) rather than a clamped "draft".
    assert db._inserts[0].get("certification_status") is None
