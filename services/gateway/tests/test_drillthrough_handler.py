"""Integration tests for the XMLA DRILLTHROUGH handler.

Tests ``handle_drillthrough`` by mocking the router client calls
(``execute_drill_options`` and ``execute_drill_through``).
Verifies: auto-hierarchy selection, MAXROWS handling, column augmentation,
Rowset format, trimmed-rows warning, error cases.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from defusedxml import ElementTree as ET

import pytest

from src.dax.drillthrough_handler import (
    handle_drillthrough,
    _extract_grouping_levels,
    _extract_measure_name,
    _member_parts_to_groupings,
)
from src.dax.ts_mdx_parser import parse_mdx

_async = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_MEASURES_META = [
    {"id": "meas-001", "name": "amount", "default_agg": "sum"},
    {"id": "meas-002", "name": "Sales", "default_agg": "sum"},
]

_DIMENSIONS_META = [
    {"name": "year", "display_name": "Year"},
    {"name": "month", "display_name": "Month"},
    {"name": "region", "display_name": "Region"},
    {"name": "day", "display_name": "Day"},
]

_HIERARCHY_DEFS = [
    {
        "name": "business_date",
        "levels": [
            {"ordinal": 0, "name": "Year", "key_attribute": {"id": "col-year", "source": "physical_column"}},
            {"ordinal": 1, "name": "Month", "key_attribute": {"id": "col-month", "source": "physical_column"}},
            {"ordinal": 2, "name": "Day", "key_attribute": {"id": "col-day", "source": "physical_column"}},
        ],
    }
]

_DIMS_WITH_IDS = [
    {"name": "year", "display_name": "Year", "source_column_id": "col-year"},
    {"name": "month", "display_name": "Month", "source_column_id": "col-month"},
    {"name": "day", "display_name": "Day", "source_column_id": "col-day"},
    {"name": "region", "display_name": "Region"},
]


def _drill_options_response(hierarchies=None):
    return {"hierarchies": hierarchies or []}


def _drill_through_response(
    *,
    columns=None,
    rows=None,
    has_more=False,
    drill_mode="hierarchy",
    hierarchy_path=None,
):
    return {
        "columns": columns or ["month", "amount"],
        "rows": rows or [{"month": 1, "amount": 100}],
        "page": {"cursor": "c0", "next_cursor": "c1" if has_more else None, "has_more": has_more},
        "drill_mode": drill_mode,
        "drill_dimension": {"id": "dim-month", "name": "month", "display_name": "Month"},
        "hierarchy_path": hierarchy_path or [],
        "drillable_hierarchies": [],
        "route_type": "source",
        "execution_ms": 42,
    }


# ---------------------------------------------------------------------------
# Auto-hierarchy selection
# ---------------------------------------------------------------------------

@_async
async def test_auto_selects_single_drillable_hierarchy():
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS "
        "FROM [modely] WHERE ([Date].[Year].&[2025])"
    )
    single_hier = [{"hierarchy_id": "hier-001", "hierarchy_name": "business_date"}]
    captured = {}

    async def _fake_drill_through(measure_id, grouping_levels, jwt_token, **kwargs):
        captured["hierarchy_id"] = kwargs.get("hierarchy_id")
        return _drill_through_response()

    with (
        patch("src.dax.drillthrough_handler.execute_drill_options",
              new=AsyncMock(return_value=_drill_options_response(single_hier))),
        patch("src.dax.drillthrough_handler.execute_drill_through",
              new=_fake_drill_through),
    ):
        xml, warnings = await handle_drillthrough(
            parsed, "acme", "jwt", _MEASURES_META, _DIMS_WITH_IDS, _HIERARCHY_DEFS,
        )

    assert captured["hierarchy_id"] == "hier-001"
    assert "<row>" in xml


@_async
async def test_falls_back_to_leaf_with_no_drillable():
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS FROM [modely]"
    )
    captured = {}

    async def _fake_drill_through(measure_id, grouping_levels, jwt_token, **kwargs):
        captured["hierarchy_id"] = kwargs.get("hierarchy_id")
        return _drill_through_response(drill_mode="leaf")

    with (
        patch("src.dax.drillthrough_handler.execute_drill_options",
              new=AsyncMock(return_value=_drill_options_response([]))),
        patch("src.dax.drillthrough_handler.execute_drill_through",
              new=_fake_drill_through),
    ):
        xml, warnings = await handle_drillthrough(
            parsed, "acme", "jwt", _MEASURES_META, _DIMS_WITH_IDS, _HIERARCHY_DEFS,
        )

    assert captured["hierarchy_id"] is None


@_async
async def test_multiple_drillable_deterministic_pick_and_warning():
    # Bug-4153 (F-P4b1-03): with 2+ drillable hierarchies the handler must
    # deterministically pick one (first by sorted hierarchy_name — grain depth
    # is unavailable) and emit a warning naming the chosen hierarchy, instead of
    # silently falling back.
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS FROM [modely]"
    )
    two_hiers = [
        {"hierarchy_id": "hier-001", "hierarchy_name": "business_date"},
        {"hierarchy_id": "hier-002", "hierarchy_name": "geography"},
    ]
    captured = {}

    async def _fake_drill_through(measure_id, grouping_levels, jwt_token, **kwargs):
        captured["hierarchy_id"] = kwargs.get("hierarchy_id")
        return _drill_through_response(drill_mode="hierarchy")

    with (
        patch("src.dax.drillthrough_handler.execute_drill_options",
              new=AsyncMock(return_value=_drill_options_response(two_hiers))),
        patch("src.dax.drillthrough_handler.execute_drill_through",
              new=_fake_drill_through),
    ):
        xml, warnings = await handle_drillthrough(
            parsed, "acme", "jwt", _MEASURES_META, _DIMS_WITH_IDS, _HIERARCHY_DEFS,
        )

    # "business_date" sorts before "geography" → deterministic pick.
    assert captured["hierarchy_id"] == "hier-001"
    assert any("business_date" in w for w in warnings)
    assert any("Multiple drillable hierarchies" in w for w in warnings)


@_async
async def test_multiple_drillable_return_clause_pick():
    # Bug-4153: a RETURN clause naming a level whose hierarchy is drillable
    # overrides the alphabetical-by-name default.
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS FROM [modely] "
        "RETURN [geography].[Region]"
    )
    two_hiers = [
        {"hierarchy_id": "hier-001", "hierarchy_name": "business_date"},
        {"hierarchy_id": "hier-002", "hierarchy_name": "geography"},
    ]
    captured = {}

    async def _fake_drill_through(measure_id, grouping_levels, jwt_token, **kwargs):
        captured["hierarchy_id"] = kwargs.get("hierarchy_id")
        return _drill_through_response(drill_mode="hierarchy")

    with (
        patch("src.dax.drillthrough_handler.execute_drill_options",
              new=AsyncMock(return_value=_drill_options_response(two_hiers))),
        patch("src.dax.drillthrough_handler.execute_drill_through",
              new=_fake_drill_through),
    ):
        xml, warnings = await handle_drillthrough(
            parsed, "acme", "jwt", _MEASURES_META, _DIMS_WITH_IDS, _HIERARCHY_DEFS,
        )

    assert captured["hierarchy_id"] == "hier-002"
    assert any("geography" in w for w in warnings)


# ---------------------------------------------------------------------------
# MAXROWS
# ---------------------------------------------------------------------------

@_async
async def test_maxrows_passed_as_limit():
    parsed = parse_mdx(
        "DRILLTHROUGH MAXROWS 50 "
        "SELECT {[Measures].[amount]} ON COLUMNS FROM [modely]"
    )
    captured = {}

    async def _fake_drill_through(measure_id, grouping_levels, jwt_token, **kwargs):
        captured["limit"] = kwargs.get("limit")
        return _drill_through_response()

    with (
        patch("src.dax.drillthrough_handler.execute_drill_options",
              new=AsyncMock(return_value=_drill_options_response([]))),
        patch("src.dax.drillthrough_handler.execute_drill_through",
              new=_fake_drill_through),
    ):
        await handle_drillthrough(
            parsed, "acme", "jwt", _MEASURES_META, _DIMS_WITH_IDS, _HIERARCHY_DEFS,
        )

    assert captured["limit"] == 50


@_async
async def test_no_maxrows_passes_none_limit():
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS FROM [modely]"
    )
    captured = {}

    async def _fake_drill_through(measure_id, grouping_levels, jwt_token, **kwargs):
        captured["limit"] = kwargs.get("limit")
        return _drill_through_response()

    with (
        patch("src.dax.drillthrough_handler.execute_drill_options",
              new=AsyncMock(return_value=_drill_options_response([]))),
        patch("src.dax.drillthrough_handler.execute_drill_through",
              new=_fake_drill_through),
    ):
        await handle_drillthrough(
            parsed, "acme", "jwt", _MEASURES_META, _DIMS_WITH_IDS, _HIERARCHY_DEFS,
        )

    assert captured["limit"] is None


# ---------------------------------------------------------------------------
# Trimmed-rows warning
# ---------------------------------------------------------------------------

@_async
async def test_trimmed_rows_produces_warning():
    parsed = parse_mdx(
        "DRILLTHROUGH MAXROWS 100 "
        "SELECT {[Measures].[amount]} ON COLUMNS FROM [modely]"
    )

    with (
        patch("src.dax.drillthrough_handler.execute_drill_options",
              new=AsyncMock(return_value=_drill_options_response([]))),
        patch("src.dax.drillthrough_handler.execute_drill_through",
              new=AsyncMock(return_value=_drill_through_response(has_more=True))),
    ):
        xml, warnings = await handle_drillthrough(
            parsed, "acme", "jwt", _MEASURES_META, _DIMS_WITH_IDS, _HIERARCHY_DEFS,
        )

    assert len(warnings) == 1
    assert "truncated" in warnings[0].lower() or "100" in warnings[0]


@_async
async def test_no_warning_when_all_rows_returned():
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS FROM [modely]"
    )

    with (
        patch("src.dax.drillthrough_handler.execute_drill_options",
              new=AsyncMock(return_value=_drill_options_response([]))),
        patch("src.dax.drillthrough_handler.execute_drill_through",
              new=AsyncMock(return_value=_drill_through_response(has_more=False))),
    ):
        xml, warnings = await handle_drillthrough(
            parsed, "acme", "jwt", _MEASURES_META, _DIMS_WITH_IDS, _HIERARCHY_DEFS,
        )

    assert warnings == []


# ---------------------------------------------------------------------------
# Column augmentation with hierarchy path
# ---------------------------------------------------------------------------

@_async
async def test_columns_augmented_with_hierarchy_ancestors():
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS "
        "FROM [modely] WHERE ([Date].[Year].&[2025])"
    )
    path = [{"level_name": "Year", "dimension_name": "year", "value": 2025}]

    with (
        patch("src.dax.drillthrough_handler.execute_drill_options",
              new=AsyncMock(return_value=_drill_options_response(
                  [{"hierarchy_id": "h1", "hierarchy_name": "date"}]))),
        patch("src.dax.drillthrough_handler.execute_drill_through",
              new=AsyncMock(return_value=_drill_through_response(
                  columns=["month", "amount"],
                  rows=[{"month": 1, "amount": 100}],
                  hierarchy_path=path,
              ))),
    ):
        xml, warnings = await handle_drillthrough(
            parsed, "acme", "jwt", _MEASURES_META, _DIMS_WITH_IDS, _HIERARCHY_DEFS,
        )

    assert 'name="year"' in xml
    assert 'name="month"' in xml
    assert 'name="amount"' in xml


# ---------------------------------------------------------------------------
# Rowset format (not MDDataSet)
# ---------------------------------------------------------------------------

@_async
async def test_response_is_rowset_not_mddataset():
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS FROM [modely]"
    )

    with (
        patch("src.dax.drillthrough_handler.execute_drill_options",
              new=AsyncMock(return_value=_drill_options_response([]))),
        patch("src.dax.drillthrough_handler.execute_drill_through",
              new=AsyncMock(return_value=_drill_through_response())),
    ):
        xml, _ = await handle_drillthrough(
            parsed, "acme", "jwt", _MEASURES_META, _DIMS_WITH_IDS, _HIERARCHY_DEFS,
        )

    assert "<row>" in xml
    assert "MDDataSet" not in xml
    assert "urn:schemas-microsoft-com:xml-analysis:rowset" in xml


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

@_async
async def test_measure_not_found_raises():
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[nonexistent]} ON COLUMNS FROM [modely]"
    )

    with pytest.raises(ValueError, match="not found"):
        await handle_drillthrough(
            parsed, "acme", "jwt", _MEASURES_META, _DIMS_WITH_IDS, _HIERARCHY_DEFS,
        )


@_async
async def test_no_measure_on_columns_raises():
    parsed = parse_mdx("DRILLTHROUGH SELECT FROM [modely]")

    with pytest.raises(ValueError, match="no measure"):
        await handle_drillthrough(
            parsed, "acme", "jwt", _MEASURES_META, _DIMS_WITH_IDS, _HIERARCHY_DEFS,
        )


# ---------------------------------------------------------------------------
# Persona passthrough
# ---------------------------------------------------------------------------

@_async
async def test_persona_id_passed_to_drill_through():
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS FROM [modely]"
    )
    captured = {}

    async def _fake_drill_through(measure_id, grouping_levels, jwt_token, **kwargs):
        captured["persona_id"] = kwargs.get("persona_id")
        return _drill_through_response()

    with (
        patch("src.dax.drillthrough_handler.execute_drill_options",
              new=AsyncMock(return_value=_drill_options_response([]))),
        patch("src.dax.drillthrough_handler.execute_drill_through",
              new=_fake_drill_through),
    ):
        await handle_drillthrough(
            parsed, "acme", "jwt", _MEASURES_META, _DIMS_WITH_IDS, _HIERARCHY_DEFS,
            persona_id="persona-xyz",
        )

    assert captured["persona_id"] == "persona-xyz"


# ---------------------------------------------------------------------------
# Grouping level extraction
# ---------------------------------------------------------------------------

def test_extract_grouping_from_where():
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS "
        "FROM [modely] WHERE ([Date].[Year].&[2025])"
    )
    levels = _extract_grouping_levels(parsed, _DIMS_WITH_IDS, _HIERARCHY_DEFS)
    assert len(levels) >= 1
    year_entries = [l for l in levels if l["column"] == "year"]
    assert len(year_entries) == 1
    assert year_entries[0]["value"] == 2025


def test_extract_grouping_from_rows_axis():
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS, "
        "{[Date].[Year].&[2025]} ON ROWS FROM [modely]"
    )
    levels = _extract_grouping_levels(parsed, _DIMS_WITH_IDS, _HIERARCHY_DEFS)
    year_entries = [l for l in levels if l["column"] == "year"]
    assert len(year_entries) == 1
    assert year_entries[0]["value"] == 2025


def test_extract_grouping_from_where_multiple():
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS "
        "FROM [modely] WHERE ([Date].[Year].&[2025], [region].[region].&[US])"
    )
    levels = _extract_grouping_levels(parsed, _DIMS_WITH_IDS, _HIERARCHY_DEFS)
    columns = {l["column"] for l in levels}
    assert "year" in columns


def test_extract_grouping_unresolvable_member_fails_loud():
    """Bug-3622: a WHERE-tuple member naming a non-existent level/dimension must
    fault rather than silently drop the filter and widen the drill rows."""
    from src.dax.drillthrough_handler import DrillThroughResolutionError

    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS "
        "FROM [modely] WHERE ([Nonexistent].[Nonexistent].&[X])"
    )
    with pytest.raises(DrillThroughResolutionError):
        _extract_grouping_levels(parsed, _DIMS_WITH_IDS, _HIERARCHY_DEFS)


def test_extract_grouping_measures_ref_skipped_not_faulted():
    """Bug-3622: a ``[Measures]`` ref in the WHERE tuple is a measure context,
    not a grouping filter — it must be skipped, never faulted."""
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS "
        "FROM [modely] WHERE ([Date].[Year].&[2025], [Measures].[amount])"
    )
    levels = _extract_grouping_levels(parsed, _DIMS_WITH_IDS, _HIERARCHY_DEFS)
    columns = {l["column"] for l in levels}
    assert "year" in columns
    assert "amount" not in columns


def test_extract_grouping_where_key_with_escaped_bracket():
    """Bug-1056: a WHERE member key containing ``]`` (escaped ``]]``) must not
    truncate at the first ``]`` — the full key ``A]B`` must reach the filter."""
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS "
        "FROM [modely] WHERE ([region].[region].&[A]]B])"
    )
    levels = _extract_grouping_levels(parsed, _DIMS_WITH_IDS, _HIERARCHY_DEFS)
    region_entries = [l for l in levels if l["column"] == "region"]
    assert len(region_entries) == 1
    assert region_entries[0]["value"] == "A]B"


# ---------------------------------------------------------------------------
# _member_parts_to_groupings
# ---------------------------------------------------------------------------

def test_member_parts_simple():
    dim_names = {"year": {}, "region": {}}
    result = _member_parts_to_groupings(
        ["Date", "Year", "&2025"], dim_names, {},
    )
    assert result == [] or result[0]["value"] == 2025


def test_member_parts_direct_dim():
    dim_names = {"region": {}}
    result = _member_parts_to_groupings(
        ["region", "&US"], dim_names, {},
    )
    assert result == [{"column": "region", "value": "US"}]


# ---------------------------------------------------------------------------
# Composite key paths (B8 round-3, Bug-1049): the deepest key filters the
# named level, ancestor keys filter their own level dims. The tuple echoed
# by Excel on double-click of a subtotal-pivot cell must drill the EXACT
# clicked member, not its ancestor.
# ---------------------------------------------------------------------------

_HIER_LEVEL_TO_DIM = {
    "business_date": {"year": "year", "month": "month", "day": "day"},
}


def test_member_parts_composite_deepest_key_filters_named_level():
    dim_names = {"year": {}, "month": {}, "day": {}}
    result = _member_parts_to_groupings(
        ["Date", "business_date", "Month", "&2025", "&4"],
        dim_names, _HIER_LEVEL_TO_DIM,
    )
    assert {"column": "month", "value": 4} in result
    assert {"column": "year", "value": 2025} in result
    assert len(result) == 2
    # The named level's filter comes first (deepest key — the clicked member).
    assert result[0] == {"column": "month", "value": 4}


def test_member_parts_composite_no_level_positional():
    dim_names = {"year": {}, "month": {}, "day": {}}
    result = _member_parts_to_groupings(
        ["Date", "business_date", "&2025", "&4"],
        dim_names, _HIER_LEVEL_TO_DIM,
    )
    assert {"column": "month", "value": 4} in result
    assert {"column": "year", "value": 2025} in result
    assert len(result) == 2


def test_member_parts_composite_deeper_than_hierarchy_keeps_deepest(caplog):
    dim_names = {"year": {}, "month": {}, "day": {}}
    with caplog.at_level("WARNING"):
        result = _member_parts_to_groupings(
            ["Date", "business_date", "Year", "&X", "&2025"],
            dim_names, _HIER_LEVEL_TO_DIM,
        )
    assert result == [{"column": "year", "value": 2025}]
    assert any("deeper" in r.message for r in caplog.records)


def test_member_parts_single_key_unchanged():
    dim_names = {"year": {}, "month": {}, "day": {}}
    result = _member_parts_to_groupings(
        ["Date", "business_date", "Month", "&4"],
        dim_names, _HIER_LEVEL_TO_DIM,
    )
    assert result == [{"column": "month", "value": 4}]


def test_extract_grouping_composite_where_tuple():
    """Reviewer's mandated shape: full composite tuple in WHERE."""
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS "
        "FROM [modely] WHERE ([Date].[business_date].[Month].&[2025]&[4], "
        "[region].[region].&[US])"
    )
    levels = _extract_grouping_levels(parsed, _DIMS_WITH_IDS, _HIERARCHY_DEFS)
    assert {"column": "month", "value": 4} in levels
    assert {"column": "year", "value": 2025} in levels
    assert {"column": "region", "value": "US"} in levels
    assert len(levels) == 3


def test_extract_grouping_composite_rows_axis():
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS, "
        "{[Date].[business_date].[Month].&[2026]&[4]} ON ROWS FROM [modely]"
    )
    levels = _extract_grouping_levels(parsed, _DIMS_WITH_IDS, _HIERARCHY_DEFS)
    assert {"column": "month", "value": 4} in levels
    assert {"column": "year", "value": 2026} in levels
    assert len(levels) == 2


def test_extract_grouping_dedupes_where_and_rows():
    """The same member in WHERE and ROWS yields one filter, not two."""
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS, "
        "{[Date].[business_date].[Month].&[2025]&[4]} ON ROWS "
        "FROM [modely] WHERE ([Date].[business_date].[Month].&[2025]&[4])"
    )
    levels = _extract_grouping_levels(parsed, _DIMS_WITH_IDS, _HIERARCHY_DEFS)
    assert levels.count({"column": "month", "value": 4}) == 1
    assert levels.count({"column": "year", "value": 2025}) == 1
    assert len(levels) == 2
