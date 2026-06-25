"""Bug-378: aggregate/pocket route with missing target metadata must
raise ValueError, not silently fall through to source execution.

Bug-5325: aggregate/pocket TARGET and normal-query SOURCE execution paths must
fail closed when the resolved ProjectConnection belongs to a DIFFERENT project
than the model owning the query — a cross-project connection must never execute.
"""
from __future__ import annotations

import types
import uuid
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from shared.connection_scope import CrossProjectConnectionError
from shared.db.models import (
    AggregateDefinition,
    DataSource,
    DataTarget,
    PocketDefinition,
    ProjectConnection,
)
from src.ir.logical_query import RouteDecision


pytestmark = pytest.mark.unit


@dataclass
class _FakeBound:
    logical_query: types.SimpleNamespace
    model: types.SimpleNamespace


def _bound(project_id=None):
    return _FakeBound(
        logical_query=types.SimpleNamespace(
            model_id="model-1",
            raw_query="SELECT 1",
        ),
        model=types.SimpleNamespace(
            id="model-1", slug="test", project_id=project_id or uuid.uuid4()
        ),
    )


class _MappedDB:
    """Fake async DB that returns objects keyed by ORM class via ``get``."""

    def __init__(self, mapping):
        # mapping: {ORM class: object}
        self._mapping = mapping

    async def get(self, cls, key):
        return self._mapping.get(cls)


async def test_aggregate_route_missing_target_errors():
    """When aggregate metadata is missing, _execute must raise ValueError
    instead of falling through to source."""
    from src.api.routes import execute_routed_query as _execute

    decision = RouteDecision(
        route_type="aggregate",
        rewritten_query="SELECT sum(revenue) FROM agg_missing",
        reason="aggregate match",
        aggregate_id="agg-nonexistent",
    )

    db = AsyncMock()
    db.get = AsyncMock(return_value=None)

    with pytest.raises(ValueError, match="AggregateDefinition.*not found"):
        await _execute(_bound(), decision, db)


async def test_pocket_route_missing_target_errors():
    """When pocket metadata is missing, _execute must raise ValueError."""
    from src.api.routes import execute_routed_query as _execute

    decision = RouteDecision(
        route_type="pocket",
        rewritten_query="SELECT * FROM pocket_missing",
        reason="pocket match",
        pocket_id="pocket-nonexistent",
    )

    db = AsyncMock()
    db.get = AsyncMock(return_value=None)

    with pytest.raises(ValueError, match="PocketDefinition.*not found"):
        await _execute(_bound(), decision, db)


# ---------------------------------------------------------------------------
# Bug-5325 — cross-project connection must fail closed at every execution site.
# These tests do NOT stub the resolver under test (resolve_endpoint_connection);
# they feed a connection whose project_id differs from the model's project_id
# and assert execution is rejected BEFORE execute_on_connection runs.
# ---------------------------------------------------------------------------


async def test_aggregate_target_cross_project_connection_rejected(monkeypatch):
    """An aggregate target whose connection belongs to another project must be
    rejected before execute_on_connection — no cross-project execution."""
    from src.api import routes as routes_mod
    from src.api.routes import execute_routed_query as _execute

    model_project = uuid.uuid4()
    other_project = uuid.uuid4()
    target = DataTarget(
        model_id="model-1", project_connection_id=uuid.uuid4()
    )
    conn = ProjectConnection(project_id=other_project)
    agg = AggregateDefinition(target_id=uuid.uuid4())
    db = _MappedDB({
        AggregateDefinition: agg,
        DataTarget: target,
        ProjectConnection: conn,
    })

    exec_spy = AsyncMock(return_value=([], 0, []))
    monkeypatch.setattr(routes_mod, "execute_on_connection", exec_spy)

    decision = RouteDecision(
        route_type="aggregate",
        rewritten_query="SELECT 1",
        reason="agg",
        aggregate_id="agg-1",
    )
    with pytest.raises(CrossProjectConnectionError):
        await _execute(_bound(project_id=model_project), decision, db)
    exec_spy.assert_not_called()


async def test_pocket_target_cross_project_connection_rejected(monkeypatch):
    """A pocket target whose connection belongs to another project must be
    rejected before execute_on_connection."""
    from src.api import routes as routes_mod
    from src.api.routes import execute_routed_query as _execute

    model_project = uuid.uuid4()
    other_project = uuid.uuid4()
    target = DataTarget(model_id="model-1", project_connection_id=uuid.uuid4())
    conn = ProjectConnection(project_id=other_project)
    pocket = PocketDefinition(target_id=uuid.uuid4())
    db = _MappedDB({
        PocketDefinition: pocket,
        DataTarget: target,
        ProjectConnection: conn,
    })

    exec_spy = AsyncMock(return_value=([], 0, []))
    monkeypatch.setattr(routes_mod, "execute_on_connection", exec_spy)

    decision = RouteDecision(
        route_type="pocket",
        rewritten_query="SELECT 1",
        reason="pocket",
        pocket_id="pocket-1",
    )
    with pytest.raises(CrossProjectConnectionError):
        await _execute(_bound(project_id=model_project), decision, db)
    exec_spy.assert_not_called()


async def test_source_cross_project_connection_rejected(monkeypatch):
    """A normal source-route query whose source connection belongs to another
    project must be rejected before execute_on_connection."""
    from src.api import routes as routes_mod
    from src.api.routes import execute_routed_query as _execute

    model_project = uuid.uuid4()
    other_project = uuid.uuid4()
    source = DataSource(model_id="model-1", project_connection_id=uuid.uuid4())
    conn = ProjectConnection(project_id=other_project)
    db = _MappedDB({ProjectConnection: conn})

    # _resolve_query_source issues its own DB queries; bypass it and hand back
    # the source directly so the test isolates the connection guard.
    async def _fake_resolve_source(model_id, _db, *, bound=None):
        return source

    monkeypatch.setattr(routes_mod, "_resolve_query_source", _fake_resolve_source)
    exec_spy = AsyncMock(return_value=([], 0, []))
    monkeypatch.setattr(routes_mod, "execute_on_connection", exec_spy)

    decision = RouteDecision(
        route_type="source",
        rewritten_query="SELECT 1",
        reason="source",
    )
    with pytest.raises(CrossProjectConnectionError):
        await _execute(_bound(project_id=model_project), decision, db)
    exec_spy.assert_not_called()


async def test_source_same_project_connection_executes(monkeypatch):
    """Allow-path: a source connection in the SAME project executes normally."""
    from src.api import routes as routes_mod
    from src.api.routes import execute_routed_query as _execute

    model_project = uuid.uuid4()
    source = DataSource(model_id="model-1", project_connection_id=uuid.uuid4())
    conn = ProjectConnection(project_id=model_project)
    db = _MappedDB({ProjectConnection: conn})

    async def _fake_resolve_source(model_id, _db, *, bound=None):
        return source

    monkeypatch.setattr(routes_mod, "_resolve_query_source", _fake_resolve_source)
    exec_spy = AsyncMock(return_value=([{"x": 1}], 1, ["x"]))
    monkeypatch.setattr(routes_mod, "execute_on_connection", exec_spy)

    decision = RouteDecision(
        route_type="source",
        rewritten_query="SELECT 1",
        reason="source",
    )
    rows, _bytes, cols, endpoint = await _execute(
        _bound(project_id=model_project), decision, db
    )
    exec_spy.assert_called_once()
    assert rows == [{"x": 1}]
    assert cols == ["x"]
    assert endpoint is source


async def test_cross_project_connection_maps_to_422_at_handler(monkeypatch):
    """Bug-5325: when execute_routed_query raises CrossProjectConnectionError
    (the guard fired before any SQL ran), execute_with_observation must surface
    it as a clean 422 — NOT fall through to the generic 502 execution path."""
    from fastapi import HTTPException
    from src.api import routes as routes_mod
    from src.api.routes import execute_with_observation

    # Neutralise the guardrails that run before execution so the test isolates
    # the error-mapping behaviour of the execution try/except.
    monkeypatch.setattr(
        routes_mod, "resolve_filter_anchors", AsyncMock(return_value={})
    )
    monkeypatch.setattr(routes_mod, "audit_filters_present", lambda *a, **k: None)
    monkeypatch.setattr(
        routes_mod, "_log_query_failure", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        routes_mod,
        "execute_routed_query",
        AsyncMock(side_effect=CrossProjectConnectionError("wrong project")),
    )

    decision = RouteDecision(
        route_type="source",
        rewritten_query="SELECT 1",
        reason="source",
    )

    with pytest.raises(HTTPException) as exc:
        await execute_with_observation(
            bound=_bound(),
            decision=decision,
            db=_MappedDB({}),
            user_identity="user@example.com",
            tenant_id="tenant-1",
        )

    assert exc.value.status_code == 422
    assert "different project" in exc.value.detail
