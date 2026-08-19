"""Variant calendar decision — the measure's pinned calendar wins.

Intake 2026-07-07 (dominant facet of Bug-6682): the expression-vs-table
calendar decision in the source_sql preflight used the QUERY-GRAIN time
dimension's hierarchy calendar_type only. A retail_445/hijri variant grouped
by a Gregorian date dimension therefore took the expression path, emitted
Gregorian EXTRACT period math and never joined the retail calendar table —
silently wrong numbers.

These tests drive the real ``rewrite_for_source`` path (unpatched emitter)
and assert the emitted SQL: a variant measure whose ``resolved_calendar_id``
points at a retail_445 calendar must LEFT JOIN that calendar table and order
its ytd_prior_year window on the retail year/week columns REGARDLESS of the
grouping dimension's own hierarchy calendar type; measures without a pinned
calendar keep the expression path unchanged.
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from conftest import attach_fixture_deployed_shape

from src.ir.logical_query import LogicalQuery, BoundQuery

pytestmark = pytest.mark.unit


_RETAIL_CAL_ID = "cal-retail-1"


def _retail_calendar() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=_RETAIL_CAL_ID,
        table_name="demo_data.cal_retail_445",
        calendar_type="retail_445",
        fiscal_year_start_month=1,
        date_column="date_key",
        year_column="retail_year",
        half_column=None,
        quarter_column="retail_quarter",
        month_column="retail_period",
        week_column="retail_week",
        day_column=None,
    )


def _bound_query(
    *,
    variant_kind: str = "ytd_prior_year",
    resolved_calendar_id: str | None = _RETAIL_CAL_ID,
) -> BoundQuery:
    model = types.SimpleNamespace(
        id="m-1", slug="testmodel", display_name="Test Model",
        deployed_version_id="v1",
    )
    measure = types.SimpleNamespace(
        id="meas-1",
        name="base_amount_ytd_prior_year_retail",
        default_agg="sum",
        is_additive=True,
        measure_type="standard",
        expression=None,
        calc_agg_mode=None,
        semi_additive_behavior=None,
        variant_kind=variant_kind,
        variant_n=None,
        source_column_id="col-1",
        user_defined_attribute_id=None,
        calendar_model_table_id=None,
        resolved_calendar_id=resolved_calendar_id,
        resolved_date_col_id=None,
    )
    # The grouping dimension is a PLAIN GREGORIAN date (standard hierarchy) —
    # the repro shape: the measure's calendar must still win.
    dim = types.SimpleNamespace(
        id="dim-1",
        name="business_date",
        source_column_id="col-2",
        user_defined_attribute_id=None,
        calc_expression=None,
        dimension_kind="time",
        is_time_dim=True,
        time_grain="day",
        hierarchy_id=None,
    )
    lq = LogicalQuery(
        model_id="m-1",
        protocol="jdbc",
        raw_query=(
            "SELECT business_date, base_amount_ytd_prior_year_retail "
            "FROM testmodel GROUP BY business_date"
        ),
        requested_measures=[measure.name],
        requested_dimensions=[dim.name],
        filters=[],
        grain=[dim.name],
        order_by=[],
        limit=None,
        offset=None,
        query_fingerprint="abc123",
    )
    return BoundQuery(
        logical_query=lq,
        model=model,
        resolved_measures=[measure],
        resolved_dimensions=[dim],
        resolved_filters=[],
    )


def _db(hierarchy_calendar_type: str = "standard") -> AsyncMock:
    """DB mock: one fact table, an amount column + a DATE time column, the
    grouping dim's hierarchy resolving to *hierarchy_calendar_type*, and the
    retail CalendarTable fetchable by id."""
    _col = types.SimpleNamespace(
        id="col-1", column_name="base_amount", model_table_id="tbl-1",
        data_type="numeric",
    )
    _time_col = types.SimpleNamespace(
        id="col-2", column_name="business_date", model_table_id="tbl-1",
        data_type="date",
    )
    _table = types.SimpleNamespace(
        id="tbl-1", physical_name="demo_data.payment_transaction",
        alias="base", table_type="fact", source_id="src-1",
    )

    async def _db_execute(stmt):
        text = str(stmt)
        if "hierarchy_definitions" in text.lower():
            r = MagicMock()
            r.first.return_value = (hierarchy_calendar_type, None)
            r.all.return_value = [(hierarchy_calendar_type, None)]
            return r
        if "ModelColumn" in text or "model_column" in text.lower():
            r = MagicMock()
            r.scalars.return_value.all.return_value = [_col, _time_col]
            return r
        if "ModelTable" in text or "model_table" in text.lower():
            r = MagicMock()
            r.scalars.return_value.all.return_value = [_table]
            return r
        r = MagicMock()
        r.scalars.return_value.all.return_value = []
        r.scalar_one_or_none.return_value = None
        r.all.return_value = []
        r.first.return_value = None
        return r

    _src_col = types.SimpleNamespace(id="col-2", model_table_id="tbl-1")

    async def _db_get(entity, pk, *args, **kwargs):
        if pk == _RETAIL_CAL_ID:
            return _retail_calendar()
        return _src_col

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_db_execute)
    db.get = AsyncMock(side_effect=_db_get)
    return db


class TestMeasureCalendarWinsOverQueryGrainHierarchy:
    @pytest.mark.asyncio
    async def test_retail_variant_grouped_by_gregorian_dim_joins_retail_calendar(self):
        """The repro: retail_445 variant + standard-hierarchy grouping dim.
        The emitted SQL must join the retail calendar and order the
        ytd_prior_year window on the retail columns — never Gregorian
        EXTRACT month/day math."""
        from src.rewrite.query_rewriter import rewrite_for_source

        sql = await rewrite_for_source(await attach_fixture_deployed_shape(_bound_query(), _db("standard")), _db("standard"))

        assert 'LEFT JOIN "demo_data"."cal_retail_445" AS cal' in sql
        assert 'ON "cal"."date_key" = ' in sql
        # Retail-system composite ordering key (year key AND position from the
        # same calendar). BOTH are aggregate-wrapped (MIN) so neither forces a
        # GROUP BY entry (Bug-6682 follow-up + Codex grain-split finding).
        assert 'MIN("cal"."retail_year") * 1000' in sql
        assert 'MIN("cal"."retail_week") * 7' in sql
        # The defective Gregorian shape must be gone.
        assert "EXTRACT(MONTH FROM" not in sql
        assert "EXTRACT(YEAR FROM" not in sql
        # Frame constants unchanged.
        assert "RANGE BETWEEN 1403 PRECEDING AND 1000 PRECEDING" in sql
        # No calendar column may enter GROUP BY, or a coarse-grain query
        # grouped by a mismatched dimension would split at every retail
        # period/week/year boundary. Only the fact grouping column is grouped.
        _group_by = sql.split("GROUP BY", 1)[1]
        assert '"cal"."retail_year"' not in _group_by
        assert '"cal"."retail_week"' not in _group_by
        assert '"business_date"' in _group_by

    @pytest.mark.asyncio
    async def test_retail_variant_on_retail_hierarchy_dim_still_joins(self):
        """Pre-existing behavior preserved: when the grouping dim's own
        hierarchy already says retail_445, the join is (still) emitted."""
        from src.rewrite.query_rewriter import rewrite_for_source

        sql = await rewrite_for_source(await attach_fixture_deployed_shape(_bound_query(), _db("retail_445")), _db("retail_445"))
        assert 'LEFT JOIN "demo_data"."cal_retail_445" AS cal' in sql
        assert 'MIN("cal"."retail_year") * 1000' in sql

    @pytest.mark.asyncio
    async def test_unpinned_variant_keeps_expression_path(self):
        """Regression guard: a variant with NO pinned calendar on a standard
        hierarchy must keep the expression path — no calendar join forced."""
        from src.rewrite.query_rewriter import rewrite_for_source

        sql = await rewrite_for_source(
            await attach_fixture_deployed_shape(_bound_query(resolved_calendar_id=None), _db("standard")), _db("standard")
        )
        assert "LEFT JOIN" not in sql
        assert "EXTRACT(YEAR FROM" in sql
        assert "RANGE BETWEEN 1403 PRECEDING AND 1000 PRECEDING" in sql


class TestCalendarJoinTypeCoercion:
    """Codex finding: the retail-calendar JOIN must coerce using the type of
    the column the variant date anchor RESOLVED to (resolved_date_col_id),
    not the SELECTED grain time dimension. When the anchor column is a
    TIMESTAMP, the DATE calendar key must be joined with a CAST(... AS DATE)
    on the timestamp side."""

    @pytest.mark.asyncio
    async def test_timestamp_anchor_is_cast_to_date_in_join(self):
        from src.rewrite.query_rewriter import rewrite_for_source

        # Fact table with a TIMESTAMP settlement column; the variant pins its
        # date anchor to that timestamp column via resolved_date_col_id, while
        # the grain dim (business_date) is a plain DATE.
        _amount = types.SimpleNamespace(
            id="col-1", column_name="base_amount", model_table_id="tbl-1",
            data_type="numeric",
        )
        _date_dim_col = types.SimpleNamespace(
            id="col-2", column_name="business_date", model_table_id="tbl-1",
            data_type="date",
        )
        _ts_anchor = types.SimpleNamespace(
            id="col-ts", column_name="settlement_ts", model_table_id="tbl-1",
            data_type="timestamp without time zone",
        )
        _table = types.SimpleNamespace(
            id="tbl-1", physical_name="demo_data.payment_transaction",
            alias="base", table_type="fact", source_id="src-1",
        )

        async def _db_execute(stmt):
            text = str(stmt)
            if "hierarchy_definitions" in text.lower():
                r = MagicMock()
                r.first.return_value = ("standard", None)
                r.all.return_value = [("standard", None)]
                return r
            if "ModelColumn" in text or "model_column" in text.lower():
                r = MagicMock()
                r.scalars.return_value.all.return_value = [
                    _amount, _date_dim_col, _ts_anchor]
                return r
            if "ModelTable" in text or "model_table" in text.lower():
                r = MagicMock()
                r.scalars.return_value.all.return_value = [_table]
                return r
            r = MagicMock()
            r.scalars.return_value.all.return_value = []
            r.scalar_one_or_none.return_value = None
            r.all.return_value = []
            r.first.return_value = None
            return r

        async def _db_get(entity, pk, *args, **kwargs):
            if pk == _RETAIL_CAL_ID:
                return _retail_calendar()
            return _date_dim_col

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_db_execute)
        db.get = AsyncMock(side_effect=_db_get)

        bound = _bound_query()
        bound.resolved_measures[0].resolved_date_col_id = "col-ts"

        await attach_fixture_deployed_shape(bound, db)
        sql = await rewrite_for_source(bound, db)
        # The timestamp anchor side of the DATE-calendar JOIN must be CAST.
        assert 'LEFT JOIN "demo_data"."cal_retail_445" AS cal' in sql
        assert "CAST(" in sql.split("LEFT JOIN", 1)[1]
        assert "AS DATE)" in sql.split("LEFT JOIN", 1)[1]
        assert "settlement_ts" in sql


def _mixed_bound_query() -> BoundQuery:
    """Two period-aware variants: one pinned to the retail calendar, one NOT
    pinned (would use its hierarchy's expression calendar)."""
    model = types.SimpleNamespace(
        id="m-1", slug="testmodel", display_name="Test Model",
        deployed_version_id="v1",
    )
    pinned = types.SimpleNamespace(
        id="meas-1", name="retail_ytd_py", default_agg="sum", is_additive=True,
        measure_type="standard", expression=None, calc_agg_mode=None,
        semi_additive_behavior=None, variant_kind="ytd_prior_year",
        variant_n=None, source_column_id="col-1",
        user_defined_attribute_id=None, calendar_model_table_id=None,
        resolved_calendar_id=_RETAIL_CAL_ID, resolved_date_col_id=None,
    )
    unpinned = types.SimpleNamespace(
        id="meas-2", name="std_ytd_py", default_agg="sum", is_additive=True,
        measure_type="standard", expression=None, calc_agg_mode=None,
        semi_additive_behavior=None, variant_kind="ytd_prior_year",
        variant_n=None, source_column_id="col-1",
        user_defined_attribute_id=None, calendar_model_table_id=None,
        resolved_calendar_id=None, resolved_date_col_id=None,
    )
    dim = types.SimpleNamespace(
        id="dim-1", name="business_date", source_column_id="col-2",
        user_defined_attribute_id=None, calc_expression=None,
        dimension_kind="time", is_time_dim=True, time_grain="day",
        hierarchy_id=None,
    )
    lq = LogicalQuery(
        model_id="m-1", protocol="jdbc",
        raw_query="SELECT business_date, retail_ytd_py, std_ytd_py "
                  "FROM testmodel GROUP BY business_date",
        requested_measures=["retail_ytd_py", "std_ytd_py"],
        requested_dimensions=["business_date"], filters=[],
        grain=["business_date"], order_by=[], limit=None, offset=None,
        query_fingerprint="mixcal",
    )
    return BoundQuery(
        logical_query=lq, model=model,
        resolved_measures=[pinned, unpinned], resolved_dimensions=[dim],
        resolved_filters=[],
    )


class TestMixedCalendarRejected:
    """Codex finding: a single query-scoped calendar cannot serve one
    TABLE-BOUND-pinned variant AND one unpinned expression-calendar variant —
    the unpinned one would be silently computed against the pinned table
    calendar. Fail loud instead of returning wrong numbers. The guard is gated
    on the pinned calendar being TABLE-BOUND (retail_445/hijri): an
    expression-capable pinned calendar (e.g. standard) computes identically to
    an unpinned standard measure, so mixing those must NOT be rejected (Codex
    over-broad-guard finding)."""

    @pytest.mark.asyncio
    async def test_table_bound_pinned_plus_unpinned_variant_raises(self):
        from shared.semantic.time_variants_sql import VariantSqlError
        from src.rewrite.query_rewriter import rewrite_for_source

        with pytest.raises(VariantSqlError):
            await rewrite_for_source(await attach_fixture_deployed_shape(_mixed_bound_query(), _db("standard")), _db("standard"))

    @pytest.mark.asyncio
    async def test_standard_pinned_plus_unpinned_variant_allowed(self):
        """Regression guard: a variant pinned to an expression-capable
        (standard) calendar mixed with an unpinned standard variant must NOT
        be rejected — both compute identical Gregorian expression math. It
        stays on the expression path (no calendar JOIN)."""
        from src.rewrite.query_rewriter import rewrite_for_source

        bound = _mixed_bound_query()
        # Re-point the "pinned" measure at a STANDARD (expression-capable)
        # calendar instead of retail.
        bound.resolved_measures[0].resolved_calendar_id = "cal-standard-1"

        _standard_cal = types.SimpleNamespace(
            id="cal-standard-1", table_name="demo_data.cal_standard",
            calendar_type="standard", fiscal_year_start_month=1,
            date_column="date_key", year_column="year_no", half_column=None,
            quarter_column="quarter_no", month_column="month_no",
            week_column="week_no", day_column="day_no",
        )
        _amount = types.SimpleNamespace(
            id="col-1", column_name="base_amount", model_table_id="tbl-1",
            data_type="numeric",
        )
        _time_col = types.SimpleNamespace(
            id="col-2", column_name="business_date", model_table_id="tbl-1",
            data_type="date",
        )
        _table = types.SimpleNamespace(
            id="tbl-1", physical_name="demo_data.payment_transaction",
            alias="base", table_type="fact", source_id="src-1",
        )

        async def _db_execute(stmt):
            text = str(stmt)
            if "hierarchy_definitions" in text.lower():
                r = MagicMock()
                r.first.return_value = ("standard", None)
                r.all.return_value = [("standard", None)]
                return r
            if "ModelColumn" in text or "model_column" in text.lower():
                r = MagicMock()
                r.scalars.return_value.all.return_value = [_amount, _time_col]
                return r
            if "ModelTable" in text or "model_table" in text.lower():
                r = MagicMock()
                r.scalars.return_value.all.return_value = [_table]
                return r
            r = MagicMock()
            r.scalars.return_value.all.return_value = []
            r.scalar_one_or_none.return_value = None
            r.all.return_value = []
            r.first.return_value = None
            return r

        async def _db_get(entity, pk, *args, **kwargs):
            if pk == "cal-standard-1":
                return _standard_cal
            return types.SimpleNamespace(id="col-2", model_table_id="tbl-1")

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_db_execute)
        db.get = AsyncMock(side_effect=_db_get)

        # Must NOT raise, and must stay on the expression path (no JOIN).
        await attach_fixture_deployed_shape(bound, db)
        sql = await rewrite_for_source(bound, db)
        assert "LEFT JOIN" not in sql
        assert "RANGE BETWEEN 1403 PRECEDING AND 1000 PRECEDING" in sql


class TestCalendarBindingResolver:
    @pytest.mark.asyncio
    async def test_mixed_resolved_and_legacy_same_calendar_allowed(self):
        from src.rewrite.calendar_support import _resolve_calendar_binding

        resolved = types.SimpleNamespace(
            id="m-resolved",
            resolved_calendar_id=_RETAIL_CAL_ID,
            calendar_model_table_id=None,
        )
        legacy = types.SimpleNamespace(
            id="m-legacy",
            resolved_calendar_id=None,
            calendar_model_table_id="alias-retail",
        )
        alias_table = types.SimpleNamespace(
            id="alias-retail",
            calendar_table_id=_RETAIL_CAL_ID,
        )
        calendar = _retail_calendar()

        async def _db_get(_entity, pk, *args, **kwargs):
            if pk == "alias-retail":
                return alias_table
            if pk == _RETAIL_CAL_ID:
                return calendar
            return None

        db = AsyncMock()
        db.get = AsyncMock(side_effect=_db_get)

        assert await _resolve_calendar_binding(db, [resolved, legacy]) is calendar

    @pytest.mark.asyncio
    async def test_mixed_resolved_and_legacy_different_calendars_rejected(self):
        from src.ir.logical_query import SemanticBindingError
        from src.rewrite.calendar_support import _resolve_calendar_binding

        resolved = types.SimpleNamespace(
            id="m-retail",
            resolved_calendar_id=_RETAIL_CAL_ID,
            calendar_model_table_id=None,
        )
        legacy = types.SimpleNamespace(
            id="m-hijri",
            resolved_calendar_id=None,
            calendar_model_table_id="alias-hijri",
        )
        alias_table = types.SimpleNamespace(
            id="alias-hijri",
            calendar_table_id="cal-hijri-1",
        )

        async def _db_get(_entity, pk, *args, **kwargs):
            if pk == "alias-hijri":
                return alias_table
            return None

        db = AsyncMock()
        db.get = AsyncMock(side_effect=_db_get)

        with pytest.raises(SemanticBindingError, match="more than one calendar"):
            await _resolve_calendar_binding(db, [resolved, legacy])
