"""Bug-9979: ordinary XMLA Execute must not wait on unrelated named sets."""
from __future__ import annotations

import httpx
import pytest
from defusedxml import ElementTree as ET

from src.dax import xmla_server as xs


@pytest.mark.parametrize(
    "mdx",
    [
        (
            "SELECT NON EMPTY CrossJoin({[Measures].[base_amount]}, "
            "{[Gender].[Gender].[(All)].Members}) ON COLUMNS FROM [modely]"
        ),
        (
            "SELECT NON EMPTY Hierarchize(DrilldownMember("
            "{[account_type].[account_type].[(All)]}, "
            "{[account_type].[account_type].[(All)].Children})) ON ROWS, "
            "{[Measures].[avg_base_amount]} ON COLUMNS FROM [modely]"
        ),
    ],
)
def test_qualified_pivot_mdx_produces_candidates_for_exact_service_filter(mdx):
    candidates = xs._mdx_named_set_reference_candidates(mdx)
    assert "Measures" in candidates
    assert "modely" in candidates


@pytest.mark.parametrize(
    "mdx",
    [
        "SELECT {[Measures].[m]} ON 0, {[Top Customers]} ON 1 FROM [modely]",
        "SELECT {[Measures].[m]} ON 0, TopCustomers ON 1 FROM [modely]",
        (
            "SELECT {[Measures].[m]} ON 0, "
            "Union(TopCustomers, {[customer].[customer].Members}) ON 1 "
            "FROM [modely]"
        ),
    ],
)
def test_saved_set_reference_shapes_include_the_referenced_name(mdx):
    candidates = {name.casefold() for name in xs._mdx_named_set_reference_candidates(mdx)}
    assert "top customers" in candidates or "topcustomers" in candidates


def test_unparseable_mdx_still_produces_exact_match_candidates():
    candidates = xs._mdx_named_set_reference_candidates(
        "SELECT {[Measures].[m]} ON 0, [Saved Set] ??? ON 1 FROM [modely]"
    )
    assert "Saved Set" in candidates


def test_excel_add_calculated_members_needs_no_function_allow_list():
    candidates = xs._mdx_named_set_reference_candidates(
        "SELECT AddCalculatedMembers({[Measures].[Revenue]}) ON COLUMNS "
        "FROM [modelx]"
    )
    assert "AddCalculatedMembers" in candidates
    assert "Revenue" in candidates


def test_dax_does_not_use_mdx_saved_sets():
    assert xs._mdx_named_set_reference_candidates('EVALUATE ROW("x", 1)') == []


@pytest.mark.asyncio
async def test_ordinary_execute_requests_only_names_present_in_the_mdx(monkeypatch):
    mdx = "SELECT {[Measures].[Revenue]} ON COLUMNS FROM [modelx]"
    exec_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command><Statement>{mdx}</Statement></Command>
      <Properties><PropertyList><Catalog>model_1</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""

    async def fake_resolve_model_id(*_args, **_kwargs):
        return "model-1", "proj-1", None, None

    async def fake_resolve_model_slug(*_args, **_kwargs):
        return "modelx"

    async def fake_meta(**_kwargs):
        return (
            [{"id": "m1", "name": "Revenue", "default_agg": "sum"}],
            [{"id": "d1", "name": "Region", "source": "column"}],
            [],
        )

    captured: dict = {}

    async def filtered_named_sets(*_args, **kwargs):
        captured.update(kwargs)
        return []

    def stop_after_named_set_gate(_method_el):
        raise ValueError("reached the next Execute validation stage")

    monkeypatch.setattr(xs, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xs, "_resolve_model_slug", fake_resolve_model_slug)
    monkeypatch.setattr(xs, "_load_model_metadata_cached", fake_meta)
    monkeypatch.setattr(xs, "get_model_named_sets", filtered_named_sets)
    monkeypatch.setattr(xs, "_parse_xmla_parameters", stop_after_named_set_gate)

    root = ET.fromstring(exec_xml)
    response = await xs._handle_execute(
        xs._find_method(root),
        tenant_slug="acme",
        jwt_token="tok",
        session_id="s1",
    )

    assert response.status_code == 200
    assert b"reached the next Execute validation stage" in response.body
    assert "Revenue" in captured["reference_names"]
    assert "modelx" in captured["reference_names"]


@pytest.mark.asyncio
async def test_required_named_set_timeout_returns_readable_soap_fault(monkeypatch):
    mdx = "SELECT {[Measures].[Revenue]} ON 0, {[Saved Set]} ON 1 FROM [modelx]"
    exec_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command><Statement>{mdx}</Statement></Command>
      <Properties><PropertyList><Catalog>model_1</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""

    async def fake_resolve_model_id(*_args, **_kwargs):
        return "model-1", "proj-1", None, None

    async def fake_resolve_model_slug(*_args, **_kwargs):
        return "modelx"

    async def fake_meta(**_kwargs):
        return (
            [{"id": "m1", "name": "Revenue", "default_agg": "sum"}],
            [{"id": "d1", "name": "Region", "source": "column"}],
            [],
        )

    async def timeout(*_args, **_kwargs):
        request = httpx.Request("GET", "http://model-service/named-sets")
        raise httpx.ReadTimeout("timed out", request=request)

    monkeypatch.setattr(xs, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xs, "_resolve_model_slug", fake_resolve_model_slug)
    monkeypatch.setattr(xs, "_load_model_metadata_cached", fake_meta)
    monkeypatch.setattr(xs, "get_model_named_sets", timeout)

    root = ET.fromstring(exec_xml)
    response = await xs._handle_execute(
        xs._find_method(root),
        tenant_slug="acme",
        jwt_token="tok",
        session_id="s1",
    )

    body = response.body.decode()
    assert response.status_code == 503
    assert "Fault" in body
    assert "saved named-set catalogue could not be checked" in body
    assert "query was refused" in body
