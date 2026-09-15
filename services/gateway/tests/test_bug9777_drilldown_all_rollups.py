"""Regression tests for Bug-9777 -- rollups for ``DrilldownLevel`` on the All member.

Bug-9772 started advertising ``ALL_MEMBER`` in ``MDSCHEMA_HIERARCHIES``. That
changed what Excel BELIEVES about the cube, and therefore what it asks for:
having learned the hierarchies do have an All member, Excel began sending
``DrilldownLevel({[Dimensions].[account_type].[All]})`` where it previously sent
a bare ``{...Members}`` enumeration. That shape reached a detector that only
recognized ``.Members``, so no rollup query was generated -- the parent rows
rendered with expand/collapse arrows and correct children but NO VALUE of their
own, which is what the user reported seeing in the live PivotTable.

Two distinct properties are pinned here, and the second is the one that live
testing caught after the first was already working:

1. THE TRIGGER. ``DrilldownLevel`` on an All member is a rollup request even for
   a SINGLE dimension. The pre-existing two-dimension rule exists because a bare
   ``.Members`` is indistinguishable from ordinary field enumeration; naming the
   All member removes that ambiguity, because a plain enumeration never does it.

2. THE COVERAGE INVARIANT. Whatever triggers a rollup, the returned list must
   name EVERY dimension on that axis. Grain queries GROUP BY only the rollup
   dimensions and the merge keys rows on them, so a rollup set covering a SUBSET
   of the axis silently drops the uncovered dimension from the merged result --
   the axis then renders with that hierarchy missing altogether. The first
   version of this fix detected only the drilled dimension and did exactly that:
   a live ``CrossJoin(DrilldownLevel({...[All]}), {...Members})`` came back with
   ``channel_name`` gone from the axis entirely. With ``.Members`` alone the
   invariant held by accident (every axis dimension matches, or none does), so
   requiring 2+ was both the ambiguity guard and, incidentally, full coverage.
"""

import pytest

from src.dax.subtotal_engine import detect_flat_attribute_rollups

ATTRS = {"account_type", "channel_name", "aml_flag"}

# The shapes as the DETECTOR sees them: internal grammar. Excel sends the wire
# form (``[Dimensions].[account_type]``); ``_normalize_wire_mdx`` rewrites it to
# the self-qualified internal form before any of this runs, and the
# self-qualification is load-bearing here -- see
# ``test_cross_dimension_qualified_name_is_ignored``.
DRILL = "DrilldownLevel({[account_type].[account_type].[All]})"
DRILL_PAREN = "DrilldownLevel({[account_type].[account_type].[(All)]})"
MEMBERS_CHANNEL = "{[channel_name].[channel_name].Members}"
MEMBERS_ACCOUNT = "{[account_type].[account_type].Members}"


def _names(rollups):
    return sorted(r.mdx_dim_name.lower() for r in rollups)


class TestTriggerOnDrilldownAll:
    """Property 1 -- naming the All member is an unambiguous rollup request."""

    @pytest.mark.parametrize("expr", [DRILL, DRILL_PAREN])
    def test_lone_drilled_dimension_is_a_rollup(self, expr):
        """The reported symptom: one field in Rows, expanded, no total row.

        A lone ``.Members`` is deliberately NOT enough (see the sibling test);
        a lone DrilldownLevel-on-All is, and this is the difference the fix
        turns on.
        """
        assert _names(detect_flat_attribute_rollups("", expr, ATTRS)) == ["account_type"]

    def test_drilldown_on_the_column_axis_counts_too(self):
        assert _names(detect_flat_attribute_rollups(DRILL, "", ATTRS)) == ["account_type"]

    def test_drilldown_on_a_non_all_member_is_not_a_rollup(self):
        """``DrilldownLevel({[Cal].[Cal].[2024]})`` expands one member's
        children. It asks for no total and must not manufacture one."""
        expr = "DrilldownLevel({[account_type].[account_type].[CREDIT]})"
        assert detect_flat_attribute_rollups("", expr, ATTRS) == []

    def test_drilldown_on_an_unknown_dimension_is_ignored(self):
        """Only dimensions the catalogue classifies as standalone attributes are
        rollup-able here; a genuine multi-level hierarchy is owned by
        ``detect_subtotal_hierarchies`` instead."""
        expr = "DrilldownLevel({[not_an_attribute].[not_an_attribute].[All]})"
        assert detect_flat_attribute_rollups("", expr, ATTRS) == []

    def test_cross_dimension_qualified_name_is_ignored(self):
        """``[a].[b].[All]`` names a hierarchy under a different dimension; the
        flat-attribute path requires the self-qualified ``[a].[a]`` form."""
        expr = "DrilldownLevel({[account_type].[channel_name].[All]})"
        assert detect_flat_attribute_rollups("", expr, ATTRS) == []


class TestCoverageInvariant:
    """Property 2 -- the rollup set must cover the whole axis, or the merge
    drops the uncovered dimension's column and the axis loses a hierarchy."""

    def test_drilled_dimension_pulls_in_its_crossjoin_siblings(self):
        """THE live failure. Detecting only ``account_type`` here produced a
        merged result with no ``channel_name`` column, and an Axis1 carrying a
        single HierarchyInfo -- 42 tuples that all looked alike in Excel."""
        expr = f"CrossJoin({DRILL}, {MEMBERS_CHANNEL})"
        assert _names(detect_flat_attribute_rollups("", expr, ATTRS)) == [
            "account_type", "channel_name"]

    def test_sibling_order_does_not_matter(self):
        expr = f"CrossJoin({MEMBERS_CHANNEL}, {DRILL})"
        assert _names(detect_flat_attribute_rollups("", expr, ATTRS)) == [
            "account_type", "channel_name"]

    def test_a_dimension_named_by_both_shapes_is_returned_once(self):
        """``seen`` must dedupe across the two detection passes, or the
        dimension gets two grain plans and the merge double-counts."""
        expr = f"CrossJoin({DRILL}, {MEMBERS_ACCOUNT})"
        assert _names(detect_flat_attribute_rollups("", expr, ATTRS)) == ["account_type"]

    def test_each_axis_is_covered_independently(self):
        """A drilldown on ROWS must not lower the threshold for COLUMNS: the
        column axis is still a lone ambiguous ``.Members`` and stays out."""
        assert _names(detect_flat_attribute_rollups(MEMBERS_CHANNEL, DRILL, ATTRS)) == [
            "account_type"]


class TestExistingBehaviourUnchanged:
    """The Bug-9766 rules the fix must not disturb. A regression in either of
    these is how the previous two attempts at that bug failed."""

    def test_lone_members_dimension_is_still_not_a_rollup(self):
        """The ordinary single-field PivotTable axis. Treating this as a rollup
        is what wrongly refused four working flat-pivot LNE queries."""
        assert detect_flat_attribute_rollups("", MEMBERS_ACCOUNT, ATTRS) == []

    def test_two_members_dimensions_are_still_a_rollup(self):
        expr = f"CrossJoin({MEMBERS_ACCOUNT}, {MEMBERS_CHANNEL})"
        assert _names(detect_flat_attribute_rollups("", expr, ATTRS)) == [
            "account_type", "channel_name"]

    def test_no_axis_expression_detects_nothing(self):
        assert detect_flat_attribute_rollups("", "", ATTRS) == []


class TestRestrictedAxisGetsNoRollup:
    """A DrilldownLevel nested inside a set-RESTRICTING function no longer
    describes the whole member set, so a grand total over the unrestricted
    dimension is not the total of the rows shown.

    Caught live, and introduced by the first version of this fix: the regex
    matches anywhere in the axis expression, so
    ``DrilldownMember(DrilldownLevel({[a].[a].[All]}), {[a].[a].[CREDIT]})``
    produced an All row carrying 36,179,774.10 -- CREDIT's value -- against a
    true grand total of 180,720,566.17. Falling back to no rollup restores the
    pre-fix outcome for these shapes: a MISSING All row, which is a display gap,
    rather than a WRONG one, which is a wrong number reported to the user.
    """

    @pytest.mark.parametrize("wrapper", [
        "DrilldownMember({inner}, {{[account_type].[account_type].[CREDIT]}})",
        "Filter({inner}, [Measures].[base_amount] > 0)",
        "TopCount({inner}, 3, [Measures].[base_amount])",
        "Head({inner}, 2)",
        "Except({inner}, {{[account_type].[account_type].[LOAN]}})",
    ])
    def test_restricted_axis_produces_no_rollup(self, wrapper):
        assert detect_flat_attribute_rollups(
            "", wrapper.format(inner=DRILL), ATTRS) == []

    def test_crossjoin_is_not_treated_as_a_restriction(self):
        """CrossJoin combines dimensions, it does not restrict members -- and it
        is the shape a nested PivotTable actually sends, so treating it as a
        restriction would disable the fix for the main reported case."""
        expr = f"CrossJoin({DRILL}, {MEMBERS_CHANNEL})"
        assert _names(detect_flat_attribute_rollups("", expr, ATTRS)) == [
            "account_type", "channel_name"]

    def test_restriction_on_one_axis_does_not_disable_the_other(self):
        """A Filter on ROWS says nothing about COLUMNS."""
        rows = f"Filter({DRILL}, [Measures].[base_amount] > 0)"
        assert _names(detect_flat_attribute_rollups(DRILL, rows, ATTRS)) == [
            "account_type"]
