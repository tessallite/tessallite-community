"""Regression tests for Bug-5888 (GPT-2026-07-02 F-002-04).

Before the fix, an XMLA <Cancel> command always returned an empty-success
ExecuteResponse without touching the in-flight query -- a query cancelled by
the user in Excel would keep running to completion on the query-router side,
wasting source capacity and misleading the caller about what happened.

The fix registers the in-flight `execute_query` call as an asyncio task keyed
by the XMLA SessionId (mirroring the JDBC CancelRequest pattern, Bug-5188),
so a same-session Cancel actually cancels it. Cancel remains a "best effort,
session-scoped" acknowledgement: a Cancel with no matching in-flight task (no
session, unknown session, or the query already finished) still reports
success, honestly, because there is genuinely nothing left to cancel.
"""

import asyncio

from defusedxml import ElementTree as ET
from xml.etree.ElementTree import Element

import pytest

from src.dax import xmla_server


def _execute_method(xml_body: str) -> Element:
    root = ET.fromstring(xml_body)
    method_el = xmla_server._find_method(root)
    assert method_el is not None
    return method_el


_STATEMENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command>
        <Statement>SELECT {[Measures].[Revenue]} ON COLUMNS FROM [m]</Statement>
      </Command>
      <Properties>
        <PropertyList>
          <Catalog>m</Catalog>
        </PropertyList>
      </Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""

_CANCEL_XML = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command><Cancel><ConnectionID>7</ConnectionID></Cancel></Command>
    </Execute>
  </soap:Body>
</soap:Envelope>"""


def _patch_common(monkeypatch, execute_query_impl):
    async def fake_resolve_model_id(catalog: str, tenant_slug: str, jwt_token: str):
        return "model-1", "project-1", None, None

    async def fake_get_model_measures(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return [{"name": "Revenue", "default_agg": "sum", "id": "m-revenue"}]

    async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, project_id="", **kw):
        return []

    async def fake_get_model_hierarchies(model_id, tenant_slug, jwt_token, **kw):
        return []

    async def fake_get_model_named_sets(*_args, **_kwargs):
        return []

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "get_model_hierarchies", fake_get_model_hierarchies)
    monkeypatch.setattr(xmla_server, "get_model_named_sets", fake_get_model_named_sets)
    monkeypatch.setattr(xmla_server, "execute_query", execute_query_impl)


@pytest.mark.asyncio
async def test_cancel_actually_cancels_the_in_flight_query_for_the_same_session(monkeypatch):
    started = asyncio.Event()

    async def slow_execute_query(*_args, **_kwargs):
        started.set()
        await asyncio.sleep(30)  # would hang the test if not cancelled
        return {"columns": ["Revenue"], "rows": [{"Revenue": 100}]}

    _patch_common(monkeypatch, slow_execute_query)

    session_id = "sid-cancel-1"
    execute_task = asyncio.ensure_future(
        xmla_server._handle_execute(
            _execute_method(_STATEMENT_XML),
            tenant_slug="demo",
            jwt_token="token",
            session_id=session_id,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    # Give the registration line (right after `asyncio.ensure_future`) a tick
    # to run before we try to cancel it.
    await asyncio.sleep(0)

    cancel_response = await xmla_server._handle_execute(
        _execute_method(_CANCEL_XML),
        tenant_slug="demo",
        jwt_token="token",
        session_id=session_id,
    )
    cancel_body = cancel_response.body.decode("utf-8")
    assert cancel_response.status_code == 200
    assert "xml-analysis:empty" in cancel_body

    execute_response = await asyncio.wait_for(execute_task, timeout=2)
    execute_body = execute_response.body.decode("utf-8")
    assert execute_response.status_code == 200
    assert "<soap11env:Fault>" in execute_body
    assert "cancelled" in execute_body.lower()


@pytest.mark.asyncio
async def test_cancel_with_no_matching_session_still_reports_success(monkeypatch):
    """Cancel-with-nothing-to-cancel is legitimate XMLA semantics (the
    operation may already be complete) -- it must not raise or fault."""
    async def fake_execute_query(*_args, **_kwargs):
        return {"columns": [], "rows": []}

    _patch_common(monkeypatch, fake_execute_query)

    response = await xmla_server._handle_execute(
        _execute_method(_CANCEL_XML),
        tenant_slug="demo",
        jwt_token="token",
        session_id="sid-never-seen",
    )
    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "<soap11env:Fault>" not in body
    assert "xml-analysis:empty" in body


@pytest.mark.asyncio
async def test_cancel_on_a_different_session_does_not_cancel_this_one(monkeypatch):
    started = asyncio.Event()
    completed = asyncio.Event()

    async def fast_execute_query(*_args, **_kwargs):
        started.set()
        await asyncio.sleep(0.05)
        completed.set()
        return {"columns": ["Revenue"], "rows": [{"Revenue": 100}]}

    _patch_common(monkeypatch, fast_execute_query)

    session_id = "sid-owner"
    execute_task = asyncio.ensure_future(
        xmla_server._handle_execute(
            _execute_method(_STATEMENT_XML),
            tenant_slug="demo",
            jwt_token="token",
            session_id=session_id,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)

    # Cancel arrives for an unrelated session -- must not touch sid-owner's task.
    await xmla_server._handle_execute(
        _execute_method(_CANCEL_XML),
        tenant_slug="demo",
        jwt_token="token",
        session_id="sid-bystander",
    )

    execute_response = await asyncio.wait_for(execute_task, timeout=2)
    assert completed.is_set()
    body = execute_response.body.decode("utf-8")
    assert "<soap11env:Fault>" not in body
    assert "<tns:ExecuteResponse>" in body
