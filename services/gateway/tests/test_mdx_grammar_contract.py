"""Wave C #3 — MDX grammar has_error contract.

The Execute admission gate (``_parse_mdx_for_execute``) fails closed on
``root.has_error``, so the grammar's has_error signal must be RELIABLE: VALID
SSAS MDX must parse clean (has_error False) and genuinely malformed MDX must
error. This pins the committed grammar/parser.c/.so against a regeneration that
re-introduces a false positive or drops a malformed-rejection.
"""
from __future__ import annotations

import pytest

from src.dax.ts_mdx_parser import parse_mdx


VALID = [
    "SELECT {[Measures].[amount]} ON COLUMNS FROM [modely]",
    "// note\nSELECT {[Measures].[amount]} ON COLUMNS FROM [demo]",
    "-- note\nSELECT {[Measures].[amount]} ON COLUMNS FROM [demo]",
    "/* block */ SELECT {[Measures].[amount]} ON COLUMNS FROM [demo]",
    # NOTE: NESTED block comments (/* /* */ */) are NOT supported by the grammar's
    # regex comment token — they would need an external scanner and are never
    # emitted by real BI clients, so they are intentionally out of scope (the
    # strict gate rejects them with a clear fault). See lane report / Bug-9443.
    'SELECT Filter([Geo].[Geo].Members, Left([Geo].[Geo].CurrentMember.Name, 2) = "US") '
    "ON ROWS, {[Measures].[Amount]} ON COLUMNS FROM [m]",
    "SELECT {[Measures].[base_amount]} ON COLUMNS, {[region_dim].[region_dim].Members} "
    "ON ROWS FROM (SELECT ({[city_dim].[city_dim].&[Berlin]}) ON COLUMNS FROM [m])",
    "WITH SET FS As '{[a].[a].[All]}'\nSelect {[Measures].[R]} on ROWS, "
    "Hierarchize(Generate(FS, Ascendants([a].[a].currentmember))) "
    "DIMENSION PROPERTIES PARENT_UNIQUE_NAME, MEMBER_TYPE ON COLUMNS FROM [m]",
    "WITH MEMBER [Product].[Product].[G] AS AGGREGATE({[Product].[Product].[A], "
    "[Product].[Product].[B]}) SELECT {[Measures].[Sales]} ON COLUMNS FROM [m]",
    "WITH MEMBER [Measures].[D] AS '[Measures].[H] * 2' "
    "SELECT {[Measures].[D]} ON COLUMNS FROM [m]",
    "DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS FROM [m] "
    "WHERE ([Date].[Year].&[2025]) RETURN [Region].[City]",
    "SELECT {[Measures].[amount]} ON COLUMNS FROM [m] CELL PROPERTIES VALUE, FORMATTED_VALUE",
    # WC3-B1: valid WHERE slicers — a KPI/STRTOSET/STRTOMEMBER function, an inline
    # @param, a nested-paren tuple, and an arithmetic slicer must all parse clean.
    'SELECT FROM [modely] WHERE (KPIValue("Net Revenue"))',
    "SELECT {[Measures].[amount]} ON 0 FROM [m] WHERE (STRTOSET(@Region, CONSTRAINED))",
    "SELECT {[Measures].[amount]} ON 0 FROM [m] WHERE (STRTOMEMBER(@Year))",
    "SELECT {[Measures].[amount]} ON 0 FROM [m] WHERE "
    "(([geo].[geo].&[US]), ([time].[time].&[2024]))",
    "SELECT {[Measures].[amount]} ON 0 FROM [m] WHERE ([Measures].[amount] * 2)",
    "SELECT {[Measures].[amount]} ON 0 FROM [m] WHERE ([geo].[geo].&[US])",
]

MALFORMED = [
    "SELECT {[Measures].[amount]} ON COLUMNS FROM",   # no cube
    "SELECT {[Measures].[amount]} ON COLUMNS",         # no FROM
    "DROP TABLE users; SELECT 1",                       # not MDX
    "SELECT {[Measures].[x]} ON 0 FROM [c] WHERE ((((",  # unbalanced
    "SELECT {[Measures].[amount]} ON",                 # dangling ON
    # WC3-B1 guardrail: a permissive WHERE must NOT under-reject a malformed slicer.
    "SELECT {[Measures].[amount]} ON 0 FROM [m] WHERE (",        # unclosed
    "SELECT {[Measures].[amount]} ON 0 FROM [m] WHERE ()",       # empty tuple
    "SELECT {[Measures].[amount]} ON 0 FROM [m] WHERE ([geo].[geo].&[US]",  # unbalanced
    "SELECT {[Measures].[amount]} ON 0 FROM [m] WHERE",          # no tuple
]


@pytest.mark.parametrize("mdx", VALID)
def test_valid_mdx_parses_clean(mdx):
    assert parse_mdx(mdx).has_error is False, mdx


@pytest.mark.parametrize("mdx", MALFORMED)
def test_malformed_mdx_has_error(mdx):
    assert parse_mdx(mdx).has_error is True, mdx


def test_calc_member_quote_delimiters_are_stripped():
    # SSAS wraps calc bodies in single quotes; the walker must strip them so the
    # evaluator sees the raw expression, not a quoted string.
    p = parse_mdx(
        "WITH MEMBER [Measures].[D] AS '[Measures].[H] * 2' "
        "SELECT {[Measures].[D]} ON COLUMNS FROM [m]"
    )
    assert p.with_members[0].expression == "[Measures].[H] * 2"


def test_multi_atom_calc_body_beginning_and_ending_with_quote_not_mangled():
    # WC3-U1: a multi-atom calc body that merely BEGINS and ENDS with a quote
    # ('pre' + [M].[X] + 'post') must be returned VERBATIM, not have its outer
    # quotes stripped (which would corrupt the expression).
    p = parse_mdx(
        "WITH MEMBER [Measures].[C] AS 'pre' + [Measures].[X] + 'post' "
        "SELECT {[Measures].[C]} ON COLUMNS FROM [m]"
    )
    assert p.with_members[0].expression == "'pre' + [Measures].[X] + 'post'"


def test_nested_paren_where_members_extracted():
    # WC3-B1: a nested-paren WHERE tuple parses clean AND every member reaches
    # where_members (so the drillthrough Bug-3622 fail-loud filter audit sees it).
    p = parse_mdx(
        "SELECT {[Measures].[amount]} ON 0 FROM [m] WHERE "
        "(([geo].[geo].&[US]), ([time].[time].&[2024]))"
    )
    assert p.has_error is False
    cols = {tuple(wm.parts) for wm in p.where_members}
    assert ("geo", "geo", "&US") in cols
    assert ("time", "time", "&2024") in cols
