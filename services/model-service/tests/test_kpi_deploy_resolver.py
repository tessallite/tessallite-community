"""Tests for the KPI deployed-snapshot serving authority (F-017-01 / F-013-04).

Contract under test: for a DEPLOYED model, a served KPI's DEFINITION comes from
the deployed snapshot while its GOVERNANCE is overlaid from the live row. A live
definition edit is invisible to serving until model deploy; an invalid deployed
snapshot fails closed; an undeployed model serves the live draft.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from shared.db.models import KPI, Model, ModelVersion
from src.kpi_deploy_resolver import (
    KpiSnapshotInvalidError,
    ResolvedKpi,
    Undeployed,
    Withheld,
    build_served_kpi,
    resolve_served_kpi,
    resolve_served_kpis,
)

pytestmark = pytest.mark.unit


class _FakeDb:
    """Minimal async ``db.get`` stub keyed by (type, id)."""

    def __init__(self, objects):
        self._by_key = {(type(o).__name__, str(o.id)): o for o in objects}

    async def get(self, model_cls, obj_id):
        return self._by_key.get((model_cls.__name__, str(obj_id)))


def _live_kpi(kpi_id, model_id, **overrides):
    base = dict(
        id=kpi_id,
        model_id=model_id,
        name="Revenue KPI",
        kpi_type="simple_measure",
        expression="measure('Revenue')",
        direction="higher_is_better",
        target_type="static",
        target_value=100.0,
        certification_status="certified",
        owner_user_id="owner@acme.com",
        is_deployed=True,
        deployed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        calc_agg_mode="automatic",
        trend_period="month",
        trend_threshold=0.01,
        trend_sparkline_periods=12,
        null_display_value="N/A",
    )
    base.update(overrides)
    return KPI(**base)


def _snapshot_kpi_dict(kpi_id, model_id, **overrides):
    """A serialised snapshot KPI dict (UUIDs as strings, as the serialiser stores)."""
    d = {
        "id": str(kpi_id),
        "model_id": str(model_id),
        "name": "Revenue KPI",
        "kpi_type": "simple_measure",
        "expression": "measure('Revenue')",
        "direction": "higher_is_better",
        "target_type": "static",
        "target_value": 100.0,
        "calc_agg_mode": "automatic",
        "trend_period": "month",
        "trend_threshold": 0.01,
        "trend_sparkline_periods": 12,
        "null_display_value": "N/A",
        # Governance fields present in the snapshot must be IGNORED (live wins).
        "certification_status": "draft",
        "is_deployed": False,
        "owner_user_id": "stale@acme.com",
    }
    d.update(overrides)
    return d


def _deployed_model(model_id, version_id, epoch=1):
    return Model(id=model_id, deployed_version_id=version_id, deploy_epoch=epoch)


def _version(version_id, model_id, kpi_dicts):
    return ModelVersion(
        id=version_id,
        model_id=model_id,
        snapshot_json={
            "schema_version": "1.0",
            "measures": [{"id": str(uuid.uuid4()), "name": "Revenue"}],
            "kpis": kpi_dicts,
        },
    )


@pytest.mark.asyncio
async def test_deployed_definition_wins_over_live_edit():
    kpi_id, model_id, version_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    # Live row has been EDITED after deploy: new expression/target/direction.
    live = _live_kpi(
        kpi_id, model_id,
        expression="measure('Revenue') * 2",
        target_value=999.0,
        direction="lower_is_better",
        certification_status="certified",
    )
    snap = _snapshot_kpi_dict(kpi_id, model_id)  # deployed = original definition
    model = _deployed_model(model_id, version_id, epoch=3)
    db = _FakeDb([model, _version(version_id, model_id, [snap])])

    result = await resolve_served_kpi(db, model, live)

    assert isinstance(result, ResolvedKpi)
    served = result.kpi
    # DEFINITION comes from the deployed snapshot, NOT the live edit.
    assert served.expression == "measure('Revenue')"
    assert float(served.target_value) == 100.0
    assert served.direction == "higher_is_better"
    # GOVERNANCE comes from the live row.
    assert served.certification_status == "certified"
    assert served.owner_user_id == "owner@acme.com"
    assert served.is_deployed is True
    # Identity from live.
    assert served.id == kpi_id
    # Cache anchor is the deployed (version, epoch).
    assert result.deployed_version_id == version_id
    assert result.deploy_epoch == 3


@pytest.mark.asyncio
async def test_undeployed_model_serves_live_draft():
    kpi_id, model_id = uuid.uuid4(), uuid.uuid4()
    live = _live_kpi(kpi_id, model_id, is_deployed=False)
    model = Model(id=model_id, deployed_version_id=None, deploy_epoch=0)
    db = _FakeDb([model])

    result = await resolve_served_kpi(db, model, live)
    assert isinstance(result, Undeployed)


@pytest.mark.asyncio
async def test_kpi_absent_from_snapshot_is_withheld():
    kpi_id, other_id = uuid.uuid4(), uuid.uuid4()
    model_id, version_id = uuid.uuid4(), uuid.uuid4()
    live = _live_kpi(kpi_id, model_id)
    # Snapshot pins a DIFFERENT KPI -> this one was never deployed.
    snap_other = _snapshot_kpi_dict(other_id, model_id)
    model = _deployed_model(model_id, version_id)
    db = _FakeDb([model, _version(version_id, model_id, [snap_other])])

    result = await resolve_served_kpi(db, model, live)
    assert isinstance(result, Withheld)


@pytest.mark.asyncio
async def test_invalid_snapshot_fails_closed():
    kpi_id, model_id, version_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    live = _live_kpi(kpi_id, model_id)
    model = _deployed_model(model_id, version_id)
    # Empty/placeholder snapshot -> not a valid serving authority.
    empty_version = ModelVersion(
        id=version_id, model_id=model_id, snapshot_json={"schema_version": "1.0"}
    )
    db = _FakeDb([model, empty_version])

    with pytest.raises(KpiSnapshotInvalidError):
        await resolve_served_kpi(db, model, live)


@pytest.mark.asyncio
async def test_missing_version_row_fails_closed():
    kpi_id, model_id, version_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    live = _live_kpi(kpi_id, model_id)
    model = _deployed_model(model_id, version_id)
    db = _FakeDb([model])  # version row absent

    with pytest.raises(KpiSnapshotInvalidError):
        await resolve_served_kpi(db, model, live)


@pytest.mark.asyncio
async def test_resolve_batch_partitions_resolved_and_withheld():
    model_id, version_id = uuid.uuid4(), uuid.uuid4()
    kept_id, dropped_id = uuid.uuid4(), uuid.uuid4()
    kept_live = _live_kpi(kept_id, model_id, name="Kept")
    dropped_live = _live_kpi(dropped_id, model_id, name="Dropped")
    snap = _snapshot_kpi_dict(kept_id, model_id, name="Kept")
    model = _deployed_model(model_id, version_id, epoch=2)
    db = _FakeDb([model, _version(version_id, model_id, [snap])])

    resolved, withheld = await resolve_served_kpis(
        db, model, [kept_live, dropped_live]
    )
    assert [r.kpi.id for r in resolved] == [kept_id]
    assert withheld == [dropped_id]
    assert resolved[0].deploy_epoch == 2


@pytest.mark.asyncio
async def test_bug8711_deleted_kpi_is_removed_from_serving_immediately():
    """Bug-8711: deleting a KPI removes it from BI serving IMMEDIATELY, with no
    Deploy wait. Membership is live-driven — the resolver iterates the live rows
    and can only WITHHOLD, never re-add a row the snapshot still pins. So a KPI
    still present in the deployed snapshot but with NO live row (it was deleted)
    must NOT be resurrected from the snapshot. This mirrors the named-set guard
    ``test_membership_stays_live_driven_and_governance_is_never_snapshot_sourced``
    and pins the same product decision for the KPI family.

    Ordinary DEFINITION edits stay deploy-gated (proven by
    ``test_deployed_definition_wins_over_live_edit``); only DELETE is immediate.

    If you are here because you want deleted-but-deployed KPIs to keep serving,
    that is the reverted snapshot-orphan design — reopen Bug-8753 and change this
    test deliberately, together with the named-set sibling.
    """
    kpi_id, model_id, version_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    # The snapshot still pins the KPI, but it no longer exists live (deleted).
    snap = _snapshot_kpi_dict(kpi_id, model_id)
    model = _deployed_model(model_id, version_id)
    db = _FakeDb([model, _version(version_id, model_id, [snap])])

    resolved, withheld = await resolve_served_kpis(db, model, [])

    assert resolved == [], (
        "A KPI with no live row must not be served from the snapshot: a deleted "
        "KPI must disappear from BI serving immediately, without a redeploy."
    )
    assert withheld == []


def test_build_served_kpi_coerces_uuid_fields():
    kpi_id, model_id, dim_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    live = _live_kpi(kpi_id, model_id)
    snap = _snapshot_kpi_dict(kpi_id, model_id, time_dimension_id=str(dim_id))
    served = build_served_kpi(snap, live)
    # A stringified UUID definition field is coerced back to UUID so the
    # evaluator's ``db.get(Dimension, kpi.time_dimension_id)`` works.
    assert served.time_dimension_id == dim_id
