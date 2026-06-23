"""Bug-378: aggregate/pocket route with missing target metadata must
raise ValueError, not silently fall through to source execution.
"""
from __future__ import annotations

import types
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from src.ir.logical_query import RouteDecision


pytestmark = pytest.mark.unit


@dataclass
class _FakeBound:
    logical_query: types.SimpleNamespace
    model: types.SimpleNamespace


def _bound():
    return _FakeBound(
        logical_query=types.SimpleNamespace(
            model_id="model-1",
            raw_query="SELECT 1",
        ),
        model=types.SimpleNamespace(id="model-1", slug="test"),
    )


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
