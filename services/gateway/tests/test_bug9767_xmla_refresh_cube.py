"""Regression tests for Bug-9767.

Excel issues ``REFRESH CUBE [<cube>]`` as a DDL Execute statement every time
the user hits Refresh on an OLAP PivotTable connection. Before the fix, this
statement was not intercepted anywhere in ``_handle_execute`` and fell
through to ``_parse_mdx_for_execute`` — the structured MDX parser, which only
understands the MDX SELECT grammar and reports ``has_error`` for DDL, so the
Execute was refused as a SOAP client fault. Excel surfaced this to the user
as a generic connection error ("The query did not run, or the database table
could not be opened...") on every Refresh — reproduced live on the
investor-demo-rc gateway.

The fix intercepts ``REFRESH CUBE`` before the MDX parser (mirroring the
existing empty-handshake/Cancel/DMV interceptions in ``_handle_execute``) and
acknowledges it with the same benign empty-root success response used for
those. No catalog/model resolution is needed or attempted.
"""

from defusedxml import ElementTree as ET
from xml.etree.ElementTree import Element

import pytest

from src.dax import xmla_server


def _execute_method(xml_body: str) -> Element:
    root = ET.fromstring(xml_body)
    method_el = xmla_server._find_method(root)
    assert method_el is not None
    return method_el


def _refresh_cube_xml(statement: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command>
        <Statement>{statement}</Statement>
      </Command>
      <Properties>
        <PropertyList>
          <Catalog>modely</Catalog>
        </PropertyList>
      </Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""


@pytest.mark.parametrize(
    "statement",
    [
        "REFRESH CUBE [modely]",
        "refresh cube [modely]",
        "  REFRESH   CUBE  [modely]  ",
        "REFRESH CUBE modely",
    ],
)
def test_is_refresh_cube_statement_matches_excel_variants(statement):
    assert xmla_server._is_refresh_cube_statement(statement) is True


@pytest.mark.parametrize(
    "statement",
    [
        None,
        "",
        "SELECT {[Measures].[Revenue]} ON COLUMNS FROM [m]",
        "SELECT {[account_type].[account_type].Members} ON ROWS FROM [modely]",
    ],
)
def test_is_refresh_cube_statement_does_not_match_normal_queries(statement):
    assert xmla_server._is_refresh_cube_statement(statement) is False


@pytest.mark.asyncio
async def test_refresh_cube_execute_acknowledges_without_touching_model_resolution(monkeypatch):
    """The interception must happen BEFORE catalog/model resolution — proven
    by leaving _resolve_model_id and friends unmocked. If REFRESH CUBE fell
    through to normal Execute handling, this test would fail with a real
    network/attribute error instead of a clean empty-success response."""
    def _boom(*_args, **_kwargs):
        raise AssertionError(
            "REFRESH CUBE must be acknowledged before any model/catalog "
            "resolution is attempted"
        )

    monkeypatch.setattr(xmla_server, "_resolve_model_id", _boom)

    response = await xmla_server._handle_execute(
        _execute_method(_refresh_cube_xml("REFRESH CUBE [modely]")),
        tenant_slug="acme-demo",
        jwt_token="token",
        session_id="sid-refresh-1",
    )
    body = response.body.decode("utf-8")

    assert response.status_code == 200
    assert "<soap11env:Fault>" not in body
    assert "xml-analysis:empty" in body
    assert "<tns:ExecuteResponse>" in body


@pytest.mark.asyncio
async def test_refresh_cube_execute_is_case_and_whitespace_tolerant(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("must not resolve model for a REFRESH CUBE statement")

    monkeypatch.setattr(xmla_server, "_resolve_model_id", _boom)

    response = await xmla_server._handle_execute(
        _execute_method(_refresh_cube_xml("  refresh   cube  [modely]  ")),
        tenant_slug="acme-demo",
        jwt_token="token",
        session_id="sid-refresh-2",
    )
    body = response.body.decode("utf-8")

    assert response.status_code == 200
    assert "<soap11env:Fault>" not in body
    assert "xml-analysis:empty" in body
