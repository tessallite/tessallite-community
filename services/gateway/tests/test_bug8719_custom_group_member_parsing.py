"""Bug-8719 — a custom group must not absorb its own dimension name.

``Aggregate({...})`` custom groups were parsed by collecting bracket bodies and
keeping the deepest one. That pattern cannot cross the ``&`` of a key-form
member: given ``[Product].[Product].&[Widget]`` it matched only
``[Product].[Product]`` and contributed "Product" — the HIERARCHY name — as a
group member, then picked up ``[Widget]`` separately.

So a source member that happens to be named like its own dimension was pulled
into every custom group built from key-form members. The group then aggregates
rows nobody put in it: a wrong number, reported as a legitimate subtotal.

The shape is not rare. The canonical member producer emits the key form for any
reserved value (Bug-8970 / Bug-9789), so Excel sends ``&[...]`` routinely — which
is why this outranks the LOW severity it was first filed under.

The fix reads each whole member reference through ``parse_member_uname``, the
same grammar the rest of the XMLA surface uses, so a dimension or hierarchy name
can never be mistaken for a member value.
"""
from __future__ import annotations

import pytest

from src.dax.mdx_calc_members import (
    _AGGREGATE_MEMBER_REF_RE,
    _aggregate_set_member_name,
    _classify_dim_expression,
)


def _members(member_list: str) -> list[str]:
    return [
        name
        for ref in _AGGREGATE_MEMBER_REF_RE.findall(member_list)
        if (name := _aggregate_set_member_name(ref)) is not None
    ]


class _Calc:
    def __init__(self, expression: str):
        self.expression = expression
        self.calc_type = None
        self.aggregate_members = None


class TestTheDefect:
    def test_key_form_members_do_not_contribute_the_hierarchy_name(self):
        """THE defect: "Product" is the hierarchy, not a member of the group."""
        assert _members(
            "[Product].[Product].&[Widget], [Product].[Product].&[Gadget]"
        ) == ["Widget", "Gadget"]

    def test_a_member_named_like_its_dimension_is_not_absorbed(self):
        """The reported symptom. A real member keyed "Product" belongs to the
        group only when it is actually listed."""
        assert _members("[Product].[Product].&[Widget]") == ["Widget"]
        assert "Product" not in _members("[Product].[Product].&[Widget]")

    def test_the_group_is_built_from_exactly_what_was_listed(self):
        calc = _Calc(
            "Aggregate({[Product].[Product].&[Widget], "
            "[Product].[Product].&[Gadget]})"
        )
        _classify_dim_expression(calc)
        assert calc.calc_type == "aggregate_set"
        assert calc.aggregate_members == ["Widget", "Gadget"]


class TestPreviouslyCorrectReadingsAreUnchanged:
    """The caption form already worked; the fix must not disturb it."""

    def test_caption_form_members(self):
        assert _members(
            "[Product].[Product].[Widget], [Product].[Product].[Gadget]"
        ) == ["Widget", "Gadget"]

    def test_two_part_caption_form(self):
        assert _members("[Product].[Widget]") == ["Widget"]

    def test_a_bare_member_token(self):
        assert _members("[Widget]") == ["Widget"]

    def test_level_qualified_key_form(self):
        assert _members("[Product].[Product].[Category].&[Tools]") == ["Tools"]

    def test_escaped_brackets_are_unescaped_exactly_once(self):
        """Bug-6746: ``]]`` is an MDX-escaped ``]``. The canonical parser already
        unescapes, so the caller must not do it again."""
        assert _members("[Dim].[Hier].&[A]]B]") == ["A]B"]
        assert _members("[Dim].[Hier].[A]]B]") == ["A]B"]

    def test_a_name_that_really_contains_two_brackets_survives(self):
        """The case that proves the unescape happens once and only once.

        A member whose real name is ``A]]B`` is written ``A]]]]B`` in MDX. If the
        caller unescapes again after the parser already did, it collapses to
        ``A]B`` and the group silently matches the wrong member — invisible in
        every example where the unescaped name contains no bracket pair at all.
        """
        assert _members("[Dim].[Hier].&[A]]]]B]") == ["A]]B"]


class TestMixedAndAwkwardSets:
    def test_a_set_mixing_both_grammars(self):
        assert _members(
            "[Product].[Product].[Widget], [Product].[Product].&[Gadget]"
        ) == ["Widget", "Gadget"]

    def test_the_all_member_still_reads_as_all(self):
        """Unchanged for the bare form. For the key form this is an improvement:
        it used to yield ["Product", "All"]."""
        assert _members("[Product].[Product].[All]") == ["All"]
        assert _members("[Product].[Product].&[All]") == ["All"]

    def test_member_names_containing_a_dot(self):
        assert _members("[Product].[Product].&[3.5in Widget]") == ["3.5in Widget"]

    @pytest.mark.parametrize("member_list", ["", "   ", "no brackets here"])
    def test_a_set_with_no_member_references_yields_nothing(self, member_list):
        assert _members(member_list) == []
