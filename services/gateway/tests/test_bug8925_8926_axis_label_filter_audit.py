"""Bug-8925 + Bug-8926 — axis label filter vs the enumerated-member audit.

Bug-8925 (fails CLOSED): an Excel "Label Filter -> Begins With / Contains /
    Ends With" on a row axis faulted the whole Execute. The axis member audit
    saw `[Dim].[Hier]` inside `[Dim].[Hier].CurrentMember.Name` as an enumerated
    member that produced no WHERE filter, and rejected the query. The shipped
    feature was unavailable.

Bug-8926 (fails OPEN, worse in kind): two label predicates inside ONE `Filter()`
    call joined by `OR` were each extracted independently and rendered as two
    SQL clauses joined by `AND`. The query ran and silently returned a subset of
    the requested rows. The old accounting compared a COUNT of `Filter(` tokens
    against a COUNT of extracted specs, so one call producing two specs tested
    `1 > 2` and passed — and passed more easily the more predicates one call
    carried.

Third defect at the same site: the Left/Right character count was matched as a
    bare `\\d+` and never captured, so `Left(name, 2) = "USA"` — unsatisfiable in
    MDX — rendered as `LIKE 'usa%'` and returned rows.

The fail-closed cases below are the load-bearing Bug-1060 / Bug-5548 preservation
tests: the exemption must be occurrence-bound, never a token- or dimension-level
excuse.
"""
from __future__ import annotations

import inspect
import re

import pytest

from src.dax.xmla_server import (
    _AppliedLabelFilter,
    _assert_axis_member_references_applied,
    _assert_where_members_applied,
    _iter_mdx_filter_calls,
    _mdx_to_sql,
    _translate_label_filter_calls,
)


_MEASURES = [{"name": "Amount", "default_agg": "sum"}]
_DIMS = [{"name": "region"}, {"name": "product"}]


def _rows_mdx(rows_expr: str, cols_expr: str = "{[Measures].[Amount]}") -> str:
    return (
        f"SELECT {cols_expr} ON COLUMNS, {rows_expr} ON ROWS FROM [demo]"
    )


def _q(name: str) -> str:
    return f'"{name}"'


# ---------------------------------------------------------------------------
# 1 + 2 — positive: the shipped Excel label filters translate
# ---------------------------------------------------------------------------


def test_excel_begins_with_label_filter_translates_bug8925():
    """The exact Excel "Begins With" shape reaches SQL instead of faulting."""
    mdx = _rows_mdx(
        'Filter([region].[region].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "US")'
    )
    sql, protocol = _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")
    assert protocol == "jdbc"
    assert 'LOWER("region") LIKE \'us%\'' in sql
    assert 'GROUP BY "region"' in sql


@pytest.mark.parametrize(
    "condition,expected",
    [
        # begins with / does not begin with
        ('Left([region].[region].CurrentMember.Name, 2) = "US"',
         'LOWER("region") LIKE \'us%\''),
        ('Left([region].[region].CurrentMember.Name, 2) <> "US"',
         'LOWER("region") NOT LIKE \'us%\''),
        # contains / does not contain
        ('InStr([region].[region].CurrentMember.Name, "east") > 0',
         'LOWER("region") LIKE \'%east%\''),
        ('InStr([region].[region].CurrentMember.Name, "east") = 0',
         'LOWER("region") NOT LIKE \'%east%\''),
        # ends with / does not end with
        ('Right([region].[region].CurrentMember.Name, 4) = "East"',
         'LOWER("region") LIKE \'%east\''),
        ('Right([region].[region].CurrentMember.Name, 4) <> "East"',
         'LOWER("region") NOT LIKE \'%east\''),
    ],
)
def test_all_six_shipped_label_filter_forms_bug8925(condition, expected):
    mdx = _rows_mdx(f'Filter([region].[region].Members, {condition})')
    sql, protocol = _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")
    assert protocol == "jdbc"
    assert expected in sql


def test_separate_filter_calls_compose_conjunctively_bug8926():
    """Two label filters in SEPARATE (nested) calls stay supported.

    Nested `Filter()` calls compose conjunctively in MDX, so AND-joining the two
    rendered clauses is faithful. This is the supported way to express multiple
    label predicates — unlike two predicates inside ONE call, which is rejected.
    """
    mdx = _rows_mdx(
        'Filter('
        'Filter([region].[region].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "US"), '
        'InStr([region].[region].CurrentMember.Name, "east") > 0)'
    )
    sql, _ = _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")
    assert 'LOWER("region") LIKE \'us%\'' in sql
    assert 'LOWER("region") LIKE \'%east%\'' in sql


# ---------------------------------------------------------------------------
# Composition: a label filter alongside the other axis restriction channels.
# The exemption is occurrence-bound, so every other channel must still apply.
# ---------------------------------------------------------------------------


def test_label_filter_composes_with_enumerated_set_on_another_dim_bug8925():
    mdx = _rows_mdx(
        'CrossJoin('
        'Filter([region].[region].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "US"), '
        '{[product].[product].&[A], [product].[product].&[B]})'
    )
    sql, _ = _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")
    assert '"product" IN (\'A\', \'B\')' in sql
    assert 'LOWER("region") LIKE \'us%\'' in sql


def test_label_filters_on_two_dimensions_both_apply_bug8925():
    """Two dimensions, each filtered over ITS OWN set, CrossJoin'd.

    This is the Excel shape for a label filter on two row fields, and the only
    two-dimension shape the translator accepts: each ``Filter()`` iterates the
    set of the dimension its condition names. CrossJoin means the cartesian
    product of the two filtered sets, so AND-joining the rendered clauses is
    faithful.
    """
    mdx = _rows_mdx(
        'CrossJoin('
        'Filter([region].[region].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "US"), '
        'Filter([product].[product].Members, '
        'InStr([product].[product].CurrentMember.Name, "x") > 0))'
    )
    sql, _ = _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")
    assert 'LOWER("region") LIKE \'us%\'' in sql
    assert 'LOWER("product") LIKE \'%x%\'' in sql


def test_label_filter_composes_with_a_where_slicer_bug8925():
    """The axis exemption must not leak into the WHERE slicer's own filter."""
    mdx = (
        "SELECT {[Measures].[Amount]} ON COLUMNS, "
        'Filter([region].[region].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "US") ON ROWS '
        "FROM [demo] WHERE ([product].[product].&[A])"
    )
    sql, _ = _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")
    assert '"product" = \'A\'' in sql
    assert 'LOWER("region") LIKE \'us%\'' in sql


def test_label_filter_on_the_columns_axis_bug8925():
    """Both axes are audited from the one concatenated axis_text."""
    mdx = (
        "SELECT Filter([region].[region].Members, "
        'Left([region].[region].CurrentMember.Name, 2) = "US") ON COLUMNS, '
        "{[product].[product].Members} ON ROWS FROM [demo]"
    )
    sql, _ = _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")
    assert 'LOWER("region") LIKE \'us%\'' in sql


# ---------------------------------------------------------------------------
# 4 — an unsupported .CurrentMember function stays rejected
# ---------------------------------------------------------------------------


def test_unsupported_currentmember_function_still_rejected_bug8925():
    """`UCase(...)` is not a translatable label predicate: fail closed.

    This is the Bug-1060 preservation case a global `.CurrentMember` token
    exemption would have broken.
    """
    mdx = _rows_mdx(
        'Filter([region].[region].Members, '
        'UCase([region].[region].CurrentMember.Name) = "US")'
    )
    with pytest.raises(ValueError, match="Unsupported Filter"):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")


# ---------------------------------------------------------------------------
# 5 + 9 — composite conditions inside ONE Filter() call are rejected
# ---------------------------------------------------------------------------


# The single-bracket dimension form (`[region].CurrentMember.Name`) is the one
# that FAILED OPEN on the pre-fix tip: the audit's single-bracket pattern needs a
# second bracket, so it never matched, the query ran, and the composite condition
# was mistranslated. These four are therefore the load-bearing Bug-8926
# regression guards — the two-bracket variants above were already rejected before
# the fix (for the wrong reason: the Bug-8925 fault).


def test_or_composite_single_bracket_ran_with_and_semantics_bug8926():
    """Pre-fix this produced
    `LOWER("region") LIKE 'us%' AND LOWER("region") LIKE '%east'`
    for an OR condition, silently returning too few rows."""
    mdx = _rows_mdx(
        'Filter([region].[region].Members, '
        'Left([region].CurrentMember.Name, 2) = "US" '
        'OR Right([region].CurrentMember.Name, 4) = "East")'
    )
    with pytest.raises(ValueError, match="Unsupported Filter"):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")


def test_and_composite_single_bracket_rejected_bug8926():
    mdx = _rows_mdx(
        'Filter([region].[region].Members, '
        'Left([region].CurrentMember.Name, 2) = "US" '
        'AND Right([region].CurrentMember.Name, 4) = "East")'
    )
    with pytest.raises(ValueError, match="Unsupported Filter"):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")


def test_label_plus_measure_predicate_single_bracket_rejected_bug8926():
    """Pre-fix the `[Measures].[Amount] > 100` half of the condition was
    silently DROPPED — no HAVING was emitted at all."""
    mdx = _rows_mdx(
        'Filter([region].[region].Members, '
        'Left([region].CurrentMember.Name, 2) = "US" '
        'AND [Measures].[Amount] > 100)'
    )
    with pytest.raises(ValueError, match="Unsupported Filter"):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")


def test_count_mismatch_single_bracket_rejected_bug8925():
    """Pre-fix `Left(name, 2) = "USA"` rendered as `LIKE 'usa%'` and returned
    rows for a condition MDX can never satisfy."""
    mdx = _rows_mdx(
        'Filter([region].[region].Members, '
        'Left([region].CurrentMember.Name, 2) = "USA")'
    )
    with pytest.raises(ValueError, match="Unsupported Filter"):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")


def test_single_bracket_label_filter_still_translates_bug8925():
    """Control: the single-bracket form that worked before still works."""
    mdx = _rows_mdx(
        'Filter([region].[region].Members, '
        'Left([region].CurrentMember.Name, 2) = "US")'
    )
    sql, _ = _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")
    assert 'LOWER("region") LIKE \'us%\'' in sql


def test_label_filter_plus_unsupported_condition_rejected_bug8926():
    """A recognised predicate does not license the rest of the condition."""
    mdx = _rows_mdx(
        'Filter([region].[region].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "US" '
        'AND UCase([region].[region].CurrentMember.Name) = "X")'
    )
    with pytest.raises(ValueError, match="Unsupported Filter"):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")


def test_or_joined_label_predicates_in_one_call_rejected_bug8926():
    """The Bug-8926 wrong-numbers case: OR must never render as AND.

    On the pre-fix code this produced
    `... LIKE 'us%' AND ... LIKE '%east'` and returned too few rows.
    """
    mdx = _rows_mdx(
        'Filter([region].[region].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "US" '
        'OR Right([region].[region].CurrentMember.Name, 4) = "East")'
    )
    with pytest.raises(ValueError, match="Unsupported Filter"):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")


def test_and_joined_label_predicates_in_one_call_rejected_bug8926():
    """AND is rejected too — Boolean semantics inside one call are not
    implemented, and silently reinterpreting half a condition is the fault."""
    mdx = _rows_mdx(
        'Filter([region].[region].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "US" '
        'AND Right([region].[region].CurrentMember.Name, 4) = "East")'
    )
    with pytest.raises(ValueError, match="Unsupported Filter"):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")


def test_label_predicate_mixed_with_measure_predicate_rejected_bug8926():
    mdx = _rows_mdx(
        'Filter([region].[region].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "US" '
        'AND [Measures].[Amount] > 100)'
    )
    with pytest.raises(ValueError, match="Unsupported Filter"):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")


# ---------------------------------------------------------------------------
# 6 — the exemption is occurrence-bound, not dimension-level
# ---------------------------------------------------------------------------


def test_translated_filter_does_not_exempt_other_currentmember_same_dim_bug8925():
    """A second `.CurrentMember` on the SAME dimension is still rejected.

    This is the counter-example that rules out a dimension-level exemption:
    only one of the two references was translated.
    """
    axis_text = (
        'Filter([region].[region].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "US") '
        '[region].[region].CurrentMember'
    )
    applied = _translate_label_filter_calls(
        axis_text, {"region"}, {}, {}, quote_fn=_q,
    )
    assert len(applied) == 1
    with pytest.raises(ValueError, match="could not be applied as a filter"):
        _assert_axis_member_references_applied(
            axis_text, {}, {"region"},
            translated_label_filters=applied,
        )


def test_translated_filter_exempts_only_its_own_occurrence_bug8925():
    """Positive control for the test above: with the second occurrence removed
    the SAME evidence lets the audit pass. The difference is the occurrence,
    not the dimension."""
    axis_text = (
        'Filter([region].[region].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "US")'
    )
    applied = _translate_label_filter_calls(
        axis_text, {"region"}, {}, {}, quote_fn=_q,
    )
    assert len(applied) == 1
    _assert_axis_member_references_applied(
        axis_text, {}, {"region"}, translated_label_filters=applied,
    )


# ---------------------------------------------------------------------------
# 7 — an unapplied enumerated member elsewhere on the axis is still rejected
# ---------------------------------------------------------------------------


def test_label_filter_does_not_exempt_unapplied_enumerated_member_bug8925():
    """A single-bracket attribute member on the SAME dimension produces no
    filter (Bug-1060's uncaptured shape) and must still fail the query."""
    mdx = _rows_mdx(
        '{Filter([region].[region].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "US"), '
        '[region].&[X]}'
    )
    with pytest.raises(ValueError, match="could not be applied as a filter"):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")


def test_label_filter_does_not_exempt_unknown_dimension_member_bug8925():
    """An enumerated member on an unresolvable dimension still fails loud."""
    mdx = _rows_mdx(
        '{Filter([region].[region].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "US"), '
        '[no_such_dim].[no_such_dim].[X]}'
    )
    with pytest.raises(ValueError, match="unknown dimension or hierarchy"):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")


# ---------------------------------------------------------------------------
# 8 — an unknown label-filter dimension / hierarchy stays rejected
# ---------------------------------------------------------------------------


def test_unknown_label_filter_dimension_rejected_bug8925():
    mdx = _rows_mdx(
        'Filter([no_such_dim].[no_such_dim].Members, '
        'Left([no_such_dim].[no_such_dim].CurrentMember.Name, 2) = "US")'
    )
    with pytest.raises(ValueError):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")


def test_unknown_label_filter_dimension_produces_no_evidence_bug8925():
    """No resolution -> no rendered clause -> no exemption evidence at all."""
    axis_text = (
        'Filter([no_such_dim].[no_such_dim].Members, '
        'Left([no_such_dim].[no_such_dim].CurrentMember.Name, 2) = "US")'
    )
    assert _translate_label_filter_calls(
        axis_text, {"region"}, {}, {}, quote_fn=_q,
    ) == []


# ---------------------------------------------------------------------------
# 10 — the Left/Right character count must agree with the literal
# ---------------------------------------------------------------------------


def test_left_count_disagreeing_with_literal_rejected_bug8925():
    """`Left(name, 2) = "USA"` is unsatisfiable in MDX; it used to render as
    `LIKE 'usa%'` and return rows because the count was never captured."""
    mdx = _rows_mdx(
        'Filter([region].[region].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "USA")'
    )
    with pytest.raises(ValueError, match="Unsupported Filter"):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")


def test_right_count_disagreeing_with_literal_rejected_bug8925():
    mdx = _rows_mdx(
        'Filter([region].[region].Members, '
        'Right([region].[region].CurrentMember.Name, 9) = "East")'
    )
    with pytest.raises(ValueError, match="Unsupported Filter"):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")


# ---------------------------------------------------------------------------
# 11 — the WHERE-slicer path (Bug-1060) is unchanged
# ---------------------------------------------------------------------------


def test_where_slicer_guard_still_fires_on_unapplied_member_bug1060():
    """The original Bug-1060 guard, unchanged by the axis-audit split."""
    with pytest.raises(ValueError, match="could not be applied as a filter"):
        _assert_where_members_applied(
            "[region].[region].&[4]", {}, {"region"},
        )


def test_where_slicer_guard_still_fires_on_unknown_dimension_bug1060():
    with pytest.raises(ValueError, match="unknown dimension or hierarchy"):
        _assert_where_members_applied(
            "[no_such_dim].[no_such_dim].&[4]", {}, {"region"},
        )


def test_where_slicer_audit_cannot_receive_exemption_evidence_bug8925():
    """Structural proof the axis fix cannot weaken the WHERE path.

    `_assert_where_members_applied` has no parameter through which a label
    filter, a span, or any other exemption can be threaded, so the WHERE slicer
    audit is unreachable from the label-filter channel by construction.
    """
    params = set(inspect.signature(_assert_where_members_applied).parameters)
    assert params == {
        "where_expr", "where_filters", "dim_names",
        "hierarchy_level_dim_map", "hierarchy_default_dim_map",
    }


def test_where_slicer_still_rejects_label_filter_syntax_bug8925():
    """A `.CurrentMember` reference in a WHERE slicer is NOT exempted: the
    label-filter channel only ever restricts an axis."""
    with pytest.raises(ValueError, match="could not be applied as a filter"):
        _assert_where_members_applied(
            'Left([region].[region].CurrentMember.Name, 2) = "US"',
            {}, {"region"},
        )


# ---------------------------------------------------------------------------
# Blind-spot audit of the span mechanism itself (spec addition 2)
# ---------------------------------------------------------------------------


def test_spans_are_measured_on_the_unnormalized_axis_text_bug8925():
    """A string that "wants" normalizing is handled without normalizing it.

    Irregular whitespace and a newline inside the `Filter()` call: extraction and
    the audit must both work on this exact string.
    """
    axis_text = (
        'Filter(  [region].[region].Members ,\n'
        '        Left([region].[region].CurrentMember.Name,  2)  =  "US"  )'
    )
    applied = _translate_label_filter_calls(
        axis_text, {"region"}, {}, {}, quote_fn=_q,
    )
    assert len(applied) == 1
    start, end = applied[0].context_span
    assert axis_text[start:end] == "[region].[region].CurrentMember.Name"
    _assert_axis_member_references_applied(
        axis_text, {}, {"region"}, translated_label_filters=applied,
    )


def test_span_measured_on_a_different_string_fails_closed_bug8925():
    """The blind spot itself: a span measured on a normalized variant.

    A shifted span would exempt the WRONG bracket reference — an exemption is
    the unsafe direction — so the audit re-slices and refuses rather than
    exempting anything it cannot verify.
    """
    raw = (
        'Filter([region].[region].Members,\n'
        '   Left([region].[region].CurrentMember.Name, 2) = "US")'
    )
    normalized = re.sub(r"\s+", " ", raw)
    assert normalized != raw
    applied = _translate_label_filter_calls(
        normalized, {"region"}, {}, {}, quote_fn=_q,
    )
    assert len(applied) == 1
    with pytest.raises(ValueError, match="does not match the audited axis text"):
        _assert_axis_member_references_applied(
            raw, {}, {"region"}, translated_label_filters=applied,
        )


def test_out_of_range_span_fails_closed_bug8925():
    """A span pointing past the end of the audited text grants nothing."""
    axis_text = "[region].[region].&[X]"
    bogus = _AppliedLabelFilter(
        dim_ref="region", operation="begins_with", value="US", negated=False,
        context_span=(0, len(axis_text) + 50),
        context_text="[region].[region].CurrentMember.Name",
        filter_call_span=(0, 1),
        sql_clause="LOWER(\"region\") LIKE 'us%'",
    )
    with pytest.raises(ValueError, match="does not match the audited axis text"):
        _assert_axis_member_references_applied(
            axis_text, {}, {"region"}, translated_label_filters=(bogus,),
        )


def test_exemption_requires_containment_not_overlap_bug8925():
    """A span that only OVERLAPS a member reference must not exempt it.

    Containment is the deliberate choice: text outside a proven translated
    context was never proven translated.
    """
    axis_text = "[region].[region].&[X]"
    # A context that starts mid-reference: it overlaps the audited match but
    # does not contain it.
    partial = axis_text[8:]
    overlapping = _AppliedLabelFilter(
        dim_ref="region", operation="begins_with", value="US", negated=False,
        context_span=(8, len(axis_text)),
        context_text=partial,
        filter_call_span=(0, 1),
        sql_clause="LOWER(\"region\") LIKE 'us%'",
    )
    with pytest.raises(ValueError, match="could not be applied as a filter"):
        _assert_axis_member_references_applied(
            axis_text, {}, {"region"},
            translated_label_filters=(overlapping,),
        )


# ---------------------------------------------------------------------------
# The Filter()-call parser fails closed on shapes it cannot account for
# ---------------------------------------------------------------------------


def test_unbalanced_filter_call_fails_closed_bug8926():
    calls = _iter_mdx_filter_calls('Filter([region].[region].Members, Left(')
    assert len(calls) == 1
    assert calls[0].cond_span is None
    mdx = _rows_mdx('Filter([region].[region].Members, Left(')
    with pytest.raises(ValueError, match="Unsupported Filter"):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")


def test_filter_call_without_condition_argument_fails_closed_bug8926():
    calls = _iter_mdx_filter_calls("Filter([region].[region].Members)")
    assert len(calls) == 1
    assert calls[0].cond_span is None
    mdx = _rows_mdx("Filter([region].[region].Members)")
    with pytest.raises(ValueError, match="Unsupported Filter"):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")


def test_parenthesis_inside_string_literal_does_not_mis_bound_condition():
    """Depth tracking ignores parentheses inside a double-quoted literal."""
    axis_text = (
        'Filter([region].[region].Members, '
        'InStr([region].[region].CurrentMember.Name, "a)b") > 0)'
    )
    calls = _iter_mdx_filter_calls(axis_text)
    assert len(calls) == 1
    assert calls[0].cond_text.strip() == (
        'InStr([region].[region].CurrentMember.Name, "a)b") > 0'
    )
    applied = _translate_label_filter_calls(
        axis_text, {"region"}, {}, {}, quote_fn=_q,
    )
    assert len(applied) == 1
    assert applied[0].value == "a)b"


def test_every_filter_occurrence_must_be_consumed_bug8926():
    """A second, untranslatable `Filter()` call is never excused by the first."""
    mdx = _rows_mdx(
        '{Filter([region].[region].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "US"), '
        'Filter([product].[product].Members, '
        'UCase([product].[product].CurrentMember.Name) = "X")}'
    )
    with pytest.raises(ValueError, match="Unsupported Filter"):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")


# ---------------------------------------------------------------------------
# Finding XMLA-LF-B1 — sibling label filters under a SET-COMBINING function mean OR
#
# The Bug-8926 guard proved consumption per SYNTACTIC UNIT (one `Filter()`
# call). It said nothing about how the units are combined with each other. Two
# `Filter()` calls under `Union()` each translate cleanly, each is consumed, and
# their clauses are then AND-joined — but MDX `Union` means OR, so the query
# silently returns a SUBSET of the requested rows. Same wrong-numbers class as
# Bug-8926, expressed across calls instead of inside one.
# ---------------------------------------------------------------------------


def test_union_of_two_label_filters_rejected_xmla_lf_b1():
    """`Union(Filter(...), Filter(...))` is OR; AND-joining it loses rows."""
    mdx = _rows_mdx(
        'Union('
        'Filter([region].[region].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "US"), '
        'Filter([region].[region].Members, '
        'Right([region].[region].CurrentMember.Name, 4) = "East"))'
    )
    with pytest.raises(ValueError, match="combined with other set elements"):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")


def test_union_of_label_filters_on_two_dimensions_rejected_xmla_lf_b1():
    """Different dimensions does not make Union conjunctive either."""
    mdx = _rows_mdx(
        'Union('
        'Filter([region].[region].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "US"), '
        'Filter([product].[product].Members, '
        'InStr([product].[product].CurrentMember.Name, "x") > 0))'
    )
    with pytest.raises(ValueError, match="combined with other set elements"):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")


def test_braced_multi_element_set_of_label_filters_rejected_xmla_lf_b1():
    """A multi-element `{a, b}` set literal is a union, exactly like Union()."""
    mdx = _rows_mdx(
        '{Filter([region].[region].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "US"), '
        'Filter([region].[region].Members, '
        'Right([region].[region].CurrentMember.Name, 4) = "East")}'
    )
    with pytest.raises(ValueError, match="combined with other set elements"):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")


def test_label_filter_unioned_with_an_enumerated_member_rejected_xmla_lf_b1():
    """One label filter is enough: its sibling element is OR'd, not AND'd.

    `Union(Filter(region begins "US"), [region].[region].&[East])` asks for
    US-prefixed regions OR East. The enumerated member resolves and produces a
    WHERE filter, so the axis audit is satisfied and the two restrictions were
    then AND-joined into an empty result.
    """
    mdx = _rows_mdx(
        'Union('
        'Filter([region].[region].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "US"), '
        '{[region].[region].&[East]})'
    )
    with pytest.raises(ValueError, match="combined with other set elements"):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")


def test_nested_filter_calls_still_and_after_the_union_guard_xmla_lf_b1():
    """Positive control: NESTED `Filter(Filter(...), ...)` composes as AND.

    Nesting restricts the already-restricted set, so AND-joining the rendered
    clauses is faithful. The set-combination guard must not touch it.
    """
    mdx = _rows_mdx(
        'Filter('
        'Filter([region].[region].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "US"), '
        'InStr([region].[region].CurrentMember.Name, "east") > 0)'
    )
    sql, _ = _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")
    where = sql.split(" WHERE ", 1)[1]
    assert 'LOWER("region") LIKE \'us%\'' in where
    assert 'LOWER("region") LIKE \'%east%\'' in where
    assert " AND " in where


def test_crossjoin_of_two_label_filters_still_ands_xmla_lf_b1():
    """Positive control: CrossJoin is the cartesian product — AND is faithful."""
    mdx = _rows_mdx(
        'CrossJoin('
        'Filter([region].[region].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "US"), '
        'Filter([product].[product].Members, '
        'InStr([product].[product].CurrentMember.Name, "x") > 0))'
    )
    sql, _ = _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")
    assert 'LOWER("region") LIKE \'us%\'' in sql
    assert 'LOWER("product") LIKE \'%x%\'' in sql


def test_single_element_braced_label_filter_still_translates_xmla_lf_b1():
    """`{Filter(...)}` is grouping, not combination — it must keep working."""
    mdx = _rows_mdx(
        '{Filter([region].[region].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "US")}'
    )
    sql, _ = _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")
    assert 'LOWER("region") LIKE \'us%\'' in sql


def test_label_filter_under_an_unrecognised_combiner_fails_closed_xmla_lf_b1():
    """An unknown multi-argument enclosing function is REJECTED, not assumed
    conjunctive. Only CrossJoin and Filter nesting are proven AND."""
    mdx = _rows_mdx(
        'Hierarchize('
        'Filter([region].[region].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "US"), '
        'POST)'
    )
    with pytest.raises(ValueError, match="combined with other set elements"):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")


# ---------------------------------------------------------------------------
# Finding XMLA-LF-B2 — the Filter() SET argument must be the condition's dimension
#
# The set argument was parsed ONLY to find the top-level comma. Nothing checked
# that the dimension named in the CONDITION is the dimension of the set being
# iterated, so `Filter([Product].[Product].Members, Left([Region]...))` was
# accepted and became a row-level `region LIKE 'us%'` — a restriction on a
# dimension whose current member comes from surrounding context, not from the
# iterated set. Silently incorrect filtering, and dimension extraction can add
# the condition's dimension to the SQL grain on top of that.
# ---------------------------------------------------------------------------


def test_set_argument_dimension_must_match_condition_dimension_xmla_lf_b2():
    mdx = _rows_mdx(
        'Filter([product].[product].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "US")'
    )
    with pytest.raises(ValueError, match="Unsupported Filter"):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")


def test_mismatched_set_argument_produces_no_evidence_xmla_lf_b2():
    """No agreement -> no translation -> no rendered clause, no exemption."""
    axis_text = (
        'Filter([product].[product].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "US")'
    )
    assert _translate_label_filter_calls(
        axis_text, {"region", "product"}, {}, {}, quote_fn=_q,
    ) == []


def test_nested_filter_over_another_dimensions_set_rejected_xmla_lf_b2():
    """The nested form of the same defect: the outer call iterates regions and
    tests the PRODUCT current member. Supported before this guard."""
    mdx = _rows_mdx(
        'Filter('
        'Filter([region].[region].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "US"), '
        'InStr([product].[product].CurrentMember.Name, "x") > 0)'
    )
    with pytest.raises(ValueError, match="Unsupported Filter"):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")


def test_ambiguous_multi_dimension_set_argument_fails_closed_xmla_lf_b2():
    """A set spanning two dimensions cannot be proven to be the condition's
    own set, so it is refused rather than accepted on a partial match."""
    mdx = _rows_mdx(
        'Filter('
        'CrossJoin([region].[region].Members, [product].[product].Members), '
        'Left([region].[region].CurrentMember.Name, 2) = "US")'
    )
    with pytest.raises(ValueError, match="Unsupported Filter"):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")


def test_unresolvable_set_argument_fails_closed_xmla_lf_b2():
    """An unresolvable set argument is not accepted just because the CONDITION
    resolved — 'could not parse it' is never a reason to accept."""
    axis_text = (
        'Filter([no_such_dim].[no_such_dim].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "US")'
    )
    assert _translate_label_filter_calls(
        axis_text, {"region", "product"}, {}, {}, quote_fn=_q,
    ) == []


def test_partly_unresolvable_set_argument_fails_closed_xmla_lf_b2():
    """EVERY reference in the set must resolve, not merely one of them.

    Otherwise "the condition agrees with the bit I could read" licenses a filter
    on a dimension the query does not actually iterate.
    """
    axis_text = (
        'Filter({[region].[region].&[US-East], [no_such_dim].[no_such_dim].&[X]}, '
        'Left([region].[region].CurrentMember.Name, 2) = "US")'
    )
    assert _translate_label_filter_calls(
        axis_text, {"region", "product"}, {}, {}, quote_fn=_q,
    ) == []


def test_bracket_inside_a_string_literal_is_not_a_set_dimension_xmla_lf_b2():
    """A nested `Filter()` set argument carries that call's condition. A bracket
    inside its string literal is not an unresolvable dimension."""
    axis_text = (
        'Filter('
        'Filter([region].[region].Members, '
        'InStr([region].[region].CurrentMember.Name, "[b]c") > 0), '
        'Left([region].[region].CurrentMember.Name, 2) = "US")'
    )
    applied = _translate_label_filter_calls(
        axis_text, {"region"}, {}, {}, quote_fn=_q,
    )
    assert len(applied) == 2
    assert {lf.dim_ref for lf in applied} == {"region"}


def test_enumerated_member_set_argument_on_the_same_dimension_translates_xmla_lf_b2():
    """Positive control: the set need not be `.Members`, only the same
    dimension. An explicit member list over the condition's own dimension is
    still a set of that dimension."""
    axis_text = (
        'Filter({[region].[region].&[US-East], [region].[region].&[US-West]}, '
        'Left([region].[region].CurrentMember.Name, 2) = "US")'
    )
    applied = _translate_label_filter_calls(
        axis_text, {"region"}, {}, {}, quote_fn=_q,
    )
    assert len(applied) == 1
    assert applied[0].dim_ref == "region"


def test_set_argument_dimension_resolves_through_a_hierarchy_xmla_lf_b2():
    """Set and condition may spell the dimension differently as long as both
    resolve to the SAME grain column."""
    axis_text = (
        'Filter([Geo].[Geo].Members, '
        'Left([Geo].CurrentMember.Name, 2) = "US")'
    )
    applied = _translate_label_filter_calls(
        axis_text, {"country"},
        {"geo": {"country": "country"}}, {"geo": "country"},
        quote_fn=_q,
    )
    assert len(applied) == 1
    assert applied[0].dim_ref == "country"


# ---------------------------------------------------------------------------
# The subtotal / grand-total GRAIN queries carry the same label filter (F-002-02)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subtotal_grain_queries_carry_the_label_filter_f00202(monkeypatch):
    """Every grain query must be filtered exactly like the detail query.

    Otherwise an expanded pivot shows filtered detail rows beneath an
    unfiltered subtotal / grand total — the Bug-1050 class of wrong number.
    This is the third consumer of the shared label-filter translator (the
    detail SQL and the AVG/COUNT_DISTINCT re-query are the other two); before
    this test, neutering it left the whole gateway suite green.
    """
    from defusedxml import ElementTree as ET

    from src.dax import xmla_server

    captured: list[str] = []

    async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def fake_measures(model_id, tenant_slug, jwt_token, **kw):
        return [{"id": "m1", "name": "Amount", "default_agg": "sum"}]

    async def fake_dims(model_id, tenant_slug, jwt_token, **kw):
        return [{"id": "d1", "name": "country"}, {"id": "d2", "name": "city"}]

    async def fake_hiers(model_id, tenant_slug, jwt_token, **kw):
        return [{
            "name": "Geo",
            "levels": [
                {"name": "country", "ordinal": 0},
                {"name": "city", "ordinal": 1},
            ],
        }]

    async def fake_named_sets(*a, **kw):
        return []

    async def fake_execute_query(sql, model_id, tenant_slug, jwt_token,
                                 protocol="dax", **_kw):
        captured.append(sql)
        return {
            "columns": ["country", "city", "Amount"],
            "rows": [
                {"country": "US", "city": "US-East", "Amount": 100},
                {"country": "US", "city": "US-West", "Amount": 200},
            ],
        }

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_dims)
    monkeypatch.setattr(xmla_server, "get_model_hierarchies", fake_hiers)
    monkeypatch.setattr(xmla_server, "get_model_named_sets", fake_named_sets)
    monkeypatch.setattr(xmla_server, "execute_query", fake_execute_query)

    execute_xml = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command>
        <Statement>
SELECT
  Filter([Geo].[Geo].Members,
    Left([Geo].[Geo].CurrentMember.Name, 2) = "US") ON ROWS,
  {[Measures].[Amount]} ON COLUMNS
FROM [m]
        </Statement>
      </Command>
      <Properties><PropertyList><Catalog>m</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""

    root = ET.fromstring(execute_xml)
    method_el = xmla_server._find_method(root)
    assert method_el is not None
    response = await xmla_server._handle_execute(
        method_el, tenant_slug="demo", jwt_token="tok", session_id="sid-f00202",
    )
    body = response.body.decode("utf-8", "replace")
    assert "Fault" not in body, f"Execute faulted: {body}"

    assert len(captured) > 1, (
        f"expected a detail query plus at least one subtotal GRAIN query; "
        f"got {captured}"
    )
    for sql in captured:
        assert "LIKE 'us%'" in sql, (
            f"a query ran WITHOUT the label filter — a subtotal or grand total "
            f"would sit above filtered detail rows: {sql}"
        )


def test_evidence_carries_the_rendered_clause_that_reaches_sql_bug8925():
    """The exemption is a proof: the clause that earned it is the clause used."""
    axis_text = (
        'Filter([region].[region].Members, '
        'Left([region].[region].CurrentMember.Name, 2) = "US")'
    )
    applied = _translate_label_filter_calls(
        axis_text, {"region"}, {}, {},
        quote_fn=lambda n: f'"{n}"',
    )
    assert applied[0].sql_clause == "LOWER(\"region\") LIKE 'us%' ESCAPE '\\'"
    mdx = _rows_mdx(axis_text)
    sql, _ = _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="demo")
    assert applied[0].sql_clause in sql
