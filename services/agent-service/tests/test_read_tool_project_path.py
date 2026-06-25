"""F-023-09 — evaluate_kpi / preview_named_set must call model-service with
the real project id in the URL path.

The branches previously built ``/api/v1/projects/_/models/...``; model-service
validates the ``project_id`` path segment as a UUID, so the literal ``_``
returned HTTP 422 before any handler ran and both advertised tools were dead on
arrival. These tests assert the real project id is threaded into the URL and a
2xx response from model-service is narrated as a successful turn.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.pipeline import (
    _run_evaluate_kpi_branch,
    _run_preview_named_set_branch,
)
from src.tools.spec import EvaluateKpiToolCall, PreviewNamedSetToolCall


PROJECT_ID = uuid.uuid4()
MODEL_ID = str(uuid.uuid4())
KPI_ID = str(uuid.uuid4())
NAMED_SET_ID = str(uuid.uuid4())


def _cfg():
    return types.SimpleNamespace(
        project_id=PROJECT_ID,
        agent_output_format="plain",
        disclosure_text=None,
    )


def _mock_client(captured: dict, payload: dict, status_code: int = 200):
    """Build a mock httpx.AsyncClient context manager capturing the POST URL."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=payload)
    resp.text = ""

    async def _post(url, *args, **kwargs):
        captured["url"] = url
        return resp

    client = MagicMock()
    client.post = AsyncMock(side_effect=_post)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx)


@pytest.mark.asyncio
async def test_evaluate_kpi_url_uses_real_project_id():
    captured: dict = {}
    payload = {
        "formatted_value": "USD 1,000.00",
        "formatted_goal": "USD 1,200.00",
        "status_label": "On track",
        "trend_label": "Up",
    }
    with (
        patch("src.pipeline.httpx.AsyncClient", _mock_client(captured, payload)),
        patch("src.pipeline.apply_output_guardrails",
              lambda cfg, text: types.SimpleNamespace(text=text, actions=[])),
        patch("src.pipeline._narration_publisher", lambda cfg, pub: None),
    ):
        outcome = await _run_evaluate_kpi_branch(
            _cfg(),
            EvaluateKpiToolCall(model_id=MODEL_ID, kpi_id=KPI_ID),
            "jwt", None, project_id=PROJECT_ID,
        )

    assert "/projects/_/" not in captured["url"]
    assert f"/projects/{PROJECT_ID}/models/{MODEL_ID}/kpis/{KPI_ID}/evaluate" in captured["url"]
    assert outcome.status == "ok"
    assert "USD 1,000.00" in outcome.answer_text


@pytest.mark.asyncio
async def test_preview_named_set_url_uses_real_project_id():
    captured: dict = {}
    payload = {
        "items": [{"caption": "North"}, {"caption": "South"}],
        "total_count": 2,
        "truncated": False,
    }
    with (
        patch("src.pipeline.httpx.AsyncClient", _mock_client(captured, payload)),
        patch("src.pipeline.apply_output_guardrails",
              lambda cfg, text: types.SimpleNamespace(text=text, actions=[])),
        patch("src.pipeline._narration_publisher", lambda cfg, pub: None),
    ):
        outcome = await _run_preview_named_set_branch(
            _cfg(),
            PreviewNamedSetToolCall(model_id=MODEL_ID, named_set_id=NAMED_SET_ID),
            "jwt", None, project_id=PROJECT_ID,
        )

    assert "/projects/_/" not in captured["url"]
    assert (
        f"/projects/{PROJECT_ID}/models/{MODEL_ID}/named-sets/{NAMED_SET_ID}/preview"
        in captured["url"]
    )
    assert outcome.status == "ok"
    assert "2 members" in outcome.answer_text


@pytest.mark.asyncio
async def test_evaluate_kpi_falls_back_to_cfg_project_id():
    """When the caller omits project_id, the branch uses cfg.project_id."""
    captured: dict = {}
    payload = {"formatted_value": "10"}
    with (
        patch("src.pipeline.httpx.AsyncClient", _mock_client(captured, payload)),
        patch("src.pipeline.apply_output_guardrails",
              lambda cfg, text: types.SimpleNamespace(text=text, actions=[])),
        patch("src.pipeline._narration_publisher", lambda cfg, pub: None),
    ):
        await _run_evaluate_kpi_branch(
            _cfg(),
            EvaluateKpiToolCall(model_id=MODEL_ID, kpi_id=KPI_ID),
            "jwt", None,
        )
    assert f"/projects/{PROJECT_ID}/" in captured["url"]
