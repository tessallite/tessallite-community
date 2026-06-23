"""Phase 2 — eligible_variant_kinds surfacing on MeasureResponse.

Two layers under test:
  * ``admissible_variant_kinds`` in ``shared.schemas.measure_formats`` — pure
    function mirroring the migration-side admissibility rule.
  * ``_eligible_variant_kinds`` in ``src.api.measures`` — DB plumbing that
    feeds the pure function from ``measure.hierarchy_id`` +
    ``HierarchyLevel`` + ``HierarchyDefinition.calendar_type``.

The catalog contract the frontend relies on is that the field is
populated only for non-variant base measures with an associated hierarchy,
and that variant rows always come back with ``None``.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.db.models import HierarchyDefinition, HierarchyLevel
from shared.schemas.measure_formats import admissible_variant_kinds
from src.api.measures import _eligible_variant_kinds

pytestmark = pytest.mark.unit


def test_admissible_rejects_variants_whose_family_is_missing():
    kinds = admissible_variant_kinds(
        level_units={"year"},
        level_calcs={"period_to_date"},
        calendar_bound=True,
    )
    assert "ytd" in kinds
    assert "prior_year" not in kinds
    assert "yoy_growth" not in kinds


def test_admissible_rejects_variants_whose_unit_is_missing():
    kinds = admissible_variant_kinds(
        level_units={"month"},
        level_calcs={"parallel_period"},
        calendar_bound=True,
    )
    assert "prior_month" in kinds
    assert "prior_year" not in kinds


def test_admissible_rejects_calendar_bound_variants_when_no_calendar():
    kinds = admissible_variant_kinds(
        level_units={"year", "day"},
        level_calcs={"period_to_date", "lag", "moving_window"},
        calendar_bound=False,
    )
    assert "ytd" not in kinds
    assert "lag" in kinds
    assert "trailing_n" in kinds
    assert "moving_avg_n" in kinds


# ---------------------------------------------------------------------------
# _eligible_variant_kinds — DB plumbing
# ---------------------------------------------------------------------------


def _fake_measure(*, variant_kind: str | None = None, hierarchy_id=None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=uuid.uuid4(),
        variant_kind=variant_kind,
        hierarchy_id=hierarchy_id,
    )


def _db_for(
    *,
    levels: list,
    calendar_type: str | None = "standard",
) -> AsyncMock:
    """Mock DB whose execute() dispatches on the selected entity."""
    db = AsyncMock()

    async def _execute(stmt):
        desc = getattr(stmt, "column_descriptions", None) or []
        entities = [d.get("entity") for d in desc]
        result = MagicMock()
        if HierarchyDefinition in entities:
            result.scalars.return_value.all.return_value = (
                [calendar_type] if calendar_type is not None else [None]
            )
            return result
        if HierarchyLevel in entities:
            result.all.return_value = list(levels)
            return result
        result.scalars.return_value.all.return_value = []
        result.all.return_value = []
        result.first.return_value = None
        return result

    db.execute = AsyncMock(side_effect=_execute)
    return db


async def test_variant_row_gets_none():
    m = _fake_measure(variant_kind="ytd")
    db = _db_for(levels=[])
    assert await _eligible_variant_kinds(db, m) is None


async def test_unlinked_base_gets_none():
    m = _fake_measure(hierarchy_id=None)
    db = _db_for(levels=[])
    assert await _eligible_variant_kinds(db, m) is None


async def test_base_with_hierarchy_and_calendar_returns_admissible_list():
    m = _fake_measure(hierarchy_id=uuid.uuid4())
    levels = [
        ("year", ["period_to_date", "parallel_period"]),
        ("month", ["period_to_date", "parallel_period"]),
    ]
    db = _db_for(levels=levels)
    kinds = await _eligible_variant_kinds(db, m)
    assert kinds is not None
    assert "ytd" in kinds
    assert "mtd" in kinds
    assert "prior_year" in kinds
    assert "prior_month" in kinds


async def test_base_without_calendar_includes_period_aware_variants():
    """Period-aware variants no longer require a calendar table;
    expression-based period boundaries are derived from the hierarchy."""
    m = _fake_measure(hierarchy_id=uuid.uuid4())
    levels = [("year", ["period_to_date", "lag"])]
    db = _db_for(levels=levels)
    kinds = await _eligible_variant_kinds(db, m)
    assert "lag" in kinds
    assert "ytd" in kinds


async def test_hierarchy_without_calendar_type_blocks_period_aware():
    """A hierarchy with calendar_type=NULL should not grant period-aware
    variant eligibility."""
    m = _fake_measure(hierarchy_id=uuid.uuid4())
    levels = [
        ("year", ["period_to_date", "parallel_period"]),
        ("month", ["period_to_date", "parallel_period"]),
    ]
    db = _db_for(levels=levels, calendar_type=None)
    kinds = await _eligible_variant_kinds(db, m)
    assert kinds is not None
    assert "lag" not in kinds
    assert "ytd" not in kinds
    assert "prior_year" not in kinds
