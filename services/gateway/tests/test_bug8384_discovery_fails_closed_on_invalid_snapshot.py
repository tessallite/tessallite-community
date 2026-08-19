"""Bug-8384: MDSCHEMA_SETS must not hide a broken deployed serving authority.

`get_model_named_sets` now requests `deployed_only=true`, so the model-service
can answer 409 DEPLOYED_SNAPSHOT_INVALID — a NEW, genuinely-broken-state error
class on the discovery path. That path's blanket `except Exception: warning`
would render an EMPTY set catalogue in Excel with no error, which is
indistinguishable from "this model has no named sets": the same deceptive-empty
failure Bug-7254 rejected on the Execute path, which re-raises for exactly this
reason. Discovery and Execute must agree.

Transient fetch failures must STILL degrade gracefully — a flaky network blip
should not blank a user's whole catalogue with a fault.
"""
from __future__ import annotations

import httpx
import pytest
from defusedxml import ElementTree as ET

from src.dax import xmla_server as xs


def _discover_xml(request_type: str, catalog: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Discover xmlns="urn:schemas-microsoft-com:xml-analysis">
      <RequestType>{request_type}</RequestType>
      <Restrictions><RestrictionList/></Restrictions>
      <Properties><PropertyList><Catalog>{catalog}</Catalog></PropertyList></Properties>
    </Discover>
  </soap:Body>
</soap:Envelope>"""


def _status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://model-service/named-sets")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


@pytest.fixture
def _patched(monkeypatch):
    async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "proj-1", None, None

    async def fake_measures(model_id, tenant_slug, jwt_token, **kw):
        return [{"id": "m1", "name": "Revenue", "default_agg": "sum"}]

    async def fake_dimensions(model_id, tenant_slug, jwt_token, **kw):
        return [{"id": "d1", "name": "Region", "source": "column"}]

    async def fake_hierarchies(model_id, tenant_slug, jwt_token, **kw):
        return []

    async def fake_list_all(tenant_slug, jwt_token):
        return [{"id": "model-1", "project_id": "proj-1", "trust_meta": {}}]

    monkeypatch.setattr(xs, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xs, "get_model_measures", fake_measures)
    monkeypatch.setattr(xs, "get_model_dimensions", fake_dimensions)
    monkeypatch.setattr(xs, "get_model_hierarchies", fake_hierarchies)
    monkeypatch.setattr(xs, "list_all_models_for_tenant", fake_list_all)


async def _discover(request_type="MDSCHEMA_SETS", catalog="model_1"):
    root = ET.fromstring(_discover_xml(request_type, catalog))
    method_el = xs._find_method(root)
    return await xs._handle_discover(
        method_el, tenant_slug="acme", jwt_token="tok",
    )


@pytest.mark.asyncio
async def test_409_invalid_deployed_snapshot_returns_a_diagnosable_soap_fault(
    monkeypatch, _patched,
):
    """A broken deployed snapshot must fault, not advertise an empty catalogue.

    Asserts the CLIENT-VISIBLE outcome, not the internal exception: a bare
    re-raise would surface in Excel as an unformatted HTTP 500 "connection lost"
    carrying none of the diagnosis, which is loud but not diagnosable.
    """
    async def boom(*a, **kw):
        raise _status_error(409)

    monkeypatch.setattr(xs, "get_model_named_sets", boom)

    resp = await _discover()
    body = resp.body.decode()
    assert "Fault" in body
    assert "DEPLOYED_SNAPSHOT_INVALID" in body
    assert "Redeploy the model" in body
    # The empty-catalogue answer must NOT be what the client receives.
    assert "MDSCHEMA_SETS" not in body or "Fault" in body


@pytest.mark.asyncio
async def test_transient_fetch_failure_still_degrades_gracefully(
    monkeypatch, _patched,
):
    """A network blip must not fault the whole discovery response."""
    async def boom(*a, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(xs, "get_model_named_sets", boom)

    resp = await _discover()
    assert resp.status_code == 200
    assert b"MDSCHEMA_SETS" in resp.body or b"return" in resp.body


@pytest.mark.asyncio
async def test_non_409_http_error_still_degrades_gracefully(monkeypatch, _patched):
    """Only the deployed-authority 409 is promoted to a fault."""
    async def boom(*a, **kw):
        raise _status_error(500)

    monkeypatch.setattr(xs, "get_model_named_sets", boom)

    resp = await _discover()
    assert resp.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("request_type", ["MDSCHEMA_SETS", "MDSCHEMA_KPIS"])
async def test_every_discover_request_type_faults_on_the_409(
    monkeypatch, _patched, request_type,
):
    """The 409 is reachable from the named-set AND the KPI fetch."""
    async def boom(*a, **kw):
        raise _status_error(409)

    monkeypatch.setattr(xs, "get_model_named_sets", boom)
    monkeypatch.setattr(xs, "get_model_kpis", boom)

    resp = await _discover(request_type=request_type)
    body = resp.body.decode()
    assert "Fault" in body
    assert "DEPLOYED_SNAPSHOT_INVALID" in body


@pytest.mark.asyncio
async def test_execute_409_invalid_snapshot_is_a_soap_fault_not_a_bare_500(monkeypatch):
    """Bug-8384: the Execute half must agree with Discover.

    ``deployed_only=true`` made 409 DEPLOYED_SNAPSHOT_INVALID reachable on the
    Execute-time named-set fetch too. Bug-7254's bare ``raise`` there is loud but
    NOT readable: ``dispatch_xmla`` only converts a downstream 401, so everything
    else leaves the route as an unformatted HTTP 500 and Excel shows a generic
    "connection lost" with no diagnosis -- while a fresh Discover on the SAME
    broken model returns a clear fault. Two surfaces, one broken state,
    contradictory answers.
    """
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

    async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "proj-1", None, None

    async def fake_meta(**kw):
        return (
            [{"id": "m1", "name": "Revenue", "default_agg": "sum"}],
            [{"id": "d1", "name": "Region", "source": "column"}],
            [],
        )

    async def boom(*a, **kw):
        raise _status_error(409)

    monkeypatch.setattr(xs, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xs, "_load_model_metadata_cached", fake_meta)
    monkeypatch.setattr(xs, "get_model_named_sets", boom)

    root = ET.fromstring(exec_xml)
    resp = await xs._handle_execute(
        xs._find_method(root), tenant_slug="acme", jwt_token="tok", session_id="s1",
    )

    body = resp.body.decode()
    assert "Fault" in body, (
        "The 409 escaped _handle_execute; dispatch_xmla re-raises non-401 so the "
        "client gets an unformatted HTTP 500 with no diagnosis."
    )
    assert "DEPLOYED_SNAPSHOT_INVALID" in body
    assert "Redeploy the model" in body
