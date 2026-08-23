"""
F-003-01 — outer DAX TOPN and trailing clauses must be REJECTED, not narrowed.

The DAX normalizer used to recognise the query by a substring / ``\\b`` search
for ``SUMMARIZECOLUMNS`` / ``SUMMARIZE`` and then extract only the inner call's
parenthesised content. Any text OUTSIDE that call was silently discarded:

  * ``EVALUATE TOPN(1, SUMMARIZECOLUMNS(...), [Revenue], DESC)`` narrowed to the
    inner ``SUMMARIZECOLUMNS`` and returned ALL rows unsorted (the top-N request
    vanished);
  * ``EVALUATE SUMMARIZECOLUMNS(...) ORDER BY Sales[Region] DESC`` dropped the
    ORDER BY entirely.

The IR has no slot for TOPN / ORDER BY / START AT (``order_by=[]``,
``limit=None`` are hard-coded), so the only representable envelope is a single
top-level ``EVALUATE SUMMARIZECOLUMNS(...)`` / ``EVALUATE SUMMARIZE(...)`` with
nothing but whitespace around it. Anything else must FAIL LOUD with a typed
``UnsupportedSQL`` (mapped to 422 feature_not_supported), restoring the
loud-unsupported intent of closed Bug-5194 / Bug-3710.

Test escape: prior suites only exercised plain ``EVALUATE SUMMARIZECOLUMNS(...)``
inputs, so no test asserted that an OUTER wrapper or a TRAILING clause is
rejected rather than silently narrowed.
Guard: this file — top-level TOPN and trailing ORDER BY / START AT rejected.
Tier: T2 (fixed-bug regression guard).

Run from tessallite/services/query-router/:
    pytest tests/test_dax_envelope_f003_01.py
"""
from __future__ import annotations

import pytest

from src.parsing.dax_normalizer import parse_dax_to_ir
from src.ir.logical_query import UnsupportedSQL


# ---------------------------------------------------------------------------
# Supported single-call envelopes still parse (no regression).
# ---------------------------------------------------------------------------

def test_plain_summarizecolumns_still_parses():
    q = parse_dax_to_ir(
        'EVALUATE SUMMARIZECOLUMNS(Sales[Region], "Revenue", [Revenue])', "m1"
    )
    assert q.requested_dimensions == ["Region"]
    assert q.requested_measures == ["Revenue"]
    # The unrepresentable clauses stay empty because there are none.
    assert q.order_by == []
    assert q.limit is None


def test_plain_summarize_still_parses():
    q = parse_dax_to_ir(
        'EVALUATE SUMMARIZE(Sales, Sales[Region], "Revenue", SUM(Sales[Amount]))',
        "m1",
    )
    assert q.requested_dimensions == ["Region"]
    assert q.requested_measures == ["Amount"]


def test_leading_and_trailing_whitespace_tolerated():
    q = parse_dax_to_ir(
        '   EVALUATE   SUMMARIZECOLUMNS(Sales[Region])   \n', "m1"
    )
    assert q.requested_dimensions == ["Region"]


def test_balanced_paren_inside_string_literal_does_not_false_reject():
    # A BALANCED parenthesis INSIDE a measure alias string (``"Revenue (USD)"``)
    # is literal text and must not move the envelope's paren-depth counter, so the
    # envelope still recognises the single top-level call (Opus review NIT). A rare
    # UNBALANCED paren inside a string alias is left to fail safe downstream (loud
    # rejection), consistent with the existing argument splitter.
    q = parse_dax_to_ir(
        'EVALUATE SUMMARIZECOLUMNS(Sales[Region], "Revenue (USD)", [Revenue])',
        "m1",
    )
    assert q.requested_dimensions == ["Region"]
    assert q.requested_measures == ["Revenue"]


# ---------------------------------------------------------------------------
# F-003-01 — outer wrapper / trailing clause must be REJECTED (loud), not
# silently narrowed to the inner call.
# ---------------------------------------------------------------------------

def test_outer_topn_wrapper_rejected_not_narrowed():
    dax = (
        'EVALUATE TOPN(1, SUMMARIZECOLUMNS(Sales[Region], "Revenue", [Revenue]), '
        "[Revenue], DESC)"
    )
    with pytest.raises(UnsupportedSQL):
        parse_dax_to_ir(dax, "m1")


def test_trailing_order_by_rejected_not_dropped():
    dax = (
        'EVALUATE SUMMARIZECOLUMNS(Sales[Region], "Revenue", [Revenue]) '
        "ORDER BY Sales[Region] DESC"
    )
    with pytest.raises(UnsupportedSQL):
        parse_dax_to_ir(dax, "m1")


def test_trailing_order_by_on_summarize_rejected():
    dax = (
        'EVALUATE SUMMARIZE(Sales, Sales[Region], "Revenue", SUM(Sales[Amount])) '
        "ORDER BY Sales[Region]"
    )
    with pytest.raises(UnsupportedSQL):
        parse_dax_to_ir(dax, "m1")


def test_trailing_start_at_rejected():
    dax = "EVALUATE SUMMARIZECOLUMNS(Sales[Region]) START AT 5"
    with pytest.raises(UnsupportedSQL):
        parse_dax_to_ir(dax, "m1")


def test_topn_over_summarize_rejected():
    dax = (
        "EVALUATE TOPN(10, SUMMARIZE(Sales, Sales[Region], "
        '"Revenue", SUM(Sales[Amount])), [Revenue])'
    )
    with pytest.raises(UnsupportedSQL):
        parse_dax_to_ir(dax, "m1")


def test_second_statement_after_call_rejected():
    # Residue after the closing paren of the single top-level call.
    dax = "EVALUATE SUMMARIZECOLUMNS(Sales[Region]) EVALUATE SUMMARIZE(Sales)"
    with pytest.raises(UnsupportedSQL):
        parse_dax_to_ir(dax, "m1")


def test_bare_summarizecolumns_without_evaluate_rejected():
    # No EVALUATE head -> not a recognisable envelope.
    dax = "SUMMARIZECOLUMNS(Sales[Region], \"Revenue\", [Revenue])"
    with pytest.raises(UnsupportedSQL):
        parse_dax_to_ir(dax, "m1")
