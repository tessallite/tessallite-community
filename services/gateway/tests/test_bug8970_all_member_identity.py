"""Bug-8970 — a data member named "All" must not collide with the (All) member.

In the level-less three-part grammar, a caption member whose value happens to be
"All" is written exactly like the synthetic hierarchy total: both render as
``[Dim].[Hier].[All]``. A client asking for one gets the other — a real row
value silently answered with a grand total, or the reverse.

Decided 2026-09-01 (r7 contract Q1): emit a KEY-BEARING identity
``[Dim].[Hier].&[All]`` for the reserved values ONLY, keep ordinary flat member
identities stable, and keep accepting the unambiguous historical inbound forms.

The parser half of this contract is exercised across the drill-through, MDX and
production-path suites. The PRODUCER half — the part that actually removes the
collision — had no test at all: ``canonical_member_uname`` was never called
directly by any test in this suite, so nothing pinned that a member named "All"
comes out in key form. That is what this file adds.
"""
from __future__ import annotations

import pytest

from src.dax.member_uname import (
    canonical_member_uname,
    is_all_member_token,
    parse_member_uname,
    synthetic_all_member_uname,
)

HIER = "[status].[status]"


def _emit(key: str) -> str:
    return canonical_member_uname(HIER, "status", [key], is_multi_level=False)


def _grammar(uname: str) -> str:
    return parse_member_uname(uname)[2]


class TestReservedValuesGetAKeyBearingIdentity:
    @pytest.mark.parametrize("key", ["All", "all", "ALL", "(All)", "(all)"])
    def test_a_member_named_all_is_emitted_in_key_form(self, key):
        """THE collision. Emitted bare, this member would be indistinguishable
        from the hierarchy total."""
        emitted = _emit(key)
        assert emitted.startswith(f"{HIER}.&["), (
            f"a data member keyed {key!r} was emitted as {emitted!r}, which "
            f"reads as the synthetic All member"
        )
        assert _grammar(emitted) == "key"

    def test_the_synthetic_all_member_is_not_key_borne(self):
        """The other side of the same contract: the hierarchy total keeps the
        bare form, so the two are structurally different strings."""
        synthetic = synthetic_all_member_uname(HIER)
        assert ".&[" not in synthetic
        assert _grammar(synthetic) == "all"

    def test_the_two_are_distinguishable(self):
        """Stated directly, because this is the whole defect."""
        assert _emit("All") != synthetic_all_member_uname(HIER)
        assert _grammar(_emit("All")) != _grammar(synthetic_all_member_uname(HIER))


class TestOrdinaryIdentitiesStayStable:
    """"Reserved values ONLY" — the fix must not churn every other member's
    identity, which would invalidate saved client state for no reason."""

    @pytest.mark.parametrize("key", ["EMEA", "Allocated", "Overall", "小計"])
    def test_an_ordinary_member_keeps_the_caption_form(self, key):
        emitted = _emit(key)
        assert emitted == f"{HIER}.[{key}]"
        assert _grammar(emitted) == "caption"

    def test_a_name_merely_containing_all_is_not_reserved(self):
        """``Allocated`` starts with "All" and must not be treated as reserved."""
        assert ".&[" not in _emit("Allocated")


class TestHistoricalInboundFormsStillParse:
    """Third half of the decision: existing clients keep working."""

    @pytest.mark.parametrize("uname", [f"{HIER}.[All]", f"{HIER}.[(All)]"])
    def test_bare_all_forms_still_read_as_the_hierarchy_total(self, uname):
        assert _grammar(uname) == "all"

    def test_the_key_form_reads_as_a_data_member(self):
        assert parse_member_uname(f"{HIER}.&[All]") == (HIER, None, "key", ["All"])

    def test_the_parser_and_the_recogniser_share_one_case_rule(self):
        """Bug-9854. ``parse_member_uname`` used to recognise only the exact
        spellings ``All`` and ``(All)`` while ``is_all_member_token`` was
        case-insensitive (Excel and Power BI lower-case these, Bug-5519 round 2),
        so a lower-cased ``[all]`` reached the parser as an ordinary caption
        member that does not exist. One rule now governs both, and a real key
        spelled ``all`` stays a data member because producers emit it in the key
        grammar."""
        assert _grammar(f"{HIER}.[all]") == "all"
        assert _grammar(f"{HIER}.[(ALL)]") == "all"
        assert parse_member_uname(f"{HIER}.&[all]") == (HIER, None, "key", ["all"])
        assert is_all_member_token("all") is True
        assert is_all_member_token("All") is True

    @pytest.mark.parametrize("token", ["All)", "(All", "((All))", "Allocated"])
    def test_near_miss_spellings_are_not_the_grand_total(self, token):
        """sol F-CR-01: an earlier recogniser used ``strip("()")`` and classified
        these as the total. A consumer that drops a filter for an ordinary
        caption widens the result set silently."""
        assert is_all_member_token(token) is False


class TestProducerConsumerRoundTrip:
    """What the producer emits, the parser must classify back the same way —
    the contract is only useful if both halves agree."""

    @pytest.mark.parametrize("key", ["All", "(All)", "EMEA", "Allocated"])
    def test_emitted_identities_parse_back_to_the_same_key(self, key):
        emitted = _emit(key)
        _hier, _level, grammar, keys = parse_member_uname(emitted)
        if grammar == "key":
            assert keys == [key]
        else:
            assert grammar == "caption" and keys == [key]
