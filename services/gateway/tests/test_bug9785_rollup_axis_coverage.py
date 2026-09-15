"""Bug-9785 — a partial rollup set produces an axis that CRASHES Excel.

Reported live: adding `account type` to a PivotTable that already had a date
field made the date field disappear from the sheet while staying selected in the
Rows panel; removing and re-adding it, or adding the two in the other order,
**crashed Excel outright**.

The response audit found the cause. For
``CrossJoin(DrilldownLevel({[business_date]...[All]}), DrilldownLevel({[account_type]...[All]}))``
the Axis1 ``AxisInfo`` declared exactly ONE ``HierarchyInfo``
(``[Dimensions].[account_type]``) against 2203 tuples. The gateway's own comments
in ``mdx_execute`` record why that is fatal: emitting a property or member for a
hierarchy the axis did not declare crashes MSOLAP. The same shape with the
Calendar hierarchy declared 1 against 1836 tuples.

THE COVERAGE INVARIANT, third occurrence. Grain queries GROUP BY only the rollup
dimensions and the merge keys its rows on them, so a rollup set covering a SUBSET
of the axis drops the uncovered dimension's column -- and the axis loses that
hierarchy. It was violated three different ways:

  Bug-9777  a CrossJoin sibling matched by no detector
  Bug-9783  the outer dimension of a drill plan
  Bug-9785  ANY time dimension or user hierarchy sharing an axis with a flat
            attribute -- and this class can NEVER be covered by the detectors,
            because they recognise standalone ATTRIBUTES by design

That third class is why the check cannot live inside the detectors and is
enforced after detection instead.

Failing safe drops the ROLLUPS, never the query. The plain path emits every
hierarchy correctly and merely lacks the All/subtotal rows: a missing subtotal
row is a display gap, a malformed axis takes the client down.
"""

import pytest

from src.dax.subtotal_engine import (
    SubtotalHierarchy,
    SubtotalLevel,
    uncovered_axis_dimensions,
)


def _flat(name):
    return SubtotalHierarchy(
        hierarchy_name=name, mdx_dim_name=name, mdx_hier_name=name,
        levels=[SubtotalLevel(name=name, ordinal=0, dim_name=name)], axis=1,
    )


def _multi(name, dims):
    return SubtotalHierarchy(
        hierarchy_name=name, mdx_dim_name=name, mdx_hier_name=name,
        levels=[SubtotalLevel(name=d, ordinal=i, dim_name=d)
                for i, d in enumerate(dims)],
        axis=1,
    )


class TestTheCrashingShapes:
    """Each of these returned a partial axis live, and Excel died on two."""

    def test_time_dimension_beside_a_flat_attribute_is_uncovered(self):
        """The reported crash. `business_date` is a flat TIME dimension, which
        `is_standalone_attribute` deliberately excludes, so no detector can ever
        name it -- the rollup set covers only `account_type`."""
        assert uncovered_axis_dimensions(
            {"business_date", "account_type"}, [_flat("account_type")],
        ) == {"business_date"}

    def test_calendar_hierarchy_beside_a_flat_attribute_is_uncovered(self):
        assert uncovered_axis_dimensions(
            {"business_date_calendar_day", "account_type"},
            [_flat("account_type")],
        ) == {"business_date_calendar_day"}

    def test_a_crossjoin_sibling_matched_by_no_detector_is_uncovered(self):
        """Bug-9777's original failure: 42 tuples with channel_name absent."""
        assert uncovered_axis_dimensions(
            {"account_type", "channel_name"}, [_flat("account_type")],
        ) == {"channel_name"}


class TestFullCoverageIsAllowed:
    """The working cases must keep their subtotals — the guard must not be so
    blunt that it disables the feature it protects."""

    def test_two_flat_attributes_both_rolled_up(self):
        assert uncovered_axis_dimensions(
            {"account_type", "channel_name"},
            [_flat("account_type"), _flat("channel_name")],
        ) == set()

    def test_a_single_flat_attribute(self):
        assert uncovered_axis_dimensions({"account_type"}, [_flat("account_type")]) == set()

    def test_a_multi_level_hierarchy_covers_every_one_of_its_level_dims(self):
        """A hierarchy contributes one dim per LEVEL, not one per hierarchy."""
        assert uncovered_axis_dimensions(
            {"cal_year", "cal_month", "cal_day"},
            [_multi("Calendar", ["cal_year", "cal_month", "cal_day"])],
        ) == set()

    def test_a_hierarchy_covering_only_some_of_its_levels_is_uncovered(self):
        assert uncovered_axis_dimensions(
            {"cal_year", "cal_month", "cal_day"},
            [_multi("Calendar", ["cal_year"])],
        ) == {"cal_month", "cal_day"}

    def test_extra_rollups_beyond_the_axis_are_not_a_violation(self):
        """Coverage is one-directional: every AXIS dim must be rolled up. A
        rollup naming something not on the axis is harmless here."""
        assert uncovered_axis_dimensions(
            {"account_type"}, [_flat("account_type"), _flat("channel_name")],
        ) == set()


class TestDegenerateInput:
    def test_no_rollups_leaves_every_axis_dimension_uncovered(self):
        assert uncovered_axis_dimensions({"a", "b"}, []) == {"a", "b"}

    def test_an_empty_axis_is_trivially_covered(self):
        assert uncovered_axis_dimensions(set(), [_flat("account_type")]) == set()

    def test_both_empty_is_covered(self):
        assert uncovered_axis_dimensions(set(), []) == set()

    def test_a_list_axis_argument_behaves_like_a_set(self):
        assert uncovered_axis_dimensions(
            ["account_type", "account_type", "channel_name"], [_flat("account_type")],
        ) == {"channel_name"}
