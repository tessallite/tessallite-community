"""Bug-6717: MEASURE_UNIQUE_NAME must MDX-escape ] inside bracketed names.

A measure name containing ']' produces a unique name clients cannot
round-trip unless ']' is doubled to ']]' inside the brackets.

Tests pin:
  1. The mdschema emitter doubles ] in measure unique names
  2. The Execute-side regex-based extractors accept ]] and unescape
  3. The member_uname parser (already correct) round-trips
  4. The mdx_calc_members extractor accepts ]] and unescapes
"""
from __future__ import annotations

import re
import pytest

from src.dax.mdschema import _escape_mdx_bracket, _rows_measures
from src.dax.mdx_execute import (
    _escape_mdx_bracket as _exec_escape,
    _extract_measure_names,
    _MEASURE_REF,
)
from src.dax.mdx_calc_members import _extract_member_name
from src.dax.member_uname import parse_member_uname


# --- Escape helper ---

def test_escape_no_bracket():
    assert _escape_mdx_bracket("Revenue") == "Revenue"


def test_escape_single_bracket():
    assert _escape_mdx_bracket("Revenue]Total") == "Revenue]]Total"


def test_escape_multiple_brackets():
    assert _escape_mdx_bracket("a]b]c") == "a]]b]]c"


def test_exec_escape_matches_mdschema():
    """The mdx_execute helper must agree with the mdschema helper."""
    for name in ("Revenue]Total", "a]b]c", "plain"):
        assert _exec_escape(name) == _escape_mdx_bracket(name)


# --- MDSCHEMA emitter ---

def test_rows_measures_unique_name_escaped():
    measures = [{"name": "Revenue]Total", "default_agg": "sum"}]
    rows = _rows_measures("cat", measures)
    assert len(rows) == 1
    assert rows[0]["MEASURE_UNIQUE_NAME"] == "[Measures].[Revenue]]Total]"
    # MEASURE_NAME stays raw (display name, not an MDX identifier)
    assert rows[0]["MEASURE_NAME"] == "Revenue]Total"


# --- _MEASURE_REF regex ---

def test_measure_ref_accepts_escaped_bracket():
    mdx = "SELECT {[Measures].[Revenue]]Total]} ON COLUMNS FROM [cube]"
    m = re.search(_MEASURE_REF, mdx)
    assert m is not None
    assert m.group(0) == "[Measures].[Revenue]]Total]"


def test_measure_ref_plain_still_works():
    mdx = "SELECT {[Measures].[Revenue]} ON COLUMNS FROM [cube]"
    m = re.search(_MEASURE_REF, mdx)
    assert m is not None
    assert m.group(0) == "[Measures].[Revenue]"


# --- Execute-side extractors ---

def test_extract_measure_names_plain():
    names = _extract_measure_names("{[Measures].[Revenue]}")
    assert names == ["Revenue"]


def test_extract_measure_names_escaped_bracket():
    names = _extract_measure_names("{[Measures].[Revenue]]Total]}")
    assert names == ["Revenue]Total"]


def test_extract_measure_names_multiple_escaped():
    expr = "{[Measures].[A]]B], [Measures].[Plain], [Measures].[C]]D]]E]}"
    names = _extract_measure_names(expr)
    assert names == ["A]B", "Plain", "C]D]E"]


# --- member_uname parser (already correct, regression guard) ---

def test_member_uname_measures_escaped():
    hier, level, grammar, keys = parse_member_uname("[Measures].[Revenue]]Total]")
    assert hier == "[Measures]"
    assert grammar == "measure"
    assert keys == ["Revenue]Total"]


def test_member_uname_measures_plain():
    hier, level, grammar, keys = parse_member_uname("[Measures].[Revenue]")
    assert hier == "[Measures]"
    assert grammar == "measure"
    assert keys == ["Revenue"]


# --- mdx_calc_members extractor ---

def test_calc_member_extract_escaped():
    name = _extract_member_name("[Measures].[Revenue]]Total]")
    assert name == "Revenue]Total"


def test_calc_member_extract_plain():
    name = _extract_member_name("[Measures].[Revenue]")
    assert name == "Revenue"


# --- _parse_where_measure (R1 F-1) ---

from src.dax.mdx_execute import _parse_where_measure


def test_parse_where_measure_plain():
    mdx = "SELECT {[Measures].[Revenue]} ON COLUMNS FROM [cube] WHERE ([Measures].[Revenue])"
    assert _parse_where_measure(mdx) == "Revenue"


def test_parse_where_measure_escaped_bracket():
    mdx = "SELECT {[Measures].[Rev]]Total]} ON COLUMNS FROM [cube] WHERE ([Measures].[Rev]]Total])"
    assert _parse_where_measure(mdx) == "Rev]Total"


# --- drillthrough _extract_measure_name (R1 F-2) ---

def test_drillthrough_extract_measure_plain():
    """Plain measure name extracted from DRILLTHROUGH COLUMNS axis."""
    from src.dax.drillthrough_handler import _extract_measure_name

    class FakeParsed:
        def axis_expr(self, axis):
            return "{[Measures].[Revenue]}" if axis == "COLUMNS" else None

    assert _extract_measure_name(FakeParsed()) == "Revenue"


def test_drillthrough_extract_measure_escaped_bracket():
    """Escaped ]] measure name extracted and unescaped from DRILLTHROUGH COLUMNS axis."""
    from src.dax.drillthrough_handler import _extract_measure_name

    class FakeParsed:
        def axis_expr(self, axis):
            return "{[Measures].[Rev]]Total]}" if axis == "COLUMNS" else None

    assert _extract_measure_name(FakeParsed()) == "Rev]Total"
