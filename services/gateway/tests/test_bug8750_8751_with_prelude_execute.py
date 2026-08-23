"""Bug-8750 + Bug-8751: a ``WITH`` prelude must not corrupt axis extraction, and
a Measures-namespace calculated member must not be resolved as a SQL column.

Both were found while proving Bug-8379 end-to-end, and together they made the
whole calculated-member ("Show Values As") family unreachable through XMLA
Execute:

Bug-8750 — ``_mdx_axis_expr(mdx, 0)`` fell back to
``(?:SELECT|,)\\s+(.*?)\\s+ON\\s+(?:COLUMNS|0)`` and ``re.search`` takes the
LEFTMOST anchor, so ANY comma inside the ``WITH MEMBER ... AS ...`` prelude
anchored the axis-0 capture mid-expression. The captured fragment then read as
axis content: an enumerated member in it became a keep-only ``WHERE`` filter on
the MAIN detail SQL (a pivot asking for every product silently returned one), a
ROWS-axis dimension was attributed to the COLUMNS axis (wrong % of Row/Column
Total split), and an unfilterable reference tripped the Bug-1060 fail-loud audit
and refused the Execute outright. ``_mdx_axis_has_non_empty`` carried the
identical fallback and the identical hole.

Bug-8751 — ``_mdx_to_sql`` excluded the Info/trust measures (Bug-6887) and the
KPI goal/status constants (Bug-6888) from SQL measure resolution but not the
statement's OWN ``WITH MEMBER [Measures].[X]`` declarations, so every such pivot
faulted with "Measure not available to this persona: X" before issuing any SQL.

Test escape: every calc-member test either called ``mdx_calc_members`` /
``build_real_execute_response`` directly, or drove ``_handle_execute`` with a
DIMENSION-namespace member — and mocked ``execute_query`` to return fixed rows
regardless of the SQL, so a spurious ``WHERE`` was invisible. Guard: this module
asserts the SQL TEXT and the rendered cell values. Tier: T2.
"""
from __future__ import annotations

import re

import pytest
from defusedxml import ElementTree as ET

from src.dax.xmla_server import (
    _mdx_axis_expr,
    _mdx_axis_has_non_empty,
    _mdx_declared_calc_measures,
    _mdx_extract_axis_member_filters,
    _mdx_to_sql,
)

_AGG_PRELUDE_STMT = (
    "WITH MEMBER [Product].[Product].[Grp] AS "
    "AGGREGATE({[Product].[Product].&[P1], [Product].[Product].&[P2]})\n"
    "SELECT {[Measures].[Amt]} ON COLUMNS, "
    "{[Product].[Product].Members} ON ROWS FROM [m]"
)

_PCT_PRELUDE_STMT = (
    "WITH MEMBER [Measures].[PctGT] AS "
    "'[Measures].[Amt] / ([Measures].[Amt], [Product].[Product].[All])' "
    "SELECT {[Measures].[Amt], [Measures].[PctGT]} ON COLUMNS, "
    "{[Product].[Product].Members} ON ROWS FROM [m]"
)

_MEASURES = [{"id": "m1", "name": "Amt", "default_agg": "avg"}]
_DIMENSIONS = [{"id": "d1", "name": "Product"}]


# ---------------------------------------------------------------------------
# Bug-8750 — axis extraction
# ---------------------------------------------------------------------------

def test_axis0_is_not_anchored_on_a_comma_inside_the_with_prelude():
    assert _mdx_axis_expr(_AGG_PRELUDE_STMT, 0) == "{[Measures].[Amt]}"
    assert _mdx_axis_expr(_AGG_PRELUDE_STMT, 1) == "{[Product].[Product].Members}"


def test_leaked_prelude_member_no_longer_becomes_a_keep_only_filter():
    """The silent wrong number: an enumerated member from the WITH clause was
    merged into ``where_filters`` (Bug-5548 path) and restricted the MAIN SQL."""
    col = _mdx_axis_expr(_AGG_PRELUDE_STMT, 0)
    row = _mdx_axis_expr(_AGG_PRELUDE_STMT, 1)
    assert _mdx_extract_axis_member_filters(
        col + " " + row, {"Product"},
        hierarchy_level_dim_map={}, hierarchy_default_dim_map={},
    ) == {}


def test_main_sql_for_a_custom_group_pivot_is_not_narrowed_to_one_member():
    """End of the same chain, at the SQL the router actually receives."""
    sql, _ = _mdx_to_sql(_AGG_PRELUDE_STMT, _MEASURES, _DIMENSIONS, model_slug="m")
    assert "WHERE" not in sql.upper(), (
        f"the WITH prelude leaked a keep-only filter into the detail SQL: {sql}"
    )
    assert 'GROUP BY "Product"' in sql, sql


def test_subselect_and_rows_first_axis_shapes_are_unchanged():
    """The fix slices at the statement's TOP-LEVEL SELECT, so a subselect's inner
    SELECT (inside ``FROM ( ... )``) and a ROWS-first axis order must be
    unaffected — these are the two shapes an over-eager slice would break."""
    subselect = (
        "SELECT {[Measures].[Amt]} ON COLUMNS, {[Product].[Product].Members} ON ROWS "
        "FROM (SELECT ({[Product].[Product].&[P1]}) ON COLUMNS FROM [m])"
    )
    assert _mdx_axis_expr(subselect, 0) == "{[Measures].[Amt]}"
    assert _mdx_axis_expr(subselect, 1) == "{[Product].[Product].Members}"

    rows_first = (
        "SELECT NON EMPTY {[Product].[Product].Members} ON ROWS, "
        "{[Measures].[Amt]} ON COLUMNS FROM [m]"
    )
    assert _mdx_axis_expr(rows_first, 0) == "{[Measures].[Amt]}"
    assert _mdx_axis_expr(rows_first, 1) == "{[Product].[Product].Members}"
    assert _mdx_axis_has_non_empty(rows_first, 1) is True
    assert _mdx_axis_has_non_empty(rows_first, 0) is False


def test_non_empty_detection_also_survives_a_with_prelude():
    """``_mdx_axis_has_non_empty`` shares the fallback and had the same hole; a
    wrong answer here silently prunes or restores zero-fact members."""
    stmt = (
        "WITH MEMBER [Measures].[PctGT] AS "
        "'[Measures].[Amt] / ([Measures].[Amt], [Product].[Product].[All])' "
        "SELECT NON EMPTY {[Measures].[Amt], [Measures].[PctGT]} ON COLUMNS, "
        "{[Product].[Product].Members} ON ROWS FROM [m]"
    )
    assert _mdx_axis_has_non_empty(stmt, 0) is True
    assert _mdx_axis_has_non_empty(stmt, 1) is False


# ---------------------------------------------------------------------------
# Bug-8751 — a declared calc member is not a SQL column
# ---------------------------------------------------------------------------

def test_declared_calc_measure_names_are_detected():
    assert set(_mdx_declared_calc_measures(_PCT_PRELUDE_STMT)) == {"PctGT"}
    # A dimension-namespace member is NOT a measure declaration.
    assert set(_mdx_declared_calc_measures(_AGG_PRELUDE_STMT)) == set()
    # Unbracketed form (legal MDX, seen in the wild).
    assert set(_mdx_declared_calc_measures(
        "WITH MEMBER [Measures].cChildren AS '1' "
        "SELECT {[Measures].cChildren} ON 0 FROM [m]"
    )) == {"cChildren"}
    # No prelude at all.
    assert set(_mdx_declared_calc_measures(
        "SELECT {[Measures].[Amt]} ON 0 FROM [m]"
    )) == set()


def test_calc_member_is_dropped_from_sql_and_its_base_measure_kept():
    sql, protocol = _mdx_to_sql(
        _PCT_PRELUDE_STMT, _MEASURES, _DIMENSIONS, model_slug="m",
    )
    assert protocol == "jdbc"
    assert "PctGT" not in sql, f"the calc member reached SQL resolution: {sql}"
    assert 'AVG("Amt") AS "Amt"' in sql, sql


def test_calc_member_alone_on_the_axis_still_projects_its_input_measure():
    """The client may put ONLY the calc member on COLUMNS. Its base measure is
    then referenced solely inside the WITH expression — without projecting it the
    evaluator has no input and every cell renders blank."""
    stmt = (
        "WITH MEMBER [Measures].[PctGT] AS "
        "'[Measures].[Amt] / ([Measures].[Amt], [Product].[Product].[All])' "
        "SELECT {[Measures].[PctGT]} ON COLUMNS, "
        "{[Product].[Product].Members} ON ROWS FROM [m]"
    )
    sql, _ = _mdx_to_sql(stmt, _MEASURES, _DIMENSIONS, model_slug="m")
    assert 'AVG("Amt") AS "Amt"' in sql, (
        f"the calc member's input measure was not projected: {sql}"
    )


def test_a_persona_excluded_input_measure_still_fails_loud_under_its_own_name():
    """Dropping the calc member must not weaken the Bug-1067 persona guard: an
    input measure the persona cannot see must still refuse the query, named
    honestly."""
    stmt = (
        "WITH MEMBER [Measures].[PctGT] AS "
        "'[Measures].[Secret] / ([Measures].[Secret], [Product].[Product].[All])' "
        "SELECT {[Measures].[PctGT]} ON COLUMNS, "
        "{[Product].[Product].Members} ON ROWS FROM [m]"
    )
    with pytest.raises(ValueError, match=r"not available to this persona: Secret"):
        _mdx_to_sql(stmt, _MEASURES, _DIMENSIONS, model_slug="m")


# ---------------------------------------------------------------------------
# Both, through the real Execute path, with known answers
# ---------------------------------------------------------------------------

def _envelope(statement: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command><Statement>{statement}</Statement></Command>
      <Properties><PropertyList><Catalog>m</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""


@pytest.mark.asyncio
async def test_handle_execute_renders_a_measures_calc_member_with_known_values(
    monkeypatch,
):
    """Known answer, not just a 200: with AVG("Amt") = 10 and 30 per product and
    a re-aggregated grand total of 20, ``% of Grand Total`` must render 0.5 and
    1.5 — and the detail SQL must not be narrowed by the WITH prelude."""
    from src.dax import xmla_server

    captured: list[str] = []

    async def fri(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def fmm(model_id, tenant_slug, jwt_token, **kw):
        return list(_MEASURES)

    async def fmd(model_id, tenant_slug, jwt_token, **kw):
        return list(_DIMENSIONS)

    async def fmh(*a, **kw):
        return []

    async def fns(*a, **kw):
        return []

    async def feq(sql, model_id, tenant_slug, jwt_token, protocol="dax", **_kw):
        captured.append(sql)
        if "GROUP BY" in sql.upper():
            return {"columns": ["Product", "Amt"],
                    "rows": [{"Product": "P1", "Amt": 10},
                             {"Product": "P2", "Amt": 30}]}
        # The non-additive grand-total denominator re-query.
        return {"columns": ["Amt"], "rows": [{"Amt": 20}]}

    for name, fn in {
        "_resolve_model_id": fri, "get_model_measures": fmm,
        "get_model_dimensions": fmd, "get_model_hierarchies": fmh,
        "get_model_named_sets": fns, "execute_query": feq,
    }.items():
        monkeypatch.setattr(xmla_server, name, fn)

    root = ET.fromstring(_envelope(_PCT_PRELUDE_STMT))
    resp = await xmla_server._handle_execute(
        xmla_server._find_method(root), tenant_slug="demo", jwt_token="tok",
        session_id="sid-8751",
    )
    body = resp.body.decode("utf-8") if isinstance(resp.body, bytes) else str(resp.body)

    assert "Fault" not in body, body
    assert captured, "no SQL was issued — the calc member refused the pivot"
    detail = [s for s in captured if "GROUP BY" in s.upper()]
    assert detail and "WHERE" not in detail[0].upper(), (
        f"detail SQL was narrowed by the WITH prelude: {detail}"
    )
    # Denominator re-query fired (avg is non-additive) and is unpartitioned.
    assert any("GROUP BY" not in s.upper() for s in captured), captured
    # Known answers: 10/20 and 30/20.
    assert "<Value" in body
    assert "0.5" in body, body
    assert "1.5" in body, body


def test_statement_body_is_not_fooled_by_a_nested_block_comment():
    """Bug-6612 established that SSAS MDX block comments NEST and that a
    non-nesting scanner leaks the tail. The first cut of ``_mdx_statement_body``
    used a plain ``find("*/")``, so a nested comment made it slice at a SELECT
    INSIDE the comment — fail-OPEN, not fail-safe: the axis then read as comment
    text and the Bug-8751 calc-member exclusion never fired. The scanner is now
    nesting-aware (``_mdx_visible_positions``); this pins that."""
    stmt = (
        "WITH /* outer /* inner */ SELECT junk ON COLUMNS */ "
        "MEMBER [Measures].[X] AS [Measures].[Amt] "
        "SELECT {[Measures].[X]} ON COLUMNS FROM [m]"
    )
    assert _mdx_axis_expr(stmt, 0) == "{[Measures].[X]}"
    assert set(_mdx_declared_calc_measures(stmt)) == {"X"}


# ---------------------------------------------------------------------------
# Bug-8751 consumer alignment: the flat-LAST_NON_EMPTY grain repair must see the
# SAME measure set _mdx_to_sql resolved, not the axis-only subset.
# ---------------------------------------------------------------------------

_LNE_MEASURES = [{"id": "m1", "name": "Headcount", "default_agg": "sum",
                  "semi_additive_behavior": "last_non_empty"}]
_LNE_DIMENSIONS = [
    {"id": "d1", "name": "Product"},
    {"id": "d2", "name": "Month", "data_type": "date", "dimension_type": "time",
     "is_time": True, "time_grain": "month"},
]
# Last non-empty month is 2026-02 -> P1 = 4, P2 = 6.
_LNE_FACTS = [("P1", "2026-01", 10), ("P1", "2026-02", 4),
              ("P2", "2026-01", 30), ("P2", "2026-02", 6)]


async def _run_lne(stmt, monkeypatch):
    from src.dax import xmla_server
    captured: list[str] = []

    async def fri(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def fmm(*a, **k):
        return list(_LNE_MEASURES)

    async def fmd(*a, **k):
        return list(_LNE_DIMENSIONS)

    async def fnone(*a, **k):
        return []

    async def feq(sql, model_id, tenant_slug, jwt_token, protocol="dax", **_kw):
        captured.append(sql)
        up = sql.upper()
        if "GROUP BY" in up and "MONTH" in up:
            return {"columns": ["Product", "Month", "Headcount"],
                    "rows": [{"Product": p, "Month": m, "Headcount": v}
                             for p, m, v in _LNE_FACTS]}
        if "GROUP BY" in up:
            return {"columns": ["Product", "Headcount"],
                    "rows": [{"Product": "P1", "Headcount": 4},
                             {"Product": "P2", "Headcount": 6}]}
        return {"columns": ["Headcount"], "rows": [{"Headcount": 10}]}

    for name, fn in {
        "_resolve_model_id": fri, "get_model_measures": fmm,
        "get_model_dimensions": fmd, "get_model_hierarchies": fnone,
        "get_model_named_sets": fnone, "execute_query": feq,
    }.items():
        monkeypatch.setattr(xmla_server, name, fn)

    root = ET.fromstring(_envelope(stmt))
    resp = await xmla_server._handle_execute(
        xmla_server._find_method(root), tenant_slug="demo", jwt_token="tok",
        session_id="sid-lne",
    )
    body = resp.body.decode("utf-8") if isinstance(resp.body, bytes) else str(resp.body)
    return captured, body


@pytest.mark.asyncio
async def test_lne_base_measure_referenced_only_in_the_prelude_still_collapses_grain(
    monkeypatch,
):
    """Bug-8751 taught _mdx_to_sql to project a prelude-only base measure. That
    measure can be LAST_NON_EMPTY, which makes _mdx_to_sql append a HIDDEN time
    grain to the GROUP BY. _handle_execute's flat-LNE repair derives its measure
    set from the AXIS only, so it does not know to collapse that grain back out —
    the pivot then carries a phantom time dimension, one row per period, and
    renders NO cells at all (HTTP 200, no fault, no warning).

    Excel shape: 'Show Values As' on an inventory/headcount measure. The client
    puts only the calculated member on COLUMNS.

    Known answer: last-non-empty Headcount is 4 (P1) and 6 (P2), so Doubled must
    be 8 and 12 — the same values the control below already produces.
    """
    stmt = (
        "WITH MEMBER [Measures].[Doubled] AS '[Measures].[Headcount] * 2' "
        "SELECT {[Measures].[Doubled]} ON COLUMNS, "
        "{[Product].[Product].Members} ON ROWS FROM [m]"
    )
    captured, body = await _run_lne(stmt, monkeypatch)
    assert "Fault" not in body, body
    values = re.findall(r"<Value[^>]*>([^<]*)</Value>", body)
    assert values, (
        "the pivot rendered NO cells: the hidden LAST_NON_EMPTY time grain was "
        f"never collapsed back out. SQL issued: {captured}"
    )
    assert values == ["8.0", "12.0"], (
        f"expected the calc evaluated at the requested (Product) grain; got {values}"
    )


@pytest.mark.asyncio
async def test_lne_control_base_measure_on_the_axis_is_already_correct(monkeypatch):
    """Control: the ONLY difference from the test above is that the base measure
    is also on the axis, which is what lets the flat-LNE repair see it. Identical
    SQL is issued in both cases, so any divergence is the consumer gap."""
    stmt = (
        "WITH MEMBER [Measures].[Doubled] AS '[Measures].[Headcount] * 2' "
        "SELECT {[Measures].[Headcount],[Measures].[Doubled]} ON COLUMNS, "
        "{[Product].[Product].Members} ON ROWS FROM [m]"
    )
    captured, body = await _run_lne(stmt, monkeypatch)
    assert "Fault" not in body, body
    assert re.findall(r"<Value[^>]*>([^<]*)</Value>", body) == \
        ["4.0", "8.0", "6.0", "12.0"], body


_GRP_MEASURES = [{"id": "m1", "name": "AvgAmt", "default_agg": "avg"}]
_GRP_DIMENSIONS = [{"id": "d1", "name": "Product"}]


@pytest.mark.asyncio
async def test_custom_group_requery_sees_a_prelude_only_input_measure(monkeypatch):
    """Bug-8751 consumer alignment, 4th consumer (deep-review R2 finding 1).

    ``_rq_queried`` (the ``queried_measures`` argument to
    ``plan_aggregate_requeried``) is still derived from ``_mdx_extract_measures``
    over the AXIS text, while the SQL projection now comes from
    ``_sql_measure_set``. A pivot that combines an Excel custom group
    (``Aggregate`` member) with a calculated measure whose NON-ADDITIVE input is
    referenced only from the WITH prelude therefore projects ``AvgAmt`` but plans
    NO re-query for it -- ``_eval_aggregate_set`` writes ``None`` into the group
    row and the group cell renders ``xsi:nil`` in Excel: a blank where a real
    number belongs, HTTP 200, no fault, no warning.

    Known answer: AVG over the P1+P2 group re-queries to 20, so Doubled = 40.
    """
    from src.dax import xmla_server

    captured: list[str] = []

    async def fri(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def fmm(*a, **k):
        return list(_GRP_MEASURES)

    async def fmd(*a, **k):
        return list(_GRP_DIMENSIONS)

    async def fnone(*a, **k):
        return []

    async def feq(sql, model_id, tenant_slug, jwt_token, protocol="dax", **_kw):
        captured.append(sql)
        if "GROUP BY" in sql.upper():
            return {"columns": ["Product", "AvgAmt"],
                    "rows": [{"Product": "P1", "AvgAmt": 10},
                             {"Product": "P2", "AvgAmt": 30},
                             {"Product": "P3", "AvgAmt": 50}]}
        return {"columns": ["AvgAmt"], "rows": [{"AvgAmt": 20}]}

    async def fmembers(*a, **k):
        # Real shape of the member payload; the Bug-6658 "show items with no
        # data" restore path reads ``.get("members")``.
        return {"members": [], "levels": []}

    for name, fn in {
        "_resolve_model_id": fri, "get_model_measures": fmm,
        "get_model_dimensions": fmd, "get_model_hierarchies": fnone,
        "get_model_named_sets": fnone, "execute_query": feq,
        "get_dimension_members": fmembers,
    }.items():
        monkeypatch.setattr(xmla_server, name, fn)

    stmt = (
        "WITH MEMBER [Product].[Product].[Grp] AS "
        "AGGREGATE({[Product].[Product].[P1], [Product].[Product].[P2]}) "
        "MEMBER [Measures].[Doubled] AS '[Measures].[AvgAmt] * 2' "
        "SELECT {[Measures].[Doubled]} ON COLUMNS, "
        "{[Product].[Product].Members} ON ROWS FROM [m]"
    )
    root = ET.fromstring(_envelope(stmt))
    resp = await xmla_server._handle_execute(
        xmla_server._find_method(root), tenant_slug="demo", jwt_token="tok",
        session_id="sid-grp",
    )
    body = resp.body.decode("utf-8") if isinstance(resp.body, bytes) else str(resp.body)
    assert "Fault" not in body, body
    assert "Grp" in re.findall(r"<Caption>([^<]*)</Caption>", body), body

    assert any("GROUP BY" not in s.upper() for s in captured), (
        "no custom-group re-query was planned for the prelude-only AVG input "
        f"measure; SQL issued: {captured}"
    )
    values = re.findall(r"<Value[^>]*>([^<]*)</Value>", body)
    assert values == ["20.0", "60.0", "100.0", "40.0"], (
        "the custom-group cell rendered blank instead of its re-aggregated "
        f"value; got {values}"
    )


@pytest.mark.parametrize("label,stmt", [
    ("unterminated bracket",
     "WITH MEMBER [Measures].[X AS 1 SELECT {[Measures].[X]} ON 0 FROM [m]"),
    ("unterminated string",
     "WITH MEMBER [Measures].[X] AS 'abc SELECT {[Measures].[X]} ON 0 FROM [m]"),
    ("unterminated block comment",
     "WITH /* oops MEMBER [Measures].[X] AS 1 SELECT {[Measures].[X]} ON 0 FROM [m]"),
    ("unbalanced open paren",
     "WITH MEMBER [Measures].[X] AS ([Measures].[A] SELECT {[Measures].[X]} ON 0 FROM [m]"),
])
def test_statement_body_fails_safe_on_a_malformed_statement(label, stmt):
    """``_mdx_visible_positions`` is the single lexer behind four helpers.

    Every unterminated construct swallows the rest of the text, so no top-level
    ``SELECT`` is found. That MUST degrade to "return the statement unchanged"
    (pre-Bug-8750 behaviour), never to a slice at a wrong offset -- a wrong slice
    hands the axis extractors a fragment and silently changes the pivot's
    filters.
    """
    from src.dax.xmla_server import _mdx_statement_body
    assert _mdx_statement_body(stmt) == stmt, label


@pytest.mark.parametrize("label,stmt,expected", [
    ("SELECT inside a member caption",
     "WITH MEMBER [Measures].[SELECT ON COLUMNS] AS 1 "
     "SELECT {[Measures].[X]} ON 0 FROM [m]", "{[Measures].[X]}"),
    ("SELECT inside a double-quoted literal",
     'WITH MEMBER [Measures].[X] AS "a, SELECT b ON COLUMNS" '
     'SELECT {[Measures].[X]} ON 0 FROM [m]', "{[Measures].[X]}"),
    ("SELECT inside a // line comment",
     "WITH // SELECT junk ON COLUMNS\n MEMBER [Measures].[X] AS 1 "
     "SELECT {[Measures].[X]} ON 0 FROM [m]", "{[Measures].[X]}"),
    ("SELECT inside a -- line comment",
     "WITH -- SELECT junk ON COLUMNS\n MEMBER [Measures].[X] AS 1 "
     "SELECT {[Measures].[X]} ON 0 FROM [m]", "{[Measures].[X]}"),
    ("]]-escaped caption",
     "WITH MEMBER [Measures].[A]]B] AS [Measures].[Amt] "
     "SELECT {[Measures].[A]]B]} ON 0 FROM [m]", "{[Measures].[A]]B]}"),
    ("lowercase keywords",
     "with member [Measures].[X] as [Measures].[A] "
     "select {[Measures].[X]} on 0 from [m]", "{[Measures].[X]}"),
])
def test_statement_body_never_slices_at_a_non_syntax_select(label, stmt, expected):
    assert _mdx_axis_expr(stmt, 0) == expected, label


def test_declared_calc_measures_survive_a_prelude_comment_and_escape():
    assert set(_mdx_declared_calc_measures(
        "WITH -- MEMBER [Measures].[Fake] AS 1\n"
        " MEMBER [Measures].[A]]B] AS [Measures].[Amt] "
        "SELECT {[Measures].[A]]B]} ON 0 FROM [m]"
    )) == {"A]B"}


@pytest.mark.asyncio
async def test_consts_only_shortcircuit_ignores_a_declared_calc_member(monkeypatch):
    """Bug-8751 consumer alignment, 5th consumer (deep-review R2 finding 6).

    The Bug-8288 consts-only short-circuit answers a statement that references
    ONLY KPI goal/status constant members from a single synthetic cell, with no
    SQL at all. It decided "is any REAL measure referenced?" from the raw axis
    extraction, which counts a WITH-declared calc member as a real measure — so a
    statement whose only other member is calculated fell through to
    ``_mdx_to_sql``, whose empty measure set then expands to EVERY model measure
    (a needless full scan, and a spurious "LAST_NON_EMPTY requires a DATE/TIME
    grain" fault on a model carrying an LNE measure).
    """
    from src.dax import xmla_server

    captured: list[str] = []
    goal_kpi = {
        "id": "k-nr", "name": "Net Revenue", "display_name": "Net Revenue",
        "expression": 'measure("fee_amount")', "value_measure_id": None,
        "target_type": "static", "target_value": 188914000.0,
        "status_expression": "", "trend_expression": "", "parent_kpi_id": None,
    }

    async def fri(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def fmm(*a, **k):
        return [{"id": "m-fee", "name": "fee_amount", "default_agg": "sum",
                 "is_hidden": False}]

    async def fmd(*a, **k):
        return [{"id": "d1", "name": "Region"}]

    async def fnone(*a, **k):
        return []

    async def fkpis(*a, **k):
        return [dict(goal_kpi)]

    async def feq(sql, model_id, tenant_slug, jwt_token, protocol="dax", **_kw):
        captured.append(sql)
        return {"columns": [], "rows": []}

    for name, fn in {
        "_resolve_model_id": fri, "get_model_measures": fmm,
        "get_model_dimensions": fmd, "get_model_hierarchies": fnone,
        "get_model_named_sets": fnone, "get_model_kpis": fkpis,
        "execute_query": feq,
    }.items():
        monkeypatch.setattr(xmla_server, name, fn)

    stmt = (
        "WITH MEMBER [Measures].[Gap] AS "
        "'[Measures].[Net Revenue Goal] - 1' "
        "SELECT {[Measures].[Net Revenue Goal], [Measures].[Gap]} ON COLUMNS "
        "FROM [m]"
    )
    root = ET.fromstring(_envelope(stmt))
    resp = await xmla_server._handle_execute(
        xmla_server._find_method(root), tenant_slug="demo", jwt_token="tok",
        session_id="sid-consts",
    )
    body = resp.body.decode("utf-8") if isinstance(resp.body, bytes) else str(resp.body)
    assert "Fault" not in body, body
    assert captured == [], (
        "the consts-only short-circuit did not fire; the gateway issued SQL for a "
        f"statement that needs no facts: {captured}"
    )
    assert "188914000" in body, body


def test_unbracketed_input_measure_reference_is_still_projected():
    """R3 finding 1. ``_WITH_MEMBER_MEASURE_DECL_RE`` accepts BOTH the bracketed
    and the unbracketed ``[Measures].Name`` declaration form, but the reference
    extractor ``_sql_measure_set`` uses is bracket-only. So a member declared
    with brackets whose INPUT is referenced bare is recognised as a calc member
    (correctly dropped from SQL) and then its input is never added back: the
    detail SQL projects NO measure at all, and the pivot renders every product
    row with a blank cell -- HTTP 200, no fault. Before this lane the same
    statement failed LOUD ("Measure not available to this persona: Doubled"),
    so this is a silent-blank regression, not a pre-existing gap.
    """
    stmt = (
        "WITH MEMBER [Measures].[Doubled] AS [Measures].Amt * 2 "
        "SELECT {[Measures].[Doubled]} ON COLUMNS, "
        "{[Product].[Product].Members} ON ROWS FROM [m]"
    )
    from src.dax.xmla_server import _sql_measure_set
    assert _sql_measure_set(
        stmt, "{[Measures].[Doubled]} {[Product].[Product].Members} "
    ) == ["Amt"]

    sql, _ = _mdx_to_sql(
        stmt,
        [{"id": "m1", "name": "Amt", "default_agg": "sum"}],
        [{"id": "d1", "name": "Product"}],
        model_slug="m",
    )
    assert 'SUM("Amt") AS "Amt"' in sql, (
        "the calc member's input measure was not projected; the pivot will "
        f"render blank cells with a 200: {sql}"
    )


def test_unbracketed_measure_on_the_axis_is_still_projected():
    """Same asymmetry reached from the other side: the calc member itself is
    placed on the axis in the bare form, so ``used`` is empty and no input is
    followed. Widening only the prelude scan leaves this leg open."""
    stmt = (
        "WITH MEMBER [Measures].[Doubled] AS [Measures].[Amt] * 2 "
        "SELECT {[Measures].Doubled} ON COLUMNS, "
        "{[Product].[Product].Members} ON ROWS FROM [m]"
    )
    sql, _ = _mdx_to_sql(
        stmt,
        [{"id": "m1", "name": "Amt", "default_agg": "sum"}],
        [{"id": "d1", "name": "Product"}],
        model_slug="m",
    )
    assert 'SUM("Amt") AS "Amt"' in sql, sql


def test_large_statements_are_not_pinned_in_the_offset_cache():
    """R3 finding 4. ``_XMLA_MAX_REQUEST_BYTES`` defaults to 10 MB and the memo
    holds the only strong reference to each key for the process lifetime, so a
    count-bounded (maxsize=32) cache is byte-UNBOUNDED: ~320 MB worst case, and
    _handle_execute stores two keys per query (dax_statement and cleaned). Small
    statements must still be memoised; large ones must bypass."""
    from src.dax.xmla_server import (
        _mdx_statement_body, _mdx_statement_body_offset_cached,
    )

    small = "WITH MEMBER [Measures].[X] AS 1 SELECT {[Measures].[X]} ON 0 FROM [m]"
    _mdx_statement_body_offset_cached.cache_clear()
    _mdx_statement_body(small)
    _mdx_statement_body(small)
    assert _mdx_statement_body_offset_cached.cache_info().hits >= 1

    big = ("/* " + "x" * 2_000_000 + " */ "
           "WITH MEMBER [Measures].[X] AS 1 SELECT {[Measures].[X]} ON 0 FROM [m]")
    before = _mdx_statement_body_offset_cached.cache_info().currsize
    assert _mdx_statement_body(big).lstrip().upper().startswith("SELECT")
    assert _mdx_statement_body_offset_cached.cache_info().currsize == before, (
        "a multi-megabyte statement was pinned in the offset cache"
    )


_R4_MEAS = [{"id": "m1", "name": "Amt", "default_agg": "sum"}]
_R4_DIMS = [{"id": "d1", "name": "Product"}]
_R4_ROWS = "{[Product].[Product].Members} ON ROWS FROM [m]"


@pytest.mark.parametrize("label,stmt,expect_amt", [
    ("bare .MEMBERS on the axis",
     f"SELECT [Measures].MEMBERS ON COLUMNS, {_R4_ROWS}", False),
    ("AddCalculatedMembers([Measures].AllMembers)",
     f"SELECT AddCalculatedMembers([Measures].AllMembers) ON COLUMNS, {_R4_ROWS}", False),
    ("a Measures function inside a used calc member",
     "WITH MEMBER [Measures].[D] AS [Measures].[Amt] / [Measures].Count "
     f"SELECT {{[Measures].[D]}} ON COLUMNS, {_R4_ROWS}", True),
    ("CurrentMember inside a used calc member",
     "WITH MEMBER [Measures].[D] AS "
     "IIF([Measures].CurrentMember IS [Measures].[Amt], [Measures].[Amt], 0) "
     f"SELECT {{[Measures].[D]}} ON COLUMNS, {_R4_ROWS}", True),
])
def test_measures_namespace_functions_are_not_mistaken_for_measures(
    label, stmt, expect_amt,
):
    r"""R4 finding 1. ``_MEASURE_REF_ANY_FORM_RE``'s bare alternative
    ``[A-Za-z_]\w*`` has no positional anchor, so it matches ANY identifier
    after ``[Measures].`` -- including the MDX member/set functions and
    properties that legally appear there. The token lands in the SQL measure
    set, misses ``measure_canonical``, and ``_mdx_to_sql`` refuses the whole
    statement with a FALSE persona message naming a member that does not exist.

    The first two shapes returned HTTP 200 before this lane (verified by
    executing them at 490a0266), so they are a lane-introduced regression. The
    last two leave Bug-8751's headline shape still refused whenever a calc
    member's expression touches a Measures-namespace function -- now under a
    more confusing name than before.

    ``_WITH_MEMBER_MEASURE_DECL_RE`` is safe from this because it is anchored
    after the ``MEMBER`` keyword, where an identifier IS a name by grammar. The
    reference regex must earn that guarantee some other way.
    """
    try:
        sql, _ = _mdx_to_sql(stmt, _R4_MEAS, _R4_DIMS, model_slug="m")
    except ValueError as exc:
        raise AssertionError(
            f"{label}: an MDX function/property on the Measures namespace was "
            f"collected as a measure name, so a legal statement is refused with "
            f"a false persona fault -> {exc}"
        ) from None
    if expect_amt:
        assert 'SUM("Amt") AS "Amt"' in sql, (label, sql)


def test_a_bracketed_unknown_measure_still_fails_loud():
    """Anti-overcorrection guard for the R4 fix: only the BARE alternative may
    be narrowed. A real measure can legitimately be named ``Count``, and the
    explicit bracketed spelling must keep raising the Bug-1067 persona fault
    rather than being silently dropped from the projection."""
    stmt = f"SELECT {{[Measures].[Count]}} ON COLUMNS, {_R4_ROWS}"
    with pytest.raises(ValueError, match="Count"):
        _mdx_to_sql(stmt, _R4_MEAS, _R4_DIMS, model_slug="m")


# ---------------------------------------------------------------------------
# Bug-8770 -- response-axis extractor (_get_axis_expr / _axis_dim_split) must
#             also normalise past the WITH prelude
# ---------------------------------------------------------------------------

from src.dax.mdx_execute import _get_axis_expr, _axis_dim_split


def test_response_axis_expr_is_not_anchored_on_a_select_inside_the_with_prelude():
    """Bug-8770. ``_get_axis_expr`` extracted axes from the raw statement, so a
    ``SELECT`` token inside a bracketed member caption or a string literal was
    treated as the statement's own SELECT, and the captured axis carried
    WITH-prelude text. This is the response-axis sibling of the SQL-side
    Bug-8750 fix.

    NOTE: the ROWS expression carries a leading comma (the separator between the
    two axis clauses in the SELECT body) -- this is a pre-existing behavior of
    ``_get_axis_expr``'s ``re.finditer`` capture and does not affect downstream
    consumers (``_axis_dim_split`` uses substring matching). We assert that the
    axis CONTENT is correct and contains no prelude pollution.
    """
    # Shape (a) from the registry: member NAME contains "SELECT"
    stmt_a = (
        "WITH MEMBER [Measures].[SELECT Product] AS [Measures].[Amount]*2 "
        "SELECT {[Measures].[Amount]} ON COLUMNS, "
        "{[Product].[Product].MEMBERS} ON ROWS FROM [Cube]"
    )
    col_a = _get_axis_expr(stmt_a, "COLUMNS")
    row_a = _get_axis_expr(stmt_a, "ROWS")
    assert col_a == "{[Measures].[Amount]}"
    assert "{[Product].[Product].MEMBERS}" in row_a
    # The prelude text must NOT leak into either axis.
    assert "SELECT Product" not in col_a
    assert "SELECT Product" not in row_a

    # Shape (b) from the registry: string literal contains "SELECT ... FROM ["
    stmt_b = (
        'WITH MEMBER [Measures].[X] AS "SELECT {[Measures].[Amount]} FROM [Cube]" '
        "SELECT {[Measures].[Amount]} ON COLUMNS, "
        "{[Product].[Product].MEMBERS} ON ROWS FROM [Cube]"
    )
    col_b = _get_axis_expr(stmt_b, "COLUMNS")
    row_b = _get_axis_expr(stmt_b, "ROWS")
    assert col_b == "{[Measures].[Amount]}"
    assert "{[Product].[Product].MEMBERS}" in row_b
    # Without the fix, the literal's SELECT anchors first and both axes are
    # empty or carry literal content.
    assert row_b != ""
    assert col_b != ""


def test_response_axis_dim_split_survives_a_with_prelude():
    """Bug-8770. ``_axis_dim_split`` feeds ``_get_axis_expr``, so the same
    WITH-prelude corruption reaches the ``row_axis_dims`` / ``col_axis_dims``
    split used by % of Row/Column Total. After the fix, dimensions must be
    attributed to the correct axis even when a prelude carries SELECT-like
    tokens."""
    stmt = (
        "WITH MEMBER [Measures].[PctGT] AS "
        "'[Measures].[Amt] / ([Measures].[Amt], [Product].[Product].[All])' "
        "SELECT {[Measures].[Amt], [Measures].[PctGT]} ON COLUMNS, "
        "{[Product].[Product].Members} ON ROWS FROM [m]"
    )
    row_dims, col_dims = _axis_dim_split(stmt, ["Product"])
    assert row_dims == ["Product"], f"Product should be on ROWS; got row={row_dims}"
    assert col_dims == [], f"no dimension on COLUMNS; got col={col_dims}"


def test_response_axis_dim_split_with_select_in_member_caption():
    """Bug-8770, reproducer (a): a member named ``SELECT Product`` must not
    pollute the dim split."""
    stmt = (
        "WITH MEMBER [Measures].[SELECT Product] AS [Measures].[Amt]*2 "
        "SELECT {[Measures].[Amt]} ON COLUMNS, "
        "{[Product].[Product].Members} ON ROWS FROM [m]"
    )
    row_dims, col_dims = _axis_dim_split(stmt, ["Product"])
    assert row_dims == ["Product"]
    assert col_dims == []


def test_response_axis_expr_comma_in_prelude_does_not_corrupt_axis0():
    """Bug-8770, same family as Bug-8750: a comma inside the WITH prelude
    (from an AGGREGATE member list) was the original trigger for Bug-8750 on
    the SQL side. Verify the response-axis extractor is also immune."""
    stmt = (
        "WITH MEMBER [Product].[Product].[Grp] AS "
        "AGGREGATE({[Product].[Product].&[P1], [Product].[Product].&[P2]})\n"
        "SELECT {[Measures].[Amt]} ON COLUMNS, "
        "{[Product].[Product].Members} ON ROWS FROM [m]"
    )
    col = _get_axis_expr(stmt, "COLUMNS")
    row = _get_axis_expr(stmt, "ROWS")
    assert col == "{[Measures].[Amt]}"
    assert "{[Product].[Product].Members}" in row
    # The prelude AGGREGATE member list must NOT leak into the axis.
    assert "AGGREGATE" not in col
    assert "AGGREGATE" not in row
    row_dims, col_dims = _axis_dim_split(stmt, ["Product"])
    assert row_dims == ["Product"]
    assert col_dims == []
