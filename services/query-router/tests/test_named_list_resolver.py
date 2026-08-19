"""Unit tests for named list resolution (sql_fixed named lists on the SQL path).

Covers ``src/params/named_list_resolver.py``:
  - Injection safety: members with quotes, semicolons, control chars.
  - Usage-shape whitelist: ``IN``, ``NOT IN`` accepted; ``=``, bare rejected.
  - Size caps: at-limit accepted, over-limit rejected.
  - Name collision: parameter vs named list (400).
  - MDX-type set in SQL query (specific 400).
  - Empty list rejection.
  - Case-insensitive name matching.
  - String vs number type rendering.
  - Multiple lists in one query.

All tests are deterministic and require no live database.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
import sqlglot

from src.params.named_list_resolver import (
    _ResolvedList,
    _check_in_context,
    _extract_lists_from_snapshot,
    _is_safe_number,
    _render_members,
    expand_named_lists,
    invalidate_named_list_cache,
)
from src.params.resolver import ParameterError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_list(
    name: str = "TopChannels",
    data_type: str = "string",
    members: list | None = None,
    list_type: str = "sql_fixed",
    builder_type: str = "fixedMembers",
) -> _ResolvedList:
    return _ResolvedList(
        name=name,
        data_type=data_type,
        members=members if members is not None else ["online", "retail"],
        list_type=list_type,
        builder_type=builder_type,
    )


def _lists_dict(*lists: _ResolvedList) -> dict[str, _ResolvedList]:
    """Build the lowercase-keyed dict that expand_named_lists expects."""
    return {f"@{nl.name}".lower(): nl for nl in lists}


# ---------------------------------------------------------------------------
# _render_members — typed literal rendering
# ---------------------------------------------------------------------------

class TestRenderMembers:
    def test_string_members_are_quoted(self):
        out = _render_members(["online", "retail"], "string", "postgres")
        assert out == "'online', 'retail'"

    def test_number_members_are_bare(self):
        out = _render_members([10, 20, 30], "number", "postgres")
        assert out == "10, 20, 30"

    def test_float_number_members(self):
        out = _render_members([1.5, 2.75], "number", "postgres")
        assert out == "1.5, 2.75"

    def test_string_with_single_quote_is_escaped(self):
        out = _render_members(["O'Brien"], "string", "postgres")
        assert out == "'O''Brien'"

    def test_string_with_semicolon_is_safe(self):
        out = _render_members(["a;DROP TABLE x"], "string", "postgres")
        # The semicolon must be inside the literal, not breaking out.
        assert "'" in out
        assert "a;DROP TABLE x" in out.replace("'", "")

    def test_string_with_backslash_is_safe(self):
        out = _render_members(["path\\to\\file"], "string", "postgres")
        assert "'" in out

    def test_empty_string_member(self):
        out = _render_members([""], "string", "postgres")
        assert out == "''"

    def test_inf_number_member_rejected(self):
        """float('inf') in a number-typed list must raise, not splice bare."""
        with pytest.raises(ParameterError, match="non-numeric"):
            _render_members([float("inf")], "number", "postgres", "L")

    def test_nan_number_member_rejected(self):
        """float('nan') in a number-typed list must raise."""
        with pytest.raises(ParameterError, match="non-numeric"):
            _render_members([float("nan")], "number", "postgres", "L")

    def test_inf_string_number_member_rejected(self):
        """String 'inf' in a number-typed list must raise."""
        with pytest.raises(ParameterError, match="non-numeric"):
            _render_members(["inf"], "number", "postgres", "L")


# ---------------------------------------------------------------------------
# _check_in_context — usage-shape whitelist
# ---------------------------------------------------------------------------

class TestCheckInContext:
    def test_in_paren_accepted(self):
        sql = "SELECT * FROM t WHERE channel IN (@TopChannels)"
        # Find the offset of @TopChannels
        idx = sql.index("@TopChannels")
        assert _check_in_context(sql, idx, "postgres") is True

    def test_not_in_paren_accepted(self):
        sql = "SELECT * FROM t WHERE channel NOT IN (@TopChannels)"
        idx = sql.index("@TopChannels")
        assert _check_in_context(sql, idx, "postgres") is True

    def test_equals_rejected(self):
        sql = "SELECT * FROM t WHERE channel = @TopChannels"
        idx = sql.index("@TopChannels")
        assert _check_in_context(sql, idx, "postgres") is False

    def test_bare_select_rejected(self):
        sql = "SELECT @TopChannels FROM t"
        idx = sql.index("@TopChannels")
        assert _check_in_context(sql, idx, "postgres") is False

    def test_greater_than_rejected(self):
        sql = "SELECT * FROM t WHERE amount > @TopChannels"
        idx = sql.index("@TopChannels")
        assert _check_in_context(sql, idx, "postgres") is False

    def test_in_without_parens_rejected(self):
        """``IN @L`` without parentheses is not a valid usage shape."""
        sql = "SELECT * FROM t WHERE ch IN @TopChannels"
        idx = sql.index("@TopChannels")
        assert _check_in_context(sql, idx, "postgres") is False

    def test_like_rejected(self):
        sql = "SELECT * FROM t WHERE ch LIKE @TopChannels"
        idx = sql.index("@TopChannels")
        assert _check_in_context(sql, idx, "postgres") is False

    def test_between_rejected(self):
        sql = "SELECT * FROM t WHERE ch BETWEEN @TopChannels AND 100"
        idx = sql.index("@TopChannels")
        assert _check_in_context(sql, idx, "postgres") is False

    def test_trailing_position_in_list_rejected(self):
        """``IN ('x', @List)`` — placeholder not in first position.

        Known limitation: the usage-shape whitelist requires @Name to be
        immediately after the opening L_PAREN. Mixed-position lists like
        ``IN ('extra', @List)`` are rejected. This is by design (spec 5.3).
        """
        sql = "SELECT * FROM t WHERE ch IN ('x', @TopChannels)"
        idx = sql.index("@TopChannels")
        assert _check_in_context(sql, idx, "postgres") is False

    def test_nested_parens_rejected(self):
        """``IN ((@List))`` — redundant parentheses rejected."""
        sql = "SELECT * FROM t WHERE ch IN ((@TopChannels))"
        idx = sql.index("@TopChannels")
        assert _check_in_context(sql, idx, "postgres") is False


# ---------------------------------------------------------------------------
# _is_safe_number — defense-in-depth for F-029-01
# ---------------------------------------------------------------------------

class TestIsSafeNumber:
    def test_int_is_safe(self):
        assert _is_safe_number(42) is True

    def test_float_is_safe(self):
        assert _is_safe_number(3.14) is True

    def test_numeric_string_is_safe(self):
        assert _is_safe_number("100") is True
        assert _is_safe_number("3.14") is True
        assert _is_safe_number("-42") is True

    def test_bool_is_not_safe(self):
        """Boolean is int subclass but must not pass as number."""
        assert _is_safe_number(True) is False
        assert _is_safe_number(False) is False

    def test_non_numeric_string_is_not_safe(self):
        assert _is_safe_number("abc") is False
        assert _is_safe_number("1) OR (1=1") is False
        assert _is_safe_number("'; DROP TABLE x; --") is False

    def test_none_is_not_safe(self):
        assert _is_safe_number(None) is False

    def test_list_is_not_safe(self):
        assert _is_safe_number([1, 2]) is False

    def test_inf_nan_float_rejected(self):
        """float('inf') and float('nan') must not pass — they render as bare
        identifiers, not valid SQL numeric literals."""
        assert _is_safe_number(float("inf")) is False
        assert _is_safe_number(float("-inf")) is False
        assert _is_safe_number(float("nan")) is False

    def test_inf_nan_string_rejected(self):
        """String representations of inf/nan must not pass."""
        assert _is_safe_number("inf") is False
        assert _is_safe_number("Infinity") is False
        assert _is_safe_number("-inf") is False
        assert _is_safe_number("nan") is False
        assert _is_safe_number("NaN") is False

    def test_underscore_string_rejected(self):
        """Python accepts '1_000' but it is not a valid SQL literal."""
        assert _is_safe_number("1_000") is False
        assert _is_safe_number("1_000_000") is False

    def test_whitespace_padded_string_rejected(self):
        """Whitespace-padded strings are not valid SQL numeric literals."""
        assert _is_safe_number(" 12 ") is False
        assert _is_safe_number(" 3.14") is False

    def test_overflow_string_rejected(self):
        """Strings that parse to infinity via overflow must be rejected."""
        assert _is_safe_number("1e999") is False

    def test_huge_int_is_safe(self):
        """Arbitrarily large Python ints are finite and render safely."""
        assert _is_safe_number(10**400) is True


# ---------------------------------------------------------------------------
# _extract_lists_from_snapshot
# ---------------------------------------------------------------------------

class TestExtractFromSnapshot:
    def test_extracts_sql_fixed_list(self):
        snapshot = {
            "named_sets": [
                {
                    "name": "TopChannels",
                    "list_type": "sql_fixed",
                    "builder_definition": {
                        "type": "fixedMembers",
                        "dimension": "channel",
                        "data_type": "string",
                        "members": ["online", "retail"],
                    },
                }
            ]
        }
        result = _extract_lists_from_snapshot(snapshot)
        assert "@topchannels" in result
        nl = result["@topchannels"]
        assert nl.list_type == "sql_fixed"
        assert nl.members == ["online", "retail"]
        assert nl.data_type == "string"

    def test_extracts_mdx_set_for_error_reporting(self):
        snapshot = {
            "named_sets": [
                {
                    "name": "MdxSet",
                    "list_type": "advanced_mdx",
                    "expression": "{[Dim].[All]}",
                    "builder_definition": {},
                }
            ]
        }
        result = _extract_lists_from_snapshot(snapshot)
        assert "@mdxset" in result
        assert result["@mdxset"].list_type == "advanced_mdx"

    def test_empty_snapshot(self):
        assert _extract_lists_from_snapshot({}) == {}
        assert _extract_lists_from_snapshot({"named_sets": None}) == {}
        assert _extract_lists_from_snapshot({"named_sets": []}) == {}

    def test_number_type(self):
        snapshot = {
            "named_sets": [
                {
                    "name": "TopRegions",
                    "list_type": "sql_fixed",
                    "builder_definition": {
                        "type": "fixedMembers",
                        "data_type": "number",
                        "members": [10, 20, 30],
                    },
                }
            ]
        }
        result = _extract_lists_from_snapshot(snapshot)
        nl = result["@topregions"]
        assert nl.data_type == "number"
        assert nl.members == [10, 20, 30]

    def test_case_variant_duplicate_fails_closed(self):
        """F-018-03 / Bug-7927: two names that collide only by case must raise,
        never silently overwrite one with last-write-wins.

        A legacy snapshot can contain both ``emea`` and ``EMEA`` (the old
        case-sensitive DB constraint permitted it). Lowercasing the lookup key
        would otherwise map both to ``@emea`` and shadow one membership at query
        time — the CRITICAL wrong-list-substitution class. Extraction must fail
        closed so the query errors loudly instead of returning the wrong set.
        """
        snapshot = {
            "named_sets": [
                {
                    "name": "emea",
                    "list_type": "sql_fixed",
                    "builder_definition": {
                        "type": "fixedMembers",
                        "data_type": "string",
                        "members": ["FR", "DE"],
                    },
                },
                {
                    "name": "EMEA",
                    "list_type": "sql_fixed",
                    "builder_definition": {
                        "type": "fixedMembers",
                        "data_type": "string",
                        "members": ["US", "CA"],
                    },
                },
            ]
        }
        with pytest.raises(ParameterError) as exc:
            _extract_lists_from_snapshot(snapshot)
        # The error must name the colliding key so the modeler can fix it.
        assert "@emea" in str(exc.value)


# ---------------------------------------------------------------------------
# expand_named_lists — main expansion logic
# ---------------------------------------------------------------------------

class TestExpandNamedLists:
    def test_basic_string_expansion(self):
        lists = _lists_dict(_make_list("TopChannels", "string", ["online", "retail"]))
        sql = "SELECT * FROM t WHERE channel IN (@TopChannels)"
        result, audit = expand_named_lists(sql, lists)
        assert result == "SELECT * FROM t WHERE channel IN ('online', 'retail')"
        assert len(audit) == 1
        assert "TopChannels" in audit[0]

    def test_basic_number_expansion(self):
        lists = _lists_dict(_make_list("TopRegions", "number", [10, 20, 30]))
        sql = "SELECT * FROM t WHERE region_id IN (@TopRegions)"
        result, audit = expand_named_lists(sql, lists)
        assert result == "SELECT * FROM t WHERE region_id IN (10, 20, 30)"

    def test_not_in_expansion(self):
        lists = _lists_dict(_make_list("Excluded", "string", ["x", "y"]))
        sql = "SELECT * FROM t WHERE ch NOT IN (@Excluded)"
        result, audit = expand_named_lists(sql, lists)
        assert result == "SELECT * FROM t WHERE ch NOT IN ('x', 'y')"

    def test_no_lists_returns_unchanged(self):
        sql = "SELECT * FROM t WHERE channel IN (@TopChannels)"
        result, audit = expand_named_lists(sql, {})
        assert result == sql
        assert audit == []

    def test_no_placeholders_returns_unchanged(self):
        lists = _lists_dict(_make_list())
        sql = "SELECT * FROM t WHERE channel = 'online'"
        result, audit = expand_named_lists(sql, lists)
        assert result == sql
        assert audit == []

    def test_unmatched_placeholder_left_alone(self):
        lists = _lists_dict(_make_list("TopChannels"))
        sql = "SELECT * FROM t WHERE x = @Other"
        result, audit = expand_named_lists(sql, lists)
        assert result == sql
        assert audit == []


# ---------------------------------------------------------------------------
# Case-insensitive name matching
# ---------------------------------------------------------------------------

class TestCaseInsensitive:
    def test_lowercase_query_matches_mixed_case_list(self):
        lists = _lists_dict(_make_list("TopChannels", "string", ["a", "b"]))
        sql = "SELECT * FROM t WHERE ch IN (@topchannels)"
        result, _ = expand_named_lists(sql, lists)
        assert "'a', 'b'" in result

    def test_uppercase_query_matches(self):
        lists = _lists_dict(_make_list("TopChannels", "string", ["a", "b"]))
        sql = "SELECT * FROM t WHERE ch IN (@TOPCHANNELS)"
        result, _ = expand_named_lists(sql, lists)
        assert "'a', 'b'" in result

    def test_mixed_case_query_matches(self):
        lists = _lists_dict(_make_list("TopChannels", "string", ["a", "b"]))
        sql = "SELECT * FROM t WHERE ch IN (@TopChannels)"
        result, _ = expand_named_lists(sql, lists)
        assert "'a', 'b'" in result


# ---------------------------------------------------------------------------
# Injection safety
# ---------------------------------------------------------------------------

class TestInjectionSafety:
    def test_member_with_single_quotes(self):
        lists = _lists_dict(_make_list("L", "string", ["O'Brien", "D'Arc"]))
        sql = "SELECT * FROM t WHERE name IN (@L)"
        result, _ = expand_named_lists(sql, lists)
        # Quotes must be escaped, not breaking out of the literal.
        assert "O''Brien" in result
        assert "D''Arc" in result

    def test_member_with_semicolons(self):
        lists = _lists_dict(_make_list("L", "string", ["a;DROP TABLE x"]))
        sql = "SELECT * FROM t WHERE ch IN (@L)"
        result, _ = expand_named_lists(sql, lists)
        assert "DROP" in result  # Inside a string literal
        assert result.count("'") >= 2  # Must be quoted

    def test_member_with_control_chars(self):
        lists = _lists_dict(_make_list("L", "string", ["a\x00b", "c\nd"]))
        sql = "SELECT * FROM t WHERE ch IN (@L)"
        # Should not crash — sqlglot handles it as a string literal.
        result, _ = expand_named_lists(sql, lists)
        assert isinstance(result, str)

    def test_member_with_double_quotes(self):
        lists = _lists_dict(_make_list("L", "string", ['say "hello"']))
        sql = "SELECT * FROM t WHERE ch IN (@L)"
        result, _ = expand_named_lists(sql, lists)
        assert isinstance(result, str)
        assert "hello" in result

    def test_non_numeric_string_in_number_list_rejected(self):
        """F-029-01: a non-numeric string in a number-typed list must not
        be spliced raw into the SQL. Defense-in-depth validation catches
        this at resolve time even if create-time validation was bypassed.
        """
        lists = _lists_dict(_make_list("L", "number", ["1) OR (1=1"]))
        sql = "SELECT * FROM t WHERE id IN (@L)"
        with pytest.raises(ParameterError, match="non-numeric member"):
            expand_named_lists(sql, lists)

    def test_boolean_in_number_list_rejected(self):
        """Boolean is int subclass but must not render as bare 'True'."""
        lists = _lists_dict(_make_list("L", "number", [True, False]))
        sql = "SELECT * FROM t WHERE id IN (@L)"
        with pytest.raises(ParameterError, match="non-numeric member"):
            expand_named_lists(sql, lists)

    def test_control_chars_in_number_member_rejected(self):
        lists = _lists_dict(_make_list("L", "number", ["42\x00"]))
        sql = "SELECT * FROM t WHERE id IN (@L)"
        with pytest.raises(ParameterError, match="non-numeric member"):
            expand_named_lists(sql, lists)

    def test_sql_injection_in_number_member_rejected(self):
        """Classic SQL injection attempt in a number-typed list."""
        lists = _lists_dict(_make_list("L", "number", ["1; DROP TABLE x"]))
        sql = "SELECT * FROM t WHERE id IN (@L)"
        with pytest.raises(ParameterError, match="non-numeric member"):
            expand_named_lists(sql, lists)


# ---------------------------------------------------------------------------
# Usage-shape whitelist — rejection cases
# ---------------------------------------------------------------------------

class TestUsageShapeRejection:
    def test_equals_position_rejected(self):
        lists = _lists_dict(_make_list("L", "string", ["a"]))
        sql = "SELECT * FROM t WHERE ch = @L"
        with pytest.raises(ParameterError, match="can only be used inside IN"):
            expand_named_lists(sql, lists)

    def test_bare_select_rejected(self):
        lists = _lists_dict(_make_list("L", "string", ["a"]))
        sql = "SELECT @L FROM t"
        with pytest.raises(ParameterError, match="can only be used inside IN"):
            expand_named_lists(sql, lists)

    def test_in_paren_accepted(self):
        lists = _lists_dict(_make_list("L", "string", ["a"]))
        sql = "SELECT * FROM t WHERE ch IN (@L)"
        result, _ = expand_named_lists(sql, lists)
        assert "'a'" in result

    def test_not_in_paren_accepted(self):
        lists = _lists_dict(_make_list("L", "string", ["a"]))
        sql = "SELECT * FROM t WHERE ch NOT IN (@L)"
        result, _ = expand_named_lists(sql, lists)
        assert "'a'" in result


# ---------------------------------------------------------------------------
# Size caps
# ---------------------------------------------------------------------------

class TestSizeCaps:
    def test_at_limit_accepted(self):
        members = [f"v{i}" for i in range(100)]
        lists = _lists_dict(_make_list("L", "string", members))
        sql = "SELECT * FROM t WHERE ch IN (@L)"
        with patch.dict(os.environ, {"NAMED_LIST_MAX_MEMBERS": "100"}):
            result, _ = expand_named_lists(sql, lists)
        assert "'v0'" in result
        assert "'v99'" in result

    def test_over_limit_rejected(self):
        members = [f"v{i}" for i in range(101)]
        lists = _lists_dict(_make_list("L", "string", members))
        sql = "SELECT * FROM t WHERE ch IN (@L)"
        with patch.dict(os.environ, {"NAMED_LIST_MAX_MEMBERS": "100"}):
            with pytest.raises(ParameterError, match="exceeding the maximum"):
                expand_named_lists(sql, lists)

    def test_default_cap_is_1000(self):
        members = [f"v{i}" for i in range(1001)]
        lists = _lists_dict(_make_list("L", "string", members))
        sql = "SELECT * FROM t WHERE ch IN (@L)"
        # Ensure no NAMED_LIST_MAX_MEMBERS env var is set.
        env = dict(os.environ)
        env.pop("NAMED_LIST_MAX_MEMBERS", None)
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ParameterError, match="exceeding the maximum of 1000"):
                expand_named_lists(sql, lists)


# ---------------------------------------------------------------------------
# Name collision: parameter vs named list
# ---------------------------------------------------------------------------

class TestNameCollision:
    def test_collision_raises_400(self):
        lists = _lists_dict(_make_list("Region", "string", ["EMEA"]))
        sql = "SELECT * FROM t WHERE r IN (@Region)"
        with pytest.raises(ParameterError, match="matches both a model parameter"):
            expand_named_lists(
                sql, lists, declared_param_names={"@Region"}
            )

    def test_collision_case_insensitive(self):
        lists = _lists_dict(_make_list("Region", "string", ["EMEA"]))
        sql = "SELECT * FROM t WHERE r IN (@region)"
        with pytest.raises(ParameterError, match="matches both a model parameter"):
            expand_named_lists(
                sql, lists, declared_param_names={"@REGION"}
            )

    def test_no_collision_when_different_names(self):
        lists = _lists_dict(_make_list("TopChannels", "string", ["a"]))
        sql = "SELECT * FROM t WHERE ch IN (@TopChannels)"
        result, _ = expand_named_lists(
            sql, lists, declared_param_names={"@Region"}
        )
        assert "'a'" in result


# ---------------------------------------------------------------------------
# MDX-type set referenced in SQL query
# ---------------------------------------------------------------------------

class TestMdxSetInSqlQuery:
    def test_mdx_set_returns_specific_400(self):
        lists = _lists_dict(
            _make_list("MdxSet", "string", ["a"], list_type="advanced_mdx")
        )
        sql = "SELECT * FROM t WHERE ch IN (@MdxSet)"
        with pytest.raises(ParameterError, match="MDX named set.*XMLA"):
            expand_named_lists(sql, lists)

    def test_topn_type_also_rejected(self):
        lists = _lists_dict(
            _make_list("TopN", "string", ["a"], list_type="topN")
        )
        sql = "SELECT * FROM t WHERE ch IN (@TopN)"
        with pytest.raises(ParameterError, match="MDX named set.*XMLA"):
            expand_named_lists(sql, lists)

    def test_filtered_type_rejected(self):
        lists = _lists_dict(
            _make_list("Filtered", "string", ["a"], list_type="filtered")
        )
        sql = "SELECT * FROM t WHERE ch IN (@Filtered)"
        with pytest.raises(ParameterError, match="MDX named set.*XMLA"):
            expand_named_lists(sql, lists)

    def test_fixed_members_mdx_rejected(self):
        lists = _lists_dict(
            _make_list("Fixed", "string", ["a"], list_type="fixedMembers")
        )
        sql = "SELECT * FROM t WHERE ch IN (@Fixed)"
        with pytest.raises(ParameterError, match="MDX named set.*XMLA"):
            expand_named_lists(sql, lists)


# ---------------------------------------------------------------------------
# Empty list
# ---------------------------------------------------------------------------

class TestEmptyList:
    def test_empty_members_rejected(self):
        lists = _lists_dict(_make_list("Empty", "string", []))
        sql = "SELECT * FROM t WHERE ch IN (@Empty)"
        with pytest.raises(ParameterError, match="has no members"):
            expand_named_lists(sql, lists)

    def test_empty_fixedmembers_says_add_member(self):
        lists = _lists_dict(_make_list("Empty", "string", [], builder_type="fixedMembers"))
        sql = "SELECT * FROM t WHERE ch IN (@Empty)"
        with pytest.raises(ParameterError, match="Add at least one member"):
            expand_named_lists(sql, lists)

    def test_empty_topn_says_click_refresh(self):
        lists = _lists_dict(_make_list("Top10", "string", [], builder_type="topN"))
        sql = "SELECT * FROM t WHERE ch IN (@Top10)"
        with pytest.raises(ParameterError, match="click Refresh"):
            expand_named_lists(sql, lists)

    def test_empty_filter_says_click_refresh(self):
        lists = _lists_dict(_make_list("Filt", "string", [], builder_type="filter"))
        sql = "SELECT * FROM t WHERE ch IN (@Filt)"
        with pytest.raises(ParameterError, match="click Refresh"):
            expand_named_lists(sql, lists)

    def test_empty_sql_query_says_click_refresh(self):
        lists = _lists_dict(_make_list("Custom", "string", [], builder_type="sql_query"))
        sql = "SELECT * FROM t WHERE ch IN (@Custom)"
        with pytest.raises(ParameterError, match="click Refresh"):
            expand_named_lists(sql, lists)


# ---------------------------------------------------------------------------
# Multiple lists in one query
# ---------------------------------------------------------------------------

class TestMultipleLists:
    def test_two_lists_both_expand(self):
        lists = _lists_dict(
            _make_list("Channels", "string", ["online", "retail"]),
            _make_list("Regions", "number", [1, 2, 3]),
        )
        sql = (
            "SELECT * FROM t "
            "WHERE ch IN (@Channels) AND region_id IN (@Regions)"
        )
        result, audit = expand_named_lists(sql, lists)
        assert "'online', 'retail'" in result
        assert "1, 2, 3" in result
        assert len(audit) == 2

    def test_audit_entries_in_query_order(self):
        """Audit entries must be in left-to-right query order, not reversed."""
        lists = _lists_dict(
            _make_list("Alpha", "string", ["a"]),
            _make_list("Zulu", "string", ["z"]),
        )
        sql = (
            "SELECT * FROM t "
            "WHERE x IN (@Alpha) AND y IN (@Zulu)"
        )
        _, audit = expand_named_lists(sql, lists)
        assert audit[0].startswith("Alpha")
        assert audit[1].startswith("Zulu")

    def test_same_list_twice(self):
        lists = _lists_dict(_make_list("L", "string", ["a", "b"]))
        sql = (
            "SELECT * FROM t "
            "WHERE ch IN (@L) AND other IN (@L)"
        )
        result, audit = expand_named_lists(sql, lists)
        assert result.count("'a', 'b'") == 2
        assert len(audit) == 2


# ---------------------------------------------------------------------------
# String vs number type rendering
# ---------------------------------------------------------------------------

class TestTypeRendering:
    def test_string_type_quotes_numbers(self):
        """When data_type is string, even numeric-looking members get quotes."""
        lists = _lists_dict(_make_list("L", "string", ["100", "200"]))
        sql = "SELECT * FROM t WHERE ch IN (@L)"
        result, _ = expand_named_lists(sql, lists)
        assert "'100', '200'" in result

    def test_number_type_bare_integers(self):
        lists = _lists_dict(_make_list("L", "number", [100, 200]))
        sql = "SELECT * FROM t WHERE id IN (@L)"
        result, _ = expand_named_lists(sql, lists)
        assert "100, 200" in result
        # No quotes around numbers.
        assert "'" not in result.split("IN")[1]

    def test_number_type_bare_floats(self):
        lists = _lists_dict(_make_list("L", "number", [1.5, 2.75]))
        sql = "SELECT * FROM t WHERE val IN (@L)"
        result, _ = expand_named_lists(sql, lists)
        assert "1.5" in result
        assert "2.75" in result


# ---------------------------------------------------------------------------
# Snapshot extraction edge cases
# ---------------------------------------------------------------------------

class TestSnapshotExtractionBuilderType:
    def test_extracts_builder_type_from_snapshot(self):
        snapshot = {
            "named_sets": [
                {
                    "name": "TopAccounts",
                    "list_type": "sql_fixed",
                    "builder_definition": {
                        "type": "topN",
                        "data_type": "string",
                        "members": ["a", "b"],
                    },
                }
            ]
        }
        result = _extract_lists_from_snapshot(snapshot)
        assert result["@topaccounts"].builder_type == "topN"

    def test_defaults_to_fixedmembers_when_type_missing(self):
        snapshot = {
            "named_sets": [
                {
                    "name": "Legacy",
                    "list_type": "sql_fixed",
                    "builder_definition": {
                        "data_type": "string",
                        "members": ["x"],
                    },
                }
            ]
        }
        result = _extract_lists_from_snapshot(snapshot)
        assert result["@legacy"].builder_type == "fixedMembers"

    def test_defaults_to_fixedmembers_for_non_dict_builder(self):
        snapshot = {
            "named_sets": [
                {
                    "name": "NoDict",
                    "list_type": "sql_fixed",
                    "builder_definition": None,
                }
            ]
        }
        result = _extract_lists_from_snapshot(snapshot)
        assert result["@nodict"].builder_type == "fixedMembers"


def test_refresh_named_list_reports_success_but_deployed_members_wait_for_deploy():
    """2026-08-11 named-list refresh vintage gap: refreshed live members do
    not reach the query resolver until the refreshed definition is deployed."""
    deployed_snapshot = {
        "named_sets": [
            {
                "name": "Regions",
                "list_type": "sql_fixed",
                "builder_definition": {
                    "type": "sql_query",
                    "data_type": "string",
                    "members": ["old-region"],
                    "last_refreshed_at": "2026-08-01T10:00:00+00:00",
                },
            }
        ]
    }
    refreshed_live_definition = {
        "name": "Regions",
        "list_type": "sql_fixed",
        "builder_definition": {
            "type": "sql_query",
            "data_type": "string",
            "members": ["new-region"],
            "last_refreshed_at": "2026-08-11T10:00:00+00:00",
        },
    }

    before_deploy = _extract_lists_from_snapshot(deployed_snapshot)
    assert before_deploy["@regions"].members == ["old-region"]

    after_deploy = _extract_lists_from_snapshot(
        {"named_sets": [refreshed_live_definition]}
    )
    assert after_deploy["@regions"].members == ["new-region"]


class TestSnapshotExtraction:
    def test_malformed_builder_definition(self):
        """A named set with a non-dict builder_definition should not crash."""
        snapshot = {
            "named_sets": [
                {
                    "name": "Bad",
                    "list_type": "sql_fixed",
                    "builder_definition": "not a dict",
                }
            ]
        }
        result = _extract_lists_from_snapshot(snapshot)
        assert "@bad" in result
        assert result["@bad"].members == []

    def test_missing_builder_definition(self):
        snapshot = {
            "named_sets": [
                {
                    "name": "NoBuilder",
                    "list_type": "sql_fixed",
                }
            ]
        }
        result = _extract_lists_from_snapshot(snapshot)
        assert "@nobuilder" in result
        assert result["@nobuilder"].members == []

    def test_non_dict_named_set_entry_skipped(self):
        snapshot = {"named_sets": ["not a dict", None, 42]}
        result = _extract_lists_from_snapshot(snapshot)
        assert result == {}


# ---------------------------------------------------------------------------
# Route-decision / acceleration equivalence (Bug-7920)
# ---------------------------------------------------------------------------

class TestRouteEquivalenceAcceleration:
    """Bug-7920: a Named-List-filtered query must route — and therefore
    accelerate (aggregate / pocket) — identically to its hand-typed literal
    ``IN``-list equivalent, and return the same numbers.

    Named-list resolution runs PRE-PARSE in ``routes.py``: the parser, binder,
    security layer, aggregate matcher, and pocket matcher only ever see the
    EXPANDED SQL — never the ``@ListName`` placeholder. So if the expansion of
    ``IN (@List)`` is byte-identical to a hand-typed ``IN ('a', 'b')``, then
    every downstream decision (route type, aggregate/pocket match, and the
    numeric result) is identical BY CONSTRUCTION: the routing pipeline cannot
    tell the two apart. That is the concrete meaning of "named lists are
    aggregate-eligible" — they are not a full-source-scan-only feature.

    These tests pin that identity as a regression guard. They fail (catching a
    revert) if a change either (a) stops expanding — leaving ``@List``, which
    the parser rejects / forces to source, i.e. zero acceleration — or
    (b) renders the members differently from a hand-typed literal, which would
    change the parsed query and break aggregate eligibility or the numbers.
    """

    # NOTE: single-list byte-identity of expand_named_lists output is already
    # asserted by TestExpandNamedLists (basic string / number / NOT IN). Per the
    # project's test-discipline (extend, do not duplicate), the route-equivalence
    # guard below adds only what those do NOT cover: parsed-AST equality, the
    # anti-revert placeholder guard, multi-list literal equivalence, and the
    # end-to-end routes.py BIND-WIRING equivalence (TestBindWiringRouteEquivalence).

    def test_expanded_ast_equals_literal_ast(self):
        """Stronger than string identity: the PARSED query trees are equal, so
        the binder/matcher build the same LogicalQuery -> same route -> same
        numbers, independent of any whitespace incidentals."""
        lists = _lists_dict(_make_list("TopChannels", "string", ["online", "retail"]))
        named_sql = (
            "SELECT region, SUM(amount) FROM sales "
            "WHERE channel IN (@TopChannels) GROUP BY region"
        )
        literal_sql = (
            "SELECT region, SUM(amount) FROM sales "
            "WHERE channel IN ('online', 'retail') GROUP BY region"
        )
        expanded, _ = expand_named_lists(named_sql, lists)
        assert sqlglot.parse_one(expanded, read="postgres") == sqlglot.parse_one(
            literal_sql, read="postgres"
        )

    def test_expansion_removes_placeholder_revert_guard(self):
        """Revert guard: if a regression stopped expanding, the ``@List`` token
        would survive and the query would NOT match its literal equivalent
        (parse error or forced source route = zero acceleration). Assert the
        placeholder is gone, the SQL genuinely changed, and an acceleration
        audit entry was recorded."""
        lists = _lists_dict(_make_list("TopChannels", "string", ["online", "retail"]))
        named_sql = "SELECT SUM(amount) FROM sales WHERE channel IN (@TopChannels)"
        expanded, audit = expand_named_lists(named_sql, lists)
        assert "@TopChannels" not in expanded
        assert "@" not in expanded
        assert expanded != named_sql
        assert audit  # acceleration audit trail recorded

    def test_multi_list_query_matches_literal_equivalent(self):
        lists = _lists_dict(
            _make_list("Channels", "string", ["online", "retail"]),
            _make_list("Regions", "number", [1, 2, 3]),
        )
        named_sql = (
            "SELECT SUM(amount) FROM sales "
            "WHERE ch IN (@Channels) AND region_id IN (@Regions)"
        )
        literal_sql = (
            "SELECT SUM(amount) FROM sales "
            "WHERE ch IN ('online', 'retail') AND region_id IN (1, 2, 3)"
        )
        expanded, audit = expand_named_lists(named_sql, lists)
        assert expanded == literal_sql
        assert sqlglot.parse_one(expanded, read="postgres") == sqlglot.parse_one(
            literal_sql, read="postgres"
        )
        assert len(audit) == 2


# ---------------------------------------------------------------------------
# End-to-end bind-wiring route equivalence (Bug-7920) — routes.py
# ---------------------------------------------------------------------------

class TestBindWiringRouteEquivalence:
    """Bug-7920 (closure): prove the PRODUCTION wiring — not just the resolver
    in isolation — turns a named-list query into its literal-IN equivalent
    BEFORE parse. ``_bind_query_parameters`` mutates ``body.raw_query`` in place
    and is the single funnel every SQL protocol path calls (routes.py:1604,
    2610, 2756) ahead of ``_parse``. Asserting the post-bind SQL is byte-identical
    to the hand-typed literal query closes the gap the resolver-only tests leave:
    a regression that unwired expansion here would leave ``@List`` in the query.

    That failure is loud, not silent (the leftover-placeholder check at
    routes.py:1182-1192 raises 400), so this can never degrade into a silent
    wrong-route/full-scan — but this test pins the positive path so the
    acceleration equivalence is guarded end to end, honestly discharging the
    registry's route-decision-equality closure wording.
    """

    async def test_bind_yields_literal_identical_sql(self, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        from src.api import routes

        lists = _lists_dict(_make_list("TopChannels", "string", ["online", "retail"]))

        async def _fake_load(model_id, db):
            return lists

        async def _fake_apply(**kwargs):
            # No model parameters declared: return the SQL untouched, exactly as
            # apply_parameters' fast path does. Named-list expansion (real) then
            # runs on the still-present @TopChannels placeholder.
            return kwargs["sql"]

        monkeypatch.setattr(routes, "load_named_lists", _fake_load)
        monkeypatch.setattr(routes, "apply_parameters", _fake_apply)

        body = SimpleNamespace(
            protocol="jdbc",
            model_id="m1",
            session_vars={},
            raw_query=(
                "SELECT region, SUM(amount) FROM sales "
                "WHERE channel IN (@TopChannels) GROUP BY region"
            ),
        )

        db = AsyncMock()
        # Law-6 collision probe: select(ModelParameter) -> no declared params.
        # db.execute is awaited then .scalars().all() is called synchronously, so
        # its awaited result must be a plain (non-async) MagicMock.
        _param_result = MagicMock()
        _param_result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=_param_result)
        # Deployed-snapshot param lookup: model not found -> skip (undeployed path).
        db.get = AsyncMock(return_value=None)

        await routes._bind_query_parameters(body, db, None)

        assert body.raw_query == (
            "SELECT region, SUM(amount) FROM sales "
            "WHERE channel IN ('online', 'retail') GROUP BY region"
        )
        assert "@" not in body.raw_query

    async def test_bind_leaves_no_named_list_query_unchanged(self, monkeypatch):
        """Control: a query with no named-list placeholder is untouched by the
        wiring (guards against an over-eager expansion regression)."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from src.api import routes

        async def _fake_load(model_id, db):
            return _lists_dict(_make_list("TopChannels", "string", ["online", "retail"]))

        monkeypatch.setattr(routes, "load_named_lists", _fake_load)

        body = SimpleNamespace(
            protocol="jdbc",
            model_id="m1",
            session_vars={},
            raw_query="SELECT region, SUM(amount) FROM sales GROUP BY region",
        )
        db = AsyncMock()

        await routes._bind_query_parameters(body, db, None)

        # No '@' present -> the fast path returns before any load/expand.
        assert body.raw_query == "SELECT region, SUM(amount) FROM sales GROUP BY region"


# ---------------------------------------------------------------------------
# Cache invalidation
# ---------------------------------------------------------------------------

class TestCacheInvalidation:
    def test_invalidate_all(self):
        invalidate_named_list_cache()  # Should not raise.

    def test_invalidate_specific_model(self):
        invalidate_named_list_cache("some-model-id")  # Should not raise.
