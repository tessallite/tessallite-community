from defusedxml import DefusedXmlException, ElementTree as ET
from xml.etree.ElementTree import Element

import pytest

from src.dax import xmla_server


def _discover_method(xml_body: str) -> Element:
    root = ET.fromstring(xml_body)
    method_el = xmla_server._find_method(root)
    assert method_el is not None
    return method_el


def _execute_method(xml_body: str) -> Element:
    root = ET.fromstring(xml_body)
    method_el = xmla_server._find_method(root)
    assert method_el is not None
    return method_el


@pytest.mark.asyncio
async def test_handle_discover_members_excel_self_query_survives_member_fetch_failure(monkeypatch):
    async def fake_resolve_model_id(catalog: str, tenant_slug: str, jwt_token: str):
        return "model-1", "project-1", None

    async def fake_get_model_measures(
        model_id: str,
        tenant_slug: str,
        jwt_token: str,
        project_id: str = "",
    ):
        return [{"name": "base_amount", "default_agg": "sum"}]

    async def fake_get_model_dimensions(
        model_id: str,
        tenant_slug: str,
        jwt_token: str,
        project_id: str = "",
    ):
        return [{
            "name": "account_type",
            "source_column_name": "account_type",
            "user_defined_attribute_id": "uda-1",
            "user_defined_attribute_name": "fx_account_type",
        }]

    async def failing_get_dimension_members(
        model_id: str,
        dimension_name: str,
        tenant_slug: str,
        jwt_token: str,
        *,
        persona_id: str | None = None,
    ):
        raise RuntimeError("simulated member discovery failure")

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "get_dimension_members", failing_get_dimension_members)

    discover = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Discover xmlns="urn:schemas-microsoft-com:xml-analysis">
      <RequestType>MDSCHEMA_MEMBERS</RequestType>
      <Restrictions>
        <RestrictionList>
          <CUBE_NAME>m</CUBE_NAME>
          <MEMBER_UNIQUE_NAME>[account_type].[account_type].[All]</MEMBER_UNIQUE_NAME>
          <TREE_OP>8</TREE_OP>
        </RestrictionList>
      </Restrictions>
      <Properties>
        <PropertyList>
          <Catalog>m</Catalog>
        </PropertyList>
      </Properties>
    </Discover>
  </soap:Body>
</soap:Envelope>"""

    response = await xmla_server._handle_discover(
        _discover_method(discover),
        tenant_slug="demo",
        jwt_token="token",
        endpoint_url="http://localhost:8080/api/v1/xmla/",
        session_id="sid-1",
    )

    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "<soap11env:Fault>" not in body
    assert "<tns:DiscoverResponse>" in body
    assert "[account_type].[account_type].[All]" in body


@pytest.mark.asyncio
async def test_handle_discover_members_excel_children_query_returns_members_for_uda_enriched_dimension(monkeypatch):
    async def fake_resolve_model_id(catalog: str, tenant_slug: str, jwt_token: str):
        return "model-1", "project-1", None

    async def fake_get_model_measures(
        model_id: str,
        tenant_slug: str,
        jwt_token: str,
        project_id: str = "",
    ):
        return [{"name": "base_amount", "default_agg": "sum"}]

    async def fake_get_model_dimensions(
        model_id: str,
        tenant_slug: str,
        jwt_token: str,
        project_id: str = "",
    ):
        return [{
            "name": "account_type",
            "source_column_name": "account_type",
            "user_defined_attribute_id": "uda-1",
            "user_defined_attribute_name": "fx_account_type",
        }]

    async def fake_get_dimension_members(
        model_id: str,
        dimension_name: str,
        tenant_slug: str,
        jwt_token: str,
        *,
        persona_id: str | None = None,
    ):
        return {
            "members": [
                {"name": "CURRENT", "ordinal": 0},
                {"name": "LOAN", "ordinal": 1},
            ],
            "levels": [dimension_name],
        }

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "get_dimension_members", fake_get_dimension_members)

    discover = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Discover xmlns="urn:schemas-microsoft-com:xml-analysis">
      <RequestType>MDSCHEMA_MEMBERS</RequestType>
      <Restrictions>
        <RestrictionList>
          <CUBE_NAME>m</CUBE_NAME>
          <MEMBER_UNIQUE_NAME>[account_type].[account_type].[All]</MEMBER_UNIQUE_NAME>
          <TREE_OP>1</TREE_OP>
        </RestrictionList>
      </Restrictions>
      <Properties>
        <PropertyList>
          <Catalog>m</Catalog>
        </PropertyList>
      </Properties>
    </Discover>
  </soap:Body>
</soap:Envelope>"""

    response = await xmla_server._handle_discover(
        _discover_method(discover),
        tenant_slug="demo",
        jwt_token="token",
        endpoint_url="http://localhost:8080/api/v1/xmla/",
        session_id="sid-2",
    )

    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "<soap11env:Fault>" not in body
    # Bug-3617 (Phase 2): account_type is a FLAT dimension — keeps caption-form
    # unames (only multi-level hierarchies switch to the canonical key form).
    assert "[account_type].[account_type].[CURRENT]" in body
    assert "[account_type].[account_type].[LOAN]" in body
    assert "<MEMBER_UNIQUE_NAME>[account_type].[account_type].[All]</MEMBER_UNIQUE_NAME>" not in body


@pytest.mark.asyncio
async def test_handle_execute_excel_member_query_remains_stable_with_uda_metadata(monkeypatch):
    async def fake_resolve_model_id(catalog: str, tenant_slug: str, jwt_token: str):
        return "model-1", "project-1", None

    async def fake_get_model_measures(
        model_id: str,
        tenant_slug: str,
        jwt_token: str,
        project_id: str = "",
    ):
        return [{
            "name": "base_amount",
            "default_agg": "sum",
            "user_defined_attribute_id": "uda-meas-1",
        }]

    async def fake_get_model_dimensions(
        model_id: str,
        tenant_slug: str,
        jwt_token: str,
        project_id: str = "",
    ):
        return [{
            "name": "account_type",
            "user_defined_attribute_id": "uda-dim-1",
            "user_defined_attribute_name": "fx_account_type",
        }]

    async def fake_execute_query(
        model_id: str,
        sql: str,
        tenant_slug: str,
        jwt_token: str,
        protocol: str = "dax",
        **_kwargs,
    ):
        return {
            "columns": ["account_type"],
            "rows": [
                {"account_type": "CURRENT"},
                {"account_type": "LOAN"},
            ],
        }

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "execute_query", fake_execute_query)

    execute = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command>
        <Statement>
          SELECT {AddCalculatedMembers({[account_type].[account_type].[(All)].Members})}
          DIMENSION PROPERTIES MEMBER_TYPE
          ON COLUMNS
          FROM [m]
          CELL PROPERTIES CELL_ORDINAL
        </Statement>
      </Command>
      <Properties>
        <PropertyList>
          <Catalog>m</Catalog>
          <SspropInitAppName>Excel</SspropInitAppName>
        </PropertyList>
      </Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""

    response = await xmla_server._handle_execute(
        _execute_method(execute),
        tenant_slug="demo",
        jwt_token="token",
        session_id="sid-3",
    )

    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "<soap11env:Fault>" not in body
    assert "<tns:ExecuteResponse>" in body
    # Bug-XMLA-001: `[(All)].Members` must expand to the children, not [All]
    assert "<Caption>CURRENT</Caption>" in body
    assert "<Caption>LOAN</Caption>" in body
    assert "<Caption>All account_type</Caption>" not in body


@pytest.mark.asyncio
async def test_handle_execute_flat_last_non_empty_uses_hidden_time_grain(monkeypatch):
    captured: dict[str, str] = {}

    async def fake_resolve_model_id(catalog: str, tenant_slug: str, jwt_token: str):
        return "model-1", "project-1", None

    async def fake_get_model_measures(
        model_id: str,
        tenant_slug: str,
        jwt_token: str,
        project_id: str = "",
    ):
        return [{
            "name": "ending_balance",
            "default_agg": "last_non_empty",
            "semi_additive_behavior": "last_non_empty",
        }]

    async def fake_get_model_dimensions(
        model_id: str,
        tenant_slug: str,
        jwt_token: str,
        project_id: str = "",
    ):
        return [
            {"name": "region"},
            {
                "name": "business_month",
                "dimension_kind": "time",
                "is_time_dim": True,
                "time_grain": "month",
            },
        ]

    async def fake_execute_query(
        model_id: str,
        sql: str,
        tenant_slug: str,
        jwt_token: str,
        protocol: str = "dax",
        **_kwargs,
    ):
        captured["sql"] = sql
        return {
            "columns": ["region", "business_month", "ending_balance"],
            "rows": [
                {"region": "East", "business_month": "2024-01", "ending_balance": 100},
                {"region": "East", "business_month": "2024-02", "ending_balance": 120},
                {"region": "West", "business_month": "2024-01", "ending_balance": 70},
                {"region": "West", "business_month": "2024-02", "ending_balance": None},
            ],
        }

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "execute_query", fake_execute_query)

    execute = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command>
        <Statement>
          SELECT {[Measures].[ending_balance]} ON COLUMNS,
                 {[region].[region].Members} ON ROWS
          FROM [m]
        </Statement>
      </Command>
      <Properties><PropertyList><Catalog>m</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""

    response = await xmla_server._handle_execute(
        _execute_method(execute),
        tenant_slug="demo",
        jwt_token="token",
        session_id="sid-lne-1",
    )

    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "<soap11env:Fault>" not in body
    assert '"business_month"' in captured["sql"]
    assert 'GROUP BY "region", "business_month"' in captured["sql"]
    expected_values = [("East", 120.0), ("West", 70.0)]
    for region, value in expected_values:
        assert f"<Caption>{region}</Caption>" in body
        assert f'<Value xsi:type="xsd:double">{value}</Value>' in body
    assert '<Caption>2024-01</Caption>' not in body
    assert '<Value xsi:type="xsd:double">220.0</Value>' not in body


@pytest.mark.asyncio
async def test_handle_execute_maps_hierarchy_level_to_dimension_before_query_router(monkeypatch):
    captured: dict[str, str] = {}

    async def fake_resolve_model_id(catalog: str, tenant_slug: str, jwt_token: str):
        return "model-1", "project-1", None

    async def fake_get_model_measures(
        model_id: str,
        tenant_slug: str,
        jwt_token: str,
        project_id: str = "",
    ):
        return [{"name": "base_amount", "default_agg": "sum"}]

    async def fake_get_model_dimensions(
        model_id: str,
        tenant_slug: str,
        jwt_token: str,
        project_id: str = "",
    ):
        return [{"name": "region_dim", "source_column_id": "col-region"}]

    async def fake_get_model_hierarchies(
        model_id: str,
        tenant_slug: str,
        jwt_token: str,
        project_id: str = "",
        include_details: bool = True,
    ):
        return [{
            "id": "h-geo",
            "name": "GeoHierarchy",
            "levels": [
                {
                    "ordinal": 0,
                    "name": "Region",
                    "key_attribute": {"id": "col-region", "source": "physical_column"},
                },
                {
                    "ordinal": 1,
                    "name": "Country",
                    "key_attribute": {"id": "col-country", "source": "physical_column"},
                },
            ],
        }]

    async def fake_execute_query(
        model_id: str,
        sql: str,
        tenant_slug: str,
        jwt_token: str,
        protocol: str = "dax",
        **_kwargs,
    ):
        captured["sql"] = sql
        captured["protocol"] = protocol
        return {
            "columns": ["region_dim", "base_amount"],
            "rows": [{"region_dim": "EMEA", "base_amount": 100}],
        }

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "get_model_hierarchies", fake_get_model_hierarchies)
    monkeypatch.setattr(xmla_server, "execute_query", fake_execute_query)

    execute = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command>
        <Statement>
          SELECT {[Measures].[base_amount]} ON COLUMNS,
                 {[GeoHierarchy].[GeoHierarchy].[Region].Members} ON ROWS
          FROM [m]
        </Statement>
      </Command>
      <Properties><PropertyList><Catalog>m</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""

    response = await xmla_server._handle_execute(
        _execute_method(execute),
        tenant_slug="demo",
        jwt_token="token",
        session_id="sid-h-1",
    )

    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "<soap11env:Fault>" not in body
    assert captured["protocol"] == "jdbc"
    assert 'GROUP BY "region_dim"' in captured["sql"]
    assert "GeoHierarchy" not in captured["sql"]
    assert "<Caption>EMEA</Caption>" in body


@pytest.mark.asyncio
async def test_handle_execute_subselect_filter_reaches_subtotal_grain_queries(monkeypatch):
    """B8 round-3 regression (Bug-1050): Excel keep-only emits a subselect.

    The subtotal GRAIN queries must carry the subselect filter exactly as
    the detail query does — otherwise the response contradicts itself:
    filtered detail cells under an unfiltered grand total (live round-2
    evidence: Berlin 1,481,133.42 under an all-time 250,470,717.81).
    Every SQL sent to the query-router for this statement must be scoped
    to the subselect member.
    """
    captured_sqls: list[str] = []

    async def fake_resolve_model_id(catalog: str, tenant_slug: str, jwt_token: str):
        return "model-1", "project-1", None

    async def fake_get_model_measures(
        model_id: str,
        tenant_slug: str,
        jwt_token: str,
        project_id: str = "",
    ):
        return [{"name": "base_amount", "default_agg": "sum"}]

    async def fake_get_model_dimensions(
        model_id: str,
        tenant_slug: str,
        jwt_token: str,
        project_id: str = "",
    ):
        return [
            {"name": "region_dim", "source_column_id": "col-region"},
            {"name": "country_dim", "source_column_id": "col-country"},
            {"name": "city_dim", "source_column_id": "col-city"},
        ]

    async def fake_get_model_hierarchies(
        model_id: str,
        tenant_slug: str,
        jwt_token: str,
        project_id: str = "",
        include_details: bool = True,
    ):
        return [{
            "id": "h-geo",
            "name": "GeoHierarchy",
            "levels": [
                {
                    "ordinal": 0,
                    "name": "Region",
                    "key_attribute": {"id": "col-region", "source": "physical_column"},
                },
                {
                    "ordinal": 1,
                    "name": "Country",
                    "key_attribute": {"id": "col-country", "source": "physical_column"},
                },
            ],
        }]

    async def fake_execute_query(
        model_id: str,
        sql: str,
        tenant_slug: str,
        jwt_token: str,
        protocol: str = "dax",
        **_kwargs,
    ):
        captured_sqls.append(sql)
        return {
            "columns": ["region_dim", "country_dim", "base_amount"],
            "rows": [{"region_dim": "EMEA", "country_dim": "DE", "base_amount": 100}],
        }

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "get_model_hierarchies", fake_get_model_hierarchies)
    monkeypatch.setattr(xmla_server, "execute_query", fake_execute_query)

    execute = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command>
        <Statement>
          SELECT {[Measures].[base_amount]} ON COLUMNS,
                 NON EMPTY {[GeoHierarchy].[GeoHierarchy].Members} ON ROWS
          FROM (SELECT ({[city_dim].[city_dim].&amp;[Berlin]}) ON COLUMNS FROM [m])
        </Statement>
      </Command>
      <Properties><PropertyList><Catalog>m</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""

    response = await xmla_server._handle_execute(
        _execute_method(execute),
        tenant_slug="demo",
        jwt_token="token",
        session_id="sid-sub-1",
    )

    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "<soap11env:Fault>" not in body
    # Detail query + at least one subtotal grain query were issued.
    assert len(captured_sqls) >= 2
    # EVERY query — detail and every grain — is scoped by the subselect
    # filter, so totals and details share one filter context.
    for sql in captured_sqls:
        assert "\"city_dim\" = 'Berlin'" in sql, (
            f"subselect filter missing from query: {sql}"
        )


@pytest.mark.asyncio
async def test_handle_execute_dax_summarizecolumns_maps_hierarchy_level_before_query_router(monkeypatch):
    captured: dict[str, str] = {}

    async def fake_resolve_model_id(catalog: str, tenant_slug: str, jwt_token: str):
        return "model-1", "project-1", None

    async def fake_get_model_measures(
        model_id: str,
        tenant_slug: str,
        jwt_token: str,
        project_id: str = "",
    ):
        return [{"name": "base_amount", "default_agg": "sum"}]

    async def fake_get_model_dimensions(
        model_id: str,
        tenant_slug: str,
        jwt_token: str,
        project_id: str = "",
    ):
        return [{"name": "region_dim", "source_column_id": "col-region"}]

    async def fake_get_model_hierarchies(
        model_id: str,
        tenant_slug: str,
        jwt_token: str,
        project_id: str = "",
        include_details: bool = True,
    ):
        return [{
            "id": "h-geo",
            "name": "Geography",
            "levels": [
                {
                    "ordinal": 0,
                    "name": "Region",
                    "key_attribute": {"id": "col-region", "source": "physical_column"},
                },
            ],
        }]

    async def fake_execute_query(
        model_id: str,
        sql: str,
        tenant_slug: str,
        jwt_token: str,
        protocol: str = "dax",
        **_kwargs,
    ):
        captured["sql"] = sql
        captured["protocol"] = protocol
        return {
            "columns": ["region_dim", "base_amount"],
            "rows": [{"region_dim": "EMEA", "base_amount": 100}],
        }

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "get_model_hierarchies", fake_get_model_hierarchies)
    monkeypatch.setattr(xmla_server, "execute_query", fake_execute_query)

    execute = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command>
        <Statement>
          EVALUATE
          SUMMARIZECOLUMNS(
            Geography[Region],
            FILTER(Geography, Geography[Region] = "EMEA"),
            "base_amount", SUM(Fact[base_amount])
          )
        </Statement>
      </Command>
      <Properties><PropertyList><Catalog>m</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""

    response = await xmla_server._handle_execute(
        _execute_method(execute),
        tenant_slug="demo",
        jwt_token="token",
        session_id="sid-h-dax-1",
    )

    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "<soap11env:Fault>" not in body
    assert captured["protocol"] == "jdbc"
    assert 'SELECT "region_dim", SUM("base_amount") AS "base_amount" FROM "m"' in captured["sql"]
    assert """WHERE "region_dim" = 'EMEA'""" in captured["sql"]
    assert 'GROUP BY "region_dim"' in captured["sql"]


@pytest.mark.asyncio
async def test_handle_execute_dax_ambiguous_hierarchy_level_returns_client_fault(monkeypatch):
    async def fake_resolve_model_id(catalog: str, tenant_slug: str, jwt_token: str):
        return "model-1", "project-1", None

    async def fake_get_model_measures(
        model_id: str,
        tenant_slug: str,
        jwt_token: str,
        project_id: str = "",
    ):
        return [{"name": "base_amount", "default_agg": "sum"}]

    async def fake_get_model_dimensions(
        model_id: str,
        tenant_slug: str,
        jwt_token: str,
        project_id: str = "",
    ):
        return [
            {"name": "region_a_dim", "source_column_id": "col-region-a"},
            {"name": "region_b_dim", "source_column_id": "col-region-b"},
        ]

    async def fake_get_model_hierarchies(
        model_id: str,
        tenant_slug: str,
        jwt_token: str,
        project_id: str = "",
        include_details: bool = True,
    ):
        return [
            {
                "id": "h-geo-a",
                "name": "GeoA",
                "levels": [
                    {
                        "ordinal": 0,
                        "name": "Region",
                        "key_attribute": {"id": "col-region-a", "source": "physical_column"},
                    },
                ],
            },
            {
                "id": "h-geo-b",
                "name": "GeoB",
                "levels": [
                    {
                        "ordinal": 0,
                        "name": "Region",
                        "key_attribute": {"id": "col-region-b", "source": "physical_column"},
                    },
                ],
            },
        ]

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "get_model_hierarchies", fake_get_model_hierarchies)

    execute = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command>
        <Statement>
          EVALUATE
          SUMMARIZECOLUMNS(
            [Region],
            "base_amount", SUM(Fact[base_amount])
          )
        </Statement>
      </Command>
      <Properties><PropertyList><Catalog>m</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""

    response = await xmla_server._handle_execute(
        _execute_method(execute),
        tenant_slug="demo",
        jwt_token="token",
        session_id="sid-h-dax-amb-1",
    )

    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "<soap11env:Fault>" in body
    assert "Ambiguous DAX dimension reference 'Region'" in body


@pytest.mark.asyncio
async def test_handle_discover_levels_includes_hierarchy_levels_from_model_service(monkeypatch):
    async def fake_resolve_model_id(catalog: str, tenant_slug: str, jwt_token: str):
        return "model-1", "project-1", None

    async def fake_get_model_measures(
        model_id: str,
        tenant_slug: str,
        jwt_token: str,
        project_id: str = "",
    ):
        return [{"name": "base_amount", "default_agg": "sum"}]

    async def fake_get_model_dimensions(
        model_id: str,
        tenant_slug: str,
        jwt_token: str,
        project_id: str = "",
    ):
        return [{"name": "account_type"}]

    async def fake_get_model_hierarchies(
        model_id: str,
        tenant_slug: str,
        jwt_token: str,
        project_id: str = "",
        include_details: bool = True,
    ):
        return [{
            "id": "h-1",
            "name": "GeoHierarchy",
            "levels": [
                {"ordinal": 0, "name": "Region"},
                {"ordinal": 1, "name": "Country"},
            ],
        }]

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "get_model_hierarchies", fake_get_model_hierarchies)

    discover = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Discover xmlns="urn:schemas-microsoft-com:xml-analysis">
      <RequestType>MDSCHEMA_LEVELS</RequestType>
      <Restrictions><RestrictionList><CUBE_NAME>m</CUBE_NAME></RestrictionList></Restrictions>
      <Properties><PropertyList><Catalog>m</Catalog></PropertyList></Properties>
    </Discover>
  </soap:Body>
</soap:Envelope>"""

    response = await xmla_server._handle_discover(
        _discover_method(discover),
        tenant_slug="demo",
        jwt_token="token",
        endpoint_url="http://localhost:8080/api/v1/xmla/",
        session_id="sid-levels-1",
    )

    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "[GeoHierarchy].[GeoHierarchy].[Region]" in body
    assert "[GeoHierarchy].[GeoHierarchy].[Country]" in body


@pytest.mark.asyncio
async def test_handle_discover_members_for_hierarchy_uses_preview_expansion(monkeypatch):
    async def fake_resolve_model_id(catalog: str, tenant_slug: str, jwt_token: str):
        return "model-1", "project-1", None

    async def fake_get_model_measures(
        model_id: str,
        tenant_slug: str,
        jwt_token: str,
        project_id: str = "",
    ):
        return [{"name": "base_amount", "default_agg": "sum"}]

    async def fake_get_model_dimensions(
        model_id: str,
        tenant_slug: str,
        jwt_token: str,
        project_id: str = "",
    ):
        return []

    async def fake_get_model_hierarchies(
        model_id: str,
        tenant_slug: str,
        jwt_token: str,
        project_id: str = "",
        include_details: bool = True,
    ):
        return [{
            "id": "h-geo",
            "name": "GeoHierarchy",
            "levels": [
                {"ordinal": 0, "name": "Region"},
                {"ordinal": 1, "name": "Country"},
            ],
        }]

    preview_calls: list[dict] = []

    async def fake_get_hierarchy_preview(
        model_id: str,
        hierarchy_id: str,
        tenant_slug: str,
        jwt_token: str,
        project_id: str = "",
        *,
        sample_size: int = 1000,
        expand_level: int | None = None,
        parent_key: str | None = None,
        persona_id: str | None = None,
        include_key_path: bool = False,
    ):
        preview_calls.append(
            {
                "expand_level": expand_level,
                "parent_key": parent_key,
                "hierarchy_id": hierarchy_id,
            }
        )
        if expand_level == 1 and parent_key == "EMEA":
            return {
                "members": [
                    {
                        "level_ordinal": 1,
                        "level_name": "Country",
                        "key_value": "United Kingdom",
                        "parent_key": "EMEA",
                    }
                ]
            }
        return {
            "members": [
                {
                    "level_ordinal": 0,
                    "level_name": "Region",
                    "key_value": "EMEA",
                    "parent_key": None,
                }
            ]
        }

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "get_model_hierarchies", fake_get_model_hierarchies)
    monkeypatch.setattr(xmla_server, "get_hierarchy_preview", fake_get_hierarchy_preview)

    discover = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Discover xmlns="urn:schemas-microsoft-com:xml-analysis">
      <RequestType>MDSCHEMA_MEMBERS</RequestType>
      <Restrictions>
        <RestrictionList>
          <HIERARCHY_UNIQUE_NAME>[GeoHierarchy].[GeoHierarchy]</HIERARCHY_UNIQUE_NAME>
          <MEMBER_UNIQUE_NAME>[GeoHierarchy].[GeoHierarchy].[EMEA]</MEMBER_UNIQUE_NAME>
          <LEVEL_UNIQUE_NAME>[GeoHierarchy].[GeoHierarchy].[Region]</LEVEL_UNIQUE_NAME>
          <TREE_OP>1</TREE_OP>
        </RestrictionList>
      </Restrictions>
      <Properties><PropertyList><Catalog>m</Catalog></PropertyList></Properties>
    </Discover>
  </soap:Body>
</soap:Envelope>"""

    response = await xmla_server._handle_discover(
        _discover_method(discover),
        tenant_slug="demo",
        jwt_token="token",
        endpoint_url="http://localhost:8080/api/v1/xmla/",
        session_id="sid-members-1",
    )

    body = response.body.decode("utf-8")
    assert response.status_code == 200
    # Bug-3617 (Phase 2): canonical path-qualified uname (Region EMEA > Country UK),
    # & XML-escaped in the response body.
    assert (
        "[GeoHierarchy].[GeoHierarchy].[Country].&amp;[EMEA]&amp;[United Kingdom]"
        in body
    )
    assert preview_calls
    assert preview_calls[-1]["expand_level"] == 1
    assert preview_calls[-1]["parent_key"] == "EMEA"


# ---------------------------------------------------------------------------
# Bug-XMLA-002 — unknown catalog must fail fast at Discover time
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_discover_unknown_catalog_returns_xmla_fault(monkeypatch):
    """Excel's connect handshake carries the `Catalog` property. When
    the catalog does not resolve to a real model, the gateway must
    respond with an XMLA Fault so Excel surfaces ``catalog not
    found`` during connect rather than deferring the failure until
    the first MDX query (Bug-XMLA-002).
    """

    async def fake_resolve_model_id(catalog: str, tenant_slug: str, jwt_token: str):
        return None, "", None

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)

    discover = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Discover xmlns="urn:schemas-microsoft-com:xml-analysis">
      <RequestType>MDSCHEMA_DIMENSIONS</RequestType>
      <Restrictions><RestrictionList/></Restrictions>
      <Properties><PropertyList><Catalog>bogus_catalog</Catalog></PropertyList></Properties>
    </Discover>
  </soap:Body>
</soap:Envelope>"""

    response = await xmla_server._handle_discover(
        _discover_method(discover),
        tenant_slug="demo",
        jwt_token="token",
    )

    body = response.body.decode("utf-8")
    assert response.status_code == 404
    assert "Fault" in body
    assert "bogus_catalog" in body
    assert "not found" in body.lower()


@pytest.mark.asyncio
async def test_handle_discover_server_level_requests_without_catalog(monkeypatch):
    """DISCOVER_PROPERTIES describes server capabilities. When the
    client sends the request with NO catalog in the PropertyList it
    must succeed regardless of tenant state — Excel issues this as
    part of its server-level handshake before it has committed to a
    specific catalog.

    Post-Bug-XMLA-005: this test only covers the no-catalog case. The
    companion ``test_handle_discover_properties_with_unknown_catalog_faults``
    covers the "Excel passes Catalog=bogus on DISCOVER_PROPERTIES
    during Connection.Open" case which must now fault.
    """

    async def fake_resolve_model_id(catalog: str, tenant_slug: str, jwt_token: str):
        return None, "", None

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)

    discover = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Discover xmlns="urn:schemas-microsoft-com:xml-analysis">
      <RequestType>DISCOVER_PROPERTIES</RequestType>
      <Restrictions><RestrictionList/></Restrictions>
      <Properties><PropertyList/></Properties>
    </Discover>
  </soap:Body>
</soap:Envelope>"""

    response = await xmla_server._handle_discover(
        _discover_method(discover),
        tenant_slug="demo",
        jwt_token="token",
    )

    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "Fault" not in body


async def test_handle_discover_properties_with_unknown_catalog_faults(monkeypatch):
    """Bug-XMLA-005 fix: Excel's Connection.Open probe issues
    DISCOVER_PROPERTIES with the target catalog in PropertyList. If
    the catalog is unknown, the gateway must fault at handshake time
    so the connection fails fast instead of deferring the error to
    the first real MDX query.
    """

    async def fake_resolve_model_id(catalog: str, tenant_slug: str, jwt_token: str):
        return None, "", None  # unknown catalog

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)

    discover = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Discover xmlns="urn:schemas-microsoft-com:xml-analysis">
      <RequestType>DISCOVER_PROPERTIES</RequestType>
      <Restrictions><RestrictionList/></Restrictions>
      <Properties><PropertyList><Catalog>does_not_exist_zzz</Catalog></PropertyList></Properties>
    </Discover>
  </soap:Body>
</soap:Envelope>"""

    response = await xmla_server._handle_discover(
        _discover_method(discover),
        tenant_slug="demo",
        jwt_token="token",
    )

    body = response.body.decode("utf-8")
    # _soap_fault returns status 404 with a SOAP Fault body
    assert response.status_code == 404
    assert "Fault" in body
    assert "does_not_exist_zzz" in body


def test_defusedxml_rejects_xxe_payload():
    """defusedxml must reject external entity payloads with DefusedXmlException,
    not ET.ParseError. This confirms the gateway's exception handling is correct."""
    xxe = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE foo [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        '<soap:Body>&xxe;</soap:Body></soap:Envelope>'
    )
    with pytest.raises(DefusedXmlException):
        ET.fromstring(xxe)
