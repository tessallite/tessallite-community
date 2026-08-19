"""Bug-5548: an enumerated member set on a ROWS/COLUMNS axis must restrict the
result to exactly those members, not expand to the whole level.

Before the fix the gateway axis translator extracted the dimension and
GROUP-BY'd the whole level, silently dropping the member enumeration, so an
explicit set (and any server-defined named list inlined onto the same axis)
returned ALL members of the level — identical to ``.Members``.

These tests prove:
- an enumerated set in KEY form (``&[X]``) filters to exactly those members,
- an enumerated set in CAPTION form (``[X]``) filters to exactly those members,
- a bare ``.Members`` / ``.Children`` / ``.AllMembers`` expansion still returns
  the full level (no WHERE filter — no regression),
- an inlined named list (``_inline_named_sets``) finally filters,
- a CrossJoin of an enumerated set with a ``.Members`` level filters only the
  enumerated dimension.
"""
from __future__ import annotations

import pytest

from src.dax.xmla_server import (
    _mdx_to_sql,
    _inline_named_sets,
    _mdx_extract_axis_member_filters,
    _mdx_extract_where_filters,
)

MEASURES = [{"name": "Amount", "default_agg": "sum"}]
DIMENSIONS = [{"name": "item_category"}]


# ---------------------------------------------------------------------------
# Enumerated member set on ROWS — KEY form
# ---------------------------------------------------------------------------

def test_enumerated_key_form_filters_to_exact_members():
    mdx = (
        "SELECT {[Measures].[Amount]} ON COLUMNS, "
        "{[item_category].[item_category].&[Shoes], "
        "[item_category].[item_category].&[Music], "
        "[item_category].[item_category].&[Jewelry]} ON ROWS "
        "FROM [demo]"
    )
    sql, protocol = _mdx_to_sql(mdx, MEASURES, DIMENSIONS)

    assert protocol == "jdbc"
    # The level is still grouped...
    assert 'GROUP BY "item_category"' in sql
    # ...but restricted to exactly the three enumerated members.
    assert 'WHERE "item_category" IN (' in sql
    for member in ("Shoes", "Music", "Jewelry"):
        assert f"'{member}'" in sql


# ---------------------------------------------------------------------------
# Enumerated member set on ROWS — CAPTION form
# ---------------------------------------------------------------------------

def test_enumerated_caption_form_filters_to_exact_members():
    mdx = (
        "SELECT {[Measures].[Amount]} ON COLUMNS, "
        "{[item_category].[item_category].[Shoes], "
        "[item_category].[item_category].[Music], "
        "[item_category].[item_category].[Jewelry]} ON ROWS "
        "FROM [demo]"
    )
    sql, protocol = _mdx_to_sql(mdx, MEASURES, DIMENSIONS)

    assert protocol == "jdbc"
    assert 'GROUP BY "item_category"' in sql
    assert 'WHERE "item_category" IN (' in sql
    for member in ("Shoes", "Music", "Jewelry"):
        assert f"'{member}'" in sql


def test_enumerated_single_member_emits_equality():
    mdx = (
        "SELECT {[Measures].[Amount]} ON COLUMNS, "
        "{[item_category].[item_category].&[Shoes]} ON ROWS "
        "FROM [demo]"
    )
    sql, _ = _mdx_to_sql(mdx, MEASURES, DIMENSIONS)
    assert 'WHERE "item_category" = \'Shoes\'' in sql
    assert 'GROUP BY "item_category"' in sql


# ---------------------------------------------------------------------------
# Bare .Members / .Children / .AllMembers — full level, NO regression
# ---------------------------------------------------------------------------

def test_bare_members_returns_full_level_no_filter():
    mdx = (
        "SELECT {[Measures].[Amount]} ON COLUMNS, "
        "{[item_category].[item_category].Members} ON ROWS "
        "FROM [demo]"
    )
    sql, protocol = _mdx_to_sql(mdx, MEASURES, DIMENSIONS)

    assert protocol == "jdbc"
    assert 'GROUP BY "item_category"' in sql
    # The whole point of the fix: a bare .Members must NOT add a filter.
    assert "WHERE" not in sql


def test_level_members_expansion_returns_full_level_no_filter():
    # [Dim].[Hier].[Level].Members — the 3-bracket level-expansion form must not
    # be mistaken for a caption member (which would wrongly filter on the level
    # name).
    mdx = (
        "SELECT {[Measures].[Amount]} ON COLUMNS, "
        "{[item_category].[item_category].[item_category].Members} ON ROWS "
        "FROM [demo]"
    )
    sql, _ = _mdx_to_sql(mdx, MEASURES, DIMENSIONS)
    assert 'GROUP BY "item_category"' in sql
    assert "WHERE" not in sql


def test_children_and_allmembers_expansions_no_filter():
    for kw in ("Children", "AllMembers"):
        mdx = (
            "SELECT {[Measures].[Amount]} ON COLUMNS, "
            f"{{[item_category].[item_category].{kw}}} ON ROWS "
            "FROM [demo]"
        )
        sql, _ = _mdx_to_sql(mdx, MEASURES, DIMENSIONS)
        assert "WHERE" not in sql, f"{kw} must not produce a filter"


# ---------------------------------------------------------------------------
# Server-defined named list inlined onto the axis (Bug-5499 + Bug-5548)
# ---------------------------------------------------------------------------

def test_inlined_named_list_filters():
    # _inline_named_sets replaces the bare set name with the compiled member
    # list (the named_list_compiler fixedMembers shape: fully-qualified key
    # members inside braces). After inlining it must FILTER, not just resolve.
    named_sets = [{
        "name": "Key Categories",
        "expression": (
            "{ [item_category].[item_category].&[Shoes], "
            "[item_category].[item_category].&[Jewelry] }"
        ),
    }]
    raw = (
        "SELECT {[Measures].[Amount]} ON COLUMNS, "
        "{[Key Categories]} ON ROWS FROM [demo]"
    )
    inlined = _inline_named_sets(raw, named_sets)
    sql, _ = _mdx_to_sql(inlined, MEASURES, DIMENSIONS)

    assert 'GROUP BY "item_category"' in sql
    assert 'WHERE "item_category" IN (' in sql
    assert "'Shoes'" in sql and "'Jewelry'" in sql


# ---------------------------------------------------------------------------
# CrossJoin: enumerated set on one dim, .Members on another
# ---------------------------------------------------------------------------

def test_crossjoin_filters_only_enumerated_dimension():
    measures = [{"name": "Amount", "default_agg": "sum"}]
    dimensions = [{"name": "item_category"}, {"name": "region"}]
    mdx = (
        "SELECT {[Measures].[Amount]} ON COLUMNS, "
        "CrossJoin("
        "{[item_category].[item_category].&[Shoes], "
        "[item_category].[item_category].&[Music]}, "
        "{[region].[region].Members}) ON ROWS "
        "FROM [demo]"
    )
    sql, _ = _mdx_to_sql(mdx, measures, dimensions)

    assert 'GROUP BY "item_category", "region"' in sql or (
        'GROUP BY "region", "item_category"' in sql
    )
    # Only item_category is restricted; region stays a full level.
    assert 'WHERE "item_category" IN (' in sql
    assert "'Shoes'" in sql and "'Music'" in sql
    assert '"region" IN' not in sql
    assert '"region" =' not in sql


# ---------------------------------------------------------------------------
# Direct extractor behaviour
# ---------------------------------------------------------------------------

def test_axis_extractor_skips_bare_members_expansion():
    dim_names = {"item_category"}
    # Bare expansion -> no filter.
    assert _mdx_extract_axis_member_filters(
        "{[item_category].[item_category].Members}", dim_names,
    ) == {}
    # Enumerated members -> filter.
    out = _mdx_extract_axis_member_filters(
        "{[item_category].[item_category].&[Shoes], "
        "[item_category].[item_category].&[Music]}",
        dim_names,
    )
    assert out == {"item_category": ["Shoes", "Music"]}


def test_where_extractor_default_is_unchanged():
    # The WHERE path (exclude_level_expansions defaults to False) must keep its
    # original behaviour: a 3-bracket reference followed by .Members is still
    # matched there as before (no expansion guard applied).
    dim_names = {"item_category"}
    out = _mdx_extract_where_filters(
        "[item_category].[item_category].[Shoes]", dim_names,
    )
    assert out == {"item_category": ["Shoes"]}


def test_axis_enumerated_set_unknown_member_fails_loud():
    # Codex-review hardening: an enumerated set on the axis that references a
    # dimension the gateway cannot resolve must FAIL LOUD, not silently drop the
    # filter and run the whole level unfiltered (the Bug-1060 fail-open seam,
    # now also guarded on the ROWS/COLUMNS axis).
    mdx = (
        "SELECT {[Measures].[Amount]} ON COLUMNS, "
        "{[no_such_dim].[no_such_dim].[X], [no_such_dim].[no_such_dim].[Y]} ON ROWS "
        "FROM [demo]"
    )
    with pytest.raises(ValueError):
        _mdx_to_sql(mdx, MEASURES, DIMENSIONS)


def test_axis_bare_members_still_allowed_after_audit():
    # The audit must NOT reject a legitimate full-level expansion on the axis.
    mdx = (
        "SELECT {[Measures].[Amount]} ON COLUMNS, "
        "{[item_category].[item_category].Members} ON ROWS FROM [demo]"
    )
    sql, _ = _mdx_to_sql(mdx, MEASURES, DIMENSIONS)
    assert "item_category" in sql  # grouped, not filtered, no exception


@pytest.mark.parametrize(
    "method",
    ["members", "Members", "MEMBERS", "children", "Children",
     "allmembers", "AllMembers", "ALLMEMBERS"],
)
def test_axis_level_expansion_is_case_insensitive(method):
    # Codex review: MDX method names are case-insensitive. A 3-bracket-level
    # expansion in ANY case must remain a full-level expansion (no filter), not
    # be mis-read as a member and turned into a bogus `WHERE dim = 'level'`.
    out = _mdx_extract_axis_member_filters(
        f"{{[item_category].[item_category].[item_category].{method}}}",
        {"item_category"},
    )
    assert out == {}
