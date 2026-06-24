"""Router-level tests for the Phase 5.2 force-live (`force_route="source"`) toggle.

Invariants under test:
  * ``force_route="source"`` forces ``route_type="source"`` even when an
    aggregate would have covered the query — aggregate loader is not
    invoked.
  * Row-security still runs first: a principal with an active rule
    gets the wrap regardless of ``force_route``.
  * The response ``reason`` identifies the force-live path so the UI
    badge tooltip can surface it verbatim.
  * Unknown ``force_route`` values are rejected by the HTTP handler's
    validator — tested via the helper directly.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from fastapi import HTTPException

from src.api.routes import _validate_force_route
from src.routing.router import route_query
from src.security import Principal

from conftest import make_aggregate, make_agg_col, make_dimension, make_measure
from test_query_flow import _bind
from test_row_security_routing import _db_returning, _role_rule

_PATCH_LOAD = "src.routing.aggregate_matcher.load_active_aggregates"

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Core invariant: force_route="source" bypasses aggregate even when one matches
# ---------------------------------------------------------------------------


async def test_force_route_source_bypasses_aggregate_path():
    m = make_measure("revenue")
    d = make_dimension("region_code")
    agg = make_aggregate(["region_code"], [make_agg_col(m)])

    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])

    db = _db_returning([])  # no row-security rules

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        decision = await route_query(bq, db, force_route="source")

    assert decision.route_type == "source"
    assert decision.aggregate_id is None
    assert decision.pocket_id is None
    assert "force_route=source" in decision.reason
    # The aggregate loader must not be touched on the force-live path.
    mock_load.assert_not_called()


async def test_force_route_none_takes_normal_path():
    m = make_measure("revenue")
    d = make_dimension("region_code")
    agg = make_aggregate(["region_code"], [make_agg_col(m)])

    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])

    db = _db_returning([])

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        decision = await route_query(bq, db)  # force_route omitted

    assert decision.route_type == "aggregate"
    assert decision.aggregate_id == str(agg.id)


# ---------------------------------------------------------------------------
# Row-security is evaluated before force_route — security predicate is applied
# ---------------------------------------------------------------------------


async def test_row_security_applied_when_force_route_source():
    m = make_measure("revenue")
    d = make_dimension("region_code")

    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])

    rule = _role_rule(
        "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')",
        ["region_manager_north"],
    )
    principal = Principal(
        user_identity="alice@x", roles=frozenset({"region_manager_north"})
    )
    db = _db_returning([rule])

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        decision = await route_query(
            bq, db, principal=principal, force_route="source"
        )

    # Row security owns the reason string; force-live cannot silence it.
    # Bug-915: inject places predicate before LIMIT; check predicate is present.
    assert decision.route_type == "source"
    assert "NORTH" in decision.rewritten_query  # security predicate is injected
    assert "Row security active" in decision.reason
    mock_load.assert_not_called()


# ---------------------------------------------------------------------------
# HTTP boundary: _validate_force_route rejects non-"source" values
# ---------------------------------------------------------------------------


def test_validate_force_route_accepts_none():
    _validate_force_route(None)


def test_validate_force_route_accepts_source():
    _validate_force_route("source")


def test_validate_force_route_accepts_aggregate():
    _validate_force_route("aggregate")


def test_validate_force_route_accepts_pocket():
    _validate_force_route("pocket")


def test_validate_force_route_rejects_empty_string():
    with pytest.raises(HTTPException) as exc:
        _validate_force_route("")
    assert exc.value.status_code == 422


def test_validate_force_route_rejects_unknown_value():
    with pytest.raises(HTTPException):
        _validate_force_route("nonexistent_route")


# ---------------------------------------------------------------------------
# F-004-08 — force_route="aggregate"/"pocket" pin a SPECIFIC path
# ---------------------------------------------------------------------------

_PATCH_POCKET = "src.routing.router.find_best_pocket"


async def test_force_aggregate_skips_pocket_even_when_pocket_matches():
    """force_route="aggregate" must NOT return a pocket route even when a pocket
    matches — the pocket matcher is skipped entirely so the forced aggregate
    path is honoured."""
    from src.ir.logical_query import PocketMatchResult

    m = make_measure("revenue")
    d = make_dimension("region_code")
    agg = make_aggregate(["region_code"], [make_agg_col(m)])
    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])
    db = _db_returning([])

    pocket = types.SimpleNamespace(id=uuid.uuid4(), target_id=None)
    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load, \
            patch(_PATCH_POCKET, new_callable=AsyncMock) as mock_pocket:
        mock_load.return_value = [agg]
        mock_pocket.return_value = PocketMatchResult(pocket=pocket)
        decision = await route_query(bq, db, force_route="aggregate")

    assert decision.route_type == "aggregate"
    assert decision.aggregate_id == str(agg.id)
    # The pocket matcher was never consulted under force_route="aggregate".
    mock_pocket.assert_not_called()


async def test_force_aggregate_no_match_raises():
    """force_route="aggregate" with no covering aggregate raises rather than
    silently returning source."""
    from src.ir.logical_query import NoAggregateMatchError

    m = make_measure("revenue")
    d = make_dimension("region_code")
    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])
    db = _db_returning([])

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = []  # no aggregate covers the query
        with pytest.raises(NoAggregateMatchError):
            await route_query(bq, db, force_route="aggregate")


async def test_force_pocket_skips_aggregate_matcher():
    """force_route="pocket" with no pocket but a matching aggregate must raise,
    not silently return the aggregate."""
    from src.ir.logical_query import NoAggregateMatchError, PocketMatchResult

    m = make_measure("revenue")
    d = make_dimension("region_code")
    agg = make_aggregate(["region_code"], [make_agg_col(m)])
    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])
    db = _db_returning([])

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load, \
            patch(_PATCH_POCKET, new_callable=AsyncMock) as mock_pocket:
        mock_load.return_value = [agg]
        mock_pocket.return_value = PocketMatchResult(skipped_reason="no_candidates")
        with pytest.raises(NoAggregateMatchError):
            await route_query(bq, db, force_route="pocket")
    # The aggregate matcher must not be consulted under force_route="pocket".
    mock_load.assert_not_called()


async def test_force_aggregate_incompatible_with_row_security_raises():
    """A force to a fast path is incompatible with active row-security (which
    requires the source route); raise rather than weaken security or silently
    serve source."""
    from src.ir.logical_query import NoAggregateMatchError

    m = make_measure("revenue")
    d = make_dimension("region_code")
    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])

    rule = _role_rule(
        "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')",
        ["region_manager_north"],
    )
    principal = Principal(
        user_identity="alice@x", roles=frozenset({"region_manager_north"})
    )
    db = _db_returning([rule])

    with patch(_PATCH_LOAD, new_callable=AsyncMock):
        with pytest.raises(NoAggregateMatchError):
            await route_query(
                bq, db, principal=principal, force_route="aggregate"
            )
