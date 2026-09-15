"""Regression tests for Bug-9766's flat-attribute-dimension scope.

Excel's native PivotTable subtotal/grand-total request for FLAT (non-
hierarchy) attribute dimensions -- e.g. dragging ``account_type`` and
``aml_flag`` into Rows together with subtotals on -- previously produced
blank rollup-row values: the gateway's subtotal-detection machinery only
recognized genuine multi-level HIERARCHIES (calendars), so no rollup query
ever ran for a plain attribute.

An initial attempt at this fix folded flat attributes directly into
``detect_subtotal_hierarchies``'s result list and was reverted before
shipping (see ``test_subtotal_engine.py``'s docstrings): doing so made
``bool(subtotal_hierarchies)`` -- which ALSO gates the semi-additive/
LAST_NON_EMPTY hidden-time-grain repair, the "no time dimension available"
fail-loud guard, and empty-axis-member restoration -- true for queries that
have nothing to do with time, producing wrong numbers and silently
swallowing a required fault.

A second attempt detected ANY self-qualified flat-attribute ``.Members``
request, including a LONE dimension with nothing else on its axis -- but
that is the exact same MDX shape Excel sends for an ordinary, no-subtotal
single-field PivotTable, so it wrongly refused several previously-working
flat-pivot LAST_NON_EMPTY queries as a fabricated subtotal-plus-LNE
conflict (caught by 4 failing tests in test_bug8750_8751_with_prelude_
execute.py / test_xmla_protocol_fidelity_h14.py / test_xmla_server_excel_
regressions.py).

The shipped fix only detects a flat-attribute rollup when 2+ dimensions are
CrossJoin'd on the SAME axis (the unambiguous signal, confirmed against the
real, DEBUG-log-captured Excel MDX for the reported bug), keeps detection
in a SEPARATE function/result list from real hierarchies so it never
affects LAST_NON_EMPTY gating, and reuses the existing tested grain-query/merge
machinery to actually compute subtotal values for the additive (non-LNE)
case. A genuine temporal hierarchy plus non-temporal hierarchy peers now use
the repaired Bug-9768 semi-additive path; flat-attribute and unsupported
temporal metadata combinations remain explicit SOAP faults.
"""

from defusedxml import ElementTree as ET
from xml.etree.ElementTree import Element

import pytest

from src.dax import xmla_server
from tests.lattice_fake import lattice_aware


def _execute_method(xml_body: str) -> Element:
    root = ET.fromstring(xml_body)
    method_el = xmla_server._find_method(root)
    assert method_el is not None
    return method_el


def _two_dim_rollup_statement(measure: str) -> str:
    """The real Excel shape: two flat attribute dimensions CrossJoin'd on
    Rows, each with its own explicit (All)-qualified Members request --
    reproduced from the live DEBUG-log capture on the investor demo."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command>
        <Statement>
          SELECT {{[Measures].[{measure}]}} ON COLUMNS,
          NON EMPTY CrossJoin(
            Hierarchize(AddCalculatedMembers({{[account_type].[account_type].[(All)].Members}})),
            Hierarchize(AddCalculatedMembers({{[aml_flag].[aml_flag].[(All)].Members}}))
          ) ON ROWS
          FROM [m]
        </Statement>
      </Command>
      <Properties><PropertyList><Catalog>m</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""


def _two_dim_member_enumeration_statement() -> str:
    """Excel's field-add request before any value field exists."""
    return """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command>
        <Statement>
          SELECT NON EMPTY CrossJoin(
            Hierarchize(AddCalculatedMembers({[account_type].[account_type].[(All)].Members})),
            Hierarchize(AddCalculatedMembers({[aml_flag].[aml_flag].[(All)].Members}))
          ) ON COLUMNS
          FROM [m]
        </Statement>
      </Command>
      <Properties><PropertyList>
        <Catalog>m</Catalog>
        <SspropInitAppName>Microsoft Excel</SspropInitAppName>
      </PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""


def _lone_dim_statement(measure: str) -> str:
    """A SINGLE flat attribute alone on Rows -- the ordinary, no-subtotal
    shape that must keep working exactly as before this fix."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command>
        <Statement>
          SELECT {{[Measures].[{measure}]}} ON COLUMNS,
          {{[account_type].[account_type].Members}} ON ROWS
          FROM [m]
        </Statement>
      </Command>
      <Properties><PropertyList><Catalog>m</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""


@pytest.mark.asyncio
async def test_flat_attribute_rollup_produces_subtotal_and_grand_total(monkeypatch):
    """The core Bug-9766 fix: a two-dimension flat-attribute rollup with an
    ADDITIVE (non-LNE) measure must actually compute and return the
    subtotal/grand-total values, not leave them blank."""
    sql_calls: list[str] = []

    async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def fake_get_model_measures(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [{"name": "base_amount", "default_agg": "sum"}]

    async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [{"name": "account_type"}, {"name": "aml_flag"}]

    async def fake_execute_query(model_id, sql, tenant_slug, jwt_token, protocol="dax", **_kw):
        has_account_type = '"account_type"' in sql
        has_aml_flag = '"aml_flag"' in sql
        has_group_by = "GROUP BY" in sql
        if has_group_by and has_account_type and has_aml_flag:
            # The original detail query (both dims at leaf grain).
            return {
                "columns": ["account_type", "aml_flag", "base_amount"],
                "rows": [
                    {"account_type": "CREDIT", "aml_flag": "False", "base_amount": 90},
                    {"account_type": "CREDIT", "aml_flag": "True", "base_amount": 10},
                ],
            }
        if has_group_by and has_account_type:
            # Subtotal grain: account_type detail, aml_flag rolled up.
            return {
                "columns": ["account_type", "base_amount"],
                "rows": [{"account_type": "CREDIT", "base_amount": 100}],
            }
        if has_group_by and has_aml_flag:
            # Bug-9845: the All-account x aml_flag family is part of the
            # requested CrossJoin and is served like any other grain.
            return {
                "columns": ["aml_flag", "base_amount"],
                "rows": [
                    {"aml_flag": "False", "base_amount": 90},
                    {"aml_flag": "True", "base_amount": 10},
                ],
            }
        # Grand Total: no GROUP BY at all.
        return {"columns": ["base_amount"], "rows": [{"base_amount": 100}]}

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(
        xmla_server, "execute_query",
        lattice_aware(fake_execute_query, sql_calls),
    )

    response = await xmla_server._handle_execute(
        _execute_method(_two_dim_rollup_statement("base_amount")),
        tenant_slug="demo",
        jwt_token="token",
        session_id="sid-flatattr-1",
    )
    body = response.body.decode("utf-8")

    assert response.status_code == 200
    assert "<soap11env:Fault>" not in body
    # Detail leaf values.
    assert '<Value xsi:type="xsd:double">90.0</Value>' in body
    assert '<Value xsi:type="xsd:double">10.0</Value>' in body
    # Subtotal (account_type grand total across aml_flag) and grand total.
    assert '<Value xsi:type="xsd:double">100.0</Value>' in body
    # The full CrossJoin the client asked for (Bug-9845) is still served:
    # both single-field subtotal grains AND the grand total. The inner-only
    # grain was once pruned because ordinal-positioned cells shifted
    # (Bug-9244); on the native-All contract cells follow tuples, so every
    # grain is served.
    #
    # Bug-9864: those three rollup grains now arrive in ONE grouping-sets
    # query instead of three, so the gateway issues the detail query plus one
    # lattice query. Every VALUE asserted above is unchanged -- that is the
    # point of the change.
    assert len(sql_calls) == 2


@pytest.mark.asyncio
async def test_bug_9837_measureless_field_add_synthesizes_required_grand_total(monkeypatch):
    """Adding a second row field must not fault before a measure is selected."""
    sql_calls: list[str] = []

    async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def fake_get_model_measures(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [{"name": "base_amount", "default_agg": "sum"}]

    async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [{"name": "account_type"}, {"name": "aml_flag"}]

    async def fake_execute_query(model_id, sql, tenant_slug, jwt_token, protocol="dax", **_kw):
        sql_calls.append(sql)
        has_account_type = '"account_type"' in sql
        has_aml_flag = '"aml_flag"' in sql
        if has_account_type and has_aml_flag:
            return {
                "columns": ["account_type", "aml_flag"],
                "rows": [
                    {"account_type": "CREDIT", "aml_flag": "False"},
                    {"account_type": "CREDIT", "aml_flag": "True"},
                ],
            }
        if has_account_type:
            return {
                "columns": ["account_type"],
                "rows": [{"account_type": "CREDIT"}],
            }
        if has_aml_flag:
            # Bug-9845: the inner-only structural grain is requested too.
            return {
                "columns": ["aml_flag"],
                "rows": [{"aml_flag": "False"}, {"aml_flag": "True"}],
            }
        pytest.fail("measureless grand total must be structural, not source SQL")

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "execute_query", fake_execute_query)

    response = await xmla_server._handle_execute(
        _execute_method(_two_dim_member_enumeration_statement()),
        tenant_slug="demo",
        jwt_token="token",
        session_id="sid-bug-9837-measureless",
    )
    body = response.body.decode("utf-8")

    assert response.status_code == 200
    assert "<soap11env:Fault>" not in body
    assert "<Caption>CREDIT</Caption>" in body
    assert "<Caption>False</Caption>" in body
    assert "<Caption>True</Caption>" in body
    # detail + account grain + aml grain; the dimension-less grand total is
    # synthesised structurally and never reaches SQL.
    assert len(sql_calls) == 3


@pytest.mark.asyncio
async def test_bug_9837_required_multi_subtotal_failure_faults_whole_execute(monkeypatch):
    """A missing required grain must never be returned as a partial cube."""
    async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def fake_get_model_measures(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [{"name": "base_amount", "default_agg": "sum"}]

    async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [{"name": "account_type"}, {"name": "aml_flag"}]

    async def fake_execute_query(model_id, sql, tenant_slug, jwt_token, protocol="dax", **_kw):
        has_account_type = '"account_type"' in sql
        has_aml_flag = '"aml_flag"' in sql
        if "GROUP BY" in sql and has_account_type and has_aml_flag:
            return {
                "columns": ["account_type", "aml_flag", "base_amount"],
                "rows": [{
                    "account_type": "CREDIT",
                    "aml_flag": "False",
                    "base_amount": 90,
                }],
            }
        if "GROUP BY" in sql and has_account_type:
            raise RuntimeError("database exploded with private detail")
        return {"columns": ["base_amount"], "rows": [{"base_amount": 90}]}

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "execute_query", fake_execute_query)

    response = await xmla_server._handle_execute(
        _execute_method(_two_dim_rollup_statement("base_amount")),
        tenant_slug="demo",
        jwt_token="token",
        session_id="sid-required-grain-failure",
    )
    body = response.body.decode("utf-8")

    assert "<soap11env:Fault" in body
    assert "A required subtotal query failed" in body
    assert "database exploded" not in body
    assert "<Messages>" not in body


@pytest.mark.asyncio
async def test_bug_9837_required_single_hierarchy_grain_failure_faults_whole_execute(monkeypatch):
    """The same fail-whole contract applies to a single real hierarchy."""
    from src.dax import subtotal_engine

    async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def fake_get_model_measures(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [{"name": "base_amount", "default_agg": "sum"}]

    async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [{"name": "region_key", "source_column_id": "col-region"}]

    async def fake_get_model_hierarchies(
        model_id, tenant_slug, jwt_token, project_id="", include_details=True, **kw,
    ):
        return [{
            "id": "h-geography",
            "name": "Geography",
            "levels": [{
                "ordinal": 0,
                "name": "Region",
                "key_attribute": {"id": "col-region", "source": "physical_column"},
            }],
        }]

    async def fake_execute_query(model_id, sql, tenant_slug, jwt_token, protocol="dax", **_kw):
        if '"region_key"' in sql:
            return {
                "columns": ["region_key", "base_amount"],
                "rows": [{
                    "region_key": "EMEA",
                    "base_amount": 90,
                }],
            }
        raise RuntimeError("private hierarchy failure")

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "get_model_hierarchies", fake_get_model_hierarchies)
    monkeypatch.setattr(xmla_server, "execute_query", fake_execute_query)
    monkeypatch.setattr(
        subtotal_engine,
        "detect_subtotal_hierarchies",
        lambda *_args, **_kwargs: [subtotal_engine.SubtotalHierarchy(
            hierarchy_name="Geography",
            mdx_dim_name="Geography",
            mdx_hier_name="Geography",
            levels=[subtotal_engine.SubtotalLevel(
                name="Region",
                ordinal=0,
                dim_name="region_key",
            )],
            axis=1,
        )],
    )

    statement = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command><Statement>
        SELECT {[Measures].[base_amount]} ON COLUMNS,
        Hierarchize(AddCalculatedMembers({[Geography].[Geography].[(All)].Members})) ON ROWS
        FROM [m]
      </Statement></Command>
      <Properties><PropertyList><Catalog>m</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""

    response = await xmla_server._handle_execute(
        _execute_method(statement),
        tenant_slug="demo",
        jwt_token="token",
        session_id="sid-required-single-grain-failure",
    )
    body = response.body.decode("utf-8")

    assert "<soap11env:Fault" in body
    assert "A required subtotal query failed" in body
    assert "private hierarchy failure" not in body
    assert "<Messages>" not in body


@pytest.mark.asyncio
async def test_lone_flat_attribute_with_lne_measure_still_works(monkeypatch):
    """Regression guard: a SINGLE flat attribute (no CrossJoin) combined
    with a LAST_NON_EMPTY measure must keep working exactly as it did
    before this fix, via the pre-existing flat-pivot hidden-time-grain
    repair -- it must NOT be refused as a fabricated subtotal conflict.
    This is the exact shape that caught the second (also reverted-before-
    ship) version of this fix during development."""
    async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def fake_get_model_measures(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [{
            "name": "ending_balance",
            "default_agg": "last_non_empty",
            "semi_additive_behavior": "last_non_empty",
        }]

    async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [
            {"name": "account_type"},
            {
                "name": "business_month",
                "dimension_kind": "time",
                "is_time_dim": True,
                "time_grain": "month",
            },
        ]

    async def fake_execute_query(model_id, sql, tenant_slug, jwt_token, protocol="dax", **_kw):
        return {
            "columns": ["account_type", "business_month", "ending_balance"],
            "rows": [
                {"account_type": "CREDIT", "business_month": "2024-01", "ending_balance": 100},
                {"account_type": "CREDIT", "business_month": "2024-02", "ending_balance": 120},
            ],
        }

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "execute_query", fake_execute_query)

    response = await xmla_server._handle_execute(
        _execute_method(_lone_dim_statement("ending_balance")),
        tenant_slug="demo",
        jwt_token="token",
        session_id="sid-flatattr-lone",
    )
    body = response.body.decode("utf-8")

    assert response.status_code == 200
    assert "<soap11env:Fault>" not in body
    # Last-non-empty value (Feb = 120), not a naive SUM (220).
    assert '<Value xsi:type="xsd:double">120.0</Value>' in body
    assert '<Value xsi:type="xsd:double">220.0</Value>' not in body


@pytest.mark.asyncio
async def test_flat_attribute_rollup_with_lne_measure_refuses_the_query(monkeypatch):
    """Bug-9766 safety guard: combining a two-dimension flat-attribute
    rollup with a LAST_NON_EMPTY measure must FAULT, never return a SUM in
    place of the required last-non-empty value.

    The model DOES carry a time dimension here (unlike the "no time
    metadata at all" case, which the pre-existing, unrelated
    ``_flat_lne_hidden_time_dim`` fail-loud guard already catches on its
    own). This is the actually dangerous combination the first reverted
    attempt at this fix got wrong: with a valid hidden time grain
    available, nothing OTHER than this new check stops the attribute-
    rollup grand-total query from silently computing a naive SUM."""
    grand_total_ran = {"ran": False}

    async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def fake_get_model_measures(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [{
            "name": "ending_balance",
            "default_agg": "last_non_empty",
            "semi_additive_behavior": "last_non_empty",
        }]

    async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [
            {"name": "account_type"},
            {"name": "aml_flag"},
            {
                "name": "business_month",
                "dimension_kind": "time",
                "is_time_dim": True,
                "time_grain": "month",
            },
        ]

    async def fake_execute_query(model_id, sql, tenant_slug, jwt_token, protocol="dax", **_kw):
        if "GROUP BY" not in sql:
            grand_total_ran["ran"] = True
        return {
            "columns": ["account_type", "aml_flag", "business_month", "ending_balance"],
            "rows": [{
                "account_type": "CREDIT", "aml_flag": "False",
                "business_month": "2024-01", "ending_balance": 100,
            }],
        }

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "execute_query", fake_execute_query)

    response = await xmla_server._handle_execute(
        _execute_method(_two_dim_rollup_statement("ending_balance")),
        tenant_slug="demo",
        jwt_token="token",
        session_id="sid-flatattr-2",
    )
    body = response.body.decode("utf-8")

    assert "<soap11env:Fault" in body
    assert "LAST_NON_EMPTY" in body
    assert "account_type" in body
    # The dangerous grand-total SUM-proxy query must never have executed.
    assert grand_total_ran["ran"] is False


@pytest.mark.asyncio
async def test_flat_attribute_rollup_with_lne_and_no_time_metadata_still_faults(monkeypatch):
    """The pre-existing "no time dimension in the model at all" guard
    (unrelated to this fix, in _flat_lne_hidden_time_dim / _mdx_to_sql)
    still correctly refuses a two-dimension attribute-rollup + LNE query
    when the model has no time dimension whatsoever -- it fires earlier,
    at SQL-translation time, before this fix's own check is ever reached.
    Both guards must cover the full space between them."""
    async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def fake_get_model_measures(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [{
            "name": "ending_balance",
            "default_agg": "last_non_empty",
            "semi_additive_behavior": "last_non_empty",
        }]

    async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [{"name": "account_type"}, {"name": "aml_flag"}]

    async def fake_execute_query(model_id, sql, tenant_slug, jwt_token, protocol="dax", **_kw):
        raise AssertionError("no query should execute when this guard fires")

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "execute_query", fake_execute_query)

    response = await xmla_server._handle_execute(
        _execute_method(_two_dim_rollup_statement("ending_balance")),
        tenant_slug="demo",
        jwt_token="token",
        session_id="sid-flatattr-3",
    )
    body = response.body.decode("utf-8")

    assert "<soap11env:Fault" in body
    assert "LAST_NON_EMPTY" in body


@pytest.mark.asyncio
async def test_two_real_hierarchies_without_time_metadata_remain_fail_closed(monkeypatch):
    """Bug-9768: multi-rollup LNE is refused when no temporal hierarchy
    metadata is available, because the engine must never guess an ordering
    axis. The supported time/non-time production shape is covered separately."""
    async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def fake_get_model_measures(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [{
            "name": "ending_balance",
            "default_agg": "last_non_empty",
            "semi_additive_behavior": "last_non_empty",
        }]

    async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [
            {"name": "region_key", "source_column_id": "col-region"},
            {"name": "product_key", "source_column_id": "col-product"},
        ]

    async def fake_get_model_hierarchies(
        model_id, tenant_slug, jwt_token, project_id="", include_details=True, **kw,
    ):
        return [
            {
                "id": "h-geo",
                "name": "Geography",
                "levels": [
                    {"ordinal": 0, "name": "Region",
                     "key_attribute": {"id": "col-region", "source": "physical_column"}},
                ],
            },
            {
                "id": "h-prod",
                "name": "Product",
                "levels": [
                    {"ordinal": 0, "name": "ProductLine",
                     "key_attribute": {"id": "col-product", "source": "physical_column"}},
                ],
            },
        ]

    subtotal_query_ran = {"ran": False}

    async def fake_execute_query(model_id, sql, tenant_slug, jwt_token, protocol="dax", **_kw):
        if "GROUP BY" not in sql:
            subtotal_query_ran["ran"] = True
        return {
            "columns": ["region_key", "product_key", "ending_balance"],
            "rows": [{"region_key": "US", "product_key": "Widgets", "ending_balance": 100}],
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
          SELECT {[Measures].[ending_balance]} ON COLUMNS,
          NON EMPTY CrossJoin(
            Hierarchize(AddCalculatedMembers({[Geography].[Geography].[(All)].Members})),
            Hierarchize(AddCalculatedMembers({[Product].[Product].[(All)].Members}))
          ) ON ROWS
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
        session_id="sid-tworhier",
    )
    body = response.body.decode("utf-8")

    assert "<soap11env:Fault" in body
    assert "LAST_NON_EMPTY" in body
    # The dangerous grand-total/subtotal query must never have executed.
    assert subtotal_query_ran["ran"] is False


@pytest.mark.asyncio
async def test_bug_9768_multi_rollup_lne_uses_xmla_handler(monkeypatch):
    """Bug-9768 production path: XMLA must admit the supported metadata shape
    and return exact semi-additive subtotal and grand-total values.

    The Calendar hierarchy supplies the sole temporal ordering axis. Region is
    the non-temporal peer hierarchy. US and DE have values at the latest date;
    CA has no value at that date and must contribute its earlier value instead.
    """
    sql_calls: list[str] = []
    detail_rows = [
        {"cal_day": "2024-01-15", "region_key": "US", "ending_balance": 10},
        {"cal_day": "2024-02-15", "region_key": "US", "ending_balance": 30},
        {"cal_day": "2024-02-15", "region_key": "DE", "ending_balance": 20},
        {"cal_day": "2024-01-15", "region_key": "CA", "ending_balance": 7},
        {"cal_day": "2024-02-15", "region_key": "CA", "ending_balance": None},
    ]

    async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def fake_get_model_measures(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [{
            "name": "ending_balance",
            "default_agg": "last_non_empty",
            "semi_additive_behavior": "last_non_empty",
        }]

    async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [
            {
                "name": "cal_day",
                "source_column_id": "col-date",
                "dimension_kind": "time",
                "is_time_dim": True,
                "time_grain": "day",
            },
            {
                "name": "region_key",
                "source_column_id": "col-region",
                "dimension_kind": "geo",
            },
        ]

    async def fake_get_model_hierarchies(
        model_id, tenant_slug, jwt_token, project_id="", include_details=True, **kw,
    ):
        return [
            {
                "id": "h-calendar",
                "name": "Calendar",
                "dimension_kind": "time",
                "levels": [{
                    "ordinal": 0,
                    "name": "Day",
                    "time_unit": "day",
                    "key_attribute": {"id": "col-date", "source": "physical_column"},
                }],
            },
            {
                "id": "h-region",
                "name": "Region",
                "dimension_kind": "geo",
                "levels": [{
                    "ordinal": 0,
                    "name": "Region",
                    "key_attribute": {"id": "col-region", "source": "physical_column"},
                }],
            },
        ]

    async def fake_execute_query(model_id, sql, tenant_slug, jwt_token, protocol="dax", **_kw):
        if '"cal_day"' in sql and '"region_key"' in sql:
            return {
                "columns": ["cal_day", "region_key", "ending_balance"],
                "rows": detail_rows,
            }
        if '"cal_day"' in sql:
            return {
                "columns": ["cal_day", "ending_balance"],
                "rows": [
                    {"cal_day": "2024-01-15", "ending_balance": None},
                    {"cal_day": "2024-02-15", "ending_balance": None},
                ],
            }
        if '"region_key"' in sql:
            return {
                "columns": ["region_key", "ending_balance"],
                "rows": [
                    {"region_key": "US", "ending_balance": None},
                    {"region_key": "DE", "ending_balance": None},
                    {"region_key": "CA", "ending_balance": None},
                ],
            }
        return {"columns": ["ending_balance"], "rows": [{"ending_balance": None}]}

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "get_model_hierarchies", fake_get_model_hierarchies)
    monkeypatch.setattr(
        xmla_server, "execute_query",
        lattice_aware(fake_execute_query, sql_calls),
    )

    statement = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command>
        <Statement>
          SELECT {[Measures].[ending_balance]} ON COLUMNS,
          NON EMPTY CrossJoin(
            Hierarchize(AddCalculatedMembers({[Calendar].[Calendar].[(All)].Members})),
            Hierarchize(AddCalculatedMembers({[Region].[Region].[(All)].Members}))
          ) ON ROWS
          FROM [m]
        </Statement>
      </Command>
      <Properties><PropertyList><Catalog>m</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""

    response = await xmla_server._handle_execute(
        _execute_method(statement),
        tenant_slug="demo",
        jwt_token="token",
        session_id="sid-bug-9768-supported",
    )
    body = response.body.decode("utf-8")

    assert response.status_code == 200
    assert "<soap11env:Fault>" not in body
    # Calendar-detail/Region-all: 10 + 7 = 17 for January, 30 + 20 = 50 for
    # February. Region-detail/Calendar-all: US=30, DE=20, CA=7. Grand total:
    # each peer's own last non-empty value = 30 + 20 + 7 = 57.
    for value in ("17.0", "50.0", "30.0", "20.0", "7.0", "57.0"):
        assert f">{value}</Value>" in body, value
    # Bug-9864: the rollup grains arrive in ONE grouping-sets query, so this
    # is the detail query plus one lattice query. The LAST_NON_EMPTY values
    # asserted above are computed in Python from the DETAIL rows and are
    # therefore identical on either path.
    assert len(sql_calls) == 2
