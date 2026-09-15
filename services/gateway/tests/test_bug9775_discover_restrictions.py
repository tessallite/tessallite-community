"""Bug-9775 — DISCOVER restrictions must actually filter the rowset.

The MDSCHEMA_* builders emitted the whole catalogue and never consulted the
client's restrictions, so a request for ONE hierarchy came back with all 86
rows, including rows belonging to other dimensions and to [Measures]. The
gateway ADVERTISES restriction support in DISCOVER_SCHEMA_ROWSETS, so returning
unrelated rows breaks the provider contract Excel relies on when assembling its
field list.

The filter is deliberately conservative — an unrecognised restriction is ignored
rather than matched against nothing — because the failure mode of over-filtering
(an empty rowset, a blank field list) is worse than the unfiltered behaviour it
replaces. These tests pin both halves of that: it narrows where it understands,
and it never blanks out where it does not.
"""

import pytest

from src.dax.mdschema import _apply_restrictions


ROWS = [
    {"HIERARCHY_UNIQUE_NAME": "[Dimensions].[account_type]",
     "DIMENSION_UNIQUE_NAME": "[Dimensions]",
     "HIERARCHY_IS_VISIBLE": "true", "HIERARCHY_ORIGIN": "2"},
    {"HIERARCHY_UNIQUE_NAME": "[Dimensions].[channel_name]",
     "DIMENSION_UNIQUE_NAME": "[Dimensions]",
     "HIERARCHY_IS_VISIBLE": "true", "HIERARCHY_ORIGIN": "2"},
    {"HIERARCHY_UNIQUE_NAME": "[Hierarchies].[business_date Calendar]",
     "DIMENSION_UNIQUE_NAME": "[Hierarchies]",
     "HIERARCHY_IS_VISIBLE": "true", "HIERARCHY_ORIGIN": "1"},
    {"HIERARCHY_UNIQUE_NAME": "[Measures]",
     "DIMENSION_UNIQUE_NAME": "[Measures]",
     "HIERARCHY_IS_VISIBLE": "false", "HIERARCHY_ORIGIN": "1"},
]


def _names(rows):
    return [r["HIERARCHY_UNIQUE_NAME"] for r in rows]


class TestNarrowing:
    def test_single_hierarchy_restriction_returns_only_that_row(self):
        out = _apply_restrictions(
            "MDSCHEMA_HIERARCHIES", ROWS,
            {"HIERARCHY_UNIQUE_NAME": ["[Dimensions].[account_type]"]},
        )
        assert _names(out) == ["[Dimensions].[account_type]"]

    def test_dimension_restriction_excludes_other_dimensions_and_measures(self):
        """The reported defect: restricting to [Dimensions] still returned the
        [Hierarchies] group rows and [Measures]."""
        out = _apply_restrictions(
            "MDSCHEMA_HIERARCHIES", ROWS,
            {"DIMENSION_UNIQUE_NAME": ["[Dimensions]"]},
        )
        assert _names(out) == [
            "[Dimensions].[account_type]", "[Dimensions].[channel_name]",
        ]

    def test_multiple_restrictions_are_combined(self):
        out = _apply_restrictions(
            "MDSCHEMA_HIERARCHIES", ROWS,
            {"DIMENSION_UNIQUE_NAME": ["[Dimensions]"],
             "HIERARCHY_UNIQUE_NAME": ["[Dimensions].[channel_name]"]},
        )
        assert _names(out) == ["[Dimensions].[channel_name]"]


class TestBitmaskColumns:
    """Visibility and origin are bitmasks; string equality would drop everything."""

    def test_visibility_pseudo_column_maps_and_filters(self):
        out = _apply_restrictions(
            "MDSCHEMA_HIERARCHIES", ROWS, {"HIERARCHY_VISIBILITY": ["1"]},
        )
        assert "[Measures]" not in _names(out)
        assert len(out) == 3

    def test_visibility_mask_accepting_both_does_not_narrow(self):
        out = _apply_restrictions(
            "MDSCHEMA_HIERARCHIES", ROWS, {"HIERARCHY_VISIBILITY": ["3"]},
        )
        assert len(out) == len(ROWS)

    def test_origin_is_matched_as_a_bitmask_not_a_string(self):
        out = _apply_restrictions(
            "MDSCHEMA_HIERARCHIES", ROWS, {"HIERARCHY_ORIGIN": ["2"]},
        )
        assert _names(out) == [
            "[Dimensions].[account_type]", "[Dimensions].[channel_name]",
        ]

    def test_origin_mask_can_select_several_kinds_at_once(self):
        out = _apply_restrictions(
            "MDSCHEMA_HIERARCHIES", ROWS, {"HIERARCHY_ORIGIN": ["3"]},
        )
        assert len(out) == len(ROWS)


class TestNeverBlanksOut:
    """Over-filtering is worse than the unfiltered behaviour being replaced."""

    def test_unknown_restriction_column_is_ignored(self):
        out = _apply_restrictions(
            "MDSCHEMA_HIERARCHIES", ROWS, {"NOT_A_REAL_COLUMN": ["whatever"]},
        )
        assert len(out) == len(ROWS)

    def test_restriction_naming_a_column_absent_from_this_rowset_is_ignored(self):
        out = _apply_restrictions(
            "MDSCHEMA_HIERARCHIES", ROWS, {"LEVEL_UNIQUE_NAME": ["[x].[y].[z]"]},
        )
        assert len(out) == len(ROWS)

    def test_empty_restriction_values_are_ignored(self):
        for value in ([], [""], [None]):
            out = _apply_restrictions(
                "MDSCHEMA_HIERARCHIES", ROWS, {"HIERARCHY_UNIQUE_NAME": value},
            )
            assert len(out) == len(ROWS)

    def test_non_numeric_bitmask_is_ignored_rather_than_dropping_rows(self):
        out = _apply_restrictions(
            "MDSCHEMA_HIERARCHIES", ROWS, {"HIERARCHY_VISIBILITY": ["not-a-number"]},
        )
        assert len(out) == len(ROWS)

    @pytest.mark.parametrize("rtype", [
        "MDSCHEMA_MEMBERS", "MDSCHEMA_PROPERTIES",
        "DISCOVER_PROPERTIES", "DISCOVER_SCHEMA_ROWSETS",
    ])
    def test_self_restricting_rowsets_are_left_alone(self, rtype):
        """These builders consume restrictions themselves with semantics the
        generic filter must not second-guess — MDSCHEMA_MEMBERS in particular
        resolves TREE_OP relative to a member, so post-filtering by
        MEMBER_UNIQUE_NAME would discard exactly the relatives it was asked for.
        """
        out = _apply_restrictions(
            rtype, ROWS, {"HIERARCHY_UNIQUE_NAME": ["[Dimensions].[account_type]"]},
        )
        assert len(out) == len(ROWS)

    def test_empty_rowset_stays_empty_without_error(self):
        assert _apply_restrictions(
            "MDSCHEMA_HIERARCHIES", [], {"HIERARCHY_UNIQUE_NAME": ["x"]},
        ) == []
