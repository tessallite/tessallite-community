"""Set F — Gateway XMLA/Excel conformance tests.

Covers:
- Bug-5430: Power BI / Tabular discovery rowsets (DISCOVER_CSDL_METADATA,
  DISCOVER_CALC_DEPENDENCY, and $SYSTEM.TMSCHEMA_* DMV Execute).
- Bug-5434: flat-dimension MEMBER_KEY vs MEMBER_CAPTION separation.
- Bug-5436b: XMLA response compression (gzip/deflate) and the <Cancel> command.
"""
from __future__ import annotations

import gzip
import re
import zlib

import pytest
from defusedxml import ElementTree as ET
from xml.etree.ElementTree import Element

from src.dax import xmla_server
from src.dax.mdschema import (
    build_discover_response,
    build_tmschema_rowset,
)


@pytest.fixture(autouse=True)
def _reset_accept_encoding():
    """The Accept-Encoding ContextVar is request-scoped in production (set at
    each HTTP entry point), but pytest runs every test in the same context, so
    a value set by one compression test would bleed into the next and gzip an
    unrelated response. Reset it to identity around every test."""
    token = xmla_server._accept_encoding.set("")
    try:
        yield
    finally:
        xmla_server._accept_encoding.reset(token)


def _execute_method(xml_body: str) -> Element:
    root = ET.fromstring(xml_body)
    method_el = xmla_server._find_method(root)
    assert method_el is not None
    return method_el


def _local(tag: str) -> str:
    """Strip any XML namespace prefix from an element tag."""
    return tag.rsplit("}", 1)[-1]


def _tmschema_measure_rows(root: Element) -> list[dict[str, str]]:
    """Parse a TMSCHEMA rowset SOAP response into a list of row dicts keyed by
    the (namespace-stripped) cell tag. Used by the Bug-5493 RBAC tests to assert
    per-measure Expression / IsHidden values rather than substring presence."""
    out: list[dict[str, str]] = []
    for el in root.iter():
        if _local(el.tag) != "row":
            continue
        row: dict[str, str] = {}
        for cell in list(el):
            row[_local(cell.tag)] = (cell.text or "")
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Bug-5430 — Power BI / Tabular discovery rowsets
# ---------------------------------------------------------------------------

def test_csdl_metadata_rowset_describes_model():
    """DISCOVER_CSDL_METADATA returns a single-row rowset whose Metadata cell
    carries a well-formed CSDL envelope naming the catalog, its dimensions, and
    its measures."""
    xml = build_discover_response(
        request_type="DISCOVER_CSDL_METADATA",
        catalog_name="mymodel",
        model_id="m1",
        measures=[{"name": "Sales"}],
        dimensions=[{"name": "Region"}],
    )
    assert "<Metadata>" in xml
    # CSDL document is embedded (XML-escaped) in the cell.
    assert "edmx:Edmx" in xml
    assert "Region" in xml
    assert "Sales" in xml
    assert "mymodel" in xml
    # Exactly one row.
    assert xml.count("<row>") == 1


def test_calc_dependency_rowset_is_conformant_empty():
    """DISCOVER_CALC_DEPENDENCY returns a conformant empty rowset (no Tabular
    calculation dependency objects), not a fault."""
    xml = build_discover_response(
        request_type="DISCOVER_CALC_DEPENDENCY",
        catalog_name="mymodel",
        model_id="m1",
        measures=[],
        dimensions=[],
    )
    assert "<root" in xml
    assert xml.count("<row>") == 0
    # Schema still declares the dependency columns so strict clients parse it.
    assert "OBJECT_TYPE" in xml


def test_schema_rowsets_advertise_tabular_surfaces():
    """A discovering client must see the new Tabular rowsets advertised in
    DISCOVER_SCHEMA_ROWSETS so it knows the gateway dispatches them.

    MSOLAP validates the advertised schema-rowset metadata strictly; an empty
    SchemaGuid element violates the inline uuid pattern and makes Excel stop
    before it asks for DBSCHEMA_CATALOGS.
    """
    xml = build_discover_response(
        request_type="DISCOVER_SCHEMA_ROWSETS",
        catalog_name="",
        model_id="",
        measures=[],
        dimensions=[],
    )
    assert "DISCOVER_CSDL_METADATA" in xml
    assert "DISCOVER_CALC_DEPENDENCY" in xml
    uuid_pattern = re.compile(
        r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
        r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
    )
    for row_xml in re.findall(r"<row>(.*?)</row>", xml, flags=re.S):
        name = re.search(r"<SchemaName>(.*?)</SchemaName>", row_xml)
        guid = re.search(r"<SchemaGuid>(.*?)</SchemaGuid>", row_xml)
        assert name is not None
        assert guid is not None, f"{name.group(1)} missing SchemaGuid"
        assert uuid_pattern.match(guid.group(1)), (
            f"{name.group(1)} has invalid SchemaGuid {guid.group(1)!r}"
        )


def test_tmschema_measures_rowset_from_metadata():
    cols, rows = build_tmschema_rowset(
        "TMSCHEMA_MEASURES",
        "mymodel",
        measures=[{"name": "Sales", "expression": "SUM(amount)"}],
        dimensions=[{"name": "Region"}],
        hierarchy_defs=[],
    )
    names = {c["name"] for c in cols}
    assert {"ID", "Name", "Expression"} <= names
    assert len(rows) == 1
    assert rows[0]["Name"] == "Sales"
    assert rows[0]["Expression"] == "SUM(amount)"


def test_tmschema_unknown_table_returns_conformant_empty():
    cols, rows = build_tmschema_rowset(
        "TMSCHEMA_RELATIONSHIPS", "mymodel", [], [], [],
    )
    assert rows == []
    assert [c["name"] for c in cols] == ["ID"]


def test_is_tmschema_dmv_detection():
    assert xmla_server._is_tmschema_dmv(
        "SELECT [Name] FROM $SYSTEM.TMSCHEMA_MEASURES"
    )
    assert xmla_server._is_tmschema_dmv(
        "select * from [$SYSTEM].[TMSCHEMA_TABLES]"
    )
    assert (
        xmla_server._tmschema_table_name(
            "SELECT * FROM $SYSTEM.TMSCHEMA_COLUMNS"
        )
        == "TMSCHEMA_COLUMNS"
    )
    # A normal MDX Execute is not a TMSCHEMA DMV.
    assert not xmla_server._is_tmschema_dmv(
        "SELECT {[Measures].[Sales]} ON 0 FROM [cube]"
    )
    assert not xmla_server._is_tmschema_dmv(None)


@pytest.mark.parametrize(
    "statement",
    [
        # Wave C #3: a missing structured parser now fails closed for EVERY
        # statement class — simple SELECT too — never a fallback interpretation.
        "SELECT {[Measures].[Sales]} ON 0 FROM [cube]",
        "DRILLTHROUGH SELECT {[Measures].[Sales]} ON 0 FROM [cube]",
        "WITH MEMBER [Measures].[X] AS '1' SELECT {[Measures].[X]} ON 0 FROM [cube]",
    ],
)
def test_parse_mdx_for_execute_fails_closed_when_parser_missing(
    monkeypatch, statement,
):
    def missing_parser(_statement):
        raise xmla_server.MDXParserUnavailableError("tree_sitter missing")

    monkeypatch.setattr(xmla_server, "parse_mdx_statement", missing_parser)

    with pytest.raises(ValueError, match="structured MDX parser"):
        xmla_server._parse_mdx_for_execute(statement)


@pytest.mark.asyncio
async def test_handle_execute_dispatches_tmschema_dmv(monkeypatch):
    """A $SYSTEM.TMSCHEMA_* Execute is answered from model metadata as a flat
    Rowset, not routed through MDX translation."""
    async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def fake_get_model_measures(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [{"name": "Sales", "expression": "SUM(x)"}]

    async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [{"name": "Region"}]

    async def fake_get_model_hierarchies(model_id, tenant_slug, jwt_token, project_id="", include_details=False, **kw):
        return []

    def _boom(*_a, **_k):
        raise AssertionError("TMSCHEMA DMV must not reach MDX translation")

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "get_model_hierarchies", fake_get_model_hierarchies)
    monkeypatch.setattr(xmla_server, "_statement_to_sql", _boom)

    execute = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command><Statement>SELECT [Name] FROM $SYSTEM.TMSCHEMA_MEASURES</Statement></Command>
      <Properties><PropertyList><Catalog>mymodel</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""

    response = await xmla_server._handle_execute(
        _execute_method(execute), tenant_slug="demo", jwt_token="token", session_id="sid-tm",
    )
    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "<soap11env:Fault>" not in body
    assert "<tns:ExecuteResponse>" in body
    assert "Sales" in body


@pytest.mark.asyncio
async def test_tmschema_dmv_persona_excludes_access_restricted_includes_hidden(monkeypatch):
    """Bug-5493 — the TMSCHEMA_MEASURES DMV must distinguish ACCESS from CURATION:

    - A measure outside the persona allow list (ACCESS restriction) must be
      absent, and its DAX expression must NOT leak.
    - A measure restricted ONLY by the hidden/visible flag (CURATION) must be
      PRESENT with its expression and IsHidden=true — matching the real SSAS
      Tabular contract for TMSCHEMA_MEASURES.
    """
    persona = {
        "id": "persona-1",
        "includes_hidden_columns": False,
        # Allow Sales AND the hidden measure — the hidden flag alone must not
        # remove the measure from the allow list.
        "included_measure_ids": ["m-sales", "m-hidden"],
    }

    async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", persona, None

    async def fake_get_model_measures(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [
            {"id": "m-sales", "name": "Sales", "expression": "SUM(amount)"},
            {"id": "m-secret", "name": "SecretMargin", "expression": "SUM(secret_margin)"},
            {"id": "m-hidden", "name": "HiddenCost", "expression": "SUM(cost)", "is_hidden": True},
        ]

    async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [{"id": "d-region", "name": "Region"}]

    async def fake_get_model_hierarchies(model_id, tenant_slug, jwt_token, project_id="", include_details=False, **kw):
        return []

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "get_model_hierarchies", fake_get_model_hierarchies)

    execute = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command><Statement>SELECT * FROM $SYSTEM.TMSCHEMA_MEASURES</Statement></Command>
      <Properties><PropertyList><Catalog>mymodel_restricted</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""

    response = await xmla_server._handle_execute(
        _execute_method(execute), tenant_slug="demo", jwt_token="token", session_id="sid-rbac",
    )
    body = response.body.decode("utf-8")
    root = ET.fromstring(body)
    rows = _tmschema_measure_rows(root)
    by_name = {r.get("Name"): r for r in rows}

    assert response.status_code == 200
    # ACCESS-restricted (non-allowed) measure: absent, no DAX leak.
    assert "SecretMargin" not in by_name
    assert "SecretMargin" not in body
    assert "secret_margin" not in body
    # Allowed visible measure: present with its expression, IsHidden=false.
    assert by_name["Sales"]["Expression"] == "SUM(amount)"
    assert by_name["Sales"]["IsHidden"] == "false"
    # CURATION-only (hidden) measure: PRESENT with expression + IsHidden=true.
    assert "HiddenCost" in by_name
    assert by_name["HiddenCost"]["Expression"] == "SUM(cost)"
    assert by_name["HiddenCost"]["IsHidden"] == "true"


@pytest.mark.asyncio
async def test_tmschema_dmv_cls_restricted_column_measure_excluded(monkeypatch):
    """Bug-5493 — a measure bound to a persona/CLS restricted source column is an
    ACCESS restriction at column granularity: it must be excluded entirely so
    neither its name nor its DAX expression reaches the client, even when it sits
    inside the persona's measure allow list."""
    persona = {
        "id": "persona-2",
        "includes_hidden_columns": False,
        # Both measures are allow-listed; column-level CLS still removes the one
        # bound to the restricted column.
        "included_measure_ids": ["m-sales", "m-salary"],
        "restricted_column_ids": ["col-salary"],
    }

    async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", persona, None

    async def fake_get_model_measures(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [
            {"id": "m-sales", "name": "Sales", "expression": "SUM(amount)",
             "source_column_id": "col-amount"},
            {"id": "m-salary", "name": "AvgSalary", "expression": "AVG(salary)",
             "source_column_id": "col-salary"},
        ]

    async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [{"id": "d-region", "name": "Region", "source_column_id": "col-region"}]

    async def fake_get_model_hierarchies(model_id, tenant_slug, jwt_token, project_id="", include_details=False, **kw):
        return []

    async def fake_get_model_snapshot(model_id, tenant_slug, jwt_token, project_id=""):
        # Real snapshot columns are serialised from ModelColumn: name key is
        # `column_name` (not `name`).
        return {"columns": [
            {"id": "col-amount", "column_name": "amount"},
            {"id": "col-salary", "column_name": "salary"},
            {"id": "col-region", "column_name": "region"},
        ]}

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "get_model_hierarchies", fake_get_model_hierarchies)
    monkeypatch.setattr(xmla_server, "get_model_snapshot", fake_get_model_snapshot)

    execute = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command><Statement>SELECT * FROM $SYSTEM.TMSCHEMA_MEASURES</Statement></Command>
      <Properties><PropertyList><Catalog>mymodel_cls</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""

    response = await xmla_server._handle_execute(
        _execute_method(execute), tenant_slug="demo", jwt_token="token", session_id="sid-cls",
    )
    body = response.body.decode("utf-8")
    root = ET.fromstring(body)
    by_name = {r.get("Name"): r for r in _tmschema_measure_rows(root)}

    assert response.status_code == 200
    # Allowed measure on a non-restricted column stays, with its DAX.
    assert by_name["Sales"]["Expression"] == "SUM(amount)"
    # CLS-restricted-column measure: gone entirely — name and DAX must not leak.
    assert "AvgSalary" not in by_name
    assert "AvgSalary" not in body
    assert "salary" not in body


@pytest.mark.asyncio
async def test_tmschema_dmv_cls_transitive_variant_and_uda_excluded(monkeypatch):
    """F-008-04 — the XMLA/Excel catalogue must hide objects that reach a
    restricted column TRANSITIVELY (a variant of a restricted base measure, a
    UDA-backed dimension whose UDA references a restricted column), matching the
    JDBC catalogue and the runtime serving gate. Before the fix these carried no
    direct source_column_id and were advertised in full over XMLA."""
    persona = {
        "id": "persona-tr",
        "includes_hidden_columns": False,
        "included_measure_ids": ["m-salary", "m-salary-ytd", "m-sales"],
        "included_dimension_ids": ["d-region", "d-band"],
        "restricted_column_ids": ["col-salary"],
    }

    async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", persona, None

    async def fake_get_model_measures(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [
            {"id": "m-sales", "name": "Sales", "expression": "SUM(amount)",
             "source_column_id": "col-amount"},
            {"id": "m-salary", "name": "Salary", "expression": "SUM(salary)",
             "source_column_id": "col-salary"},
            # Variant of the restricted base — no direct source column.
            {"id": "m-salary-ytd", "name": "SalaryYTD", "expression": "",
             "source_column_id": None, "variant_of_measure_id": "m-salary"},
        ]

    async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [
            {"id": "d-region", "name": "Region", "source_column_id": "col-region"},
            # UDA-backed dimension whose UDA references the restricted column.
            {"id": "d-band", "name": "SalaryBand", "source_column_id": None,
             "user_defined_attribute_id": "uda-band"},
        ]

    async def fake_get_model_hierarchies(model_id, tenant_slug, jwt_token, project_id="", include_details=False, **kw):
        return []

    async def fake_get_model_snapshot(model_id, tenant_slug, jwt_token, project_id=""):
        return {
            "columns": [
                {"id": "col-amount", "column_name": "amount"},
                {"id": "col-salary", "column_name": "salary"},
                {"id": "col-region", "column_name": "region"},
            ],
            "uda_column_refs": [
                {"attribute_id": "uda-band", "column_id": "col-salary"},
            ],
            "tables": [],
        }

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "get_model_hierarchies", fake_get_model_hierarchies)
    monkeypatch.setattr(xmla_server, "get_model_snapshot", fake_get_model_snapshot)

    execute = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command><Statement>SELECT * FROM $SYSTEM.TMSCHEMA_MEASURES</Statement></Command>
      <Properties><PropertyList><Catalog>mymodel_tr</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""

    response = await xmla_server._handle_execute(
        _execute_method(execute), tenant_slug="demo", jwt_token="token", session_id="sid-tr",
    )
    body = response.body.decode("utf-8")
    root = ET.fromstring(body)
    by_name = {r.get("Name"): r for r in _tmschema_measure_rows(root)}

    assert response.status_code == 200
    # Non-restricted measure stays.
    assert "Sales" in by_name
    # Direct restricted + its variant are both gone (transitive channel).
    assert "Salary" not in by_name
    assert "SalaryYTD" not in by_name
    assert "salary" not in body


@pytest.mark.asyncio
async def test_tmschema_dmv_cls_blanks_calculated_expression_referencing_restricted_column(monkeypatch):
    """Bug-5493 column-disclosure guard — an allow-listed CALCULATED measure that
    carries no source_column_id but whose free-text DAX expression references a
    persona/CLS restricted source column must keep its row + name yet have its
    Expression BLANKED, so the restricted column name cannot leak via the DAX."""
    persona = {
        "id": "persona-3",
        "includes_hidden_columns": False,
        "included_measure_ids": ["m-margin"],
        "restricted_column_ids": ["col-salary"],
    }

    async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", persona, None

    async def fake_get_model_measures(model_id, tenant_slug, jwt_token, project_id="", **kw):
        # Calculated measure: no source_column_id, expression names the restricted
        # column "salary" (and a safe column "amount").
        return [
            {"id": "m-margin", "name": "Margin", "measure_type": "calculated",
             "source_column_id": None,
             "expression": "SUM(amount) - SUM(salary)"},
        ]

    async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return []

    async def fake_get_model_hierarchies(model_id, tenant_slug, jwt_token, project_id="", include_details=False, **kw):
        return []

    async def fake_get_model_snapshot(model_id, tenant_slug, jwt_token, project_id=""):
        # Real snapshot columns serialise the name as `column_name`.
        return {"columns": [
            {"id": "col-amount", "column_name": "amount"},
            {"id": "col-salary", "column_name": "salary"},
        ]}

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "get_model_hierarchies", fake_get_model_hierarchies)
    monkeypatch.setattr(xmla_server, "get_model_snapshot", fake_get_model_snapshot)

    execute = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command><Statement>SELECT * FROM $SYSTEM.TMSCHEMA_MEASURES</Statement></Command>
      <Properties><PropertyList><Catalog>mymodel_calc</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""

    response = await xmla_server._handle_execute(
        _execute_method(execute), tenant_slug="demo", jwt_token="token", session_id="sid-calc",
    )
    body = response.body.decode("utf-8")
    root = ET.fromstring(body)
    by_name = {r.get("Name"): r for r in _tmschema_measure_rows(root)}

    assert response.status_code == 200
    # The measure row + name survive (it is allow-listed and not column-bound).
    assert "Margin" in by_name
    # Its expression is blanked — the restricted column name must not leak.
    assert by_name["Margin"].get("Expression", "") == ""
    assert "salary" not in body


@pytest.mark.asyncio
async def test_tmschema_dmv_cls_fails_closed_when_snapshot_unavailable(monkeypatch):
    """Bug-5493 fail-closed — when the persona HAS restricted columns but the
    snapshot fetch fails (so restricted column names cannot be resolved), the
    guard must blank EVERY surviving included measure's Expression rather than
    risk leaking a restricted column name through an unscanned expression. Rows +
    names remain; source_column_id exclusion still applies."""
    persona = {
        "id": "persona-4",
        "includes_hidden_columns": False,
        "included_measure_ids": ["m-margin", "m-bound"],
        "restricted_column_ids": ["col-salary"],
    }

    async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", persona, None

    async def fake_get_model_measures(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [
            # Calculated measure: expression references the restricted column.
            {"id": "m-margin", "name": "Margin", "measure_type": "calculated",
             "source_column_id": None,
             "expression": "SUM(amount) - SUM(salary)"},
            # Standard measure bound directly to the restricted column: excluded
            # by rule 1 even though names are unresolved.
            {"id": "m-bound", "name": "DirectSalary", "expression": "AVG(salary)",
             "source_column_id": "col-salary"},
        ]

    async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return []

    async def fake_get_model_hierarchies(model_id, tenant_slug, jwt_token, project_id="", include_details=False, **kw):
        return []

    async def fake_get_model_snapshot(model_id, tenant_slug, jwt_token, project_id=""):
        raise RuntimeError("snapshot service unavailable")

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "get_model_hierarchies", fake_get_model_hierarchies)
    monkeypatch.setattr(xmla_server, "get_model_snapshot", fake_get_model_snapshot)

    execute = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command><Statement>SELECT * FROM $SYSTEM.TMSCHEMA_MEASURES</Statement></Command>
      <Properties><PropertyList><Catalog>mymodel_failclosed</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""

    response = await xmla_server._handle_execute(
        _execute_method(execute), tenant_slug="demo", jwt_token="token", session_id="sid-fc",
    )
    body = response.body.decode("utf-8")
    root = ET.fromstring(body)
    by_name = {r.get("Name"): r for r in _tmschema_measure_rows(root)}

    assert response.status_code == 200
    # Calculated measure: row + name remain, expression blanked (fail closed).
    assert "Margin" in by_name
    assert by_name["Margin"].get("Expression", "") == ""
    # Column-bound measure: excluded entirely (rule 1 independent of name set).
    assert "DirectSalary" not in by_name
    # The restricted column name leaks nowhere.
    assert "salary" not in body


@pytest.mark.asyncio
async def test_tmschema_dmv_cls_fails_closed_when_restricted_id_missing_from_snapshot(monkeypatch):
    """Bug-5493 fail-closed — a snapshot fetch can SUCCEED yet not contain a
    restricted column id (or map it to a blank name). The name set is then
    incomplete, so the guard must still fail closed and blank every surviving
    expression rather than scan with a partial name set."""
    persona = {
        "id": "persona-5",
        "includes_hidden_columns": False,
        "included_measure_ids": ["m-margin"],
        # Restrict two columns; the snapshot below only carries one of them.
        "restricted_column_ids": ["col-salary", "col-bonus"],
    }

    async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", persona, None

    async def fake_get_model_measures(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [
            {"id": "m-margin", "name": "Margin", "measure_type": "calculated",
             "source_column_id": None,
             "expression": "SUM(amount) - SUM(bonus)"},
        ]

    async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return []

    async def fake_get_model_hierarchies(model_id, tenant_slug, jwt_token, project_id="", include_details=False, **kw):
        return []

    async def fake_get_model_snapshot(model_id, tenant_slug, jwt_token, project_id=""):
        # Snapshot succeeds but is missing col-bonus -> name set incomplete.
        return {"columns": [
            {"id": "col-amount", "column_name": "amount"},
            {"id": "col-salary", "column_name": "salary"},
        ]}

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "get_model_hierarchies", fake_get_model_hierarchies)
    monkeypatch.setattr(xmla_server, "get_model_snapshot", fake_get_model_snapshot)

    execute = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command><Statement>SELECT * FROM $SYSTEM.TMSCHEMA_MEASURES</Statement></Command>
      <Properties><PropertyList><Catalog>mymodel_partial</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""

    response = await xmla_server._handle_execute(
        _execute_method(execute), tenant_slug="demo", jwt_token="token", session_id="sid-partial",
    )
    body = response.body.decode("utf-8")
    root = ET.fromstring(body)
    by_name = {r.get("Name"): r for r in _tmschema_measure_rows(root)}

    assert response.status_code == 200
    # Name set is incomplete (col-bonus unresolved) -> fail closed -> blanked.
    assert "Margin" in by_name
    assert by_name["Margin"].get("Expression", "") == ""
    assert "bonus" not in body


# ---------------------------------------------------------------------------
# Bug-5434 — flat-dimension MEMBER_KEY vs MEMBER_CAPTION
# ---------------------------------------------------------------------------

def _member_rows(member_data):
    from src.dax.mdschema import _rows_members
    dims = [{"name": "Product", "levels": ["Product"]}]
    rows = _rows_members("cat", [], dims, {}, member_data)
    # Drop the synthetic (All) member (MEMBER_TYPE 2).
    return [r for r in rows if r.get("MEMBER_TYPE") != "2"]


def test_flat_dim_distinct_display_attribute_separates_key_and_caption():
    """A flat dim whose member data carries a distinct display caption emits
    MEMBER_KEY (the key) separate from MEMBER_CAPTION/MEMBER_NAME (the display)."""
    member_data = {
        "Product": {
            "levels": ["Product"],
            "members": [
                {"name": "P1", "key": "P1", "caption": "Widget",
                 "level": "Product", "ordinal": 0, "parent": ""},
            ],
        }
    }
    rows = _member_rows(member_data)
    assert len(rows) == 1
    r = rows[0]
    assert r["MEMBER_KEY"] == "P1"
    assert r["MEMBER_CAPTION"] == "Widget"
    assert r["MEMBER_NAME"] == "Widget"
    assert r["MEMBER_KEY"] != r["MEMBER_CAPTION"]
    # Member is still identified by its KEY in the unique name.
    assert r["MEMBER_UNIQUE_NAME"] == "[Product].[Product].[P1]"


def test_flat_dim_without_display_attribute_is_unchanged():
    """When no display caption is present, key == caption == name (no regression)."""
    member_data = {
        "Product": {
            "levels": ["Product"],
            "members": [
                {"name": "P1", "key": "P1",
                 "level": "Product", "ordinal": 0, "parent": ""},
            ],
        }
    }
    r = _member_rows(member_data)[0]
    assert r["MEMBER_KEY"] == "P1"
    assert r["MEMBER_CAPTION"] == "P1"
    assert r["MEMBER_NAME"] == "P1"
    assert r["MEMBER_UNIQUE_NAME"] == "[Product].[Product].[P1]"


# ---------------------------------------------------------------------------
# Bug-5436b — compression + Cancel
# ---------------------------------------------------------------------------

def _big_body() -> str:
    return (
        "<tns:DiscoverResponse>"
        + ("<row>payload</row>" * 200)
        + "</tns:DiscoverResponse>"
    )


def test_soap_response_gzip_when_client_accepts(monkeypatch):
    xmla_server._accept_encoding.set("gzip, deflate")
    resp = xmla_server._soap_response(_big_body(), session_id="sess-1")
    assert resp.headers.get("Content-Encoding") == "gzip"
    assert resp.headers.get("Vary") == "Accept-Encoding"
    decoded = gzip.decompress(resp.body).decode("utf-8")
    assert "DiscoverResponse" in decoded
    assert 'Session SessionId="sess-1"' in decoded
    # Content-Length matches the compressed body.
    assert resp.headers["Content-Length"] == str(len(resp.body))


def test_soap_response_deflate_when_only_deflate_offered():
    xmla_server._accept_encoding.set("deflate")
    resp = xmla_server._soap_response(_big_body())
    assert resp.headers.get("Content-Encoding") == "deflate"
    assert zlib.decompress(resp.body).decode("utf-8").startswith("<?xml")


def test_soap_response_identity_when_no_accept_encoding():
    xmla_server._accept_encoding.set("")
    resp = xmla_server._soap_response(_big_body())
    assert "Content-Encoding" not in resp.headers
    # Body is the plain UTF-8 envelope.
    assert resp.body.decode("utf-8").startswith("<?xml")


def test_soap_response_small_body_not_compressed():
    xmla_server._accept_encoding.set("gzip")
    resp = xmla_server._soap_response("<tns:X/>")
    assert "Content-Encoding" not in resp.headers


def test_is_cancel_command_detection():
    cancel = (
        '<Envelope xmlns="http://schemas.xmlsoap.org/soap/envelope/"><Body>'
        '<Execute xmlns="urn:schemas-microsoft-com:xml-analysis"><Command>'
        '<Cancel><ConnectionID>7</ConnectionID></Cancel>'
        '</Command></Execute></Body></Envelope>'
    )
    method = xmla_server._find_method(ET.fromstring(cancel))
    assert xmla_server._is_cancel_command(method)

    normal = (
        '<Envelope xmlns="http://schemas.xmlsoap.org/soap/envelope/"><Body>'
        '<Execute xmlns="urn:schemas-microsoft-com:xml-analysis"><Command>'
        '<Statement>SELECT 1</Statement>'
        '</Command></Execute></Body></Envelope>'
    )
    assert not xmla_server._is_cancel_command(
        xmla_server._find_method(ET.fromstring(normal))
    )


@pytest.mark.asyncio
async def test_handle_execute_cancel_returns_empty_success(monkeypatch):
    """A <Cancel> command is acknowledged with an empty-success ExecuteResponse
    and never reaches model resolution or MDX translation."""
    def _boom(*_a, **_k):
        raise AssertionError("Cancel must not resolve a model or translate MDX")

    monkeypatch.setattr(xmla_server, "_resolve_model_id", _boom)
    monkeypatch.setattr(xmla_server, "_statement_to_sql", _boom)

    cancel = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command><Cancel><ConnectionID>7</ConnectionID></Cancel></Command>
    </Execute>
  </soap:Body>
</soap:Envelope>"""

    response = await xmla_server._handle_execute(
        _execute_method(cancel), tenant_slug="demo", jwt_token="token", session_id="sid-c",
    )
    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "<soap11env:Fault>" not in body
    assert "<tns:ExecuteResponse>" in body
    assert "xml-analysis:empty" in body
    assert 'Session SessionId="sid-c"' in body
