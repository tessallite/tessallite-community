"""Wave C #11 — declared model parameters via the scalar XMLA <Parameters> form.

An XMLA Execute <Parameters> block maps to the existing session_vars app.<name>
resolver. Every query generated for one Execute carries the SAME mapping through a
single wrapper, so a new sub-query branch cannot omit it. Duplicate / malformed /
table-valued / expression-valued / undeclared parameters fault as a SOAP client
fault. User values never enter the MDX text.
"""
from __future__ import annotations

import re
import inspect

import pytest
from defusedxml import ElementTree as ET

from src.dax import xmla_server as xs


# ---------------------------------------------------------------------------
# _parse_xmla_parameters — scalar parse + rejection matrix
# ---------------------------------------------------------------------------

def _method(parameters_xml: str):
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        '<soap:Body>'
        '<Execute xmlns="urn:schemas-microsoft-com:xml-analysis">'
        '<Command><Statement>SELECT {[Measures].[amount]} ON COLUMNS FROM [m]</Statement></Command>'
        '<Properties><PropertyList><Catalog>m</Catalog></PropertyList></Properties>'
        f'{parameters_xml}'
        '</Execute></soap:Body></soap:Envelope>'
    )
    return xs._find_method(ET.fromstring(envelope))


def _params(*pairs: tuple[str, str]) -> str:
    body = "".join(
        f"<Parameter><Name>{n}</Name><Value>{v}</Value></Parameter>" for n, v in pairs
    )
    return f"<Parameters>{body}</Parameters>"


def test_no_parameters_block_returns_empty():
    assert xs._parse_xmla_parameters(_method("")) == {}


def test_scalar_parameters_parsed_and_canonicalised():
    got = xs._parse_xmla_parameters(_method(_params(("Region", "EMEA"), ("@Year", "2025"))))
    assert got == {"region": "EMEA", "year": "2025"}


def test_json_multi_value_is_carried_as_text():
    # The resolver decodes JSON multi-value by declared param_type; the gateway
    # only carries the scalar text.
    got = xs._parse_xmla_parameters(_method(_params(("City", '["Paris","Lyon"]'))))
    assert got == {"city": '["Paris","Lyon"]'}


def test_duplicate_parameter_is_rejected():
    dup = _params(("Region", "EMEA"), ("@region", "APAC"))
    with pytest.raises(ValueError, match="more than once"):
        xs._parse_xmla_parameters(_method(dup))


def test_missing_name_is_rejected():
    xml = "<Parameters><Parameter><Value>x</Value></Parameter></Parameters>"
    with pytest.raises(ValueError, match="missing its <Name>"):
        xs._parse_xmla_parameters(_method(xml))


def test_multiple_values_is_rejected():
    xml = (
        "<Parameters><Parameter><Name>Region</Name>"
        "<Value>A</Value><Value>B</Value></Parameter></Parameters>"
    )
    with pytest.raises(ValueError, match="exactly one <Value>"):
        xs._parse_xmla_parameters(_method(xml))


def test_table_valued_parameter_is_rejected():
    # A <Value> with element children is a table/rowset-valued parameter.
    xml = (
        "<Parameters><Parameter><Name>Region</Name>"
        "<Value><row><c>A</c></row></Value></Parameter></Parameters>"
    )
    with pytest.raises(ValueError, match="not a scalar"):
        xs._parse_xmla_parameters(_method(xml))


# ---------------------------------------------------------------------------
# _handle_execute — declared → session_vars; undeclared → SOAP client fault
# ---------------------------------------------------------------------------

def _patch(monkeypatch, declared, capture):
    async def _resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def _meta(**kwargs):
        return ([{"id": "meas-1", "name": "amount"}], [], [])

    async def _named_sets(*a, **kw):
        return []

    async def _get_params(*a, **kw):
        return declared

    def _stmt_to_sql(*a, **kw):
        return "SELECT 1", "jdbc"

    async def _exec(**kwargs):
        capture.append(kwargs.get("session_vars"))
        return {"columns": ["amount"], "rows": [{"amount": 1}]}

    monkeypatch.setattr(xs, "_resolve_model_id", _resolve_model_id)
    monkeypatch.setattr(xs, "_load_model_metadata_cached", _meta)
    monkeypatch.setattr(xs, "get_model_named_sets", _named_sets)
    monkeypatch.setattr(xs, "get_model_parameters", _get_params)
    monkeypatch.setattr(xs, "_statement_to_sql", _stmt_to_sql)
    monkeypatch.setattr(xs, "execute_query", _exec)


@pytest.mark.asyncio
async def test_declared_parameter_reaches_execute_as_session_var(monkeypatch):
    capture: list = []
    _patch(monkeypatch, [{"name": "@Region", "param_type": "string"}], capture)
    resp = await xs._handle_execute(
        _method(_params(("Region", "EMEA"))),
        tenant_slug="demo", jwt_token="tok", session_id="sid-p1",
    )
    body = resp.body.decode("utf-8") if isinstance(resp.body, bytes) else str(resp.body)
    assert "Fault" not in body, body
    # The generated query carried the parameter as app.region.
    assert capture and capture[0] == {"app.region": "EMEA"}


@pytest.mark.asyncio
async def test_undeclared_parameter_is_client_fault(monkeypatch):
    capture: list = []
    _patch(monkeypatch, [{"name": "@Region", "param_type": "string"}], capture)
    resp = await xs._handle_execute(
        _method(_params(("Territory", "EMEA"))),  # not declared
        tenant_slug="demo", jwt_token="tok", session_id="sid-p2",
    )
    body = resp.body.decode("utf-8") if isinstance(resp.body, bytes) else str(resp.body)
    assert "soap11env:Client" in body, body
    assert "Unknown model parameter" in body
    assert not capture  # the query never ran


@pytest.mark.asyncio
async def test_no_parameters_leaves_session_vars_unset(monkeypatch):
    capture: list = []
    _patch(monkeypatch, [], capture)
    await xs._handle_execute(
        _method(""),
        tenant_slug="demo", jwt_token="tok", session_id="sid-p3",
    )
    assert capture == [None]


# ---------------------------------------------------------------------------
# Structural guard: every generated query in _handle_execute goes through the
# single _execute_query wrapper — a new branch cannot use bare execute_query().
# ---------------------------------------------------------------------------

def test_all_sub_queries_use_the_single_wrapper():
    src = inspect.getsource(xs._handle_execute)
    # The only bare execute_query( call is inside the local wrapper definition.
    bare = re.findall(r"(?<![_\w])execute_query\(", src)
    # One occurrence: `return await execute_query(**kwargs)` inside _execute_query.
    assert len(bare) == 1, f"unexpected bare execute_query calls: {len(bare)}"


# ---------------------------------------------------------------------------
# B1 (T3 challenger) — decision #11 on the DRILLTHROUGH result-bearing path.
#
# A DRILLTHROUGH Execute is result-bearing exactly like a SELECT, so its detail
# query MUST be scoped by the Execute's declared <Parameters> (app.<name> →
# value), and a malformed / duplicate / undeclared / table-valued <Parameters>
# on a DRILLTHROUGH Execute MUST FAULT with a SOAP client fault, never be
# silently ignored. Before the B1 fix, _handle_execute BUILT AND RETURNED the
# DRILLTHROUGH ExecuteResponse BEFORE the #11 parameter parse ran, so a
# parameterised drill ran UNSCOPED and a bad <Parameters> on a drill was dropped.
# These tests drive the whole _handle_execute path and fail against pre-fix code.
# ---------------------------------------------------------------------------

def _drill_method(parameters_xml: str):
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        '<soap:Body>'
        '<Execute xmlns="urn:schemas-microsoft-com:xml-analysis">'
        '<Command><Statement>'
        'DRILLTHROUGH SELECT {[Measures].[amount]} ON COLUMNS FROM [m]'
        '</Statement></Command>'
        '<Properties><PropertyList><Catalog>m</Catalog></PropertyList></Properties>'
        f'{parameters_xml}'
        '</Execute></soap:Body></soap:Envelope>'
    )
    return xs._find_method(ET.fromstring(envelope))


def _patch_drill(monkeypatch, declared, capture):
    """Patch the gateway Execute path so a DRILLTHROUGH reaches the router-client
    drill call; capture the session_vars the drill query is scoped by."""
    from src.dax import drillthrough_handler as dth

    async def _resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def _meta(**kwargs):
        return ([{"id": "meas-1", "name": "amount"}], [], [])

    async def _named_sets(*a, **kw):
        return []

    async def _get_params(*a, **kw):
        return declared

    async def _drill_options(*a, **kw):
        return {"hierarchies": []}

    async def _drill_through(measure_id, grouping_levels, jwt_token, **kwargs):
        capture.append(kwargs.get("session_vars"))
        return {
            "columns": ["order_id"],
            "rows": [{"order_id": "1"}],
            "page": {"has_more": False},
            "hierarchy_path": [],
        }

    monkeypatch.setattr(xs, "_resolve_model_id", _resolve_model_id)
    monkeypatch.setattr(xs, "_load_model_metadata_cached", _meta)
    monkeypatch.setattr(xs, "get_model_named_sets", _named_sets)
    monkeypatch.setattr(xs, "get_model_parameters", _get_params)
    # The router-client drill calls are module-level names on drillthrough_handler.
    monkeypatch.setattr(dth, "execute_drill_options", _drill_options)
    monkeypatch.setattr(dth, "execute_drill_through", _drill_through)


@pytest.mark.asyncio
async def test_b1_drillthrough_declared_parameter_reaches_drill_query(monkeypatch):
    # decision #11 + DRILLTHROUGH: a declared <Parameters> on a DRILLTHROUGH
    # Execute must scope the drill detail query as app.<name> session_vars.
    capture: list = []
    _patch_drill(monkeypatch, [{"name": "@Region", "param_type": "string"}], capture)
    resp = await xs._handle_execute(
        _drill_method(_params(("Region", "EMEA"))),
        tenant_slug="demo", jwt_token="tok", session_id="sid-b1a",
    )
    body = resp.body.decode("utf-8") if isinstance(resp.body, bytes) else str(resp.body)
    assert "Fault" not in body, body
    # The drill query was scoped by the parameter as app.region — pre-fix this
    # was None because the drill branch returned before the #11 parse.
    assert capture and capture[0] == {"app.region": "EMEA"}, capture


@pytest.mark.asyncio
async def test_b1_drillthrough_undeclared_parameter_faults(monkeypatch):
    # decision #11 + DRILLTHROUGH: an UNDECLARED <Parameters> on a DRILLTHROUGH
    # Execute must fault (SOAP client fault), not be silently ignored, and the
    # drill query must never run.
    capture: list = []
    _patch_drill(monkeypatch, [{"name": "@Region", "param_type": "string"}], capture)
    resp = await xs._handle_execute(
        _drill_method(_params(("Territory", "EMEA"))),  # not declared
        tenant_slug="demo", jwt_token="tok", session_id="sid-b1b",
    )
    body = resp.body.decode("utf-8") if isinstance(resp.body, bytes) else str(resp.body)
    assert "soap11env:Client" in body, body
    assert "Unknown model parameter" in body, body
    assert not capture, "drill query must not run when parameters are rejected"


@pytest.mark.asyncio
async def test_b1_drillthrough_malformed_parameter_faults(monkeypatch):
    # decision #11 + DRILLTHROUGH: a MALFORMED (duplicate) <Parameters> on a
    # DRILLTHROUGH Execute must fault with a SOAP client fault before the drill.
    capture: list = []
    _patch_drill(monkeypatch, [{"name": "@Region", "param_type": "string"}], capture)
    dup = _params(("Region", "EMEA"), ("@region", "APAC"))
    resp = await xs._handle_execute(
        _drill_method(dup),
        tenant_slug="demo", jwt_token="tok", session_id="sid-b1c",
    )
    body = resp.body.decode("utf-8") if isinstance(resp.body, bytes) else str(resp.body)
    assert "soap11env:Client" in body, body
    assert "more than once" in body, body
    assert not capture, "drill query must not run when parameters are malformed"
