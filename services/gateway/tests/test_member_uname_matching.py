"""Bug-3617 (Phase 1): the full member-unique-name parser + dual-grammar matcher.

``parse_member_uname`` decomposes every MEMBER_UNIQUE_NAME grammar;
``member_filter_matches`` compares an inbound restriction against a candidate
member, accepting BOTH the canonical key form and the legacy caption form.
"""
from __future__ import annotations

import pytest

from src.dax.member_uname import member_filter_matches, parse_member_uname

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# parse_member_uname
# --------------------------------------------------------------------------


def test_parse_measure():
    assert parse_member_uname("[Measures].[fee_amount]") == (
        "[Measures]", None, "measure", ["fee_amount"],
    )


def test_parse_canonical_full_path():
    assert parse_member_uname("[D].[H].[Month].&[2025]&[4]") == (
        "[D].[H]", "Month", "key", ["2025", "4"],
    )


def test_parse_canonical_single_key():
    assert parse_member_uname("[D].[H].[Year].&[2025]") == (
        "[D].[H]", "Year", "key", ["2025"],
    )


def test_parse_caption_form():
    assert parse_member_uname("[D].[H].[4]") == (
        "[D].[H]", None, "caption", ["4"],
    )


def test_parse_all_member_both_forms():
    assert parse_member_uname("[D].[H].[All]") == ("[D].[H]", None, "all", [])
    assert parse_member_uname("[D].[H].[(All)]") == ("[D].[H]", None, "all", [])


def test_parse_escaped_key():
    # ``]]`` in a key is unescaped to a single ``]``.
    hier, level, grammar, path = parse_member_uname("[D].[H].[Lvl].&[a]]b]")
    assert grammar == "key"
    assert path == ["a]b"]


def test_parse_invalid():
    assert parse_member_uname("garbage")[2] == "invalid"
    assert parse_member_uname("")[2] == "invalid"
    assert parse_member_uname(None)[2] == "invalid"


# --------------------------------------------------------------------------
# member_filter_matches — canonical input requires exact full-path equality
# --------------------------------------------------------------------------


def test_canonical_full_path_disambiguates_2025_vs_2026():
    # Inbound canonical month-4-of-2025 matches the 2025 candidate only.
    f = "[D].[H].[Month].&[2025]&[4]"
    assert member_filter_matches(
        f, candidate_hier_bracket="[D].[H]", candidate_level_name="Month",
        candidate_key_path=["2025", "4"],
    )
    assert not member_filter_matches(
        f, candidate_hier_bracket="[D].[H]", candidate_level_name="Month",
        candidate_key_path=["2026", "4"],
    )


def test_canonical_rejects_wrong_hierarchy():
    assert not member_filter_matches(
        "[D].[H].[Month].&[2025]&[4]",
        candidate_hier_bracket="[X].[Y]", candidate_level_name="Month",
        candidate_key_path=["2025", "4"],
    )


def test_canonical_rejects_level_mismatch():
    assert not member_filter_matches(
        "[D].[H].[Month].&[2025]&[4]",
        candidate_hier_bracket="[D].[H]", candidate_level_name="Day",
        candidate_key_path=["2025", "4"],
    )


# --------------------------------------------------------------------------
# member_filter_matches — caption input is the legacy ambiguous fallback
# --------------------------------------------------------------------------


def test_caption_matches_any_same_caption_member():
    # Legacy caption form "[D].[H].[4]" matches BOTH month-4s (ambiguous, by design).
    f = "[D].[H].[4]"
    assert member_filter_matches(
        f, candidate_hier_bracket="[D].[H]", candidate_level_name="Month",
        candidate_key_path=["2025", "4"], candidate_caption="4",
    )
    assert member_filter_matches(
        f, candidate_hier_bracket="[D].[H]", candidate_level_name="Month",
        candidate_key_path=["2026", "4"], candidate_caption="4",
    )


def test_caption_matches_on_deepest_key_when_no_caption_supplied():
    assert member_filter_matches(
        "[D].[H].[4]", candidate_hier_bracket="[D].[H]",
        candidate_level_name="Month", candidate_key_path=["2025", "4"],
    )


def test_caption_matches_distinct_caption_vs_key():
    # caption "April" matches a candidate whose caption is April though key is 4.
    assert member_filter_matches(
        "[D].[H].[April]", candidate_hier_bracket="[D].[H]",
        candidate_level_name="Month", candidate_key_path=["2025", "4"],
        candidate_caption="April",
    )


def test_caption_no_match_when_neither_caption_nor_key():
    assert not member_filter_matches(
        "[D].[H].[99]", candidate_hier_bracket="[D].[H]",
        candidate_level_name="Month", candidate_key_path=["2025", "4"],
        candidate_caption="4",
    )


# --------------------------------------------------------------------------
# non-matching grammars
# --------------------------------------------------------------------------


def test_all_and_measure_and_empty_do_not_match_data_candidates():
    for f in ("[D].[H].[All]", "[Measures].[x]", "", None):
        assert not member_filter_matches(
            f, candidate_hier_bracket="[D].[H]", candidate_level_name="Month",
            candidate_key_path=["2025", "4"], candidate_caption="4",
        )
