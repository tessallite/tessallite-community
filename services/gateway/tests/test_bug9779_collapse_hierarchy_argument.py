"""Bug-9779 — collapsing a nested PivotTable field must not be refused.

Collapsing a field in Excel produced a query error on every attempt. The gateway
logged:

    Execute translation failed: Axis member [channel_name] could not be applied
    as a filter; refusing to run the query unfiltered.

The refusal comes from ``_assert_axis_member_references_applied``, a deliberate
FAIL-CLOSED guard: every enumerated member reference on an axis must have become
a WHERE filter, or the query is refused rather than run over more data than the
client asked for. That posture is correct and is unchanged here.

The gap was in what counts as a "member reference". Excel's collapse statement,
captured verbatim from the gateway log, ends with a bare hierarchy:

    Hierarchize(DrilldownMember(
        CrossJoin({[account_type].[account_type].[All],
                   [account_type].[account_type].[account_type].Members},
                  {([channel_name].[channel_name].[All])}),
        {-{[account_type].[account_type].[account_type].&[CREDIT]}},
        [channel_name].[channel_name]))

That last argument names the HIERARCHY to drill. It restricts nothing, so it can
never be "applied as a filter" -- the audit was refusing a query that was never
unsafe.

Two properties are pinned, and the second is why the first is not enough:

1. A bare self-qualified hierarchy ARGUMENT is exempt. ``_normalize_wire_mdx``
   rewrites the wire name ``[Dimensions].[X]`` into ``[X].[X]`` before any of
   this runs, so in a normalised statement a repeated name denotes a hierarchy by
   construction; a real member is ``[X].[X].[value]`` or ``[X].&[key]``.

2. The repeated name alone is NOT sufficient. ``[X].[X].CurrentMember`` is the
   same hierarchy form followed by a member NAVIGATION, and that expression does
   denote a member. The first version of this fix exempted it and was caught by
   the existing Bug-8925 guard -- an untranslated ``.CurrentMember`` would have
   run unfiltered. A bare hierarchy argument is never followed by a dot.
"""

import pytest

from src.dax.xmla_server import (
    _assert_axis_member_references_applied,
    _drill_target_spans,
    _iter_member_references,
    _mdx_extract_axis_member_filters,
)

# Excel's real collapse statement, captured from the [XMLA-EXEC] log on ALEX.
COLLAPSE_AXIS = (
    "Hierarchize(DrilldownMember(CrossJoin("
    "{[account_type].[account_type].[All],"
    "[account_type].[account_type].[account_type].Members}, "
    "{([channel_name].[channel_name].[All])}), "
    "{-{[account_type].[account_type].[account_type].&[CREDIT]}}, "
    "[channel_name].[channel_name]))"
)


def _refs(expr):
    return [r[4] for r in _iter_member_references(expr, exclude_level_expansions=True)]


class TestExcelCollapseIsAccepted:
    def test_the_captured_collapse_statement_no_longer_raises(self):
        """The end-to-end property the user reported: collapse must not error."""
        _assert_axis_member_references_applied(
            COLLAPSE_AXIS, {}, {"account_type", "channel_name"},
            translated_label_filters=[],
        )

    def test_the_hierarchy_argument_is_not_treated_as_a_member(self):
        assert "[channel_name]" not in _refs(COLLAPSE_AXIS), (
            "the DrilldownMember hierarchy argument was enumerated as a member "
            "reference; it restricts nothing and can never be applied as a filter"
        )

    @pytest.mark.parametrize("expr", [
        "DrilldownMember(X, Y, [channel_name].[channel_name])",
        "DrilldownLevel({[a].[a].[All]}, , [a].[a])",
        "Hierarchize({[x].[x]})",
        "CrossJoin([a].[a], [b].[b])",
    ])
    def test_bare_hierarchy_arguments_are_exempt_wherever_they_appear(self, expr):
        assert _refs(expr) == []


class TestTheExemptionStaysNarrow:
    """Property 2 — what must still be rejected. Each of these would be a real
    unfiltered member reference slipping past a fail-closed guard."""

    @pytest.mark.parametrize("nav", [
        "CurrentMember", "DefaultMember", "FirstChild", "LastChild",
        "PrevMember", "NextMember", "Parent",
    ])
    def test_a_member_navigation_on_the_hierarchy_is_still_a_member(self, nav):
        assert _refs(f"[region].[region].{nav}") == ["[region]"], (
            f"[region].[region].{nav} resolves to a MEMBER and must stay audited"
        )

    def test_an_enumerated_member_is_still_a_member(self):
        assert _refs("{[region].[region].[EMEA]}") != []

    def test_a_key_qualified_member_is_still_a_member(self):
        assert _refs("{[region].[region].&[EMEA]}") != []

    def test_a_differently_named_two_part_reference_is_still_a_member(self):
        """`[account_type].[CREDIT]` is the attribute member form the pattern
        exists for; only a REPEATED name means 'hierarchy'."""
        assert _refs("{[account_type].[CREDIT]}") == ["[account_type]"]

    def test_an_unapplied_member_alongside_the_collapse_shape_still_raises(self):
        """The exemption must not become a blanket pass for the whole statement."""
        with pytest.raises(ValueError, match="could not be applied as a filter"):
            _assert_axis_member_references_applied(
                COLLAPSE_AXIS + " {[region].[region].[EMEA]}",
                {}, {"account_type", "channel_name", "region"},
                translated_label_filters=[],
            )


class TestWherePathUnchanged:
    """The iterator is shared with the WHERE audit, and Bug-1060 exists because
    an axis exemption once leaked into that path. The exemption is gated on the
    axis flag, so the WHERE path must be bit-for-bit unaffected."""

    @pytest.mark.parametrize("expr", [
        "[channel_name].[channel_name]",
        "DrilldownMember(X, Y, [channel_name].[channel_name])",
    ])
    def test_where_path_still_enumerates_the_bare_hierarchy(self, expr):
        refs = [r[4] for r in _iter_member_references(expr, exclude_level_expansions=False)]
        assert refs == ["[channel_name]"], (
            "the WHERE audit must keep its strict behaviour — a bare hierarchy "
            "there denotes its default member, a separate question needing its "
            "own evidence"
        )


class TestDrillTargetSpans:
    """The collapse statement carries a SECOND blocker: the drill target
    ``{-{[account_type].[account_type].[account_type].&[CREDIT]}}``. That names
    which member changes drill state, not which rows the axis contains.

    Exempting it is safe in the direction the audit cares about: a drill target
    is drawn from the set in the FIRST argument, which is audited in full, so it
    can only narrow or deepen an already-checked set -- never widen it.
    """

    def test_the_excel_collapse_target_is_located_exactly(self):
        spans = _drill_target_spans(COLLAPSE_AXIS)
        assert len(spans) == 1
        assert COLLAPSE_AXIS[spans[0][0]:spans[0][1]].strip() == (
            "{-{[account_type].[account_type].[account_type].&[CREDIT]}}")

    def test_the_first_argument_is_never_exempted(self):
        """The audited set must stay audited — only argument TWO is exempt."""
        s, e = _drill_target_spans(COLLAPSE_AXIS)[0]
        assert "CrossJoin" not in COLLAPSE_AXIS[s:e]
        assert "[channel_name].[channel_name].[All]" not in COLLAPSE_AXIS[s:e]

    def test_nested_drill_calls_each_yield_their_own_target(self):
        expr = ("DrilldownMember(DrilldownMember({A},{[b].[b].&[Y]}), "
                "{[a].[a].&[X]}, [h].[h])")
        found = {expr[s:e].strip() for s, e in _drill_target_spans(expr)}
        assert found == {"{[a].[a].&[X]}", "{[b].[b].&[Y]}"}

    def test_a_two_argument_call_still_yields_its_target(self):
        expr = "DrilldownMember({A}, {[a].[a].&[X]})"
        assert [expr[s:e].strip() for s, e in _drill_target_spans(expr)] == [
            "{[a].[a].&[X]}"]

    @pytest.mark.parametrize("expr", [
        "DrilldownMember({A}, {[a].[a].&[X]",        # truncated
        "DrilldownMember({A}",                       # no second argument
        "Filter({A}, {[a].[a].&[X]})",               # not a drill function
        "Except({A}, {[a].[a].&[X]})",
        "",
    ])
    def test_unparseable_or_unrelated_input_exempts_nothing(self, expr):
        """FAIL CLOSED. A statement this cannot parse must stay fully audited —
        in a fail-closed guard, exempting the wrong span means letting through a
        member that SHOULD have been filtered."""
        assert _drill_target_spans(expr) == []

    def test_a_drill_target_outside_a_drill_call_is_still_audited(self):
        """Same member text, no enclosing drill call — must NOT be exempt."""
        expr = "{-{[account_type].[account_type].[account_type].&[CREDIT]}}"
        assert _drill_target_spans(expr) == []
        assert _refs(expr) != []


class TestDrillTargetIsNotAFilter:
    """Bug-9783 — the drill target must not reach the WHERE clause.

    Extracting it as a filter INVERTED the collapse: Excel's collapse of CREDIT
    produced `WHERE account_type = 'CREDIT'`, so the response hid the SIBLINGS
    and kept the children. The user's words: "it hides the siblings not the
    childs".

    The inverted response was arithmetically perfect -- CREDIT's 7 channel values
    summed to exactly its true total -- which is why a numbers check did not
    catch it. A sum that reconciles proves the aggregation is sound; it says
    nothing about whether the requested SHAPE was returned.
    """

    def test_the_collapse_target_produces_no_filter(self):
        filters = _mdx_extract_axis_member_filters(
            COLLAPSE_AXIS, {"account_type", "channel_name"})
        assert not filters.get("account_type"), (
            "the drill target became a row filter; that inverts a collapse into "
            "'show only this member'"
        )

    def test_a_genuine_enumerated_member_still_filters(self):
        """The positive control: the masking must not disable real filtering."""
        expr = "{[account_type].[account_type].&[CREDIT]}"
        filters = _mdx_extract_axis_member_filters(expr, {"account_type"})
        assert filters.get("account_type") == ["CREDIT"]

    def test_an_enumerated_member_outside_the_drill_target_still_filters(self):
        """Masking is span-scoped: a member elsewhere in the SAME statement is
        untouched, so the exemption cannot become a blanket pass."""
        expr = COLLAPSE_AXIS + " {[region].[region].&[EMEA]}"
        filters = _mdx_extract_axis_member_filters(
            expr, {"account_type", "channel_name", "region"})
        assert filters.get("region") == ["EMEA"]
        assert not filters.get("account_type")

    def test_masking_preserves_offsets(self):
        """Spans other callers computed against the original text must stay
        valid, so the mask replaces characters rather than removing them."""
        expr = COLLAPSE_AXIS + " {[region].[region].&[EMEA]}"
        filters = _mdx_extract_axis_member_filters(
            expr, {"account_type", "channel_name", "region"})
        assert filters.get("region") == ["EMEA"]
