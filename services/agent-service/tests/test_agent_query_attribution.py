"""Bug-5985 / F-030-01 -- agent-executed queries must be attributable as
agent traffic in QueryLog/Usage Analytics, not misclassified as generic
JDBC traffic.

``execute_query`` (the agent-service execution chokepoint) posts to the
query-router's ``/api/v1/execute``. ``protocol`` must stay ``"jdbc"`` (the
parser's strict-syntax/GROUP BY enforcement keys off it -- see Bug-6012),
but ``client_kind`` must carry ``"agent"`` so the query-router's shared
observed-execution path (``execute_with_observation`` ->
``record_query_success`` -> ``query_logger.log_query``) persists the
correct attribution on the QueryLog row, the same way ``client_kind`` is
already used for headless/plugin/looker traffic.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.exec.query import PersonaScopeViolationError, execute_query
from .test_execution_scope_enforcement import ALLOWED_MODEL, _call, _scope


def _model() -> types.SimpleNamespace:
    return types.SimpleNamespace(id=ALLOWED_MODEL, slug="test-model")


class _FakeResponse:
    status_code = 200

    def json(self):
        return {
            "rows": [{"revenue": 100}],
            "columns": ["revenue"],
            "route_type": "source",
        }


class _FakeAsyncClient:
    """Captures the JSON body posted to query-router's /api/v1/execute."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeAsyncClient.captured = {"url": url, "json": json, "headers": headers}
        return _FakeResponse()


@pytest.mark.asyncio
async def test_execute_query_sends_agent_client_kind_and_jdbc_protocol():
    db = AsyncMock()
    db.get = AsyncMock(return_value=_model())
    meas_result = MagicMock()
    meas_result.all.return_value = [("revenue", "SUM")]
    db.execute = AsyncMock(return_value=meas_result)

    with patch("src.exec.query.httpx.AsyncClient", _FakeAsyncClient):
        await execute_query(
            db, _call(), "jwt",
            allowed_model_ids={ALLOWED_MODEL},
            persona_scopes=_scope(),
        )

    body = _FakeAsyncClient.captured["json"]
    assert body["client_kind"] == "agent"
    # protocol must stay "jdbc" -- the JDBC-strict parser branches (Bug-6012)
    # key off this value; a non-jdbc protocol would silently skip them.
    assert body["protocol"] == "jdbc"
    # Bug-9196/F01: this UUID would be a ProjectPersona id in agent-service,
    # while ExecuteRequest.persona_id is a model Persona id in query-router.
    # The two namespaces remain independent; the JWT is the only model-persona
    # authority crossing this boundary.
    assert "persona_id" not in body
    assert _FakeAsyncClient.captured["headers"] == {
        "Authorization": "Bearer jwt"
    }


@pytest.mark.asyncio
async def test_execute_query_preserves_unscoped_none_persona_contract():
    db = AsyncMock()
    db.get = AsyncMock(return_value=_model())
    meas_result = MagicMock()
    meas_result.all.return_value = [("revenue", "SUM")]
    db.execute = AsyncMock(return_value=meas_result)

    with patch("src.exec.query.httpx.AsyncClient", _FakeAsyncClient):
        await execute_query(
            db, _call(), "jwt",
            allowed_model_ids={ALLOWED_MODEL},
            persona_scopes=None,
        )

    assert "persona_id" not in _FakeAsyncClient.captured["json"]


@pytest.mark.asyncio
async def test_project_persona_scope_is_local_and_disjoint_from_model_persona():
    """Bug-9196/F01: deliberately disjoint UUID namespaces stay separate.

    The project persona constrains fields before any router call. The model
    persona is represented only by the authenticated JWT and is not copied into
    ExecuteRequest.persona_id, whose UUID namespace belongs to query-router's
    model Persona table.
    """
    project_persona_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    model_persona_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
    assert project_persona_id != model_persona_id

    db = AsyncMock()
    with pytest.raises(PersonaScopeViolationError):
        await execute_query(
            db,
            _call(measures=["salary"]),
            f"jwt-with-model-persona-{model_persona_id}",
            allowed_model_ids={ALLOWED_MODEL},
            persona_scopes=_scope(),
        )
    db.get.assert_not_awaited()

    db.get = AsyncMock(return_value=_model())
    meas_result = MagicMock()
    meas_result.all.return_value = [("revenue", "SUM")]
    db.execute = AsyncMock(return_value=meas_result)
    with patch("src.exec.query.httpx.AsyncClient", _FakeAsyncClient):
        await execute_query(
            db,
            _call(),
            f"jwt-with-model-persona-{model_persona_id}",
            allowed_model_ids={ALLOWED_MODEL},
            persona_scopes=_scope(),
        )
    body = _FakeAsyncClient.captured["json"]
    assert "persona_id" not in body
    assert _FakeAsyncClient.captured["headers"] == {
        "Authorization": f"Bearer jwt-with-model-persona-{model_persona_id}"
    }
