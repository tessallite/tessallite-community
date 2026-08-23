"""Bug-9216: BI KPI catalogues must read the DEPLOYED set, and only that.

The reported symptom was "seed KPIs unpublished; SPA scorecard live; JDBC
``$KPIs`` and MDSCHEMA_KPIS empty; no modeller publish control". The chain that
has to hold for a published KPI to reach Excel/Power BI is:

  seed/authoring sets ``is_deployed``
    -> ``list_kpis(deployed_only=true)`` filters on it (model-service)
    -> THE GATEWAY ASKS FOR ``deployed_only=true`` on the BI path  <- here
    -> ``$KPIs`` / MDSCHEMA_KPIS are non-empty and match the scorecard

The named-set sibling of this call has had a ``deployed_only`` guard since
Bug-8384; the KPI call did not, so the one link that makes a PUBLISHED KPI
visible to BI clients — and an UNPUBLISHED one invisible — was unguarded.
Dropping the flag would republish every draft to Excel; the model-service side
alone cannot catch that, because the filter it applies is chosen by the caller.

Test escape: the KPI half of the deployed-only contract was only ever asserted
on the model-service side. Guard: this module. Tier: T1 (producer/consumer
contract).
"""
from __future__ import annotations

import pytest

import src.router_client as rc


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    captured: dict = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None):
        _FakeClient.captured = {"url": url, "params": params}
        return _FakeResp(
            [{"name": "Revenue (YTD)", "is_deployed": True}]
        )


@pytest.fixture(autouse=True)
def _patch_httpx(monkeypatch):
    _FakeClient.captured = {}
    monkeypatch.setattr(rc.httpx, "AsyncClient", _FakeClient)


@pytest.mark.asyncio
async def test_bi_kpi_catalogue_requests_deployed_only():
    rows = await rc.get_model_kpis("m1", "acme", "jwt", project_id="p1")
    assert _FakeClient.captured["params"]["deployed_only"] == "true", (
        "the BI KPI catalogue read is not pinned to the deployed set: every "
        "undeployed draft would reach Excel/Power BI through $KPIs and "
        "MDSCHEMA_KPIS (Bug-9216)"
    )
    assert [r["name"] for r in rows] == ["Revenue (YTD)"]


@pytest.mark.asyncio
async def test_the_kpi_endpoint_is_the_one_being_called():
    """A guard on the wrong URL proves nothing."""
    await rc.get_model_kpis("m1", "acme", "jwt", project_id="p1")
    assert _FakeClient.captured["url"].endswith("/models/m1/kpis")
