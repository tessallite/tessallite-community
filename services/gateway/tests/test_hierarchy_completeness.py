"""Tests for hierarchy completeness — XMLA metadata correctness for all hierarchy types.

Covers DIMENSION_TYPE, LEVEL_TYPE, MEMBER_TYPE, PARENT_UNIQUE_NAME,
CHILDREN_CARDINALITY, TREE_OP navigation, and DrilldownMember expansion.
"""

import pytest
from src.dax.mdschema import (
    _rows_hierarchies,
    _rows_levels,
    _rows_members,
    _time_level_type,
)
from src.dax.mdx_execute import (
    _extract_drilldown_member_expansions,
)


# ---- Fixtures ----

def _explicit_hierarchy_dim():
    """Explicit multi-level hierarchy: Region > Country > City."""
    return {
        "name": "Geography",
        "is_time_dim": False,
        "display_name": "Geography",
        "levels": [
            {"ordinal": 0, "name": "Region"},
            {"ordinal": 1, "name": "Country"},
            {"ordinal": 2, "name": "City"},
        ],
    }


def _explicit_hierarchy_member_data():
    return {
        "Geography": {
            "levels": ["Region", "Country", "City"],
            "members_by_level": {
                "0": [
                    {"name": "EMEA", "ordinal": 0},
                    {"name": "Americas", "ordinal": 1},
                ],
                "1": [
                    {"name": "UK", "ordinal": 0, "parent": "EMEA"},
                    {"name": "Germany", "ordinal": 1, "parent": "EMEA"},
                    {"name": "USA", "ordinal": 2, "parent": "Americas"},
                ],
                "2": [
                    {"name": "London", "ordinal": 0, "parent": "UK"},
                    {"name": "Berlin", "ordinal": 1, "parent": "Germany"},
                    {"name": "New York", "ordinal": 2, "parent": "USA"},
                    {"name": "Chicago", "ordinal": 3, "parent": "USA"},
                ],
            },
        }
    }


def _date_embedded_dim():
    """Date embedded hierarchy: Year > Month > Day."""
    return {
        "name": "Calendar",
        "is_time_dim": True,
        "display_name": "Calendar",
        "levels": [
            {"ordinal": 0, "name": "Year"},
            {"ordinal": 1, "name": "Month"},
            {"ordinal": 2, "name": "Day"},
        ],
    }


def _date_embedded_member_data():
    return {
        "Calendar": {
            "levels": ["Year", "Month", "Day"],
            "members_by_level": {
                "0": [{"name": "2024", "ordinal": 0}],
                "1": [
                    {"name": "2024-01", "ordinal": 0, "parent": "2024"},
                    {"name": "2024-02", "ordinal": 1, "parent": "2024"},
                ],
                "2": [
                    {"name": "2024-01-15", "ordinal": 0, "parent": "2024-01"},
                    {"name": "2024-01-31", "ordinal": 1, "parent": "2024-01"},
                    {"name": "2024-02-28", "ordinal": 2, "parent": "2024-02"},
                ],
            },
        }
    }


def _flat_dim():
    """Single-level inferred dimension (attribute hierarchy)."""
    return {
        "name": "account_type",
        "is_time_dim": False,
        "display_name": "Account Type",
    }


def _flat_member_data():
    return {
        "account_type": {
            "members": [
                {"name": "CURRENT", "ordinal": 0},
                {"name": "LOAN", "ordinal": 1},
                {"name": "SAVINGS", "ordinal": 2},
            ]
        }
    }


# ---- DIMENSION_TYPE tests ----

class TestDimensionType:

    def test_time_dimension_has_type_1(self):
        rows = _rows_hierarchies("demo", [_date_embedded_dim()])
        dim_row = next(r for r in rows if r["HIERARCHY_NAME"] == "Calendar")
        assert dim_row["DIMENSION_TYPE"] == "1"

    def test_regular_dimension_has_type_3(self):
        rows = _rows_hierarchies("demo", [_explicit_hierarchy_dim()])
        dim_row = next(r for r in rows if r["HIERARCHY_NAME"] == "Geography")
        assert dim_row["DIMENSION_TYPE"] == "3"

    def test_measures_has_type_2(self):
        rows = _rows_hierarchies("demo", [], measures=[{"name": "Revenue"}])
        meas_row = next(r for r in rows if r["HIERARCHY_NAME"] == "Measures")
        assert meas_row["DIMENSION_TYPE"] == "2"


# ---- TIME LEVEL_TYPE tests (XMLA spec bitmask values) ----

class TestTimeLevelTypes:

    def test_year_level_type(self):
        assert _time_level_type("Year") == "20"

    def test_quarter_level_type(self):
        assert _time_level_type("Quarter") == "68"

    def test_month_level_type(self):
        assert _time_level_type("Month") == "132"

    def test_week_level_type(self):
        assert _time_level_type("Week") == "516"

    def test_day_level_type(self):
        assert _time_level_type("Day") == "1028"

    def test_half_year_level_type(self):
        assert _time_level_type("half_year") == "36"

    def test_regular_level_type_is_zero(self):
        assert _time_level_type("Region") == "0"

    def test_case_insensitive(self):
        assert _time_level_type("YEAR") == "20"
        assert _time_level_type("month") == "132"


# ---- MDSCHEMA_LEVELS for multi-level hierarchies ----

class TestLevelsMultiLevel:

    def test_explicit_hierarchy_has_all_plus_data_levels(self):
        rows = _rows_levels(
            "demo", [_explicit_hierarchy_dim()],
            _explicit_hierarchy_member_data(),
        )
        geo_levels = [r for r in rows if r["HIERARCHY_UNIQUE_NAME"] == "[Geography].[Geography]"]
        level_names = [r["LEVEL_NAME"] for r in geo_levels]
        assert level_names == ["(All)", "Region", "Country", "City"]

    def test_level_numbers_sequential(self):
        rows = _rows_levels(
            "demo", [_explicit_hierarchy_dim()],
            _explicit_hierarchy_member_data(),
        )
        geo_levels = [r for r in rows if r["HIERARCHY_UNIQUE_NAME"] == "[Geography].[Geography]"]
        level_nums = [r["LEVEL_NUMBER"] for r in geo_levels]
        assert level_nums == ["0", "1", "2", "3"]

    def test_all_level_type_is_one(self):
        rows = _rows_levels(
            "demo", [_explicit_hierarchy_dim()],
            _explicit_hierarchy_member_data(),
        )
        all_level = next(r for r in rows if r["LEVEL_NAME"] == "(All)" and r["HIERARCHY_UNIQUE_NAME"] == "[Geography].[Geography]")
        assert all_level["LEVEL_TYPE"] == "1"

    def test_regular_level_type_is_zero(self):
        rows = _rows_levels(
            "demo", [_explicit_hierarchy_dim()],
            _explicit_hierarchy_member_data(),
        )
        region = next(r for r in rows if r["LEVEL_NAME"] == "Region")
        assert region["LEVEL_TYPE"] == "0"

    def test_time_hierarchy_levels_have_correct_types(self):
        rows = _rows_levels(
            "demo", [_date_embedded_dim()],
            _date_embedded_member_data(),
        )
        cal_levels = {r["LEVEL_NAME"]: r for r in rows if r["HIERARCHY_UNIQUE_NAME"] == "[Calendar].[Calendar]"}
        assert cal_levels["(All)"]["LEVEL_TYPE"] == "1"
        assert cal_levels["Year"]["LEVEL_TYPE"] == "20"
        assert cal_levels["Month"]["LEVEL_TYPE"] == "132"
        assert cal_levels["Day"]["LEVEL_TYPE"] == "1028"

    def test_level_cardinality_from_member_data(self):
        rows = _rows_levels(
            "demo", [_explicit_hierarchy_dim()],
            _explicit_hierarchy_member_data(),
        )
        region = next(r for r in rows if r["LEVEL_NAME"] == "Region")
        country = next(r for r in rows if r["LEVEL_NAME"] == "Country")
        city = next(r for r in rows if r["LEVEL_NAME"] == "City")
        assert region["LEVEL_CARDINALITY"] == "2"
        assert country["LEVEL_CARDINALITY"] == "3"
        assert city["LEVEL_CARDINALITY"] == "4"


# ---- MDSCHEMA_MEMBERS parent-child relationships ----

class TestMembersParentChild:

    def test_all_member_exists_with_type_2(self):
        rows = _rows_members(
            "demo", [{"name": "Revenue"}],
            [_explicit_hierarchy_dim()],
            {},
            _explicit_hierarchy_member_data(),
        )
        all_row = next(r for r in rows if r["MEMBER_NAME"] == "All" and r["HIERARCHY_UNIQUE_NAME"] == "[Geography].[Geography]")
        assert all_row["MEMBER_TYPE"] == "2"
        assert all_row["LEVEL_NUMBER"] == "0"

    def test_root_members_have_all_as_parent(self):
        rows = _rows_members(
            "demo", [{"name": "Revenue"}],
            [_explicit_hierarchy_dim()],
            {},
            _explicit_hierarchy_member_data(),
        )
        emea = next(r for r in rows if r["MEMBER_NAME"] == "EMEA")
        assert emea["PARENT_UNIQUE_NAME"] == "[Geography].[Geography].[All]"
        assert emea["LEVEL_NUMBER"] == "1"

    def test_child_members_point_to_parent(self):
        rows = _rows_members(
            "demo", [{"name": "Revenue"}],
            [_explicit_hierarchy_dim()],
            {},
            _explicit_hierarchy_member_data(),
        )
        uk = next(r for r in rows if r["MEMBER_NAME"] == "UK")
        # Bug-3617 (Phase 2): canonical parent uname — the parent (EMEA) at its
        # level (Region) in path-qualified key form, not the old caption form.
        assert uk["PARENT_UNIQUE_NAME"] == "[Geography].[Geography].[Region].&[EMEA]"
        assert uk["LEVEL_NUMBER"] == "2"

    def test_leaf_members_have_zero_children(self):
        rows = _rows_members(
            "demo", [{"name": "Revenue"}],
            [_explicit_hierarchy_dim()],
            {},
            _explicit_hierarchy_member_data(),
        )
        london = next(r for r in rows if r["MEMBER_NAME"] == "London")
        assert london["CHILDREN_CARDINALITY"] == "0"
        assert london["LEVEL_NUMBER"] == "3"

    def test_parent_children_cardinality_matches(self):
        rows = _rows_members(
            "demo", [{"name": "Revenue"}],
            [_explicit_hierarchy_dim()],
            {},
            _explicit_hierarchy_member_data(),
        )
        emea = next(r for r in rows if r["MEMBER_NAME"] == "EMEA")
        assert emea["CHILDREN_CARDINALITY"] == "2"  # UK, Germany
        americas = next(r for r in rows if r["MEMBER_NAME"] == "Americas")
        assert americas["CHILDREN_CARDINALITY"] == "1"  # USA
        usa = next(r for r in rows if r["MEMBER_NAME"] == "USA")
        assert usa["CHILDREN_CARDINALITY"] == "2"  # New York, Chicago

    def test_all_member_children_cardinality(self):
        rows = _rows_members(
            "demo", [{"name": "Revenue"}],
            [_explicit_hierarchy_dim()],
            {},
            _explicit_hierarchy_member_data(),
        )
        all_row = next(r for r in rows if r["MEMBER_NAME"] == "All" and r["HIERARCHY_UNIQUE_NAME"] == "[Geography].[Geography]")
        assert all_row["CHILDREN_CARDINALITY"] == "2"  # EMEA, Americas


# ---- TREE_OP navigation ----

class TestTreeOp:

    def test_self_only_returns_just_the_member(self):
        rows = _rows_members(
            "demo", [],
            [_explicit_hierarchy_dim()],
            {
                "MEMBER_UNIQUE_NAME": ["[Geography].[Geography].[All]"],
                "TREE_OP": ["8"],
            },
            _explicit_hierarchy_member_data(),
        )
        assert len(rows) == 1
        assert rows[0]["MEMBER_NAME"] == "All"

    def test_children_of_all_returns_root_members(self):
        rows = _rows_members(
            "demo", [],
            [_explicit_hierarchy_dim()],
            {
                "MEMBER_UNIQUE_NAME": ["[Geography].[Geography].[All]"],
                "TREE_OP": ["1"],
            },
            _explicit_hierarchy_member_data(),
        )
        names = [r["MEMBER_NAME"] for r in rows]
        assert "EMEA" in names
        assert "Americas" in names
        assert "All" not in names

    def test_descendants_of_all_returns_all_data_members(self):
        rows = _rows_members(
            "demo", [],
            [_explicit_hierarchy_dim()],
            {
                "MEMBER_UNIQUE_NAME": ["[Geography].[Geography].[All]"],
                "TREE_OP": ["16"],
            },
            _explicit_hierarchy_member_data(),
        )
        names = {r["MEMBER_NAME"] for r in rows}
        assert names == {"EMEA", "Americas", "UK", "Germany", "USA", "London", "Berlin", "New York", "Chicago"}

    def test_children_of_member_returns_next_level(self):
        rows = _rows_members(
            "demo", [],
            [_explicit_hierarchy_dim()],
            {
                "MEMBER_UNIQUE_NAME": ["[Geography].[Geography].[EMEA]"],
                "TREE_OP": ["1"],
            },
            _explicit_hierarchy_member_data(),
        )
        names = [r["MEMBER_NAME"] for r in rows]
        assert "UK" in names
        assert "Germany" in names
        assert "USA" not in names


# ---- DrilldownMember parsing ----

class TestDrilldownMemberParsing:

    def test_parses_single_member_expansion(self):
        expr = "DrilldownMember({{DrilldownLevel({[Geography].[Geography].[All]})}}, {[Geography].[Geography].[EMEA]})"
        result = _extract_drilldown_member_expansions(expr)
        assert "[Geography].[Geography]" in result
        assert result["[Geography].[Geography]"] == ["EMEA"]

    def test_parses_multiple_member_expansions(self):
        expr = "DrilldownMember({{DrilldownLevel({[Geography].[Geography].[All]})}}, {[Geography].[Geography].[EMEA], [Geography].[Geography].[Americas]})"
        result = _extract_drilldown_member_expansions(expr)
        assert "[Geography].[Geography]" in result
        assert set(result["[Geography].[Geography]"]) == {"EMEA", "Americas"}

    def test_returns_empty_for_no_drilldown(self):
        expr = "{[Measures].[Revenue]}"
        result = _extract_drilldown_member_expansions(expr)
        assert result == {}


# ---- Flat (attribute) hierarchy ----

class TestFlatHierarchy:

    def test_single_level_hierarchy_has_all_plus_one_data_level(self):
        rows = _rows_levels(
            "demo", [_flat_dim()], _flat_member_data(),
        )
        # Bug-6603: the flat attribute dim groups under [Dimensions]; filter by its
        # (unchanged) hierarchy unique name instead of the dimension group column.
        at_levels = [
            r for r in rows
            if r["HIERARCHY_UNIQUE_NAME"] == "[account_type].[account_type]"
        ]
        assert len(at_levels) == 2
        assert at_levels[0]["LEVEL_NAME"] == "(All)"
        assert at_levels[1]["LEVEL_NAME"] == "account_type"

    def test_flat_members_all_have_all_as_parent(self):
        rows = _rows_members(
            "demo", [],
            [_flat_dim()],
            {},
            _flat_member_data(),
        )
        data_members = [r for r in rows if r["MEMBER_TYPE"] == "1"]
        for m in data_members:
            assert m["PARENT_UNIQUE_NAME"] == "[account_type].[account_type].[All]"
            assert m["CHILDREN_CARDINALITY"] == "0"

    def test_flat_hierarchy_dimension_type_is_regular(self):
        rows = _rows_hierarchies("demo", [_flat_dim()])
        dim_row = next(r for r in rows if r["HIERARCHY_NAME"] == "account_type")
        assert dim_row["DIMENSION_TYPE"] == "3"


# ---- Ragged hierarchy (missing intermediate members) ----

class TestRaggedHierarchy:

    def _ragged_dim(self):
        return {
            "name": "Location",
            "is_time_dim": False,
            "levels": [
                {"ordinal": 0, "name": "Country"},
                {"ordinal": 1, "name": "State"},
                {"ordinal": 2, "name": "City"},
            ],
        }

    def _ragged_member_data(self):
        return {
            "Location": {
                "levels": ["Country", "State", "City"],
                "members_by_level": {
                    "0": [
                        {"name": "USA", "ordinal": 0},
                        {"name": "Singapore", "ordinal": 1},
                    ],
                    "1": [
                        {"name": "California", "ordinal": 0, "parent": "USA"},
                        {"name": "New York State", "ordinal": 1, "parent": "USA"},
                    ],
                    "2": [
                        {"name": "Los Angeles", "ordinal": 0, "parent": "California"},
                        {"name": "NYC", "ordinal": 1, "parent": "New York State"},
                    ],
                },
            }
        }

    def test_ragged_hierarchy_levels_all_present(self):
        rows = _rows_levels(
            "demo", [self._ragged_dim()], self._ragged_member_data(),
        )
        loc_levels = [r["LEVEL_NAME"] for r in rows if r["HIERARCHY_UNIQUE_NAME"] == "[Location].[Location]"]
        assert loc_levels == ["(All)", "Country", "State", "City"]

    def test_ragged_leaf_without_intermediate_still_has_parent(self):
        rows = _rows_members(
            "demo", [],
            [self._ragged_dim()],
            {},
            self._ragged_member_data(),
        )
        singapore = next(r for r in rows if r["MEMBER_NAME"] == "Singapore")
        assert singapore["CHILDREN_CARDINALITY"] == "0"
        assert singapore["PARENT_UNIQUE_NAME"] == "[Location].[Location].[All]"


# ---- Hierarchy metadata fields ----

class TestHierarchyMetadataFields:

    def test_hierarchy_origin_is_one(self):
        rows = _rows_hierarchies("demo", [_explicit_hierarchy_dim()])
        dim_row = next(r for r in rows if r["HIERARCHY_NAME"] == "Geography")
        assert dim_row["HIERARCHY_ORIGIN"] == "1"

    def test_hierarchy_is_visible(self):
        rows = _rows_hierarchies("demo", [_explicit_hierarchy_dim()])
        dim_row = next(r for r in rows if r["HIERARCHY_NAME"] == "Geography")
        assert dim_row["HIERARCHY_IS_VISIBLE"] == "true"

    def test_default_member_points_to_all(self):
        rows = _rows_hierarchies("demo", [_explicit_hierarchy_dim()])
        dim_row = next(r for r in rows if r["HIERARCHY_NAME"] == "Geography")
        assert dim_row["DEFAULT_MEMBER"] == "[Geography].[Geography].[All]"

    def test_hierarchy_cardinality_from_root_members(self):
        rows = _rows_hierarchies(
            "demo", [_explicit_hierarchy_dim()],
            member_data=_explicit_hierarchy_member_data(),
        )
        dim_row = next(r for r in rows if r["HIERARCHY_NAME"] == "Geography")
        assert dim_row["HIERARCHY_CARDINALITY"] == "2"
