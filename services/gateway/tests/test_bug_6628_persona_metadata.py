"""Bug-6628: XMLA metadata fetches must forward persona_id so multi-persona
users do not trip resolve_effective_persona's 403.

Same shape as Bug-6263 (named sets): the gateway resolves the persona from the
catalog name and forwards the persona_id as a query parameter to the
model-service metadata endpoints (measures, dimensions, hierarchies). Without
it, multi-persona users see an empty XMLA catalogue because the 403 is
swallowed into empty lists.

This test pins:
  1. persona_id forwarded as query param on measures/dimensions/hierarchies
  2. No persona -> no query param (base business catalog)
  3. 403 from model-service is NOT swallowed — propagated for XMLA fault
"""
from __future__ import annotations

import pytest
import httpx

import src.router_client as rc


class _FakeResp:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload or []
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            response = httpx.Response(
                status_code=self.status_code,
                request=httpx.Request("GET", "http://fake"),
            )
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=response.request,
                response=response,
            )

    def json(self):
        return self._payload


class _FakeClient:
    """Captures the params passed to .get and returns a canned payload."""

    captured: dict = {}
    _response: _FakeResp | None = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None):
        _FakeClient.captured = {"url": url, "params": params}
        if _FakeClient._response is not None:
            return _FakeClient._response
        return _FakeResp([{"name": "Revenue"}])


@pytest.fixture(autouse=True)
def _patch_httpx(monkeypatch):
    _FakeClient.captured = {}
    _FakeClient._response = None
    monkeypatch.setattr(rc.httpx, "AsyncClient", _FakeClient)


# --- measures ---

@pytest.mark.asyncio
async def test_measures_persona_forwarded():
    await rc.get_model_measures(
        "m1", "acme", "jwt", project_id="p1", persona_id="persona-abc",
    )
    assert _FakeClient.captured["params"] == {"persona_id": "persona-abc"}


@pytest.mark.asyncio
async def test_measures_no_persona_no_param():
    await rc.get_model_measures("m1", "acme", "jwt", project_id="p1")
    assert _FakeClient.captured["params"] is None


# --- dimensions ---

@pytest.mark.asyncio
async def test_dimensions_persona_forwarded():
    await rc.get_model_dimensions(
        "m1", "acme", "jwt", project_id="p1", persona_id="persona-xyz",
    )
    assert _FakeClient.captured["params"] == {"persona_id": "persona-xyz"}


@pytest.mark.asyncio
async def test_dimensions_no_persona_no_param():
    await rc.get_model_dimensions("m1", "acme", "jwt", project_id="p1")
    assert _FakeClient.captured["params"] is None


# --- hierarchies ---

@pytest.mark.asyncio
async def test_hierarchies_persona_forwarded():
    await rc.get_model_hierarchies(
        "m1", "acme", "jwt", project_id="p1",
        include_details=False, persona_id="persona-tech",
    )
    assert _FakeClient.captured["params"] == {"persona_id": "persona-tech"}


@pytest.mark.asyncio
async def test_hierarchies_no_persona_no_param():
    await rc.get_model_hierarchies(
        "m1", "acme", "jwt", project_id="p1", include_details=False,
    )
    assert _FakeClient.captured["params"] is None


# --- Bug-6800 / 6801(a): persona_id forwarded on the per-hierarchy DETAIL fetch ---


class _RecordingClient:
    """Records EVERY .get call (url + params) so the list AND per-hierarchy
    detail fetches can both be asserted."""

    calls: list[dict] = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None):
        _RecordingClient.calls.append({"url": url, "params": params})
        # The list endpoint returns one hierarchy summary; the detail endpoint
        # (url ends with the hierarchy id) returns its full payload.
        if url.endswith("/hierarchies"):
            return _FakeResp([{"id": "hier-1", "name": "Calendar"}])
        return _FakeResp({"id": "hier-1", "name": "Calendar", "levels": []})


@pytest.mark.asyncio
async def test_hierarchy_detail_fetch_forwards_persona(monkeypatch):
    """Bug-6800 / Bug-6801(a): with include_details=True the per-hierarchy
    DETAIL GET must forward persona_id, or the detail's level list is NOT
    filtered to the persona's excluded levels — the gateway then advertises
    persona-denied levels (403 at Execute) and indexes expand_level into an
    unfiltered list that diverges from the model-service preview."""
    _RecordingClient.calls = []
    monkeypatch.setattr(rc.httpx, "AsyncClient", _RecordingClient)

    await rc.get_model_hierarchies(
        "m1", "acme", "jwt", project_id="p1",
        include_details=True, persona_id="persona-restricted",
    )

    detail_calls = [c for c in _RecordingClient.calls if c["url"].endswith("/hier-1")]
    assert detail_calls, "the per-hierarchy detail endpoint was never called"
    assert detail_calls[0]["params"] == {"persona_id": "persona-restricted"}, (
        "the hierarchy DETAIL fetch dropped persona_id, so persona-excluded "
        "levels are not filtered from the detailed level list (Bug-6801a)"
    )


@pytest.mark.asyncio
async def test_hierarchy_detail_fetch_no_persona_no_param(monkeypatch):
    """No persona -> the detail fetch carries no persona param (business base)."""
    _RecordingClient.calls = []
    monkeypatch.setattr(rc.httpx, "AsyncClient", _RecordingClient)

    await rc.get_model_hierarchies(
        "m1", "acme", "jwt", project_id="p1", include_details=True,
    )

    detail_calls = [c for c in _RecordingClient.calls if c["url"].endswith("/hier-1")]
    assert detail_calls, "the per-hierarchy detail endpoint was never called"
    assert detail_calls[0]["params"] is None


# --- 403 not swallowed ---

@pytest.mark.asyncio
async def test_measures_403_propagates():
    _FakeClient._response = _FakeResp(status_code=403)
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await rc.get_model_measures(
            "m1", "acme", "jwt", project_id="p1", persona_id="persona-abc",
        )
    assert exc_info.value.response.status_code == 403
