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
    DrillThroughResolutionError,
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
        xml, warnings, _next = await handle_drillthrough(
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
        xml, warnings, _next = await handle_drillthrough(
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
        xml, warnings, _next = await handle_drillthrough(
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
        # Wave C #4: RETURN is now projected, so the curated result must contain
        # the RETURN'd column (region) for the projection to succeed. A real
        # geography drill returns the hierarchy path columns.
        return _drill_through_response(
            columns=["region", "amount"],
            rows=[{"region": "EMEA", "amount": 100}],
            drill_mode="hierarchy",
        )

    with (
        patch("src.dax.drillthrough_handler.execute_drill_options",
              new=AsyncMock(return_value=_drill_options_response(two_hiers))),
        patch("src.dax.drillthrough_handler.execute_drill_through",
              new=_fake_drill_through),
    ):
        xml, warnings, _next = await handle_drillthrough(
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
        xml, warnings, _next = await handle_drillthrough(
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
        xml, warnings, _next = await handle_drillthrough(
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
        xml, warnings, _next = await handle_drillthrough(
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
        xml, _w, _next = await handle_drillthrough(
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
    # Bug-6662: lossless coercion preserves clean integers (BigQuery needs them).
    assert levels.count({"column": "month", "value": 4}) == 1
    assert levels.count({"column": "year", "value": 2025}) == 1
    assert len(levels) == 2


# ---------------------------------------------------------------------------
# Bug-6662: _parse_value must not corrupt zero-padded / typed member keys
# ---------------------------------------------------------------------------


def test_parse_value_preserves_zero_padded_key_bug6662():
    """Bug-6662: '007' must stay '007', not become 7 (which would filter
    on the wrong value and return empty/wrong drill-through rows)."""
    from src.dax.drillthrough_handler import _parse_value

    assert _parse_value("007") == "007"
    assert _parse_value("0042") == "0042"
    assert _parse_value("00") == "00"


def test_parse_value_preserves_scientific_notation_key_bug6662():
    """Bug-6662: '1e5' must stay '1e5', not become 100000.0."""
    from src.dax.drillthrough_handler import _parse_value

    assert _parse_value("1e5") == "1e5"
    assert _parse_value("2E10") == "2E10"


def test_parse_value_coerces_clean_integers_losslessly():
    """Bug-6662: clean integer strings still coerce (BigQuery needs numeric
    literals for INT64 columns). The lossless round-trip (str(int(x)) == x)
    ensures no leading zeros are dropped."""
    from src.dax.drillthrough_handler import _parse_value

    assert _parse_value("2025") == 2025
    assert _parse_value("4") == 4
    assert _parse_value("-1") == -1
    assert _parse_value("0") == 0


# ---------------------------------------------------------------------------
# Bug-8047 (XMLA surface) — the grand-total (All) coordinate must drop the
# filter, not filter ON the literal string "(All)".
#
# Registry Bug-8047 was raised against the WEB PIVOT (already fixed there:
# PivotGrid total-cell drill coordinates + drill.test.ts REST contract). The
# XMLA/Excel path had the same class of defect and no coverage at all: an
# (All) member arriving in the DRILLTHROUGH slicer became an ordinary caption
# member, producing a filter that matches no row.
#
# Test escape: every existing grouping-level test uses key-form members
# (``&[2025]``); no test ever sent a caption-form (All) member, so the
# caption branch was never exercised with the one token that means "no
# filter". Guard: the cases below. Tier: T1.
# ---------------------------------------------------------------------------

_GEO_DIMS = [
    {"name": "region", "display_name": "Region", "source_column_id": "col-region"},
    {"name": "city", "display_name": "City", "source_column_id": "col-city"},
]

_GEO_HIERARCHY = [
    {
        "name": "geo",
        "levels": [
            {"ordinal": 0, "name": "Region",
             "key_attribute": {"id": "col-region", "source": "physical_column"}},
            {"ordinal": 1, "name": "City",
             "key_attribute": {"id": "col-city", "source": "physical_column"}},
        ],
    }
]


@pytest.mark.parametrize(
    "member",
    [
        "[geo].[geo].[(All)]",   # SSAS LEVEL_UNIQUE_NAME form
        "[geo].[geo].[All]",     # SSAS MEMBER_UNIQUE_NAME form
        "[geo].[geo].[(all)]",   # client-lower-cased (Bug-5519 r2 evidence)
        "[geo].[geo].[ALL]",
        "[geo].[(All)]",         # two-part form Excel also emits
        "[geo].[All]",
    ],
)
def test_all_member_slicer_produces_no_filter_bug8047(member):
    """A grand-total coordinate must contribute ZERO grouping levels.

    Before the fix this produced ``{"column": "geo"/"region", "value":
    "(All)"}`` (a filter matching no row) or raised
    DrillThroughResolutionError, so the grand-total cell was undrillable.
    """
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS "
        f"FROM [modely] WHERE ({member})"
    )
    assert _extract_grouping_levels(parsed, _GEO_DIMS, _GEO_HIERARCHY) == []


def test_all_member_does_not_suppress_sibling_filters_bug8047():
    """Only the (All) dimension loses its filter; every other member in the
    same slicer tuple keeps its exact value.

    Both members are caption-form on purpose so both travel the same
    ``where_members`` branch the (All) skip lives on — a mixed key/caption
    tuple never reaches it, because the key-form regex short-circuits the
    fallback.
    """
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS FROM [modely] "
        "WHERE ([geo].[geo].[(All)], [geo].[City].[Berlin])"
    )
    levels = _extract_grouping_levels(parsed, _GEO_DIMS, _GEO_HIERARCHY)
    assert levels == [{"column": "city", "value": "Berlin"}]


def test_key_form_all_member_keeps_its_filter_bug8047():
    """A DATA member whose key is literally "All" must NOT be treated as the
    grand total.

    ``&[All]`` is the key grammar — it addresses one real row value. Dropping
    its filter would silently widen the drill and return rows the user never
    asked for, the exact failure Bug-3622 fails loud about. This is the guard
    against fixing the grand total by over-skipping.
    """
    levels = _member_parts_to_groupings(
        ["geo", "geo", "City", "&All"],
        {d["name"]: d for d in _GEO_DIMS},
        {"geo": {"region": "region", "city": "city"}},
    )
    assert levels == [{"column": "city", "value": "All"}]


def test_caption_member_named_like_all_is_still_distinguished_bug8047():
    """A caption member that merely CONTAINS "all" is untouched."""
    levels = _member_parts_to_groupings(
        ["geo", "City", "Alladale"],
        {d["name"]: d for d in _GEO_DIMS},
        {"geo": {"region": "region", "city": "city"}},
    )
    assert levels == [{"column": "city", "value": "Alladale"}]


# ---------------------------------------------------------------------------
# sol T3 review F-CR-01 / F-CR-02 — the (All) skip must not silently WIDEN a
# drill, and a mixed key/caption slicer must not silently DROP a filter.
#
# Both failure modes return more rows than the user selected, which is the one
# outcome a drill-through may never produce.
#
# Test escape (F-CR-01): the only negative case was ``Alladale`` — a caption
# that merely contains "all" — so an EXACT caption ``All`` at an explicit level
# was never exercised. Guard: the exact-caption cases below. Tier: T1.
# Test escape (F-CR-02): every mixed-grammar slicer test used two members of
# the SAME grammar, so the all-or-nothing fallback in ``_extract_grouping_levels``
# was never crossed. Guard: the mixed-slicer cases below. Tier: T1.
# ---------------------------------------------------------------------------

_COMBINED_DIMS = _GEO_DIMS + [
    {"name": "year", "display_name": "Year", "source_column_id": "col-year"},
]

_COMBINED_HIERARCHIES = _GEO_HIERARCHY + [
    {
        "name": "business_date",
        "levels": [
            {"ordinal": 0, "name": "Year",
             "key_attribute": {"id": "col-year", "source": "physical_column"}},
        ],
    },
]


def test_exact_caption_all_at_explicit_level_keeps_filter_review_f_cr_01():
    """``[geo].[geo].[City].[All]`` is a DATA member whose city caption is the
    literal string "All", not the grand-total coordinate.

    The reference names an explicit level (``City``), which the synthetic All
    member never does — it sits above every level. Dropping this filter would
    hand the query-router an unfiltered city coordinate and return every city.
    """
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS "
        "FROM [modely] WHERE ([geo].[geo].[City].[All])"
    )
    assert _extract_grouping_levels(parsed, _GEO_DIMS, _GEO_HIERARCHY) == [
        {"column": "city", "value": "All"}
    ]


@pytest.mark.parametrize(
    "member,expected",
    [
        # Explicit level segment -> the trailing token is a data caption.
        ("[geo].[City].[All]", [{"column": "city", "value": "All"}]),
        ("[geo].[geo].[City].[(All)]", [{"column": "city", "value": "(All)"}]),
        # Unbalanced / doubled parentheses are ordinary captions, not totals:
        # ``strip("()")`` used to classify all three as the grand total.
        ("[geo].[geo].[All)]", [{"column": "geo", "value": "All)"}]),
        ("[geo].[geo].[(All]", [{"column": "geo", "value": "(All"}]),
        ("[geo].[geo].[((All))]", [{"column": "geo", "value": "((All))"}]),
    ],
)
def test_all_lookalike_captions_keep_their_filter_review_f_cr_01(member, expected):
    """Only an exact ``All``/``(All)`` token with no resolvable level segment is
    the grand total; every look-alike stays a data filter."""
    dims = _GEO_DIMS + [
        {"name": "geo", "display_name": "Geo", "source_column_id": "col-geo"},
    ]
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS "
        f"FROM [modely] WHERE ({member})"
    )
    assert _extract_grouping_levels(parsed, dims, _GEO_HIERARCHY) == expected


def test_mixed_key_and_caption_slicer_keeps_both_filters_review_f_cr_02():
    """A slicer mixing key-form and caption-form members must keep BOTH.

    The key-form regex used to short-circuit the caption branch entirely, so
    ``city = Berlin`` was silently dropped and the drill returned every city
    in 2025.
    """
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS FROM [modely] "
        "WHERE ([geo].[geo].[City].[Berlin], "
        "[Date].[business_date].[Year].&[2025])"
    )
    assert _extract_grouping_levels(
        parsed, _COMBINED_DIMS, _COMBINED_HIERARCHIES
    ) == [
        {"column": "year", "value": 2025},
        {"column": "city", "value": "Berlin"},
    ]


def test_mixed_slicer_with_all_member_keeps_the_key_filter_review_f_cr_02():
    """The (All) member still drops its own filter inside a mixed slicer, and
    the key-form sibling is untouched."""
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS FROM [modely] "
        "WHERE ([geo].[geo].[(All)], [Date].[business_date].[Year].&[2025])"
    )
    assert _extract_grouping_levels(
        parsed, _COMBINED_DIMS, _COMBINED_HIERARCHIES
    ) == [{"column": "year", "value": 2025}]


# ---------------------------------------------------------------------------
# Bug-8047 REOPENED (sol recheck) — the grand-total drill on a PLAIN dimension.
#
# A plain Tessallite dimension is one column exposed as its own attribute
# hierarchy, so its dimension name and hierarchy name always COINCIDE
# (cube_model._levels_are_multi -> ORIGIN_ATTRIBUTE; mdschema advertises
# HIERARCHY_UNIQUE_NAME = [dname].[dname]; mdx_execute emits the All member as
# [dname].[dname].[All]). This is the DEFAULT model shape, not an edge case.
#
# ``hierarchy_defs`` carries USER-DEFINED hierarchies only, so a plain
# dimension contributes nothing to the level map. The narrowed structural skip
# therefore resolved [dname].[dname].[All] to the column ``dname`` through the
# plain-dimension name fallback, the skip did not fire, and Excel's grand-total
# double-click produced ``WHERE dname = 'All'`` -> zero rows: Bug-8047 verbatim.
#
# Test escape: every Bug-8047 fixture was _GEO_DIMS (region/city under the
# hierarchy ``geo``), so NO fixture had a dimension sharing its hierarchy's
# name and the dominant model shape was structurally invisible to the suite.
# Guard: _PLAIN_DIMS below, covering both All spellings. Tier: T1.
# ---------------------------------------------------------------------------

_PLAIN_DIMS = [
    {"name": "country_code", "display_name": "Country Code",
     "source_column_id": "col-country"},
]

# A plain dimension has no user-defined hierarchy — this is what
# get_model_hierarchies() returns for such a model.
_PLAIN_HIERARCHIES: list[dict] = []


@pytest.mark.parametrize(
    "member",
    [
        "[country_code].[country_code].[All]",     # MEMBER_UNIQUE_NAME form
        "[country_code].[country_code].[(All)]",   # LEVEL_UNIQUE_NAME form
        "[country_code].[country_code].[all]",     # client-lower-cased
        "[country_code].[country_code].[(ALL)]",
        "[country_code].[All]",                    # two-part form
        "[country_code].[(All)]",
    ],
)
def test_plain_dimension_grand_total_produces_no_filter_bug8047_reopened(member):
    """The grand-total coordinate of a PLAIN dimension contributes no filter.

    Regression guard for the reopened Bug-8047: this returned
    ``[{"column": "country_code", "value": "All"}]`` — a filter matching no
    row — because the dimension name and the hierarchy name coincide and the
    plain-dimension fallback resolved the reference.
    """
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS "
        f"FROM [modelx] WHERE ({member})"
    )
    assert _extract_grouping_levels(
        parsed, _PLAIN_DIMS, _PLAIN_HIERARCHIES
    ) == []


def test_plain_dimension_grand_total_keeps_sibling_filters_bug8047_reopened():
    """Only the plain dimension's grand-total coordinate loses its filter."""
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS FROM [modelx] "
        "WHERE ([country_code].[country_code].[All], "
        "[geo].[geo].[City].[Berlin])"
    )
    assert _extract_grouping_levels(
        parsed, _PLAIN_DIMS + _GEO_DIMS, _GEO_HIERARCHY
    ) == [{"column": "city", "value": "Berlin"}]


def test_plain_dimension_data_member_still_filters_bug8047_reopened():
    """A plain dimension's ORDINARY caption member keeps its exact filter.

    The skip must key on the All token plus the absent level segment, never on
    the plain-dimension shape alone.
    """
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS "
        "FROM [modelx] WHERE ([country_code].[country_code].[DE])"
    )
    assert _extract_grouping_levels(
        parsed, _PLAIN_DIMS, _PLAIN_HIERARCHIES
    ) == [{"column": "country_code", "value": "DE"}]


def test_plain_dimension_key_form_all_keeps_its_filter_bug8047_reopened():
    """``[country_code].[country_code].&[All]`` is a DATA member keyed "All".

    The key grammar never enters the guard, so a country whose code is
    literally "All" still filters exactly.
    """
    levels = _member_parts_to_groupings(
        ["country_code", "country_code", "&All"],
        {d["name"]: d for d in _PLAIN_DIMS},
        {},
    )
    assert levels == [{"column": "country_code", "value": "All"}]


def test_explicit_level_caption_all_survives_a_broken_level_map_review():
    """An explicit LEVEL segment keeps its filter even when the level map is
    empty.

    ``_build_hier_level_to_dim`` silently drops a hierarchy whose level
    key_attributes do not match any dimension. If the skip depended solely on
    the level map resolving, that drop would turn ``[geo].[geo].[City].[All]``
    back into an unfiltered drill over every city. The segment COUNT is the
    primary signal, so the filter survives.
    """
    levels = _member_parts_to_groupings(
        ["geo", "geo", "City", "All"],
        {d["name"]: d for d in _GEO_DIMS},
        {},  # level map lost
    )
    assert levels == [{"column": "city", "value": "All"}]


def test_level_named_like_its_hierarchy_keeps_its_filter_review():
    """A DECLARED level whose name equals its hierarchy's still names a level.

    ``[geo].[geo].[All]`` is the hierarchy's grand total on an ordinary model,
    but if the model declares a level literally called ``geo`` the same two
    segments are ``[Hier].[Level]`` and the trailing ``All`` is a data caption.
    The level map is consulted BEFORE the identical-segments rule so the filter
    is kept — the non-widening direction.
    """
    levels = _member_parts_to_groupings(
        ["geo", "geo", "All"],
        {d["name"]: d for d in _GEO_DIMS},
        {"geo": {"geo": "region", "city": "city"}},
    )
    assert levels == [{"column": "region", "value": "All"}]


def test_unresolvable_level_with_all_caption_fails_loud_review():
    """``[Dim].[Hier].[Bogus].[All]`` names a level, so the All token is a data
    caption — and an unresolvable level must FAULT, not drop the filter.

    Three name segments mean a level was named, which takes the reference out
    of grand-total territory entirely. Bug-3622 then applies: an unresolvable
    member reference fails loud rather than running the drill unfiltered.
    """
    with pytest.raises(DrillThroughResolutionError):
        _member_parts_to_groupings(
            ["geo", "geo", "NoSuchLevel", "All"],
            {d["name"]: d for d in _GEO_DIMS},
            {"geo": {"region": "region", "city": "city"}},
        )


@_async
async def test_plain_dimension_grand_total_reaches_router_unfiltered_bug8047():
    """End to end: a plain dimension's grand-total drill sends NO grouping
    level to the query-router and returns the contributing rows."""
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS FROM [modelx] "
        "WHERE ([country_code].[country_code].[All])"
    )
    captured = {}

    async def _fake_drill_through(measure_id, grouping_levels, jwt_token, **kwargs):
        captured["grouping_levels"] = grouping_levels
        return _drill_through_response(
            columns=["country_code", "amount"],
            rows=[{"country_code": "DE", "amount": 42},
                  {"country_code": "FR", "amount": 17}],
        )

    with (
        patch("src.dax.drillthrough_handler.execute_drill_options",
              new=AsyncMock(return_value=_drill_options_response([]))),
        patch("src.dax.drillthrough_handler.execute_drill_through",
              new=_fake_drill_through),
    ):
        xml, warnings, _next = await handle_drillthrough(
            parsed, "acme", "jwt", _MEASURES_META, _PLAIN_DIMS, _PLAIN_HIERARCHIES,
        )

    assert captured["grouping_levels"] == []
    assert "<country_code>DE</country_code><amount>42</amount>" in xml
    assert "<country_code>FR</country_code><amount>17</amount>" in xml
    assert warnings == []


@_async
async def test_grand_total_drill_reaches_router_unfiltered_bug8047():
    """End to end: the grand-total drill sends NO grouping level to the
    query-router and returns the contributing rows."""
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS FROM [modely] "
        "WHERE ([geo].[geo].[(All)])"
    )
    captured = {}

    async def _fake_drill_through(measure_id, grouping_levels, jwt_token, **kwargs):
        captured["grouping_levels"] = grouping_levels
        return _drill_through_response(
            columns=["city", "amount"],
            rows=[{"city": "Berlin", "amount": 30},
                  {"city": "Hamburg", "amount": 12}],
        )

    with (
        patch("src.dax.drillthrough_handler.execute_drill_options",
              new=AsyncMock(return_value=_drill_options_response([]))),
        patch("src.dax.drillthrough_handler.execute_drill_through",
              new=_fake_drill_through),
    ):
        xml, warnings, _next = await handle_drillthrough(
            parsed, "acme", "jwt", _MEASURES_META, _GEO_DIMS, _GEO_HIERARCHY,
        )

    assert captured["grouping_levels"] == []
    assert "<city>Berlin</city><amount>30</amount>" in xml
    assert "<city>Hamburg</city><amount>12</amount>" in xml
    assert warnings == []


# ---------------------------------------------------------------------------
# Bug-8048 — keyset cursor threaded through the gateway.
#
# The query-router already mints signed keyset tokens
# (query-router/src/drill/cursor.py, drill_routes.DrillThroughPageInfo). The
# gateway dropped them on the floor, so an XMLA client could never continue a
# multi-page drill stably.
#
# Test escape: the handler's return arity was pinned by tests, but nothing
# asserted what happened to ``page.next_cursor`` or that a supplied cursor
# reached the router. Guard: the cases below. Tier: T1.
# ---------------------------------------------------------------------------


@_async
async def test_cursor_is_forwarded_to_the_router_bug8048():
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS FROM [modely]"
    )
    captured = {}

    async def _fake_drill_through(measure_id, grouping_levels, jwt_token, **kwargs):
        captured["cursor"] = kwargs.get("cursor")
        return _drill_through_response(has_more=True)

    with (
        patch("src.dax.drillthrough_handler.execute_drill_options",
              new=AsyncMock(return_value=_drill_options_response([]))),
        patch("src.dax.drillthrough_handler.execute_drill_through",
              new=_fake_drill_through),
    ):
        result = await handle_drillthrough(
            parsed, "acme", "jwt", _MEASURES_META, _DIMS_WITH_IDS,
            _HIERARCHY_DEFS, cursor="page-1-token",
        )

    assert captured["cursor"] == "page-1-token"
    assert result.next_cursor == "c1"


@_async
async def test_no_cursor_when_the_result_is_complete_bug8048():
    parsed = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS FROM [modely]"
    )
    captured = {}

    async def _fake_drill_through(measure_id, grouping_levels, jwt_token, **kwargs):
        captured["cursor"] = kwargs.get("cursor")
        return _drill_through_response(has_more=False)

    with (
        patch("src.dax.drillthrough_handler.execute_drill_options",
              new=AsyncMock(return_value=_drill_options_response([]))),
        patch("src.dax.drillthrough_handler.execute_drill_through",
              new=_fake_drill_through),
    ):
        result = await handle_drillthrough(
            parsed, "acme", "jwt", _MEASURES_META, _DIMS_WITH_IDS, _HIERARCHY_DEFS,
        )

    assert captured["cursor"] is None
    assert result.next_cursor is None
    assert result.warnings == []


# ---------------------------------------------------------------------------
# Bug-8048 — XMLA Execute wiring: opt-in cursor property in, continuation
# element out.
#
# Pagination is a Tessallite extension, so it is OPT-IN: only a client that
# sends the ``DrillthroughCursor`` property gets the continuation element
# back. Excel and Power BI never send it and must therefore keep receiving a
# response with no unknown sibling element inside <tns:ExecuteResponse>.
# ---------------------------------------------------------------------------

from src.dax import xmla_server as _xs  # noqa: E402
from src.dax.drillthrough_handler import DrillthroughResult  # noqa: E402

_DRILL_MDX = (
    "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS FROM [modely]"
)


def _execute_envelope(properties_xml: str) -> "object":
    from defusedxml import ElementTree as _ET
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        '<soap:Body>'
        '<Execute xmlns="urn:schemas-microsoft-com:xml-analysis">'
        f'<Command><Statement>{_DRILL_MDX}</Statement></Command>'
        f'<Properties><PropertyList><Catalog>modely</Catalog>'
        f'{properties_xml}</PropertyList></Properties>'
        '</Execute></soap:Body></soap:Envelope>'
    )
    root = _ET.fromstring(envelope)
    for el in root.iter():
        if el.tag.endswith("}Execute") or el.tag == "Execute":
            return el
    raise AssertionError("Execute element not found")


def _patch_execute_metadata(monkeypatch, captured: dict) -> None:
    async def _resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def _meta(**kwargs):
        return ([{"id": "meas-001", "name": "amount"}], [], [])

    async def _named_sets(*a, **kw):
        return []

    async def _drill(**kwargs):
        captured["cursor"] = kwargs.get("cursor")
        return DrillthroughResult("<return><root/></return>", [], "next-token-9")

    monkeypatch.setattr(_xs, "_resolve_model_id", _resolve_model_id)
    monkeypatch.setattr(_xs, "_load_model_metadata_cached", _meta)
    monkeypatch.setattr(_xs, "get_model_named_sets", _named_sets)
    monkeypatch.setattr(_xs, "handle_drillthrough", _drill)


@_async
async def test_execute_forwards_cursor_property_and_returns_token_bug8048(monkeypatch):
    captured: dict = {}
    _patch_execute_metadata(monkeypatch, captured)

    response = await _xs._handle_execute(
        _execute_envelope("<DrillthroughCursor>page-1-token</DrillthroughCursor>"),
        tenant_slug="acme", jwt_token="jwt", session_id="sid-1",
    )
    body = response.body.decode("utf-8")

    assert captured["cursor"] == "page-1-token"
    assert "<tns:DrillthroughCursor>next-token-9</tns:DrillthroughCursor>" in body


@_async
async def test_execute_first_page_opt_in_uses_empty_property_bug8048(monkeypatch):
    """An empty property means "page 1, and I do want tokens"."""
    captured: dict = {}
    _patch_execute_metadata(monkeypatch, captured)

    response = await _xs._handle_execute(
        _execute_envelope("<DrillthroughCursor></DrillthroughCursor>"),
        tenant_slug="acme", jwt_token="jwt", session_id="sid-2",
    )
    body = response.body.decode("utf-8")

    assert captured["cursor"] is None
    assert "<tns:DrillthroughCursor>next-token-9</tns:DrillthroughCursor>" in body


@_async
async def test_execute_without_the_property_emits_no_cursor_element_bug8048(monkeypatch):
    """Excel / Power BI must see an unchanged ExecuteResponse."""
    captured: dict = {}
    _patch_execute_metadata(monkeypatch, captured)

    response = await _xs._handle_execute(
        _execute_envelope(""),
        tenant_slug="acme", jwt_token="jwt", session_id="sid-3",
    )
    body = response.body.decode("utf-8")

    assert captured["cursor"] is None
    assert "DrillthroughCursor" not in body
    assert "<tns:ExecuteResponse><return><root/></return></tns:ExecuteResponse>" in body
