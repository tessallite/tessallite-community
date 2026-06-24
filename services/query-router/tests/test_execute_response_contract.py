"""Phase 5.2 contract regression — ExecuteResponse and ExplainResponse.

These tests lock the response envelope so the frontend badge (and
Phase 11 MCP) can rely on the fields being present on every response.
"""
from __future__ import annotations

import pytest

from src.api.routes import ExecuteResponse, ExplainResponse, PipelineTrace


def test_execute_response_includes_route_type_and_reason():
    resp = ExecuteResponse(
        rows=[{"x": 1}],
        columns=["x"],
        route_type="source",
        reason="force_route=source set on request",
        aggregate_id=None,
        pocket_id=None,
        execution_ms=1,
        bytes_processed=0,
        rows_returned=1,
        trace=PipelineTrace(),
    )
    serialised = resp.model_dump()
    assert serialised["route_type"] == "source"
    assert serialised["reason"] == "force_route=source set on request"


def test_execute_response_defaults_reason_to_empty_string():
    """A legacy caller that never sets reason must still deserialise."""
    resp = ExecuteResponse(
        rows=[],
        columns=[],
        route_type="aggregate",
        aggregate_id="agg-1",
        execution_ms=0,
        bytes_processed=0,
        rows_returned=0,
    )
    assert resp.reason == ""
    assert resp.route_type == "aggregate"


def test_explain_response_still_carries_reason():
    resp = ExplainResponse(
        route_type="pocket",
        aggregate_id=None,
        pocket_id="pk-1",
        reason="Matched pocket pk-1",
        rewritten_query="SELECT 1",
        requested_measures=[],
        requested_dimensions=[],
        grain=[],
        query_fingerprint="fp",
    )
    assert resp.route_type == "pocket"
    assert resp.reason.startswith("Matched pocket")


@pytest.mark.parametrize("route_type", ["source", "aggregate", "pocket"])
def test_route_type_accepts_every_known_kind(route_type: str):
    resp = ExecuteResponse(
        rows=[],
        columns=[],
        route_type=route_type,
        aggregate_id=None,
        execution_ms=0,
        bytes_processed=0,
        rows_returned=0,
    )
    assert resp.route_type == route_type
