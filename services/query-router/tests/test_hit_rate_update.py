"""
Tests for aggregate hit-rate crediting (F-004-07).

The hit-credit moved out of ``find_best_aggregate`` (which only *matches*) and
into ``record_aggregate_hit``, which the router calls ONLY after a route has
actually been committed to the aggregate (validation passed, percentile gate
cleared, rewrite succeeded). This prevents crediting aggregates that match but
are subsequently rejected and routed to source — which inflated hit_count /
estimated_hit_rate and skewed eviction ranking.

Run from tessallite/services/query-router/:
    pytest tests/test_hit_rate_update.py
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.routing.aggregate_matcher import find_best_aggregate, record_aggregate_hit


def _savepoint_db(execute_mock: AsyncMock) -> AsyncMock:
    """Build a db mock whose ``begin_nested()`` behaves like a real SAVEPOINT
    async context manager (Bug-6100). ``db.begin_nested`` is a SYNC call that
    returns an async context manager in SQLAlchemy, so it must not be an
    AsyncMock (which would return a coroutine and break ``async with``)."""
    db = AsyncMock()
    db.execute = execute_mock

    @asynccontextmanager
    async def _savepoint():
        yield

    db.begin_nested = MagicMock(side_effect=lambda: _savepoint())
    return db

from conftest import (
    make_measure,
    make_dimension,
    make_agg_col,
    make_aggregate,
    make_bound_query,
)

_PATCH = "src.routing.aggregate_matcher.load_active_aggregates"


def _assert_no_hit_credit_issued(db: AsyncMock) -> None:
    """The matcher must not issue the hit-credit UPDATE (F-004-07).

    This asserts the CONTRACT the module docstring states, rather than the
    older proxy ``db.execute.assert_not_called()``. That proxy read "the
    matcher touched the session at all" as "the matcher credited a hit", so it
    also fired on unrelated reads -- e.g. resolving the tenant setting
    ``query.population_mismatch_reason_mode`` (Bug-8789), which is a READ and
    credits nothing. Inspecting the statements is both narrower (no false
    positive on reads) and stronger: a hit credit issued through some other
    call shape is still caught.

    ANTI-VACUITY: this is a NEGATIVE check -- it passes when it finds nothing --
    so it would pass silently forever if ``aggregate_definitions`` /
    ``hit_count`` were renamed. That is guarded by
    ``test_record_aggregate_hit_issues_update`` below, which asserts those exact
    tokens appear in the REAL ``record_aggregate_hit`` statement, so a rename
    reds that positive test first. Keep the two token sets in sync.
    """
    for call in db.execute.call_args_list:
        if not call.args:
            continue
        try:
            compiled = str(call.args[0].compile())
        except Exception:
            compiled = str(call.args[0])
        assert not (
            "aggregate_definitions" in compiled
            and ("hit_count" in compiled or "estimated_hit_rate" in compiled)
        ), (
            "find_best_aggregate issued a hit-credit UPDATE; the credit belongs "
            f"to record_aggregate_hit after the route commits. Statement: {compiled}"
        )


async def test_matcher_does_not_credit_hit_on_match():
    """find_best_aggregate matches but must NOT issue the hit-credit UPDATE
    (F-004-07: the credit is deferred to the router after the route commits)."""
    m = make_measure("revenue")
    agg = make_aggregate(["country"], [make_agg_col(m)], agg_id="agg-hit-1")
    bq = make_bound_query([make_dimension("country")], [m])

    db = AsyncMock()
    db.execute = AsyncMock(return_value=None)

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, db)

    assert result.aggregate is agg
    _assert_no_hit_credit_issued(db)


async def test_record_aggregate_hit_issues_update():
    """record_aggregate_hit issues the UPDATE that increments hit_count and
    advances estimated_hit_rate toward 1.0."""
    m = make_measure("revenue")
    agg = make_aggregate(["country"], [make_agg_col(m)], agg_id="agg-hit-2")

    db = _savepoint_db(AsyncMock(return_value=None))

    await record_aggregate_hit(agg, db)

    db.execute.assert_called_once()
    # Bug-6100: the UPDATE must run inside a SAVEPOINT.
    db.begin_nested.assert_called_once()
    stmt = db.execute.call_args[0][0]
    compiled = str(stmt.compile())
    assert "aggregate_definitions" in compiled
    assert "hit_count" in compiled
    assert "estimated_hit_rate" in compiled


async def test_record_aggregate_hit_failure_does_not_propagate():
    """Bug-6100: a failing hit-credit UPDATE must be swallowed AND isolated in
    a SAVEPOINT so the outer session survives for the mandatory observation
    writes that follow. The failure must not propagate to the query path."""
    m = make_measure("revenue")
    agg = make_aggregate(["country"], [make_agg_col(m)], agg_id="agg-hit-3")

    failing_execute = AsyncMock(side_effect=RuntimeError("dead connection"))
    db = _savepoint_db(failing_execute)

    # Must not raise — the failure is swallowed after the SAVEPOINT rollback.
    await record_aggregate_hit(agg, db)

    db.begin_nested.assert_called_once()
    failing_execute.assert_called_once()


async def test_no_hit_rate_update_when_no_match():
    """When no aggregate matches, the matcher issues no hit-credit UPDATE."""
    m = make_measure("revenue")
    m_other = make_measure("orders")
    agg = make_aggregate(["country"], [make_agg_col(m_other)], agg_id="agg-miss-1")
    bq = make_bound_query([make_dimension("country")], [m])

    db = AsyncMock()
    db.execute = AsyncMock(return_value=None)

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, db)

    assert result.aggregate is None
    _assert_no_hit_credit_issued(db)
