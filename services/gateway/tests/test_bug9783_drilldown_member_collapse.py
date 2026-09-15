"""Bug-9783 — PivotTable expand/collapse (``DrilldownMember``).

``DrilldownMember(base, targets, [hier])`` returns the base set PLUS, for each
base member also in ``targets``, that member's children in ``[hier]``. Excel
sends it when a field of a nested PivotTable is expanded or collapsed.

The construct had never worked, failing differently at each stage: silently
absent (Excel did not know an All member existed, pre-Bug-9772), then a hard
query error (Bug-9779), then INVERTED -- the drill target was extracted as a row
filter, so collapsing CREDIT produced ``WHERE account_type = 'CREDIT'`` and the
response hid the SIBLINGS and kept the children.

That inversion is worth remembering: the wrong response was ARITHMETICALLY
PERFECT. CREDIT's seven channel values summed to exactly its true total, and
that reconciliation was mistaken for a correct answer. A sum that reconciles
proves the aggregation is sound; it says nothing about whether the requested
SHAPE came back. Every test here therefore asserts SHAPE (which members carry
detail rows) alongside value.

Design: the required result is a MIXED GRAIN -- a rollup row for every outer
member, detail rows for drilled members only -- which the existing grain-query
and merge machinery already produces. So this adds a detector and a post-merge
filter, and no new engine. The filter runs AFTER the merge rather than as a
WHERE on the grain query, deliberately: the defect being fixed was a member list
wrongly reaching the SQL, so the member list stays out of the SQL.
"""

import pytest

from src.dax.subtotal_engine import (
    SUBTOTAL_GRAIN_PREFIX,
    apply_drilldown_member_plan,
    detect_drilldown_member_plan,
    detect_flat_attribute_rollups,
)

ATTRS = {"account_type", "channel_name", "auth_method"}

BASE = ("CrossJoin({[account_type].[account_type].[All],"
        "[account_type].[account_type].[account_type].Members}, "
        "{([channel_name].[channel_name].[All])})")


def _stmt(target):
    return (f"Hierarchize(DrilldownMember({BASE}, {target}, "
            "[channel_name].[channel_name]))")


COLLAPSE = _stmt("{-{[account_type].[account_type].[account_type].&[CREDIT]}}")
EXPAND = _stmt("{[account_type].[account_type].[account_type].&[CREDIT]}")


class TestDetection:
    def test_collapse_is_read_as_a_complement(self):
        """``{-{X}}`` is set difference with an empty left side: every base
        member EXCEPT X. This is the reading the user's report requires --
        "it hides the siplings not the childs" -- so collapsing one member must
        leave its siblings expanded."""
        plan = detect_drilldown_member_plan("", COLLAPSE, ATTRS)
        assert plan is not None
        assert plan.complement is True
        assert plan.members == ["CREDIT"]
        assert plan.inner.hierarchy_name == "channel_name"
        assert plan.outer_dim == "account_type"

    def test_expand_is_read_as_a_positive_target(self):
        plan = detect_drilldown_member_plan("", EXPAND, ATTRS)
        assert plan is not None
        assert plan.complement is False
        assert plan.members == ["CREDIT"]

    @pytest.mark.parametrize("key", [" A ", "(A)", "((A))", "A]B"])
    def test_bug_9806_target_key_is_preserved(self, key):
        escaped = key.replace("]", "]]" )
        plan = detect_drilldown_member_plan(
            "", _stmt(
                "{[account_type].[account_type].&[" + escaped + "]}"
            ), ATTRS,
        )
        assert plan is not None
        assert plan.members == [key]

    def test_collapse_drills_every_sibling_and_not_the_target(self):
        plan = detect_drilldown_member_plan("", COLLAPSE, ATTRS)
        assert plan.is_drilled("CREDIT") is False
        for sibling in ("CURRENT", "LOAN", "SAVINGS", "WALLET"):
            assert plan.is_drilled(sibling) is True

    def test_expand_drills_only_the_target(self):
        plan = detect_drilldown_member_plan("", EXPAND, ATTRS)
        assert plan.is_drilled("CREDIT") is True
        assert plan.is_drilled("CURRENT") is False

    @pytest.mark.parametrize("value", [None, ""])
    def test_the_outer_all_member_is_never_drilled(self, value):
        """An All-grain row carries no outer value. Expanding the grand total
        into one row per inner member adds rows the client never displays."""
        plan = detect_drilldown_member_plan("", COLLAPSE, ATTRS)
        assert plan.is_drilled(value) is False

    def test_both_dimensions_are_rolled_up(self):
        """COVERAGE INVARIANT. Rolling up only the inner dimension drops the
        outer one from the merged result and the axis renders without it -- an
        axis of 8 channel tuples against 33 cells, observed live. The outer IS
        genuinely a rollup: the base set names its All member."""
        plan = detect_drilldown_member_plan("", COLLAPSE, ATTRS)
        assert [r.hierarchy_name for r in plan.rollups] == [
            "account_type", "channel_name"]


class TestDetectionDeclinesWhatItCannotBeSureOf:
    """Returning None leaves every existing path exactly as it was, so an
    unrecognised shape degrades to the previous no-op, never to a wrong set."""

    def test_a_two_argument_call_is_not_guessed(self):
        """Without the third argument the inner hierarchy is ambiguous."""
        assert detect_drilldown_member_plan(
            "", "DrilldownMember({A}, {[account_type].[account_type].&[X]})",
            ATTRS) is None

    def test_an_unknown_inner_dimension_is_declined(self):
        assert detect_drilldown_member_plan(
            "", _stmt("{[account_type].[account_type].&[X]}").replace(
                "[channel_name].[channel_name]))", "[nope].[nope]))"),
            ATTRS) is None

    def test_an_empty_target_set_is_declined(self):
        assert detect_drilldown_member_plan("", _stmt("{}"), ATTRS) is None

    def test_targets_spanning_two_outer_dimensions_are_declined(self):
        target = ("{[account_type].[account_type].&[A],"
                  "[auth_method].[auth_method].&[B]}")
        assert detect_drilldown_member_plan("", _stmt(target), ATTRS) is None

    def test_a_statement_without_drilldownmember_is_declined(self):
        assert detect_drilldown_member_plan(
            "", "CrossJoin({[a].[a].Members},{[b].[b].Members})", ATTRS) is None

    def test_unbalanced_input_is_declined(self):
        assert detect_drilldown_member_plan(
            "", "DrilldownMember({A}, {[account_type].[account_type].&[X]",
            ATTRS) is None

    def test_inner_equal_to_outer_is_declined(self):
        target = "{[channel_name].[channel_name].&[X]}"
        assert detect_drilldown_member_plan("", _stmt(target), ATTRS) is None


class TestPostMergeFilter:
    """SHAPE, not just values — the inversion had perfect values."""

    def _rows(self):
        out = []
        for acct in (None, "CREDIT", "CURRENT"):
            out.append({"account_type": acct, "channel_name": None,
                        SUBTOTAL_GRAIN_PREFIX + "channel_name": -1})
            for ch in ("API", "ATM"):
                out.append({"account_type": acct, "channel_name": ch,
                            SUBTOTAL_GRAIN_PREFIX + "channel_name": 0})
        return out

    def test_collapsed_member_keeps_its_rollup_and_loses_its_detail(self):
        plan = detect_drilldown_member_plan("", COLLAPSE, ATTRS)
        kept = apply_drilldown_member_plan(self._rows(), plan)
        credit = [r for r in kept if r["account_type"] == "CREDIT"]
        assert len(credit) == 1, "the collapsed member must keep exactly its own total"
        assert credit[0][SUBTOTAL_GRAIN_PREFIX + "channel_name"] == -1

    def test_siblings_keep_their_detail(self):
        plan = detect_drilldown_member_plan("", COLLAPSE, ATTRS)
        kept = apply_drilldown_member_plan(self._rows(), plan)
        current = [r for r in kept if r["account_type"] == "CURRENT"]
        assert len(current) == 3  # rollup + API + ATM

    def test_the_grand_total_row_survives_but_is_not_expanded(self):
        plan = detect_drilldown_member_plan("", COLLAPSE, ATTRS)
        kept = apply_drilldown_member_plan(self._rows(), plan)
        grand = [r for r in kept if r["account_type"] is None]
        assert len(grand) == 1
        assert grand[0][SUBTOTAL_GRAIN_PREFIX + "channel_name"] == -1

    def test_expand_is_the_mirror_image(self):
        plan = detect_drilldown_member_plan("", EXPAND, ATTRS)
        kept = apply_drilldown_member_plan(self._rows(), plan)
        assert len([r for r in kept if r["account_type"] == "CREDIT"]) == 3
        assert len([r for r in kept if r["account_type"] == "CURRENT"]) == 1

    def test_no_rollup_row_is_ever_dropped(self):
        """Every outer member must keep its own total whatever its drill state,
        or a collapsed member vanishes from the PivotTable entirely."""
        for stmt in (COLLAPSE, EXPAND):
            plan = detect_drilldown_member_plan("", stmt, ATTRS)
            kept = apply_drilldown_member_plan(self._rows(), plan)
            rollups = [r for r in kept
                       if r[SUBTOTAL_GRAIN_PREFIX + "channel_name"] == -1]
            assert len(rollups) == 3


class TestExistingDetectorsUnaffected:
    def test_the_flat_attribute_detector_ignores_this_shape(self):
        """The two detectors must not both claim the same axis; this one has no
        bare `.Members` pair, so the Bug-9766/9632 rule correctly declines it."""
        assert detect_flat_attribute_rollups("", COLLAPSE, ATTRS) == []

    def test_a_plain_crossjoin_is_still_owned_by_the_flat_detector(self):
        expr = ("CrossJoin({[account_type].[account_type].Members}, "
                "{[channel_name].[channel_name].Members})")
        assert detect_drilldown_member_plan("", expr, ATTRS) is None
        assert len(detect_flat_attribute_rollups("", expr, ATTRS)) == 2
