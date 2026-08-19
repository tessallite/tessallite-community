"""Tests for Bug-5189, Bug-5191, and Bug-5254 fixes.

Bug-5189 (HIGH, security): XMLA persona-catalog MEMBER discovery must be
    scoped to the resolved persona. get_dimension_members must forward
    persona_id to the query-router so restricted personas cannot enumerate
    dimension members they should not see.

Bug-5191: custom-group AVG/COUNT_DISTINCT re-queries must honour label
    filters (Begins/Ends-With, Contains) extracted from the MDX axis.
    Previously label_filter_specs were extracted but never propagated into
    the re-query spec's extra_where.

Bug-5254: MDSCHEMA_KPIS status and goal expressions must derive from the
    KPI's presentation_meta.bands (v2 model), not fixed 90%/110% thresholds.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.dax import mdschema
from src.dax.xmla_server import (
    _label_filter_to_sql,
    _translate_label_filter_calls,
)


# ============================================================================
# Bug-5189: persona_id threaded to get_dimension_members
# ============================================================================


class TestBug5189PersonaMemberDiscovery:
    """Verify that _load_discover_member_data threads persona_id through to
    get_dimension_members so the query-router enforces persona-level security.
    """

    @pytest.mark.asyncio
    async def test_persona_id_forwarded_to_get_dimension_members(self, monkeypatch):
        """When a persona is resolved from the catalog, persona_id must be
        passed to get_dimension_members during MDSCHEMA_MEMBERS discovery."""
        from src.dax import xmla_server
        from defusedxml import ElementTree as ET

        captured_persona_ids: list[str | None] = []

        async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
            return "model-1", "project-1", {
                "id": "persona-restricted-1",
                "slug": "restricted",
                "includes_hidden_columns": False,
                "included_dimension_ids": [],
                "included_measure_ids": [],
            }, None

        async def fake_get_model_measures(model_id, tenant_slug, jwt_token, **kw):
            return [{"id": "m1", "name": "Revenue", "default_agg": "sum"}]

        async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, **kw):
            return [{"id": "d1", "name": "Region", "source": "column"}]

        async def fake_get_model_hierarchies(model_id, tenant_slug, jwt_token, **kw):
            return []

        async def fake_list_all_models(tenant_slug, jwt_token):
            return [{"id": "model-1", "project_id": "project-1", "trust_meta": {}}]

        async def capturing_get_dimension_members(
            model_id, dimension_name, tenant_slug, jwt_token,
            *, persona_id=None,
        ):
            captured_persona_ids.append(persona_id)
            return {"members": [], "levels": []}

        monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
        monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
        monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
        monkeypatch.setattr(xmla_server, "get_model_hierarchies", fake_get_model_hierarchies)
        monkeypatch.setattr(xmla_server, "list_all_models_for_tenant", fake_list_all_models)
        monkeypatch.setattr(xmla_server, "get_dimension_members", capturing_get_dimension_members)

        discover_xml = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Discover xmlns="urn:schemas-microsoft-com:xml-analysis">
      <RequestType>MDSCHEMA_MEMBERS</RequestType>
      <Restrictions>
        <RestrictionList>
          <CUBE_NAME>m</CUBE_NAME>
          <DIMENSION_UNIQUE_NAME>[Region]</DIMENSION_UNIQUE_NAME>
        </RestrictionList>
      </Restrictions>
      <Properties>
        <PropertyList>
          <Catalog>m_restricted</Catalog>
        </PropertyList>
      </Properties>
    </Discover>
  </soap:Body>
</soap:Envelope>"""

        root = ET.fromstring(discover_xml)
        method_el = xmla_server._find_method(root)
        assert method_el is not None

        await xmla_server._handle_discover(
            method_el, tenant_slug="acme", jwt_token="tok",
        )

        assert len(captured_persona_ids) == 1, (
            "get_dimension_members should have been called once"
        )
        assert captured_persona_ids[0] == "persona-restricted-1", (
            "persona_id must be forwarded from the resolved persona"
        )

    @pytest.mark.asyncio
    async def test_business_base_persona_sends_none(self, monkeypatch):
        """When no persona is resolved (business base catalog), persona_id
        should be None so the query runs unscoped."""
        from src.dax import xmla_server
        from defusedxml import ElementTree as ET

        captured_persona_ids: list[str | None] = []

        async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
            return "model-1", "project-1", None, None  # business base

        async def fake_get_model_measures(model_id, tenant_slug, jwt_token, **kw):
            return [{"id": "m1", "name": "Revenue", "default_agg": "sum"}]

        async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, **kw):
            return [{"id": "d1", "name": "Region", "source": "column"}]

        async def fake_get_model_hierarchies(model_id, tenant_slug, jwt_token, **kw):
            return []

        async def fake_list_all_models(tenant_slug, jwt_token):
            return [{"id": "model-1", "project_id": "project-1", "trust_meta": {}}]

        async def capturing_get_dimension_members(
            model_id, dimension_name, tenant_slug, jwt_token,
            *, persona_id=None,
        ):
            captured_persona_ids.append(persona_id)
            return {"members": [], "levels": []}

        monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
        monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
        monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
        monkeypatch.setattr(xmla_server, "get_model_hierarchies", fake_get_model_hierarchies)
        monkeypatch.setattr(xmla_server, "list_all_models_for_tenant", fake_list_all_models)
        monkeypatch.setattr(xmla_server, "get_dimension_members", capturing_get_dimension_members)

        discover_xml = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Discover xmlns="urn:schemas-microsoft-com:xml-analysis">
      <RequestType>MDSCHEMA_MEMBERS</RequestType>
      <Restrictions>
        <RestrictionList>
          <CUBE_NAME>m</CUBE_NAME>
          <DIMENSION_UNIQUE_NAME>[Region]</DIMENSION_UNIQUE_NAME>
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

        root = ET.fromstring(discover_xml)
        method_el = xmla_server._find_method(root)
        assert method_el is not None

        await xmla_server._handle_discover(
            method_el, tenant_slug="acme", jwt_token="tok",
        )

        assert len(captured_persona_ids) == 1
        assert captured_persona_ids[0] is None


# ============================================================================
# Bug-5191: label_filter_specs propagated to re-query extra_where
# ============================================================================


class TestBug5191LabelFilterRequery:
    """Verify that label filter specs (Begins/Ends-With, Contains) are
    propagated into the AVG/COUNT_DISTINCT re-query extra_where clauses."""

    def test_label_filter_to_sql_begins_with(self):
        """A begins_with label filter produces the correct LIKE clause."""
        from src.dax.xmla_server import _LabelFilterSpec

        spec = _LabelFilterSpec(
            dim_ref="Product", operation="begins_with",
            value="Widget", negated=False,
        )
        sql = _label_filter_to_sql(spec, lambda n: f'"{n}"')
        assert 'LIKE' in sql
        assert '"Product"' in sql
        assert "'widget%'" in sql.lower()

    def test_label_filter_to_sql_contains_negated(self):
        """A negated contains label filter produces NOT LIKE clause."""
        from src.dax.xmla_server import _LabelFilterSpec

        spec = _LabelFilterSpec(
            dim_ref="Region", operation="contains",
            value="West", negated=True,
        )
        sql = _label_filter_to_sql(spec, lambda n: f'"{n}"')
        assert "NOT LIKE" in sql
        assert "'%west%'" in sql.lower()

    def test_label_filter_specs_extracted_from_axis_text(self):
        """Label filter specs are correctly extracted from MDX axis text."""
        axis = (
            'Filter([Product].[Product].Members, '
            'Left([Product].[Product].CurrentMember.Name, 3) = "Wid")'
        )
        specs = _translate_label_filter_calls(
            axis, {"Product"},
            hierarchy_level_dim_map={},
            hierarchy_default_dim_map={},
            quote_fn=lambda n: f'"{n}"',
        )
        assert len(specs) == 1
        assert specs[0].dim_ref == "Product"
        assert specs[0].operation == "begins_with"
        assert specs[0].value == "Wid"

    def test_requery_would_include_label_filter_clauses(self):
        """Regression test: label filter specs must be convertible to SQL
        clauses that can be appended to extra_where for re-queries."""
        from src.dax.xmla_server import _LabelFilterSpec

        specs = [
            _LabelFilterSpec("Region", "begins_with", "US", False),
            _LabelFilterSpec("Product", "contains", "Laptop", False),
        ]
        where_sql: list[str] = []
        for lf in specs:
            where_sql.append(_label_filter_to_sql(lf, lambda n: f'"{n}"'))
        assert len(where_sql) == 2
        # Each clause is a valid SQL fragment
        assert all("LIKE" in c for c in where_sql)

    @pytest.mark.asyncio
    async def test_requery_sql_includes_label_filter_integration(self, monkeypatch):
        """Integration test: an EXECUTE with a label filter AND a WITH MEMBER
        using AVG must produce re-query SQL that contains the label filter's
        LIKE clause. This verifies the wiring at xmla_server.py where
        _rq_label_specs are extracted and appended to sp.extra_where."""
        from src.dax import xmla_server
        from defusedxml import ElementTree as ET

        captured_sqls: list[str] = []

        async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
            return "model-1", "project-1", None, None

        async def fake_get_model_measures(model_id, tenant_slug, jwt_token, **kw):
            return [
                {"id": "m1", "name": "Sales", "default_agg": "avg"},
            ]

        async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, **kw):
            return [
                {"id": "d1", "name": "Region"},
            ]

        async def fake_get_model_hierarchies(model_id, tenant_slug, jwt_token, **kw):
            return []

        async def fake_execute_query(
            sql, model_id, tenant_slug, jwt_token,
            protocol="dax", **_kwargs,
        ):
            captured_sqls.append(sql)
            return {
                "columns": ["Region", "Sales"],
                "rows": [
                    {"Region": "US-East", "Sales": 100},
                    {"Region": "US-West", "Sales": 200},
                ],
            }

        monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
        monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
        monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
        monkeypatch.setattr(xmla_server, "get_model_hierarchies", fake_get_model_hierarchies)
        monkeypatch.setattr(xmla_server, "execute_query", fake_execute_query)

        # MDX with a label filter (Left(...) = "US") AND a WITH MEMBER
        # using an aggregate_set (custom group) that would trigger AVG
        # re-query. The re-query SQL must contain a LIKE clause for "US".
        execute_xml = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command>
        <Statement>
WITH MEMBER [Region].[Region].[Custom Group] AS
  AGGREGATE({[Region].[Region].[US-East], [Region].[Region].[US-West]})
SELECT
  Filter([Region].[Region].Members,
    Left([Region].[Region].CurrentMember.Name, 2) = "US")
  DIMENSION PROPERTIES MEMBER_TYPE ON ROWS,
  {[Measures].[Sales]} ON COLUMNS
FROM [m]
        </Statement>
      </Command>
      <Properties>
        <PropertyList>
          <Catalog>m</Catalog>
        </PropertyList>
      </Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""

        root = ET.fromstring(execute_xml)
        method_el = xmla_server._find_method(root)
        assert method_el is not None

        response = await xmla_server._handle_execute(
            method_el, tenant_slug="demo", jwt_token="tok",
            session_id="sid-test",
        )

        # Bug-8925 re-arm. This test used to guard its only real assertion
        # behind `if requery_sqls:`, and on the pre-fix tip the Execute never
        # reached execute_query at all — the axis audit rejected
        # `[Region].[Region].CurrentMember` inside the label filter — so the
        # body never ran and the fault went unnoticed. Every step is now a hard
        # assertion.
        body = response.body.decode("utf-8", "replace")
        assert "Fault" not in body, (
            f"_handle_execute returned a SOAP Fault instead of a result: {body}"
        )
        assert captured_sqls, "Execute never reached execute_query"
        # The MAIN detail query is the first call and must carry the filter.
        assert "LIKE" in captured_sqls[0] and "us" in captured_sqls[0].lower(), (
            f"Main SQL must carry the label filter LIKE clause. "
            f"Got: {captured_sqls[0]}"
        )
        # Every SUBSEQUENT call is a re-query. They are checked separately from
        # the main query on purpose: the main query is itself an AVG query here,
        # so an `"AVG" in s`-style filter would let the main query alone satisfy
        # the assertion and mask a re-query that dropped the label filter.
        requery_sqls = captured_sqls[1:]
        assert requery_sqls, (
            f"expected AVG/COUNT_DISTINCT re-query was not issued; "
            f"captured={captured_sqls}"
        )
        assert all("AVG" in s.upper() for s in requery_sqls), (
            f"expected the re-queries to be AVG aggregates; got {requery_sqls}"
        )
        assert all("LIKE" in s and "us" in s.lower() for s in requery_sqls), (
            f"Every re-query SQL must contain the label filter LIKE clause. "
            f"Got: {requery_sqls}"
        )

    @pytest.mark.asyncio
    async def test_requery_failure_faults_not_blank(self, monkeypatch):
        """F-002-03: when a REQUIRED custom-group AVG/COUNT_DISTINCT re-query
        fails for a non-resource reason, the Execute must fail CLOSED (SOAP
        Fault), never render the swallowed None as a legitimately-blank cell
        (which looks like real no-data while the leaf rows look fine)."""
        from src.dax import xmla_server
        from defusedxml import ElementTree as ET

        async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
            return "model-1", "project-1", None, None

        async def fake_get_model_measures(model_id, tenant_slug, jwt_token, **kw):
            return [{"id": "m1", "name": "Sales", "default_agg": "avg"}]

        async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, **kw):
            return [{"id": "d1", "name": "Region"}]

        async def fake_get_model_hierarchies(model_id, tenant_slug, jwt_token, **kw):
            return []

        calls = {"n": 0}

        async def fake_execute_query(sql, model_id, tenant_slug, jwt_token,
                                     protocol="dax", **_kwargs):
            calls["n"] += 1
            # First call = main query (leaf rows look fine). Any subsequent
            # call is the required AVG re-query -> raise a transient failure.
            if "AVG" in sql.upper():
                raise RuntimeError("transient source failure")
            return {
                "columns": ["Region", "Sales"],
                "rows": [
                    {"Region": "US-East", "Sales": 100},
                    {"Region": "US-West", "Sales": 200},
                ],
            }

        monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
        monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
        monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
        monkeypatch.setattr(xmla_server, "get_model_hierarchies", fake_get_model_hierarchies)
        monkeypatch.setattr(xmla_server, "execute_query", fake_execute_query)

        execute_xml = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command>
        <Statement>
WITH MEMBER [Region].[Region].[Custom Group] AS
  AGGREGATE({[Region].[Region].[US-East], [Region].[Region].[US-West]})
SELECT
  {[Region].[Region].Members} ON ROWS,
  {[Measures].[Sales]} ON COLUMNS
FROM [m]
        </Statement>
      </Command>
      <Properties>
        <PropertyList><Catalog>m</Catalog></PropertyList>
      </Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""

        root = ET.fromstring(execute_xml)
        method_el = xmla_server._find_method(root)
        resp = await xmla_server._handle_execute(
            method_el, tenant_slug="demo", jwt_token="tok", session_id="sid-fail",
        )
        body = resp.body.decode() if isinstance(resp.body, bytes) else str(resp.body)
        # A re-query WAS attempted and failed -> the response must be a fault,
        # not a normal result carrying a blank custom-group cell.
        assert "Fault" in body or "faultstring" in body, (
            f"Re-query failure must fail closed with a SOAP Fault. Got: {body[:500]}"
        )

    @pytest.mark.asyncio
    async def test_requery_resource_limit_faults_not_blank(self, monkeypatch):
        """R1 finding 1: a resource-limit exception on a required re-query must
        also fail CLOSED. Previously it re-raised out of _gather_bounded into the
        block-level swallow, leaving the fault list empty and rendering anyway."""
        from src.dax import xmla_server
        from src.router_client import QueryByteCeilingExceeded
        from defusedxml import ElementTree as ET

        async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
            return "model-1", "project-1", None, None

        async def fake_get_model_measures(model_id, tenant_slug, jwt_token, **kw):
            return [{"id": "m1", "name": "Sales", "default_agg": "avg"}]

        async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, **kw):
            return [{"id": "d1", "name": "Region"}]

        async def fake_get_model_hierarchies(model_id, tenant_slug, jwt_token, **kw):
            return []

        async def fake_execute_query(sql, model_id, tenant_slug, jwt_token,
                                     protocol="dax", **_kwargs):
            if "AVG" in sql.upper():
                raise QueryByteCeilingExceeded(response_bytes=10 ** 9, ceiling=1)
            return {
                "columns": ["Region", "Sales"],
                "rows": [
                    {"Region": "US-East", "Sales": 100},
                    {"Region": "US-West", "Sales": 200},
                ],
            }

        monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
        monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
        monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
        monkeypatch.setattr(xmla_server, "get_model_hierarchies", fake_get_model_hierarchies)
        monkeypatch.setattr(xmla_server, "execute_query", fake_execute_query)

        execute_xml = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command>
        <Statement>
WITH MEMBER [Region].[Region].[Custom Group] AS
  AGGREGATE({[Region].[Region].[US-East], [Region].[Region].[US-West]})
SELECT
  {[Region].[Region].Members} ON ROWS,
  {[Measures].[Sales]} ON COLUMNS
FROM [m]
        </Statement>
      </Command>
      <Properties>
        <PropertyList><Catalog>m</Catalog></PropertyList>
      </Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""

        root = ET.fromstring(execute_xml)
        method_el = xmla_server._find_method(root)
        resp = await xmla_server._handle_execute(
            method_el, tenant_slug="demo", jwt_token="tok", session_id="sid-rl",
        )
        body = resp.body.decode() if isinstance(resp.body, bytes) else str(resp.body)
        assert "Fault" in body or "faultstring" in body, (
            f"Resource-limit re-query failure must fail closed. Got: {body[:500]}"
        )

    @pytest.mark.asyncio
    async def test_requery_sql_builder_exception_faults_not_blank(self, monkeypatch):
        """R2 observation: a re-query SQL-BUILDER exception (raised inside
        _exec_one's try, not from execute_query) must also fail closed via the
        _FAIL sentinel, never render a blank cell."""
        from src.dax import xmla_server
        from src.dax import mdx_calc_members
        from defusedxml import ElementTree as ET

        async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
            return "model-1", "project-1", None, None

        async def fake_get_model_measures(model_id, tenant_slug, jwt_token, **kw):
            return [{"id": "m1", "name": "Sales", "default_agg": "avg"}]

        async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, **kw):
            return [{"id": "d1", "name": "Region"}]

        async def fake_get_model_hierarchies(model_id, tenant_slug, jwt_token, **kw):
            return []

        async def fake_execute_query(sql, model_id, tenant_slug, jwt_token,
                                     protocol="dax", **_kwargs):
            return {
                "columns": ["Region", "Sales"],
                "rows": [
                    {"Region": "US-East", "Sales": 100},
                    {"Region": "US-West", "Sales": 200},
                ],
            }

        def boom(_spec):
            raise RuntimeError("SQL builder blew up")

        monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
        monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
        monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
        monkeypatch.setattr(xmla_server, "get_model_hierarchies", fake_get_model_hierarchies)
        monkeypatch.setattr(xmla_server, "execute_query", fake_execute_query)
        # Force the aggregate-set re-query SQL builder to raise.
        monkeypatch.setattr(mdx_calc_members, "build_requery_sql", boom)

        execute_xml = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command>
        <Statement>
WITH MEMBER [Region].[Region].[Custom Group] AS
  AGGREGATE({[Region].[Region].[US-East], [Region].[Region].[US-West]})
SELECT
  {[Region].[Region].Members} ON ROWS,
  {[Measures].[Sales]} ON COLUMNS
FROM [m]
        </Statement>
      </Command>
      <Properties>
        <PropertyList><Catalog>m</Catalog></PropertyList>
      </Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""

        root = ET.fromstring(execute_xml)
        method_el = xmla_server._find_method(root)
        resp = await xmla_server._handle_execute(
            method_el, tenant_slug="demo", jwt_token="tok", session_id="sid-build",
        )
        body = resp.body.decode() if isinstance(resp.body, bytes) else str(resp.body)
        assert "Fault" in body or "faultstring" in body, (
            f"Builder exception must fail closed with a SOAP Fault. Got: {body[:500]}"
        )




# ============================================================================
# Bug-6608: KPI status serves the RAW value + annotation (no gateway band verdict)
# ============================================================================


class TestBug6608KpiRawStatus:
    """The gateway must NOT replicate the KPI band/direction matrix. KPI_STATUS
    serves the modeller-authored status expression, else (Bug-8288) the synthetic
    GOVERNED status member the Execute path resolves to the -1/0/1 verdict; never a
    gateway CASE verdict. The authored band context is still published as an
    annotation."""

    CATALOG = "sales_model"
    MEASURES = [{"id": "m1", "name": "Revenue", "default_agg": "sum"}]

    def _v2_kpi(self, *, direction="higher_is_better", presentation_meta=None):
        return {
            "id": "k1", "name": "test_kpi", "display_name": "Test KPI",
            "description": "", "display_folder": "",
            "expression": 'measure("Revenue")',
            "target_type": "static", "target_value": 100,
            "target_expression": None, "status_expression": "",
            "trend_expression": "", "weight": None, "parent_kpi_id": None,
            "presentation_type": "gauge", "value_measure_id": None,
            "goal_measure_id": None, "direction": direction,
            "presentation_meta": presentation_meta,
        }

    _BANDS_META = {
        "evaluation_type": "percentage_of_target",
        "bands": [
            {"label": "Off Target", "color": "#D32F2F", "min": None, "max": 0.80},
            {"label": "Near Target", "color": "#F57C00", "min": 0.80, "max": 1.00},
            {"label": "On Track", "color": "#388E3C", "min": 1.00, "max": None},
        ],
    }

    def test_expression_kpi_status_is_governed_member_not_case(self):
        kpi = self._v2_kpi(presentation_meta=self._BANDS_META)
        row = mdschema._rows_kpis(self.CATALOG, [kpi], self.MEASURES)[0]
        # Bug-8288: status is the synthetic GOVERNED status member (verdict basis
        # via bands/target), NEVER a gateway band CASE verdict and NEVER the raw
        # value member (which would show a business number under a -1/0/1 icon).
        assert row["KPI_STATUS"] == "[Measures].[Test KPI Status]"
        assert row["KPI_STATUS"] != row["KPI_VALUE"]
        assert not row["KPI_STATUS"].upper().startswith("CASE")

    def test_legacy_kpi_status_is_governed_member(self):
        kpi = {
            "id": "k1", "name": "legacy", "display_name": "Legacy",
            "description": "", "display_folder": "", "expression": "",
            "value_measure_id": "m1", "goal_measure_id": "m1",
            "status_expression": "", "trend_expression": "", "weight": None,
            "parent_kpi_id": None, "presentation_type": None,
            "direction": "higher_is_better", "presentation_meta": self._BANDS_META,
        }
        row = mdschema._rows_kpis(self.CATALOG, [kpi], self.MEASURES)[0]
        # Bug-8288: legacy KPI with a goal basis advertises the synthetic governed
        # status member, not the raw value member or a CASE verdict.
        assert row["KPI_STATUS"] == "[Measures].[Legacy Status]"
        assert not row["KPI_STATUS"].upper().startswith("CASE")

    def test_authored_status_expression_preserved_verbatim(self):
        kpi = self._v2_kpi(presentation_meta=self._BANDS_META)
        kpi["status_expression"] = "IIF(KpiValue > KpiGoal, 1, -1)"
        row = mdschema._rows_kpis(self.CATALOG, [kpi], self.MEASURES)[0]
        assert row["KPI_STATUS"] == "IIF(KpiValue > KpiGoal, 1, -1)"

    def test_band_context_published_as_annotation(self):
        kpi = self._v2_kpi(presentation_meta=self._BANDS_META)
        row = mdschema._rows_kpis(self.CATALOG, [kpi], self.MEASURES)[0]
        annotation = row["ANNOTATIONS"]
        assert annotation, "band context must be published for banded KPIs"
        # Bug-6608 un-gated: the status is now the governed traffic-light verdict;
        # the annotation is informational band context (labels + direction).
        assert "governed traffic-light" in annotation.lower()
        assert "On Track" in annotation
        assert "higher is better" in annotation
        # And it is also folded into the KPI description tooltip.
        assert annotation in row["KPI_DESCRIPTION"]

    def test_band_context_kpi_graphic_restored_with_governed_member(self):
        # Bug-8288: KPI_STATUS is now the synthetic GOVERNED status member (which
        # the Execute path resolves to the -1/0/1 verdict), so the status graphic is
        # advertised again over that real verdict domain (previously suppressed
        # because the member was the raw value).
        kpi = self._v2_kpi(presentation_meta=self._BANDS_META)
        row = mdschema._rows_kpis(self.CATALOG, [kpi], self.MEASURES)[0]
        assert row["KPI_STATUS"] == "[Measures].[Test KPI Status]"
        assert row["KPI_STATUS_GRAPHIC"] == "Gauge"

    def test_no_bands_still_governed_via_target_basis(self):
        # No direction and no bands -> no informational annotation. But the KPI
        # still has a static target (verdict basis), so KPI_STATUS is the governed
        # synthetic member (not the raw value), and the graphic is advertised.
        kpi = self._v2_kpi(direction="", presentation_meta=None)
        row = mdschema._rows_kpis(self.CATALOG, [kpi], self.MEASURES)[0]
        assert row["ANNOTATIONS"] == ""
        assert row["KPI_STATUS"] == "[Measures].[Test KPI Status]"
        assert row["KPI_STATUS_GRAPHIC"] == "Gauge"

    def test_annotation_builder_empty_without_context(self):
        assert mdschema._build_kpi_band_annotation({}) == ""

    # The live governed KPIStatus path (−1/0/1 through the model-service /evaluate
    # authority) is covered in test_kpi_member_functions.py::
    # test_live_status_is_governed_minus_one_zero_one and siblings.
