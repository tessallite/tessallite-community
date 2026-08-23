"""Bug-8383 — sliced governed KPI evaluation uses the user batch contract."""
from __future__ import annotations

import pytest

from src import router_client


class _Response:
    status_code = 200
    text = ""

    def json(self):
        return {
            "results": [
                {"kpi_id": "k-1", "value": 12.5, "status": 1},
            ],
        }


class _Client:
    last: dict = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, **kwargs):
        self.last = {"url": url, **kwargs}
        type(self).last = self.last
        return _Response()


@pytest.mark.asyncio
async def test_batch_kpi_call_forwards_filters_without_publish_marker(monkeypatch):
    monkeypatch.setattr(router_client.httpx, "AsyncClient", lambda **_kw: _Client())
    monkeypatch.setattr(
        router_client.settings,
        "MODEL_SERVICE_URL",
        "http://model-service",
    )

    result = await router_client.evaluate_kpi_batch(
        ["k-1"],
        model_id="m-1",
        project_id="p-1",
        tenant_slug="acme",
        jwt_token="user-jwt",
        filters=[
            {"dimension_id": "d-1", "operator": "eq", "value": "EMEA"},
        ],
        persona_id="persona-1",
    )

    assert result["k-1"]["value"] == 12.5
    assert _Client.last["url"].endswith("/models/m-1/kpis/evaluate-batch")
    assert _Client.last["json"]["filters"][0]["dimension_id"] == "d-1"
    assert _Client.last["params"] == {"persona_id": "persona-1"}
    assert _Client.last["headers"] == {"Authorization": "Bearer user-jwt"}
