"""H9 model-service: calendar_type vocabulary unification (F-016-04) and the
denormalised variant calendar snapshot write path (F-016-02)."""
from __future__ import annotations

import types
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.api.hierarchies import _validate_calendar_fields
from src.api.measures import _resolve_variant_calendar_snapshot


class TestValidateCalendarFields:
    """F-016-04: hierarchies accept the full 6-type vocabulary, and the legacy
    'iso' token is normalised (accepted, not rejected)."""

    @pytest.mark.parametrize(
        "ct",
        ["standard", "fiscal", "iso_week", "retail_445", "hijri", "thai_buddhist"],
    )
    def test_accepts_all_canonical_types(self, ct):
        # fiscal needs a start month; others must reject one.
        if ct == "fiscal":
            _validate_calendar_fields(ct, 4)
        else:
            _validate_calendar_fields(ct, None)

    def test_accepts_legacy_iso(self):
        # 'iso' normalises to 'iso_week' and must not 422.
        _validate_calendar_fields("iso", None)

    def test_rejects_unknown_type(self):
        with pytest.raises(HTTPException) as exc:
            _validate_calendar_fields("banana", None)
        assert exc.value.status_code == 422

    def test_fiscal_requires_start_month(self):
        with pytest.raises(HTTPException):
            _validate_calendar_fields("fiscal", None)

    def test_non_fiscal_rejects_start_month(self):
        with pytest.raises(HTTPException):
            _validate_calendar_fields("retail_445", 4)

    def test_unknown_type_uses_calendar_code_c1(self):
        # F-016-18: calendar_type validation carries C1, not the H3 reused
        # before (which the spec assigns to "exactly one key attribute").
        with pytest.raises(HTTPException) as exc:
            _validate_calendar_fields("banana", None)
        assert exc.value.detail["code"] == "C1"

    def test_fiscal_start_month_uses_calendar_code_c2(self):
        # F-016-18: fiscal_year_start_month validation carries C2, not H4.
        with pytest.raises(HTTPException) as exc:
            _validate_calendar_fields("fiscal", None)
        assert exc.value.detail["code"] == "C2"
        with pytest.raises(HTTPException) as exc2:
            _validate_calendar_fields("fiscal", 13)
        assert exc2.value.detail["code"] == "C2"
        with pytest.raises(HTTPException) as exc3:
            _validate_calendar_fields("standard", 4)
        assert exc3.value.detail["code"] == "C2"


def _level(attr_id, source):
    return (attr_id, source)


class TestResolveVariantCalendarSnapshot:
    """F-016-02: the producer step that resolves hierarchy -> calendar alias
    table -> CalendarTable and stores the snapshot."""

    @pytest.mark.asyncio
    async def test_none_hierarchy_returns_empty_snapshot(self):
        db = AsyncMock()
        assert await _resolve_variant_calendar_snapshot(db, "m1", None) == (None, None)
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolves_calendar_via_physical_level(self):
        # Hierarchy level keyed on a physical column whose owning ModelTable is
        # a calendar alias (calendar_table_id set).
        levels = types.SimpleNamespace(all=lambda: [_level("col-1", "physical_column")])
        db = AsyncMock()
        db.execute.side_effect = [
            levels,  # level rows
            types.SimpleNamespace(scalar_one_or_none=lambda: "datecol-1"),  # date col id
        ]
        col = types.SimpleNamespace(model_table_id="tbl-1")
        mt = types.SimpleNamespace(calendar_table_id="cal-1")
        cal = types.SimpleNamespace(date_column="date_key")
        # db.get is called for: ModelColumn(col-1), ModelTable(tbl-1), CalendarTable(cal-1)
        db.get.side_effect = [col, mt, cal]

        cal_id, date_id = await _resolve_variant_calendar_snapshot(db, "m1", "hier-1")
        assert cal_id == "cal-1"
        assert date_id == "datecol-1"

    @pytest.mark.asyncio
    async def test_resolves_via_uda_level(self):
        # Generated date hierarchy: levels keyed on UDAs (source_column_id is
        # None). The owning table comes from UserDefinedAttribute.table_id.
        levels = types.SimpleNamespace(
            all=lambda: [_level("uda-1", "user_defined_attribute")]
        )
        db = AsyncMock()
        db.execute.side_effect = [
            levels,
            types.SimpleNamespace(scalar_one_or_none=lambda: "datecol-9"),
        ]
        uda = types.SimpleNamespace(table_id="tbl-9")
        mt = types.SimpleNamespace(calendar_table_id="cal-9")
        cal = types.SimpleNamespace(date_column="date_key")
        db.get.side_effect = [uda, mt, cal]

        cal_id, date_id = await _resolve_variant_calendar_snapshot(db, "m1", "hier-9")
        assert cal_id == "cal-9"
        assert date_id == "datecol-9"

    @pytest.mark.asyncio
    async def test_expression_only_hierarchy_has_no_calendar_table(self):
        # Hierarchy built directly on a fact column (no calendar alias):
        # owning ModelTable has calendar_table_id=None -> snapshot stays empty,
        # and the query path computes period boundaries by expression.
        levels = types.SimpleNamespace(all=lambda: [_level("col-2", "physical_column")])
        db = AsyncMock()
        db.execute.side_effect = [levels]
        col = types.SimpleNamespace(model_table_id="tbl-2")
        mt = types.SimpleNamespace(calendar_table_id=None)
        db.get.side_effect = [col, mt]

        assert await _resolve_variant_calendar_snapshot(db, "m1", "hier-2") == (None, None)
