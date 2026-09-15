"""Bug-9781 — DISPLAY_INFO is a bit field, not a magic constant.

Per the XMLA specification, a member's DISPLAY_INFO packs three things:

    bits 0-15   CHILDREN_CARDINALITY
    0x10000     DRILLED_DOWN
    0x20000     PARENT_SAME_AS_PREV

The gateway emitted the literal ``131076`` for every member with children and
``0`` for every other member. That constant was copied from OlaPy with no
Tessallite rationale recorded -- the same provenance as the ALL_MEMBER
suppression that turned out to be Bug-9772's root cause. Decoded, ``131076`` is
``0x20004``, which asserted:

  * PARENT_SAME_AS_PREV on EVERY member with children, unconditionally. That is
    necessarily wrong for the first member of each group -- exactly where a
    client decides that a new group, and therefore a subtotal boundary, starts.
  * a hard-coded FOUR children, whatever the real count.
  * DRILLED_DOWN never set, so a client was told no member was expanded even in
    a response carrying that member's children.

No test asserted anything about DISPLAY_INFO before this one, which is why a
value that was wrong on three counts survived. These tests pin the decoded
MEANING of each field rather than the packed integer, so a future change that
alters the encoding has to stay semantically correct rather than merely match a
number.
"""

import re

import pytest

from src.dax.mdx_execute import _member_xml

DRILLED_DOWN = 0x10000
PARENT_SAME_AS_PREV = 0x20000
COUNT_MASK = 0xFFFF


def _display_info(**member):
    member.setdefault("hierarchy", "[Dimensions].[account_type]")
    member.setdefault("uname", "[Dimensions].[account_type].[All]")
    member.setdefault("caption", "All")
    member.setdefault("lname", "[Dimensions].[account_type].[(All)]")
    member.setdefault("lnum", "0")
    xml = _member_xml(member, [])
    return int(re.search(r"<DisplayInfo>(\d+)</DisplayInfo>", xml).group(1))


class TestChildCount:
    @pytest.mark.parametrize("count", [1, 5, 6, 42])
    def test_low_bits_carry_the_real_child_count(self, count):
        di = _display_info(has_children=True, children_cardinality=count)
        assert di & COUNT_MASK == count, (
            f"DISPLAY_INFO reported {di & COUNT_MASK} children for a member "
            f"with {count} -- the pre-fix constant always said 4"
        )

    def test_the_old_constant_is_not_emitted_for_a_five_child_member(self):
        """The exact live case: the All member of account_type has 5 children."""
        assert _display_info(has_children=True, children_cardinality=5) != 131076

    def test_count_is_clamped_to_the_field_width(self):
        """More children than the 16-bit field can hold must not overflow into
        the DRILLED_DOWN / PARENT_SAME_AS_PREV flag bits."""
        di = _display_info(has_children=True, children_cardinality=100_000)
        assert di & COUNT_MASK == 0xFFFF
        assert di & PARENT_SAME_AS_PREV == 0


class TestDrilledDown:
    def test_set_when_the_response_carries_the_member_s_children(self):
        di = _display_info(has_children=True, children_cardinality=5)
        assert di & DRILLED_DOWN, "a member whose children are in this response is drilled down"

    def test_not_set_for_an_expandable_member_with_no_children_present(self):
        di = _display_info(has_children=True, children_cardinality=0)
        assert not di & DRILLED_DOWN
        assert di & COUNT_MASK > 0, (
            "an expandable member must still read as non-leaf, or the client "
            "renders it with no expand control"
        )

    def test_not_set_for_a_leaf(self):
        assert _display_info(has_children=False, children_cardinality=0) == 0


class TestParentSameAsPrev:
    """This flag is a positional hint about the PRECEDING tuple. ``_member_xml``
    sees one member with no cross-tuple context, so it cannot know the answer --
    and omitting an optional hint is truthful where asserting it is not."""

    @pytest.mark.parametrize("cc,hc", [(5, True), (0, True), (0, False)])
    def test_never_asserted_without_the_context_to_justify_it(self, cc, hc):
        di = _display_info(has_children=hc, children_cardinality=cc)
        assert di & PARENT_SAME_AS_PREV == 0


class TestRobustness:
    @pytest.mark.parametrize("bad", [None, "", "abc", {}])
    def test_a_malformed_cardinality_degrades_to_a_leaf_not_a_crash(self, bad):
        di = _display_info(has_children=False, children_cardinality=bad)
        assert di == 0

    def test_a_member_without_the_key_at_all_still_renders(self):
        assert _display_info(has_children=False) == 0
