"""Bug-8288 / Bug-8009 — governed native-pivot KPI status + parse-fallback visibility.

Bug-8288 (MEDIUM): a native Excel pivot "Status" checkbox binds the MDSCHEMA_KPIS
``KPI_STATUS`` member DIRECTLY (its MDX carries no ``KPIStatus()`` token). When that
member was the raw VALUE member, the pivot showed the raw business number instead of
the governed -1/0/1 RAG verdict. The fix advertises a synthetic governed status
support member ``[Measures].[<caption> Status]`` (Bug-6888 goal pattern); the XMLA
Execute path drops it from SQL resolution and post-joins the governed verdict from
``evaluate_kpi_governed`` (the single model-service authority). The governed status is
model-wide, so a status member requested WITH a dimension breakdown/slicer fails loud
(client-visible) rather than repeating one verdict across slices (a sliced governed
status needs model-service /evaluate slice support — Bug-8287, cross-service).

Bug-8009 (HIGH, F-002-02): the structured MDX parser is advisory for a normal SELECT;
a tree-sitter syntax error or a missing parser silently fell back to the regex
translator. The fix surfaces that fallback as a client-visible SOAP <Warning> so a
fallback interpretation is never mistaken for a fully-parsed answer.

Test escape: rowset content and the member-function interception were pinned, but no
test asserted the NATIVE-pivot metadata member is governed, nor that a parse fallback
is disclosed. Guard: the assertions below. Tier: T1 (producer/consumer contract).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from defusedxml import ElementTree as ET

from src.dax import mdschema
from src.dax import xmla_server
from src.dax.ts_mdx_parser import ParsedMDX


_MEASURES = [
    {"id": "m-fee", "name": "fee_amount", "default_agg": "sum"},
]

# KPI "aa": value=fee_amount, static goal 10000 (verdict basis), higher is better.
_KPI_AA = {
    "id": "kpi-aa",
    "name": "aa",
    "display_name": "aa",
    "value_measure_id": "m-fee",
    "target_type": "static",
    "target_value": 10000.0,
    "direction": "higher_is_better",
    "status_expression": None,
    "trend_expression": None,
}


# ---------------------------------------------------------------------------
# Bug-8288 — synthetic governed status support member (catalogue surface)
# ---------------------------------------------------------------------------

def test_status_support_measure_name():
    assert mdschema.kpi_status_support_measure_name(_KPI_AA) == "aa Status"
    assert mdschema.kpi_status_support_measure_name({"name": ""}) == ""


def test_synthetic_status_measure_built_for_governed_kpi_only():
    # KPI with verdict basis -> synthetic status measure.
    synth = mdschema.kpi_status_synthetic_measures([_KPI_AA], _MEASURES)
    assert [m["name"] for m in synth] == ["aa Status"]
    assert synth[0]["xmla_support_measure"] is True
    # KPI with a resolvable value but NO target/bands -> no verdict basis -> none.
    noverdict = {"id": "k2", "name": "nv", "value_measure_id": "m-fee"}
    assert mdschema.kpi_status_synthetic_measures([noverdict], _MEASURES) == []
    # Authored status_expression is already addressable -> no synthetic member.
    authored = dict(_KPI_AA, status_expression="-1")
    assert mdschema.kpi_status_synthetic_measures([authored], _MEASURES) == []


def test_synthetic_status_measure_skips_real_measure_collision():
    # A synthetic name that collides with a real measure must be skipped so the
    # real measure's value is never hijacked (Bug-6942 parity).
    measures = _MEASURES + [{"id": "m-x", "name": "aa Status", "default_agg": "sum"}]
    assert mdschema.kpi_status_synthetic_measures([_KPI_AA], measures) == []


def test_status_support_member_row_emitted_invisible():
    synth = mdschema.kpi_status_synthetic_measures([_KPI_AA], _MEASURES)
    rows = mdschema._rows_measures("modely", _MEASURES + synth)
    by_name = {r["MEASURE_NAME"]: r for r in rows}
    assert "aa Status" in by_name
    assert by_name["aa Status"]["MEASURE_IS_VISIBLE"] == "false"


# ---------------------------------------------------------------------------
# Bug-8288 — Execute path resolves the synthetic member to the governed verdict
# ---------------------------------------------------------------------------

def _make_fakes(governed, rows=None, capture=None):
    async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def fake_get_model_measures(model_id, tenant_slug, jwt_token, **kw):
        return list(_MEASURES)

    async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, **kw):
        return [{"id": "d1", "name": "Region"}]

    async def fake_get_model_hierarchies(model_id, tenant_slug, jwt_token, **kw):
        return []

    async def fake_get_model_named_sets(*a, **kw):
        return []

    async def fake_get_model_kpis(model_id, tenant_slug, jwt_token, **kw):
        return [dict(_KPI_AA)]

    async def fake_execute_query(sql, model_id, tenant_slug, jwt_token,
                                 protocol="dax", **_kwargs):
        if capture is not None:
            capture.append(sql)
        return {
            "columns": ["fee_amount"],
            "rows": rows if rows is not None else [{"fee_amount": 2753735.21}],
        }

    return {
        "_resolve_model_id": fake_resolve_model_id,
        "get_model_measures": fake_get_model_measures,
        "get_model_dimensions": fake_get_model_dimensions,
        "get_model_hierarchies": fake_get_model_hierarchies,
        "get_model_named_sets": fake_get_model_named_sets,
        "get_model_kpis": fake_get_model_kpis,
        "execute_query": fake_execute_query,
    }


async def _run_execute(
    statement, governed, monkeypatch, capture=None, rows=None, batch=None,
):
    fakes = _make_fakes(governed, rows=rows, capture=capture)
    for name, fn in fakes.items():
        monkeypatch.setattr(xmla_server, name, fn)
    mock_eval = AsyncMock(return_value=governed)
    monkeypatch.setattr(xmla_server, "evaluate_kpi_governed", mock_eval)
    monkeypatch.setattr(xmla_server, "evaluate_kpi_batch", batch or AsyncMock(
        return_value={_KPI_AA["id"]: governed},
    ))

    execute_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command><Statement>{statement}</Statement></Command>
      <Properties><PropertyList><Catalog>m</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""
    root = ET.fromstring(execute_xml)
    method_el = xmla_server._find_method(root)
    resp = await xmla_server._handle_execute(
        method_el, tenant_slug="demo", jwt_token="tok", session_id="sid-8288",
    )
    body = resp.body.decode("utf-8") if isinstance(resp.body, bytes) else str(resp.body)
    return body, mock_eval


@pytest.mark.asyncio
async def test_execute_status_member_resolves_governed_verdict(monkeypatch):
    """A native pivot binding [Measures].[aa Status] alongside the value member
    resolves the status cell to the governed -1/0/1, NOT the raw value. The status
    member is dropped from the SQL (a constant), and the governed authority is
    consulted."""
    for verdict in (-1, 0, 1):
        capture: list[str] = []
        body, mock_eval = await _run_execute(
            "SELECT {[Measures].[aa Status], [Measures].[fee_amount]} "
            "ON COLUMNS FROM [m]",
            {"value": 2753735.21, "status": verdict},
            monkeypatch,
            capture=capture,
        )
        assert "Fault" not in body, body
        # Governed authority consulted (revert guard: without the fix the member
        # would go to SQL and evaluate_kpi_governed would never be called).
        mock_eval.assert_awaited_once()
        # The synthetic status member is NOT sent to the SQL router.
        assert all("aa Status" not in s for s in capture), capture
        # The governed verdict appears in the response cells.
        assert f">{verdict}<" in body or f'>{verdict}.' in body, body


@pytest.mark.asyncio
async def test_execute_status_only_single_cell_returns_verdict(monkeypatch):
    """Status ticked ALONE (no real measure) still returns the governed verdict as
    a single grand-total cell even when the router yields no rows."""
    body, mock_eval = await _run_execute(
        "SELECT {[Measures].[aa Status]} ON COLUMNS FROM [m]",
        {"value": None, "status": 1},
        monkeypatch,
        rows=[],
    )
    assert "Fault" not in body, body
    mock_eval.assert_awaited_once()
    # The governed verdict is the single grand-total cell (rendered numerically).
    assert ">1<" in body or ">1.0</Value>" in body, body


@pytest.mark.asyncio
async def test_consts_only_status_query_bypasses_sql_expansion(monkeypatch):
    """Review R2 finding 3 guard: a consts-only status query (no real measure, no
    dimension) must NOT reach the SQL router — otherwise every measure was dropped
    and _mdx_to_sql would expand to ALL model measures (a full scan + a spurious
    LAST_NON_EMPTY fault on LNE models). The short-circuit builds a single cell."""
    capture: list[str] = []
    body, mock_eval = await _run_execute(
        "SELECT {[Measures].[aa Status]} ON COLUMNS FROM [m]",
        {"value": None, "status": 0},
        monkeypatch,
        capture=capture,
        rows=[{"fee_amount": 1.0}],  # would be returned IF the SQL path ran
    )
    assert "Fault" not in body, body
    mock_eval.assert_awaited_once()
    # The SQL router must not be consulted at all for a consts-only query.
    assert capture == [], capture
    assert ">0<" in body or ">0.0</Value>" in body, body


@pytest.mark.asyncio
async def test_execute_status_member_with_axis_dimension_fails_loud(monkeypatch):
    """The governed status is model-wide; requested with a dimension breakdown it
    fails loud (client-visible) rather than repeating one verdict per slice."""
    body, mock_eval = await _run_execute(
        "SELECT {[Measures].[aa Status]} ON COLUMNS, "
        "{[Region].[Region].Members} ON ROWS FROM [m]",
        {"value": 1.0, "status": 1},
        monkeypatch,
    )
    assert "Fault" in body, body
    assert "dimension breakdown" in body, body
    mock_eval.assert_not_awaited()


@pytest.mark.asyncio
async def test_dimension_named_like_status_member_does_not_fault(monkeypatch):
    """Review finding 1 regression guard: a DIMENSION captioned 'aa Status' (next
    to a KPI 'aa') must NOT be mistaken for the synthetic status member. Detection
    matches the FULL unique name [Measures].[aa Status], which a dimension member
    reference never contains — so an innocent pivot over that dimension executes
    normally instead of hitting the dimension-breakdown fail-loud."""
    async def fri(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def fmm(model_id, tenant_slug, jwt_token, **kw):
        return list(_MEASURES)

    async def fmd(model_id, tenant_slug, jwt_token, **kw):
        return [{"id": "d1", "name": "aa Status"}]

    async def fmh(model_id, tenant_slug, jwt_token, **kw):
        return []

    async def fns(*a, **kw):
        return []

    async def fkpis(model_id, tenant_slug, jwt_token, **kw):
        return [dict(_KPI_AA)]

    async def feq(sql, model_id, tenant_slug, jwt_token, protocol="dax", **_kw):
        return {"columns": ["aa Status", "fee_amount"],
                "rows": [{"aa Status": "North", "fee_amount": 10.0},
                         {"aa Status": "South", "fee_amount": 20.0}]}

    mock_eval = AsyncMock(return_value={"value": 1.0, "status": 1})
    for name, fn in {"_resolve_model_id": fri, "get_model_measures": fmm,
                     "get_model_dimensions": fmd, "get_model_hierarchies": fmh,
                     "get_model_named_sets": fns, "get_model_kpis": fkpis,
                     "execute_query": feq}.items():
        monkeypatch.setattr(xmla_server, name, fn)
    monkeypatch.setattr(xmla_server, "evaluate_kpi_governed", mock_eval)

    stmt = ("SELECT {[Measures].[fee_amount]} ON COLUMNS, "
            "{[aa Status].[aa Status].Members} ON ROWS FROM [m]")
    execute_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command><Statement>{stmt}</Statement></Command>
      <Properties><PropertyList><Catalog>m</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""
    root = ET.fromstring(execute_xml)
    method_el = xmla_server._find_method(root)
    resp = await xmla_server._handle_execute(
        method_el, tenant_slug="demo", jwt_token="tok", session_id="sid-f1",
    )
    body = resp.body.decode("utf-8") if isinstance(resp.body, bytes) else str(resp.body)
    assert "Fault" not in body, body
    assert "dimension breakdown" not in body, body
    # The KPI status governed authority must NOT be consulted for a plain
    # dimension pivot that never references the status member.
    mock_eval.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_status_member_with_where_slicer_fails_loud(monkeypatch):
    """Bug-8383: a WHERE dimension slicer reaches governed batch evaluation."""
    mock_batch = AsyncMock(return_value={_KPI_AA["id"]: {"status": 1}})
    body, mock_eval = await _run_execute(
        "SELECT {[Measures].[aa Status]} ON COLUMNS FROM [m] "
        "WHERE ([Region].[Region].[EMEA])",
        {"value": 1.0, "status": 1},
        monkeypatch,
        batch=mock_batch,
    )
    assert "Fault" not in body, body
    mock_eval.assert_not_awaited()
    mock_batch.assert_awaited_once()
    assert mock_batch.await_args.kwargs["filters"] == [{
        "dimension_id": "d1",
        "operator": "eq",
        "value": "EMEA",
    }]


@pytest.mark.asyncio
async def test_bug8383_bare_dimension_where_batches_or_faults(monkeypatch):
    """Bug-8383/L1-R1-004: a bare dimension slicer is never silently dropped."""
    mock_batch = AsyncMock(return_value={_KPI_AA["id"]: {"status": 1}})
    body, mock_eval = await _run_execute(
        "SELECT {[Measures].[aa Status]} ON COLUMNS FROM [m] "
        "WHERE [Region].[Region].[EMEA]",
        {"value": 1.0, "status": 1},
        monkeypatch,
        batch=mock_batch,
    )
    if "Fault" in body:
        # The structured admission parser may reject this legacy bare syntax;
        # that is still fail-closed and cannot reach an unsliced evaluation.
        assert "could not be parsed" in body, body
        mock_eval.assert_not_awaited()
        mock_batch.assert_not_awaited()
    else:
        mock_eval.assert_not_awaited()
        mock_batch.assert_awaited_once()
        assert mock_batch.await_args.kwargs["filters"] == [{
            "dimension_id": "d1",
            "operator": "eq",
            "value": "EMEA",
        }]


@pytest.mark.asyncio
async def test_dimension_named_like_goal_member_not_overwritten(monkeypatch):
    """Review finding 2 regression guard (pre-existing Bug-6888 goal seam, fixed in
    this lane): a DIMENSION captioned 'Net Revenue Goal' must NOT be matched by the
    goal-constant post-join, which would overwrite the dimension column on every row
    (silent wrong numbers). Detection now matches the full unique name
    [Measures].[Net Revenue Goal], which a dimension member reference never
    contains, so the dimension values survive."""
    goal_kpi = {
        "id": "k-nr", "name": "Net Revenue", "display_name": "Net Revenue",
        "expression": 'measure("fee_amount")', "value_measure_id": None,
        "target_type": "static", "target_value": 188914000.0,
        "status_expression": "", "trend_expression": "", "parent_kpi_id": None,
    }

    async def fri(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def fmm(model_id, tenant_slug, jwt_token, **kw):
        return list(_MEASURES)

    async def fmd(model_id, tenant_slug, jwt_token, **kw):
        return [{"id": "d1", "name": "Net Revenue Goal"}]

    async def fmh(model_id, tenant_slug, jwt_token, **kw):
        return []

    async def fns(*a, **kw):
        return []

    async def fkpis(model_id, tenant_slug, jwt_token, **kw):
        return [dict(goal_kpi)]

    async def feq(sql, model_id, tenant_slug, jwt_token, protocol="dax", **_kw):
        return {"columns": ["Net Revenue Goal", "fee_amount"],
                "rows": [{"Net Revenue Goal": "North", "fee_amount": 10.0},
                         {"Net Revenue Goal": "South", "fee_amount": 20.0}]}

    for name, fn in {"_resolve_model_id": fri, "get_model_measures": fmm,
                     "get_model_dimensions": fmd, "get_model_hierarchies": fmh,
                     "get_model_named_sets": fns, "get_model_kpis": fkpis,
                     "execute_query": feq}.items():
        monkeypatch.setattr(xmla_server, name, fn)

    stmt = ("SELECT {[Measures].[fee_amount]} ON COLUMNS, "
            "{[Net Revenue Goal].[Net Revenue Goal].Members} ON ROWS FROM [m]")
    execute_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command><Statement>{stmt}</Statement></Command>
      <Properties><PropertyList><Catalog>m</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""
    root = ET.fromstring(execute_xml)
    method_el = xmla_server._find_method(root)
    resp = await xmla_server._handle_execute(
        method_el, tenant_slug="demo", jwt_token="tok", session_id="sid-f2",
    )
    body = resp.body.decode("utf-8") if isinstance(resp.body, bytes) else str(resp.body)
    assert "Fault" not in body, body
    # The dimension members must NOT be overwritten by the goal constant.
    assert "North" in body and "South" in body, body
    # The static goal constant must NOT have hijacked the dimension column.
    assert "188914000" not in body, body


@pytest.mark.asyncio
async def test_hidden_backed_kpi_status_not_served_on_nontechnical_view(monkeypatch):
    """Review R3 F1 guard (Bug-6702 parity): a KPI whose value measure is HIDDEN is
    withheld from MDSCHEMA_KPIS on a non-technical view, so a hand-written status
    member must NOT be resolved through the governed authority either — Execute and
    the catalogue must agree. The governed authority must not be consulted."""
    hidden_measures = [{"id": "m-fee", "name": "fee_amount",
                        "default_agg": "sum", "is_hidden": True}]

    async def fri(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None  # persona None -> non-technical

    async def fmm(model_id, tenant_slug, jwt_token, **kw):
        return list(hidden_measures)

    async def fmd(model_id, tenant_slug, jwt_token, **kw):
        return []

    async def fmh(model_id, tenant_slug, jwt_token, **kw):
        return []

    async def fns(*a, **kw):
        return []

    async def fkpis(model_id, tenant_slug, jwt_token, **kw):
        return [dict(_KPI_AA)]

    async def feq(sql, model_id, tenant_slug, jwt_token, protocol="dax", **_kw):
        return {"columns": [], "rows": []}

    mock_eval = AsyncMock(return_value={"value": 1.0, "status": 1})
    for name, fn in {"_resolve_model_id": fri, "get_model_measures": fmm,
                     "get_model_dimensions": fmd, "get_model_hierarchies": fmh,
                     "get_model_named_sets": fns, "get_model_kpis": fkpis,
                     "execute_query": feq}.items():
        monkeypatch.setattr(xmla_server, name, fn)
    monkeypatch.setattr(xmla_server, "evaluate_kpi_governed", mock_eval)

    stmt = "SELECT {[Measures].[aa Status]} ON COLUMNS FROM [m]"
    execute_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command><Statement>{stmt}</Statement></Command>
      <Properties><PropertyList><Catalog>m</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""
    root = ET.fromstring(execute_xml)
    method_el = xmla_server._find_method(root)
    await xmla_server._handle_execute(
        method_el, tenant_slug="demo", jwt_token="tok", session_id="sid-f1h",
    )
    # The withheld KPI's governed status must never be consulted/served.
    mock_eval.assert_not_awaited()


# ---------------------------------------------------------------------------
# Wave C #3 — the structured MDX parser is the ADMISSION AUTHORITY.
#
# Contract: the Execute path FAILS CLOSED (SOAP client fault) when the structured
# MDX parser is UNAVAILABLE or reports a syntax/error node (has_error), for EVERY
# MDX statement class. The regex/SQL translator runs only after a clean structured
# parse — a malformed statement never reaches execution via a fallback
# interpretation. The Bug-8009 "fallback interpretation — verify the result"
# WARNING is removed. The MDX grammar was FIXED (Bug-9443) so has_error is a
# RELIABLE signal — comments, Filter/Left/CurrentMember, subselects, and
# Generate/Ascendants inside WITH MEMBER/SET now parse cleanly, so the gate does
# not refuse valid Excel/Power BI queries. DAX (EVALUATE/DEFINE) is exempt.
# ---------------------------------------------------------------------------

from src.dax.ts_mdx_parser import MDXParserUnavailableError  # noqa: E402


def test_admission_admits_clean_plain_select():
    parsed = xmla_server._parse_mdx_for_execute(
        "SELECT {[Measures].[amount]} ON COLUMNS FROM [modely]"
    )
    assert parsed.cube_name == "modely"
    assert not parsed.has_error


@pytest.mark.parametrize(
    "stmt",
    [
        # line/block comments (now a grammar `extra`)
        "// pivot refresh\nSELECT {[Measures].[amount]} ON COLUMNS FROM [modely]",
        "/* x */ SELECT {[Measures].[amount]} ON COLUMNS FROM [modely]",
        # Filter/Left/CurrentMember string predicate (operators in func args)
        'SELECT Filter([Geo].[Geo].Members, Left([Geo].[Geo].CurrentMember.Name, 2) '
        '= "US") ON ROWS, {[Measures].[Amount]} ON COLUMNS FROM [m]',
        # subselect FROM-clause with a paren-wrapped tuple axis
        "SELECT {[Measures].[base_amount]} ON COLUMNS FROM "
        "(SELECT ({[city_dim].[city_dim].&[Berlin]}) ON COLUMNS FROM [m])",
        # WITH MEMBER over a set + Generate/Ascendants + DIMENSION PROPERTIES
        "WITH MEMBER [Measures].[G] AS 'AGGREGATE({[a].[a].[A],[a].[a].[B]})'\n"
        "SET FS As '{[a].[a].[All]}'\n"
        "SELECT {[Measures].[G]} on ROWS, "
        "Hierarchize(Generate(FS, Ascendants([a].[a].currentmember))) "
        "DIMENSION PROPERTIES PARENT_UNIQUE_NAME ON COLUMNS FROM [m]",
        # trailing CELL PROPERTIES clause
        "SELECT {[Measures].[amount]} ON COLUMNS FROM [m] CELL PROPERTIES VALUE, FORMATTED_VALUE",
        # WC3-B1: valid WHERE slicers (KPI/STRTOSET/@param/nested-tuple/arithmetic)
        'SELECT FROM [modely] WHERE (KPIValue("Net Revenue"))',
        "SELECT {[Measures].[amount]} ON 0 FROM [m] WHERE (STRTOSET(@Region, CONSTRAINED))",
        "SELECT {[Measures].[amount]} ON 0 FROM [m] WHERE "
        "(([geo].[geo].&[US]), ([time].[time].&[2024]))",
        "SELECT {[Measures].[amount]} ON 0 FROM [m] WHERE ([Measures].[amount] * 2)",
    ],
)
def test_admission_admits_valid_mdx_after_grammar_fix(stmt):
    # These are valid SSAS MDX shapes the grammar previously over-flagged. After
    # the Wave C grammar fix they parse CLEANLY (has_error False) and are admitted.
    parsed = xmla_server._parse_mdx_for_execute(stmt)  # must not raise
    assert not parsed.has_error


def test_admission_admits_dax_behind_leading_comment():
    # WC3-U2: a DAX statement behind a leading line/block comment is still
    # recognised as DAX and admitted past the MDX has_error gate (does not raise).
    for stmt in (
        "// refresh\nEVALUATE SUMMARIZECOLUMNS([Region])",
        "/* c */ EVALUATE ROW(\"x\", 1)",
    ):
        parsed = xmla_server._parse_mdx_for_execute(stmt)  # must not raise
        assert parsed is not None


@pytest.mark.parametrize(
    "stmt",
    [
        "SELECT {[Measures].[amount]} ON COLUMNS FROM",        # no cube
        "SELECT {[Measures].[amount]} ON COLUMNS",             # no FROM
        "DROP TABLE users; SELECT 1",                           # not MDX
        "SELECT {[Measures].[amount]} ON 0 FROM [c] WHERE ((((",  # unbalanced
    ],
)
def test_admission_refuses_malformed_mdx(stmt):
    # Wave C #3: the has_error gate now REJECTS malformed MDX — it never reaches
    # the regex translator via a fallback interpretation.
    with pytest.raises(ValueError, match="could not be parsed"):
        xmla_server._parse_mdx_for_execute(stmt)


def test_admission_admits_dax_evaluate():
    # DAX is not MDX; the MDX grammar cannot parse it (has_error), but DAX is
    # EXEMPT from the has_error gate — the DAX translator is its authority.
    parsed = xmla_server._parse_mdx_for_execute(
        'EVALUATE SUMMARIZECOLUMNS([Region], "amt", SUM(Fact[base_amount]))'
    )
    assert parsed is not None


@pytest.mark.asyncio
async def test_execute_refuses_malformed_mdx_before_dispatch(monkeypatch):
    """End-to-end: a malformed MDX Execute faults as a SOAP client error and the
    query is NEVER dispatched — the has_error gate rejects it at admission."""
    fakes = _make_fakes({"value": 1.0, "status": 1})
    for name, fn in fakes.items():
        monkeypatch.setattr(xmla_server, name, fn)
    not_called = AsyncMock(side_effect=AssertionError("execute_query was reached"))
    monkeypatch.setattr(xmla_server, "execute_query", not_called)

    stmt = "SELECT {[Measures].[fee_amount]} ON COLUMNS FROM"  # malformed: no cube
    execute_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command><Statement>{stmt}</Statement></Command>
      <Properties><PropertyList><Catalog>m</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""
    root = ET.fromstring(execute_xml)
    method_el = xmla_server._find_method(root)
    resp = await xmla_server._handle_execute(
        method_el, tenant_slug="demo", jwt_token="tok", session_id="sid-mal",
    )
    body = resp.body.decode("utf-8") if isinstance(resp.body, bytes) else str(resp.body)
    assert "Fault" in body, body
    assert "could not be parsed" in body, body
    not_called.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_admits_kpi_where_slicer_end_to_end(monkeypatch):
    """WC3-B1 gate-level guard (end-to-end, not the isolated interceptor): a
    ``WHERE (KPIValue(...))`` slicer is ADMITTED by the has_error gate and REACHES
    the KPI interceptor — it is never rejected as 'could not be parsed'."""
    async def _resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def _meta(**kwargs):
        return ([{"id": "m1", "name": "amount"}], [], [])

    async def _named_sets(*a, **kw):
        return []

    async def _no_info(*a, **kw):
        return None

    reached = {}

    async def _kpi(**kwargs):
        reached["called"] = True
        return (["Net Revenue (Value)"], [{"Net Revenue (Value)": 42.5}])

    monkeypatch.setattr(xmla_server, "_resolve_model_id", _resolve_model_id)
    monkeypatch.setattr(xmla_server, "_load_model_metadata_cached", _meta)
    monkeypatch.setattr(xmla_server, "get_model_named_sets", _named_sets)
    monkeypatch.setattr(xmla_server, "_maybe_resolve_info_measures", _no_info)
    monkeypatch.setattr(xmla_server, "_maybe_resolve_kpi_members", _kpi)
    not_called = AsyncMock(side_effect=AssertionError("execute_query was reached"))
    monkeypatch.setattr(xmla_server, "execute_query", not_called)

    stmt = 'SELECT FROM [modely] WHERE (KPIValue("Net Revenue"))'
    execute_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command><Statement>{stmt}</Statement></Command>
      <Properties><PropertyList><Catalog>modely</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""
    root = ET.fromstring(execute_xml)
    method_el = xmla_server._find_method(root)
    resp = await xmla_server._handle_execute(
        method_el, tenant_slug="demo", jwt_token="tok", session_id="sid-kpi-where",
    )
    body = resp.body.decode("utf-8") if isinstance(resp.body, bytes) else str(resp.body)
    assert "could not be parsed" not in body, body
    assert "<soap11env:Fault>" not in body, body
    assert reached.get("called") is True  # admission admitted -> KPI interceptor reached


def test_admission_fails_closed_when_parser_unavailable(monkeypatch):
    def _unavailable(_statement):
        raise MDXParserUnavailableError("no grammar")

    monkeypatch.setattr(xmla_server, "parse_mdx_statement", _unavailable)
    with pytest.raises(ValueError, match="structured MDX parser"):
        xmla_server._parse_mdx_for_execute(
            "SELECT {[Measures].[amount]} ON COLUMNS FROM [m]"
        )


@pytest.mark.asyncio
async def test_execute_fails_closed_when_parser_unavailable(monkeypatch):
    """End-to-end: when the structured parser is unavailable the Execute faults as a
    SOAP client error and the query is NEVER dispatched — no fallback interpretation."""
    fakes = _make_fakes({"value": 1.0, "status": 1})
    for name, fn in fakes.items():
        monkeypatch.setattr(xmla_server, name, fn)
    not_called = AsyncMock(side_effect=AssertionError("execute_query was reached"))
    monkeypatch.setattr(xmla_server, "execute_query", not_called)

    def _unavailable(_statement):
        raise MDXParserUnavailableError("no grammar")

    monkeypatch.setattr(xmla_server, "parse_mdx_statement", _unavailable)

    stmt = "SELECT {[Measures].[fee_amount]} ON COLUMNS FROM [m]"
    execute_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command><Statement>{stmt}</Statement></Command>
      <Properties><PropertyList><Catalog>m</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""
    root = ET.fromstring(execute_xml)
    method_el = xmla_server._find_method(root)
    resp = await xmla_server._handle_execute(
        method_el, tenant_slug="demo", jwt_token="tok", session_id="sid-8009",
    )
    body = resp.body.decode("utf-8") if isinstance(resp.body, bytes) else str(resp.body)
    assert "Fault" in body, body
    assert "structured MDX parser" in body, body
    not_called.assert_not_awaited()
