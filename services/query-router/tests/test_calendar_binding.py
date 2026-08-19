"""Phase RA-1 — calendar resolution chain in the rewriter.

Covers the small surface in ``query_rewriter`` used to drive the
period-aware ``cal`` JOIN:

  - ``_build_calendar_columns``: CalendarTable → emitter dict.
  - ``_resolve_calendar_binding``: list of period-aware measures →
    CalendarTable, via ``Measure.calendar_model_table_id →
    ModelTable.calendar_table_id → CalendarTable``.
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock

import pytest

from src.rewrite.query_rewriter import (
    SemanticBindingError,
    _build_calendar_columns,
    _resolve_calendar_binding,
)
from src.rewrite.calendar_support import (
    _calendar_type_is_expression_capable,
    _resolve_hierarchy_calendar_rules,
)


def _calendar(**overrides):
    base = dict(
        table_name="dim_calendar",
        date_column="cal_date",
        year_column="cal_year",
        half_column=None,
        quarter_column="cal_quarter",
        month_column="cal_month",
        week_column="cal_week",
        day_column=None,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _measure(calendar_model_table_id, resolved_calendar_id=None):
    return types.SimpleNamespace(
        calendar_model_table_id=calendar_model_table_id,
        resolved_calendar_id=resolved_calendar_id,
    )


def _alias_table(calendar_table_id):
    return types.SimpleNamespace(calendar_table_id=calendar_table_id)


class TestBuildCalendarColumns:
    def test_skips_null_columns(self):
        cols = _build_calendar_columns(_calendar())
        assert cols == {
            "date": "cal_date",
            "year": "cal_year",
            "quarter": "cal_quarter",
            "month": "cal_month",
            "week": "cal_week",
        }
        assert "half" not in cols
        assert "day" not in cols

    def test_includes_half_and_day_when_present(self):
        cols = _build_calendar_columns(
            _calendar(half_column="cal_half", day_column="cal_day")
        )
        assert cols["half"] == "cal_half"
        assert cols["day"] == "cal_day"

    def test_empty_when_all_null(self):
        cols = _build_calendar_columns(
            _calendar(date_column=None, year_column=None, quarter_column=None,
                      month_column=None, week_column=None)
        )
        assert cols == {}


class TestResolveCalendarBinding:
    @pytest.mark.asyncio
    async def test_returns_calendar_when_measure_pinned(self):
        calendar = _calendar()
        alias = _alias_table(calendar_table_id="cal-1")
        measures = [_measure("alias-1"), _measure("alias-1")]

        db = AsyncMock()
        db.get.side_effect = [alias, calendar]

        result = await _resolve_calendar_binding(db, measures)
        assert result is calendar
        assert db.get.call_count == 2

    @pytest.mark.asyncio
    async def test_returns_none_when_no_measure_pins_calendar(self):
        measures = [_measure(None), _measure(None)]
        db = AsyncMock()

        assert await _resolve_calendar_binding(db, measures) is None
        db.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_none_when_alias_missing(self):
        measures = [_measure("alias-1")]
        db = AsyncMock()
        db.get.side_effect = [None]

        assert await _resolve_calendar_binding(db, measures) is None

    @pytest.mark.asyncio
    async def test_returns_none_when_alias_not_calendar(self):
        measures = [_measure("alias-1")]
        alias = _alias_table(calendar_table_id=None)
        db = AsyncMock()
        db.get.side_effect = [alias]

        assert await _resolve_calendar_binding(db, measures) is None

    @pytest.mark.asyncio
    async def test_raises_when_measures_disagree_on_calendar(self):
        # Two legacy aliases that resolve to DIFFERENT calendars.
        alias_a = _alias_table(calendar_table_id="cal-A")
        alias_b = _alias_table(calendar_table_id="cal-B")
        measures = [_measure("alias-A"), _measure("alias-B")]
        db = AsyncMock()
        db.get.side_effect = [alias_a, alias_b]

        with pytest.raises(SemanticBindingError):
            await _resolve_calendar_binding(db, measures)


class TestResolvedCalendarIdPath:
    @pytest.mark.asyncio
    async def test_direct_resolution_via_resolved_calendar_id(self):
        calendar = _calendar()
        measures = [_measure(None, resolved_calendar_id="cal-direct")]
        db = AsyncMock()
        db.get.side_effect = [calendar]

        result = await _resolve_calendar_binding(db, measures)
        assert result is calendar

    @pytest.mark.asyncio
    async def test_resolved_calendar_id_takes_precedence_over_legacy(self):
        # Bug-6711: when a measure carries both resolved_calendar_id and
        # calendar_model_table_id, the legacy alias is still resolved to
        # check for calendar disagreement. If the legacy alias is stale
        # (no calendar_table_id), the new-style pin is authoritative.
        calendar = _calendar()
        alias = _alias_table(calendar_table_id=None)  # stale legacy alias
        measures = [_measure("alias-legacy", resolved_calendar_id="cal-direct")]
        db = AsyncMock()
        # Two db.get calls: (1) ModelTable lookup for legacy alias,
        # (2) CalendarTable lookup for the resolved_calendar_id.
        db.get.side_effect = [alias, calendar]

        result = await _resolve_calendar_binding(db, measures)
        assert result is calendar
        assert db.get.call_count == 2

    @pytest.mark.asyncio
    async def test_resolved_and_legacy_disagree_raises(self):
        # Bug-6711 core scenario: a measure has resolved_calendar_id="cal-A"
        # and a legacy alias that resolves to calendar_table_id="cal-B".
        # The two pins disagree, so we must raise SemanticBindingError.
        alias = _alias_table(calendar_table_id="cal-B")
        measures = [_measure("alias-legacy", resolved_calendar_id="cal-A")]
        db = AsyncMock()
        db.get.side_effect = [alias]

        with pytest.raises(SemanticBindingError):
            await _resolve_calendar_binding(db, measures)

    @pytest.mark.asyncio
    async def test_two_legacy_aliases_same_calendar_no_raise(self):
        # Two distinct ModelTable alias IDs pointing at the SAME
        # CalendarTable should NOT raise -- this is a valid product
        # scenario (two dimension aliases of one calendar).
        calendar = _calendar()
        alias1 = _alias_table(calendar_table_id="cal-shared")
        alias2 = _alias_table(calendar_table_id="cal-shared")
        measures = [
            _measure("alias-1", resolved_calendar_id=None),
            _measure("alias-2", resolved_calendar_id=None),
        ]
        db = AsyncMock()
        # db.get calls: (1) alias-1 ModelTable, (2) alias-2 ModelTable,
        # (3) CalendarTable lookup for "cal-shared"
        db.get.side_effect = [alias1, alias2, calendar]

        result = await _resolve_calendar_binding(db, measures)
        assert result is calendar

    @pytest.mark.asyncio
    async def test_two_legacy_aliases_different_calendars_raises(self):
        # Two distinct ModelTable alias IDs pointing at DIFFERENT
        # CalendarTables should raise SemanticBindingError.
        alias1 = _alias_table(calendar_table_id="cal-A")
        alias2 = _alias_table(calendar_table_id="cal-B")
        measures = [
            _measure("alias-1", resolved_calendar_id=None),
            _measure("alias-2", resolved_calendar_id=None),
        ]
        db = AsyncMock()
        db.get.side_effect = [alias1, alias2]

        with pytest.raises(SemanticBindingError):
            await _resolve_calendar_binding(db, measures)

    @pytest.mark.asyncio
    async def test_fallback_to_legacy_when_resolved_calendar_id_is_none(self):
        calendar = _calendar()
        alias = _alias_table(calendar_table_id="cal-1")
        measures = [_measure("alias-1", resolved_calendar_id=None)]
        db = AsyncMock()
        db.get.side_effect = [alias, calendar]

        result = await _resolve_calendar_binding(db, measures)
        assert result is calendar
        assert db.get.call_count == 2


def _time_dim(hierarchy_id=None, source_column_id=None):
    return types.SimpleNamespace(
        hierarchy_id=hierarchy_id,
        source_column_id=source_column_id,
        is_time_dim=True,
    )


def _result(row):
    r = types.SimpleNamespace()
    r.first = lambda: row
    return r


def _undeployed_model():
    """A model with no deploy pointer.

    ``_resolve_hierarchy_calendar_rules`` now gates on deployment authority
    (F-016-02): DEPLOYED -> pinned snapshot, UNDEPLOYED -> live rows. These
    tests exercise the LIVE (authoring) resolution, so ``db.get(Model, ...)``
    must yield an undeployed model. A bare ``AsyncMock`` would return a truthy
    mock ``deployed_version_id`` and be misclassified as deployed.
    """
    return types.SimpleNamespace(
        id="model-1", deployed_version_id=None, deploy_epoch=0
    )


class TestResolveHierarchyCalendarRulesByHierarchyId:
    """F-016-03: generated date hierarchies key levels on UDAs, so the virtual
    time dimension has source_column_id=None but carries hierarchy_id. Rules
    must resolve off the hierarchy id, not the (absent) physical column."""

    @pytest.mark.asyncio
    async def test_resolves_fiscal_via_hierarchy_id_when_no_source_column(self):
        # The production-default config: UDA-keyed generated hierarchy.
        time_dim = _time_dim(hierarchy_id="hier-1", source_column_id=None)
        db = AsyncMock()
        db.get.return_value = _undeployed_model()
        db.execute.return_value = _result(("fiscal", 4))

        cal_type, fy = await _resolve_hierarchy_calendar_rules(db, time_dim, "model-1")
        assert cal_type == "fiscal"
        assert fy == 4
        # Resolved with a single query keyed by hierarchy id.
        assert db.execute.call_count == 1

    @pytest.mark.asyncio
    async def test_normalises_legacy_iso_to_iso_week(self):
        time_dim = _time_dim(hierarchy_id="hier-iso", source_column_id=None)
        db = AsyncMock()
        db.get.return_value = _undeployed_model()
        db.execute.return_value = _result(("iso", None))

        cal_type, fy = await _resolve_hierarchy_calendar_rules(db, time_dim, "model-1")
        assert cal_type == "iso_week"

    @pytest.mark.asyncio
    async def test_standard_when_hierarchy_missing_and_no_column(self):
        time_dim = _time_dim(hierarchy_id="ghost", source_column_id=None)
        db = AsyncMock()
        db.get.return_value = _undeployed_model()
        # First query (by hierarchy id) returns no row; no source column to
        # fall back on.
        db.execute.return_value = _result(None)

        cal_type, fy = await _resolve_hierarchy_calendar_rules(db, time_dim, "model-1")
        assert cal_type == "standard"
        assert fy is None


class TestExpressionCapableHelper:
    """F-016-04 / F-016-09: the expression-vs-table decision."""

    def test_standard_fiscal_iso_thai_are_expression_capable(self):
        for t in ("standard", "fiscal", "iso_week", "thai_buddhist", None):
            assert _calendar_type_is_expression_capable(t) is True

    def test_legacy_iso_is_expression_capable(self):
        assert _calendar_type_is_expression_capable("iso") is True

    def test_retail_and_hijri_require_a_table(self):
        assert _calendar_type_is_expression_capable("retail_445") is False
        assert _calendar_type_is_expression_capable("hijri") is False
