"""F-016-01: a regenerated calendar must invalidate dependent aggregates.

A calendar regeneration shifts period boundaries, so any aggregate grouped at a
calendar-derived time grain now holds stale rows. ``_invalidate_time_grained_
aggregates`` flips each SERVABLE ("active") time-grained aggregate to
non-servable ("pending") with its prior status durably preserved, so the query
binder (which serves ``status=="active"`` only) stops serving it until a refresh
rebuilds it.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.calendar import (
    _calendar_period_math_changed,
    _invalidate_time_grained_aggregates,
)

pytestmark = pytest.mark.unit


def _agg(*, grain, status="active", prior=None):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        grain=grain,
        status=status,
        refresh_prior_status=prior,
    )


def _db(*, time_dim_names, aggregates):
    db = MagicMock()

    time_res = MagicMock()
    time_res.all.return_value = [(n,) for n in time_dim_names]
    agg_res = MagicMock()
    agg_res.scalars.return_value.all.return_value = aggregates

    calls = {"n": 0}

    async def _execute(stmt):
        calls["n"] += 1
        # First execute = time dims, second = aggregates.
        return time_res if calls["n"] == 1 else agg_res

    db.execute = AsyncMock(side_effect=_execute)
    db.flush = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_time_grained_aggregate_is_invalidated():
    agg = _agg(grain=["month", "region"])
    db = _db(time_dim_names=["month", "year"], aggregates=[agg])

    n = await _invalidate_time_grained_aggregates(db, model_id=uuid.uuid4())

    assert n == 1
    assert agg.status == "pending"
    assert agg.refresh_prior_status == "active"


@pytest.mark.asyncio
async def test_non_time_grained_aggregate_is_left_alone():
    agg = _agg(grain=["region", "product"])
    db = _db(time_dim_names=["month", "year"], aggregates=[agg])

    n = await _invalidate_time_grained_aggregates(db, model_id=uuid.uuid4())

    assert n == 0
    assert agg.status == "active"
    assert agg.refresh_prior_status is None


@pytest.mark.asyncio
async def test_no_time_dims_means_no_invalidation():
    agg = _agg(grain=["month"])
    db = _db(time_dim_names=[], aggregates=[agg])

    n = await _invalidate_time_grained_aggregates(db, model_id=uuid.uuid4())

    assert n == 0
    assert agg.status == "active"


@pytest.mark.asyncio
async def test_prior_status_not_overwritten_when_already_set():
    # An aggregate already mid-refresh (prior recorded) keeps its durable prior.
    agg = _agg(grain=["month"], status="active", prior="disabled")
    db = _db(time_dim_names=["month"], aggregates=[agg])

    await _invalidate_time_grained_aggregates(db, model_id=uuid.uuid4())

    assert agg.status == "pending"
    assert agg.refresh_prior_status == "disabled"


# ---------------------------------------------------------------------------
# F-016-03: update_calendar (a BOUND calendar edit) must invalidate time-grained
# aggregates when the period math changes — not only auto-create.
# ---------------------------------------------------------------------------

def _cal(**fields):
    base = dict(
        calendar_type="standard",
        fiscal_year_start_month=1,
        date_column="d",
        year_column="y",
        quarter_column="q",
        month_column="m",
        week_column="w",
        half_column="h",
    )
    base.update(fields)
    return types.SimpleNamespace(**base)


class TestCalendarPeriodMathChanged:
    def test_calendar_type_change_triggers(self):
        assert _calendar_period_math_changed(_cal(), {"calendar_type": "fiscal"}) is True

    def test_fiscal_start_change_triggers(self):
        assert _calendar_period_math_changed(_cal(), {"fiscal_year_start_month": 4}) is True

    def test_period_column_remap_triggers(self):
        assert _calendar_period_math_changed(_cal(), {"year_column": "fiscal_year"}) is True

    def test_same_value_does_not_trigger(self):
        # Re-sending the stored value is not a change.
        assert _calendar_period_math_changed(_cal(), {"calendar_type": "standard"}) is False

    def test_unrelated_field_does_not_trigger(self):
        assert _calendar_period_math_changed(_cal(), {"display_name": "Renamed"}) is False

    def test_empty_update_does_not_trigger(self):
        assert _calendar_period_math_changed(_cal(), {}) is False
