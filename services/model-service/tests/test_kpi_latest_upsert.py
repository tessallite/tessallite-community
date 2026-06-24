"""Defensive persistence of KPI batch results into kpi_latest (B9 round 3).

Regression coverage for the MEDIUM found in round-2 verification: fail-loud
status labels longer than the original String(64) overflowed the column and
crashed the whole /evaluate-batch upsert with StringDataRightTruncationError,
which would also starve the scheduler snapshot sweep that calls the same path.

Two layers are exercised here:
  1. `_truncate_status_label` bounds any label to the column width so the write
     can never raise on length (the column is also widened to 255 by migration
     0127; the helper is the belt-and-braces write boundary).
  2. `_upsert_kpi_latest_batch` upserts each row under its own SAVEPOINT, so one
     failing row is logged and skipped, never aborting the batch — the other
     KPIs are still materialised into $KPIs.
"""
from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.kpis import (
    _STATUS_LABEL_MAX_LEN,
    _truncate_status_label,
    _upsert_kpi_latest_batch,
)

from shared.schemas.pydantic_models import KPIEvaluateResponse

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Truncation boundary
# ---------------------------------------------------------------------------


def test_truncate_status_label_none_passthrough():
    assert _truncate_status_label(None) is None


def test_truncate_status_label_short_unchanged():
    label = "Near Target"
    assert _truncate_status_label(label) == label


def test_truncate_status_label_exact_width_unchanged():
    label = "x" * _STATUS_LABEL_MAX_LEN
    assert _truncate_status_label(label) == label
    assert len(_truncate_status_label(label)) == _STATUS_LABEL_MAX_LEN


def test_truncate_status_label_overlong_bounded():
    # A TI-decomposition label can embed arbitrary router detail of any length.
    label = "Time-intelligence evaluation failed — " + "x" * 500
    out = _truncate_status_label(label)
    assert out is not None
    assert len(out) == _STATUS_LABEL_MAX_LEN
    assert out.endswith("…")
    # The leading, human-meaningful prefix survives.
    assert out.startswith("Time-intelligence evaluation failed — ")


# ---------------------------------------------------------------------------
# Per-row resilience of the batch upsert
# ---------------------------------------------------------------------------


def _empty_select_result():
    """A result object whose .scalars().all() yields no existing rows, so the
    F-017-29 dedupe load treats every KPI as new and writes it."""
    result = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = []
    result.scalars.return_value = scalars
    return result


def _begin_nested_factory(fail_on_execute):
    """Build a db mock whose begin_nested() opens a real async ctx manager and
    whose execute() raises for upsert rows flagged by *fail_on_execute*.

    The first execute is the F-017-29 pre-load SELECT of existing kpi_latest
    rows; it returns an empty set (no existing rows) and is not counted as an
    upsert. *fail_on_execute* is indexed by upsert ordinal (1-based)."""
    calls = {"execute": 0, "upsert": 0}

    @contextlib.asynccontextmanager
    async def _nested():
        yield None

    db = AsyncMock()
    db.begin_nested = MagicMock(side_effect=lambda: _nested())
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    async def _execute(stmt):
        calls["execute"] += 1
        if calls["execute"] == 1:
            # The pre-load SELECT — no existing rows.
            return _empty_select_result()
        calls["upsert"] += 1
        if fail_on_execute(calls["upsert"]):
            raise RuntimeError("simulated persistence error")
        return MagicMock()

    db.execute = AsyncMock(side_effect=_execute)
    return db, calls


def _resp(label):
    return KPIEvaluateResponse(
        kpi_id=uuid.uuid4(),
        value=1.0,
        target=2.0,
        status=0,
        status_label=label,
        trend_pct=None,
        formatted_value="1",
    )


@pytest.mark.asyncio
async def test_upsert_one_bad_row_does_not_abort_batch():
    model_id = uuid.uuid4()
    good_a, bad, good_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    kpi_objs = {
        good_a: SimpleNamespace(name="good_a"),
        bad: SimpleNamespace(name="bad"),
        good_b: SimpleNamespace(name="good_b"),
    }
    result_map = {
        good_a: _resp("On Track"),
        bad: _resp("boom"),
        good_b: _resp("Near Target"),
    }

    # Second upsert (the "bad" row) raises; the batch must still complete.
    db, calls = _begin_nested_factory(lambda n: n == 2)

    # Must not raise.
    await _upsert_kpi_latest_batch(db, model_id, kpi_objs, result_map)

    # All three rows attempted (after the pre-load SELECT); the two good rows
    # committed.
    assert calls["upsert"] == 3
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_upsert_long_label_persisted_truncated():
    """A label far longer than the column width must be written truncated,
    never raise. We capture the values bound into the insert statement."""
    model_id = uuid.uuid4()
    kpi_id = uuid.uuid4()
    long_label = "Composite evaluation failed — " + "y" * 400
    kpi_objs = {kpi_id: SimpleNamespace(name="depthy")}
    result_map = {kpi_id: _resp(long_label)}

    captured = {}
    state = {"first": True}

    @contextlib.asynccontextmanager
    async def _nested():
        yield None

    db = AsyncMock()
    db.begin_nested = MagicMock(side_effect=lambda: _nested())
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    async def _execute(stmt):
        if state["first"]:
            # F-017-29 pre-load SELECT — no existing rows.
            state["first"] = False
            return _empty_select_result()
        # pg insert statement exposes the bound parameters via .compile()
        compiled = stmt.compile()
        captured.update(compiled.params)
        return MagicMock()

    db.execute = AsyncMock(side_effect=_execute)

    await _upsert_kpi_latest_batch(db, model_id, kpi_objs, result_map)

    written = captured.get("status_label")
    assert written is not None
    assert len(written) <= _STATUS_LABEL_MAX_LEN
    assert written.startswith("Composite evaluation failed — ")
    db.commit.assert_awaited()


# ---------------------------------------------------------------------------
# F-017-29: conditional write — unchanged rows are not re-written on a render
# ---------------------------------------------------------------------------


def _existing_row(kpi_id, *, name, value, target, status, status_label,
                  trend_pct, formatted_value):
    """A SimpleNamespace standing in for a loaded KPILatest ORM row."""
    return SimpleNamespace(
        kpi_id=kpi_id, kpi_name=name, value=value, target=target,
        status=status, status_label=status_label, trend_pct=trend_pct,
        formatted_value=formatted_value,
    )


def _select_result_with(rows):
    result = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = rows
    result.scalars.return_value = scalars
    return result


def _dedupe_db(existing_rows):
    """db mock that returns *existing_rows* from the pre-load SELECT and counts
    subsequent upsert executes."""
    counts = {"upsert": 0}
    state = {"first": True}

    @contextlib.asynccontextmanager
    async def _nested():
        yield None

    db = AsyncMock()
    db.begin_nested = MagicMock(side_effect=lambda: _nested())
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    async def _execute(stmt):
        if state["first"]:
            state["first"] = False
            return _select_result_with(existing_rows)
        counts["upsert"] += 1
        return MagicMock()

    db.execute = AsyncMock(side_effect=_execute)
    return db, counts


@pytest.mark.asyncio
async def test_upsert_skips_unchanged_row():
    """A render whose materialised value is identical writes nothing and does
    not commit — the write-amplification F-017-29 removes."""
    model_id = uuid.uuid4()
    kpi_id = uuid.uuid4()
    kpi_objs = {kpi_id: SimpleNamespace(name="rev")}
    # response value matches the existing row exactly (Decimal vs float coerced).
    result_map = {kpi_id: _resp("On Track")}  # value=1.0, target=2.0, status=0
    # _resp sets status_label, trend_pct=None, formatted_value="1".
    existing = _existing_row(
        kpi_id, name="rev", value=1.0, target=2.0, status=0,
        status_label="On Track", trend_pct=None, formatted_value="1",
    )
    db, counts = _dedupe_db([existing])

    await _upsert_kpi_latest_batch(db, model_id, kpi_objs, result_map)

    assert counts["upsert"] == 0
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_upsert_writes_changed_row():
    """A render whose value moved is written and committed — the published
    $KPIs value must track the new value."""
    model_id = uuid.uuid4()
    kpi_id = uuid.uuid4()
    kpi_objs = {kpi_id: SimpleNamespace(name="rev")}
    result_map = {kpi_id: _resp("On Track")}  # value=1.0
    existing = _existing_row(
        kpi_id, name="rev", value=999.0, target=2.0, status=0,
        status_label="On Track", trend_pct=None, formatted_value="1",
    )
    db, counts = _dedupe_db([existing])

    await _upsert_kpi_latest_batch(db, model_id, kpi_objs, result_map)

    assert counts["upsert"] == 1
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_upsert_writes_new_row_with_no_existing():
    """A KPI with no kpi_latest row yet is always written."""
    model_id = uuid.uuid4()
    kpi_id = uuid.uuid4()
    kpi_objs = {kpi_id: SimpleNamespace(name="rev")}
    result_map = {kpi_id: _resp("On Track")}
    db, counts = _dedupe_db([])  # no existing rows

    await _upsert_kpi_latest_batch(db, model_id, kpi_objs, result_map)

    assert counts["upsert"] == 1
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_upsert_decimal_equals_float_is_skipped():
    """Numeric columns load as Decimal but write as float; the normalisation
    must treat Decimal('1.0') == 1.0 as unchanged."""
    from decimal import Decimal

    model_id = uuid.uuid4()
    kpi_id = uuid.uuid4()
    kpi_objs = {kpi_id: SimpleNamespace(name="rev")}
    result_map = {kpi_id: _resp("On Track")}  # value=1.0, target=2.0
    existing = _existing_row(
        kpi_id, name="rev", value=Decimal("1.0"), target=Decimal("2.0"),
        status=0, status_label="On Track", trend_pct=None, formatted_value="1",
    )
    db, counts = _dedupe_db([existing])

    await _upsert_kpi_latest_batch(db, model_id, kpi_objs, result_map)

    assert counts["upsert"] == 0
    db.commit.assert_not_awaited()
