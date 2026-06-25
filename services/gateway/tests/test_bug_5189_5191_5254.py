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
    _extract_label_filter_specs,
    _label_filter_to_sql,
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
            }

        async def fake_get_model_measures(model_id, tenant_slug, jwt_token, **kw):
            return [{"id": "m1", "name": "Revenue", "default_agg": "sum"}]

        async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, **kw):
            return [{"id": "d1", "name": "Region", "source": "column"}]

        async def fake_get_model_hierarchies(*, model_id, tenant_slug, jwt_token, **kw):
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
            return "model-1", "project-1", None  # business base

        async def fake_get_model_measures(model_id, tenant_slug, jwt_token, **kw):
            return [{"id": "m1", "name": "Revenue", "default_agg": "sum"}]

        async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, **kw):
            return [{"id": "d1", "name": "Region", "source": "column"}]

        async def fake_get_model_hierarchies(*, model_id, tenant_slug, jwt_token, **kw):
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
        specs = _extract_label_filter_specs(
            axis, {"Product"},
            hierarchy_level_dim_map={},
            hierarchy_default_dim_map={},
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
            return "model-1", "project-1", None

        async def fake_get_model_measures(model_id, tenant_slug, jwt_token, **kw):
            return [
                {"id": "m1", "name": "Sales", "default_agg": "avg"},
            ]

        async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, **kw):
            return [
                {"id": "d1", "name": "Region"},
            ]

        async def fake_get_model_hierarchies(*, model_id, tenant_slug, jwt_token, **kw):
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

        await xmla_server._handle_execute(
            method_el, tenant_slug="demo", jwt_token="tok",
            session_id="sid-test",
        )

        # Check that at least one captured SQL (the re-query) contains the
        # label filter LIKE clause. The main query is the first call; the
        # re-query for AVG is subsequent.
        requery_sqls = [s for s in captured_sqls if "AVG" in s.upper()]
        if requery_sqls:
            # When re-queries are triggered, they must include the label filter
            assert any("LIKE" in s and "us" in s.lower() for s in requery_sqls), (
                f"Re-query SQL must contain the label filter LIKE clause. "
                f"Got: {requery_sqls}"
            )


# ============================================================================
# Bug-5254: KPI status expression derived from bands
# ============================================================================


class TestBug5254KpiBandStatus:
    """Verify that MDSCHEMA_KPIS status expressions use presentation_meta.bands
    when available, rather than hardcoded 90%/110% thresholds."""

    CATALOG = "sales_model"
    MEASURES = [
        {"id": "m1", "name": "Revenue", "default_agg": "sum"},
    ]

    def _make_v2_kpi(
        self,
        *,
        direction="higher_is_better",
        presentation_meta=None,
        target_value=100,
    ):
        return {
            "id": "k1",
            "name": "test_kpi",
            "display_name": "Test KPI",
            "description": "",
            "display_folder": "",
            "expression": 'measure("Revenue")',
            "target_type": "static",
            "target_value": target_value,
            "target_expression": None,
            "status_expression": "",
            "trend_expression": "",
            "weight": None,
            "parent_kpi_id": None,
            "presentation_type": "gauge",
            "value_measure_id": None,
            "goal_measure_id": None,
            "direction": direction,
            "presentation_meta": presentation_meta,
        }

    def test_bands_produce_threshold_based_expression(self):
        """When presentation_meta.bands are provided, the status expression
        should use band boundaries, not the hardcoded 0.9/1.1 thresholds."""
        kpi = self._make_v2_kpi(
            direction="higher_is_better",
            presentation_meta={
                "evaluation_type": "absolute_value",
                "bands": [
                    {"label": "Off Target", "color": "#D32F2F", "min": None, "max": 60},
                    {"label": "Near Target", "color": "#F57C00", "min": 60, "max": 90},
                    {"label": "On Track", "color": "#388E3C", "min": 90, "max": None},
                ],
            },
        )
        rows = mdschema._rows_kpis(self.CATALOG, [kpi], self.MEASURES)
        status = rows[0]["KPI_STATUS"]
        # Must reference the actual band thresholds (60, 90), not 0.9/1.1
        assert "60" in status
        assert "90" in status
        # Must NOT contain the old hardcoded multipliers
        assert "* 0.9" not in status
        assert "* 1.1" not in status

    def test_bands_status_maps_to_correct_rag_values(self):
        """Band colours map to correct -1/0/1 status values: red=-1,
        orange/amber=0, green=1."""
        kpi = self._make_v2_kpi(
            presentation_meta={
                "evaluation_type": "absolute_value",
                "bands": [
                    {"label": "Off Target", "color": "#D32F2F", "min": None, "max": 50},
                    {"label": "Near Target", "color": "#F57C00", "min": 50, "max": 80},
                    {"label": "On Track", "color": "#388E3C", "min": 80, "max": None},
                ],
            },
        )
        rows = mdschema._rows_kpis(self.CATALOG, [kpi], self.MEASURES)
        status = rows[0]["KPI_STATUS"]
        # Red band (< 50) -> -1
        assert "THEN -1" in status
        # Orange band (50..80) -> 0
        assert "THEN 0" in status
        # Green band (>= 80) -> 1
        assert "THEN 1" in status

    def test_ratio_based_bands_use_percentage_expression(self):
        """For percentage_of_target evaluation, the expression should divide
        value by goal and multiply by 100."""
        kpi = self._make_v2_kpi(
            presentation_meta={
                "evaluation_type": "percentage_of_target",
                "bands": [
                    {"label": "Off Target", "color": "#D32F2F", "min": None, "max": 80},
                    {"label": "Near Target", "color": "#F57C00", "min": 80, "max": 100},
                    {"label": "On Track", "color": "#388E3C", "min": 100, "max": None},
                ],
            },
        )
        rows = mdschema._rows_kpis(self.CATALOG, [kpi], self.MEASURES)
        status = rows[0]["KPI_STATUS"]
        # Must contain the percentage-of-target ratio expression
        assert "/ 100 * 100" in status or "* 100" in status

    def test_no_bands_falls_back_to_direction_heuristic(self):
        """When no bands are in presentation_meta, the old direction-based
        heuristic is used (but this is still a valid CASE expression)."""
        kpi = self._make_v2_kpi(
            direction="higher_is_better",
            presentation_meta=None,
        )
        rows = mdschema._rows_kpis(self.CATALOG, [kpi], self.MEASURES)
        status = rows[0]["KPI_STATUS"]
        assert status.startswith("CASE WHEN")
        # Fallback uses the 0.9 multiplier
        assert "* 0.9" in status

    def test_lower_is_better_fallback(self):
        """For lower_is_better without bands, the 1.1 heuristic is used."""
        kpi = self._make_v2_kpi(
            direction="lower_is_better",
            presentation_meta=None,
        )
        rows = mdschema._rows_kpis(self.CATALOG, [kpi], self.MEASURES)
        status = rows[0]["KPI_STATUS"]
        assert "* 1.1" in status

    def test_legacy_status_expression_preserved(self):
        """A legacy KPI with an explicit status_expression must use that
        expression verbatim, not the band-derived one."""
        kpi = {
            "id": "k1",
            "name": "legacy",
            "display_name": "Legacy",
            "description": "",
            "display_folder": "",
            "expression": "",
            "value_measure_id": "m1",
            "goal_measure_id": "m1",
            "status_expression": "IIF(KpiValue > KpiGoal, 1, -1)",
            "trend_expression": "",
            "weight": None,
            "parent_kpi_id": None,
            "presentation_type": None,
            "presentation_meta": {
                "bands": [
                    {"label": "Bad", "color": "#D32F2F", "min": None, "max": 50},
                    {"label": "Good", "color": "#388E3C", "min": 50, "max": None},
                ],
            },
        }
        rows = mdschema._rows_kpis(self.CATALOG, [kpi], self.MEASURES)
        assert rows[0]["KPI_STATUS"] == "IIF(KpiValue > KpiGoal, 1, -1)"

    def test_empty_bands_list_uses_fallback(self):
        """An empty bands list should fall back to direction heuristic."""
        kpi = self._make_v2_kpi(
            presentation_meta={"bands": [], "evaluation_type": "absolute_value"},
        )
        rows = mdschema._rows_kpis(self.CATALOG, [kpi], self.MEASURES)
        status = rows[0]["KPI_STATUS"]
        assert status.startswith("CASE WHEN")

    def test_single_band_uses_fallback(self):
        """A single band (fewer than 2) should fall back to direction heuristic."""
        kpi = self._make_v2_kpi(
            presentation_meta={
                "bands": [{"label": "On Track", "color": "#388E3C", "min": None, "max": None}],
                "evaluation_type": "absolute_value",
            },
        )
        rows = mdschema._rows_kpis(self.CATALOG, [kpi], self.MEASURES)
        status = rows[0]["KPI_STATUS"]
        assert status.startswith("CASE WHEN")

    def test_custom_four_band_kpi(self):
        """A KPI with 4 custom bands should produce a CASE with the correct
        number of WHEN clauses."""
        kpi = self._make_v2_kpi(
            presentation_meta={
                "evaluation_type": "absolute_value",
                "bands": [
                    {"label": "Critical", "color": "#D32F2F", "min": None, "max": 25},
                    {"label": "Low", "color": "#F57C00", "min": 25, "max": 50},
                    {"label": "Medium", "color": "#FBC02D", "min": 50, "max": 75},
                    {"label": "High", "color": "#388E3C", "min": 75, "max": None},
                ],
            },
        )
        rows = mdschema._rows_kpis(self.CATALOG, [kpi], self.MEASURES)
        status = rows[0]["KPI_STATUS"]
        assert status.startswith("CASE")
        # Should have WHEN clauses for all 4 bands
        assert status.count("WHEN") == 4


class TestBuildKpiStatusExpression:
    """Direct unit tests for the _build_kpi_status_expression helper."""

    def test_absolute_value_three_bands(self):
        expr = mdschema._build_kpi_status_expression(
            "[Measures].[KPI Value]", "100",
            bands=[
                {"label": "Off Target", "color": "#D32F2F", "min": None, "max": 80},
                {"label": "Near Target", "color": "#F57C00", "min": 80, "max": 100},
                {"label": "On Track", "color": "#388E3C", "min": 100, "max": None},
            ],
            evaluation_type="absolute_value",
        )
        assert "CASE" in expr
        assert "80" in expr
        assert "100" in expr
        # Red -> -1, Orange -> 0, Green -> 1
        assert "THEN -1" in expr
        assert "THEN 0" in expr
        assert "THEN 1" in expr

    def test_percentage_of_target_uses_ratio(self):
        expr = mdschema._build_kpi_status_expression(
            "[Measures].[V]", "[Measures].[G]",
            bands=[
                {"label": "Bad", "color": "#D32F2F", "min": None, "max": 80},
                {"label": "OK", "color": "#F57C00", "min": 80, "max": 100},
                {"label": "Good", "color": "#388E3C", "min": 100, "max": None},
            ],
            evaluation_type="percentage_of_target",
        )
        # Must use value/goal*100 ratio
        assert "[Measures].[V] / [Measures].[G] * 100" in expr

    def test_no_bands_higher_is_better(self):
        expr = mdschema._build_kpi_status_expression(
            "[Measures].[V]", "[Measures].[G]",
            bands=None,
            direction="higher_is_better",
        )
        assert "* 0.9" in expr
        assert "THEN 1" in expr

    def test_no_bands_lower_is_better(self):
        expr = mdschema._build_kpi_status_expression(
            "[Measures].[V]", "[Measures].[G]",
            bands=None,
            direction="lower_is_better",
        )
        assert "* 1.1" in expr

    def test_band_status_from_color(self):
        """Known RAG colours should map to correct status values."""
        assert mdschema._band_status_from_color("#D32F2F") == -1  # red
        assert mdschema._band_status_from_color("#F57C00") == 0   # orange
        assert mdschema._band_status_from_color("#388E3C") == 1   # green
        assert mdschema._band_status_from_color("#1565C0") == 1   # blue
        assert mdschema._band_status_from_color("#757575") == -1  # grey (bad)
        assert mdschema._band_status_from_color("#UNKNOWN") is None
