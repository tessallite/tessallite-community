"""Tests for SELECT-vs-GROUP-BY semantic validation (C4).

The router rejects queries where a non-aggregated SELECT dimension
is absent from GROUP BY when aggregate measures are present.
This validates at the routing layer as a safety net beyond the
parser-level GroupByError (which only covers JDBC protocol).
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from src.routing.router import route_query
from src.ir.logical_query import BoundQuery, LogicalQuery, PocketMatchResult

from conftest import make_measure, make_dimension

pytestmark = pytest.mark.integration


def _make_bq(
    requested_dims: list[str],
    grain: list[str],
    measures: list,
    dimensions: list,
) -> BoundQuery:
    lq = LogicalQuery(
        model_id="model-1",
        protocol="dax",
        raw_query="SELECT ...",
        requested_measures=[m.name for m in measures],
        requested_dimensions=requested_dims,
        filters=[],
        grain=grain,
        order_by=[],
        limit=None,
        offset=None,
        query_fingerprint="abc123",
    )
    model = types.SimpleNamespace(id="model-1", slug="test")
    return BoundQuery(
        logical_query=lq,
        model=model,
        resolved_measures=measures,
        resolved_dimensions=dimensions,
        resolved_filters=[],
    )


async def test_select_dim_not_in_group_by_raises_422():
    m = make_measure("revenue")
    d_region = make_dimension("region")
    d_city = make_dimension("city")

    bq = _make_bq(
        requested_dims=["region", "city"],
        grain=["region"],
        measures=[m],
        dimensions=[d_region, d_city],
    )

    with pytest.raises(HTTPException) as exc_info:
        await route_query(bq, AsyncMock())

    assert exc_info.value.status_code == 422
    assert "city" in str(exc_info.value.detail)
    assert "GROUP BY" in str(exc_info.value.detail)


async def test_all_select_dims_in_group_by_passes_validation():
    m = make_measure("revenue")
    d_region = make_dimension("region")

    bq = _make_bq(
        requested_dims=["region"],
        grain=["region"],
        measures=[m],
        dimensions=[d_region],
    )

    db = AsyncMock()
    no_aggregate = types.SimpleNamespace(aggregate=None, skip_reasons=[])
    with (
        patch(
            "src.routing.router.find_best_pocket",
            new=AsyncMock(return_value=PocketMatchResult(skipped_reason="no_candidates")),
        ),
        patch("src.routing.router.find_best_aggregate", new=AsyncMock(return_value=no_aggregate)),
        patch("src.routing.router.rewrite_for_source", new=AsyncMock(return_value="SELECT 1")),
    ):
        await route_query(bq, db)


async def test_no_measures_skips_validation():
    """Queries without aggregate measures should not be validated."""
    d_region = make_dimension("region")
    d_city = make_dimension("city")

    bq = _make_bq(
        requested_dims=["region", "city"],
        grain=["region"],
        measures=[],
        dimensions=[d_region, d_city],
    )

    db = AsyncMock()
    no_aggregate = types.SimpleNamespace(aggregate=None, skip_reasons=[])
    with (
        patch(
            "src.routing.router.find_best_pocket",
            new=AsyncMock(return_value=PocketMatchResult(skipped_reason="no_candidates")),
        ),
        patch("src.routing.router.find_best_aggregate", new=AsyncMock(return_value=no_aggregate)),
        patch("src.routing.router.rewrite_for_source", new=AsyncMock(return_value="SELECT 1")),
    ):
        await route_query(bq, db)


async def test_empty_group_by_with_measures_raises_422():
    """SELECT dim, SUM(measure) with no GROUP BY should be rejected."""
    m = make_measure("revenue")
    d_city = make_dimension("city")

    bq = _make_bq(
        requested_dims=["city"],
        grain=[],
        measures=[m],
        dimensions=[d_city],
    )

    with pytest.raises(HTTPException) as exc_info:
        await route_query(bq, AsyncMock())

    assert exc_info.value.status_code == 422
    assert "city" in str(exc_info.value.detail)
    assert "GROUP BY" in str(exc_info.value.detail)
