"""Regression tests for Bug-9769 (partial): LAST_NON_EMPTY measures must
resolve their OWN explicit ``date_dimension_column_id`` binding, not always
a model-wide "finest time dimension" guess.

Flagged during Bug-9766's GPT design consultation and independently
verified in-code: ``_flat_lne_hidden_time_dim`` never consulted a measure's
``date_dimension_column_id`` at all, always falling back to
``_finest_time_dimension_name`` (the single finest-grain time dimension in
the WHOLE model). The investor-demo model has exactly this shape --
multiple date-like dimensions (business_date, created_at, posting_ts,
settlement_ts, transaction_ts, updated_at) alongside one real LAST_NON_EMPTY
measure (``account_balance``) with its own explicit
``date_dimension_column_id`` -- so picking the wrong one could silently
evaluate "last non-empty" against a date column that isn't the measure's
actual intended period column.

The fix resolves each requested LNE measure's own binding first (via
``_explicit_lne_time_dimension_names``) and only falls back to the
model-wide heuristic when no measure declares one; if requested LNE
measures disagree on their bindings, the query is refused (pre-existing
"no time dimension available" guard) rather than guessing.
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


_LONE_DIM_STATEMENT = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command>
        <Statement>
          SELECT {[Measures].[account_balance]} ON COLUMNS,
          {[account_type].[account_type].Members} ON ROWS
          FROM [m]
        </Statement>
      </Command>
      <Properties><PropertyList><Catalog>m</Catalog></PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""


@pytest.mark.asyncio
async def test_lne_measure_uses_its_own_explicit_date_binding_not_the_wrong_finer_one(monkeypatch):
    """Model has TWO date dimensions: transaction_ts (day grain, finer --
    what the old "finest time dimension" heuristic would pick) and
    posting_month (month grain, coarser -- but the ACTUAL explicit binding
    account_balance declares via date_dimension_column_id). The fix must
    group by posting_month, not transaction_ts, and must select the
    balance value at the latest POSTING month, not the naive SUM."""
    sql_calls: list[str] = []

    async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def fake_get_model_measures(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [{
            "name": "account_balance",
            "default_agg": "sum",
            "semi_additive_behavior": "last_non_empty",
            "date_dimension_column_id": "col-posting-month",
        }]

    async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [
            {"name": "account_type"},
            {
                "name": "transaction_ts",
                "source_column_id": "col-transaction-ts",
                "dimension_kind": "time",
                "is_time_dim": True,
                "time_grain": "day",
            },
            {
                "name": "posting_month",
                "source_column_id": "col-posting-month",
                "dimension_kind": "time",
                "is_time_dim": True,
                "time_grain": "month",
            },
        ]

    async def fake_execute_query(model_id, sql, tenant_slug, jwt_token, protocol="dax", **_kw):
        sql_calls.append(sql)
        return {
            "columns": ["account_type", "posting_month", "account_balance"],
            "rows": [
                {"account_type": "CREDIT", "posting_month": "2024-01", "account_balance": 100},
                {"account_type": "CREDIT", "posting_month": "2024-02", "account_balance": 120},
            ],
        }

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "execute_query", fake_execute_query)

    response = await xmla_server._handle_execute(
        _execute_method(_LONE_DIM_STATEMENT),
        tenant_slug="demo",
        jwt_token="token",
        session_id="sid-explicit-binding",
    )
    body = response.body.decode("utf-8")

    assert response.status_code == 200
    assert "<soap11env:Fault>" not in body
    assert all('"posting_month"' in s for s in sql_calls)
    assert not any('"transaction_ts"' in s for s in sql_calls)
    # Last-non-empty by posting_month (Feb=120), not a naive SUM (220).
    assert '<Value xsi:type="xsd:double">120.0</Value>' in body
    assert '<Value xsi:type="xsd:double">220.0</Value>' not in body


@pytest.mark.asyncio
async def test_lne_measure_without_explicit_binding_still_uses_finest_time_fallback(monkeypatch):
    """No date_dimension_column_id set on the measure -- must fall back to
    the pre-existing model-wide "finest time dimension" heuristic exactly
    as before this fix (regression guard for the common/simple case)."""
    sql_calls: list[str] = []

    async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def fake_get_model_measures(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [{
            "name": "account_balance",
            "default_agg": "sum",
            "semi_additive_behavior": "last_non_empty",
        }]

    async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [
            {"name": "account_type"},
            {
                "name": "transaction_ts",
                "source_column_id": "col-transaction-ts",
                "dimension_kind": "time",
                "is_time_dim": True,
                "time_grain": "day",
            },
        ]

    async def fake_execute_query(model_id, sql, tenant_slug, jwt_token, protocol="dax", **_kw):
        sql_calls.append(sql)
        return {
            "columns": ["account_type", "transaction_ts", "account_balance"],
            "rows": [{"account_type": "CREDIT", "transaction_ts": "2024-02-01", "account_balance": 120}],
        }

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "execute_query", fake_execute_query)

    response = await xmla_server._handle_execute(
        _execute_method(_LONE_DIM_STATEMENT),
        tenant_slug="demo",
        jwt_token="token",
        session_id="sid-fallback-binding",
    )
    body = response.body.decode("utf-8")

    assert response.status_code == 200
    assert "<soap11env:Fault>" not in body
    assert all('"transaction_ts"' in s for s in sql_calls)
