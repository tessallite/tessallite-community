"""
Tests for Bug-5195 hit-credit wiring: aggregate hit_count must be credited
ONLY after the routed query executes successfully, exactly once, and never
on failure.

The router sets ``pending_hit_credit`` on the RouteDecision but does NOT
call ``record_aggregate_hit`` itself. The shared execution pipeline
(``execute_with_observation``) consumes ``pending_hit_credit`` after
successful execution + security audits.

Run from tessallite/services/query-router/:
    pytest tests/test_hit_credit_wiring.py -v
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ir.logical_query import RouteDecision
from src.routing.aggregate_matcher import record_aggregate_hit

from conftest import (
    make_aggregate,
    make_agg_col,
    make_bound_query,
    make_dimension,
    make_measure,
)


# ---------------------------------------------------------------------------
# 1. Router does NOT credit at route time (no pre-execution credit)
# ---------------------------------------------------------------------------

class TestRouterNoPreExecutionCredit:
    """The router must set pending_hit_credit but never call
    record_aggregate_hit itself."""

    async def test_router_does_not_call_record_aggregate_hit(self):
        """An aggregate route must NOT credit hit_count in the router."""
        from src.routing.router import route_query
        from src.routing.aggregate_matcher import AggregateMatchResult

        m = make_measure("revenue")
        agg = make_aggregate(["country"], [make_agg_col(m)])
        bq = make_bound_query([make_dimension("country")], [m])
        db = AsyncMock()
        db.execute = AsyncMock(return_value=None)
        db.get = AsyncMock(return_value=None)

        with (
            patch(
                "src.routing.router.find_best_aggregate",
                new_callable=AsyncMock,
                return_value=AggregateMatchResult(aggregate=agg),
            ),
            patch(
                "src.routing.router.find_best_pocket",
                new_callable=AsyncMock,
                return_value=types.SimpleNamespace(pocket=None, skipped_reason=None),
            ),
            patch(
                "src.routing.router.rewrite_for_aggregate",
                return_value="SELECT SUM(revenue__sum) FROM agg_table",
            ),
            patch(
                "src.routing.router.validate_aggregate_route",
                return_value=(True, "ok"),
            ),
            patch(
                "src.routing.router._resolve_aggregate_target_dialect",
                new_callable=AsyncMock,
                return_value="postgres",
            ),
            patch(
                "src.routing.router._resolve_aggregate_source_dialect",
                new_callable=AsyncMock,
                return_value="postgres",
            ),
            patch(
                "src.routing.router._ensure_valid_user_defined_attributes",
                new_callable=AsyncMock,
            ),
            patch(
                "src.routing.router.compile_row_security",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "src.routing.router._check_column_restrictions",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            decision = await route_query(bq, db)

        assert decision.route_type == "aggregate"
        assert decision.pending_hit_credit is agg
        # The router must NOT have issued an UPDATE (record_aggregate_hit).
        # db.execute is used for other lookups; check that no UPDATE on
        # aggregate_definitions was issued.
        for call in db.execute.call_args_list:
            stmt = call[0][0] if call[0] else None
            if stmt is not None:
                compiled = str(getattr(stmt, "compile", lambda: stmt)())
                assert "hit_count" not in compiled, (
                    "Router must not issue a hit_count UPDATE (record_aggregate_hit)"
                )


# ---------------------------------------------------------------------------
# 2. execute_with_observation credits exactly once on success
# ---------------------------------------------------------------------------

class TestExecuteWithObservationCreditsOnSuccess:
    """execute_with_observation must call record_aggregate_hit exactly once
    after a successful aggregate-routed execution."""

    async def test_success_credits_hit_exactly_once(self):
        """A successful aggregate execution must credit exactly once."""
        from src.api.routes import execute_with_observation

        m = make_measure("revenue")
        agg = make_aggregate(["country"], [make_agg_col(m)])
        bq = make_bound_query([make_dimension("country")], [m])

        decision = RouteDecision(
            route_type="aggregate",
            rewritten_query="SELECT SUM(revenue__sum) FROM agg_table",
            reason="Matched aggregate agg-1",
            aggregate_id="agg-1",
            pending_hit_credit=agg,
        )

        db = AsyncMock()
        db.execute = AsyncMock(return_value=None)
        db.get = AsyncMock(return_value=None)

        mock_target = types.SimpleNamespace(
            project_connection_id="conn-1",
            display_name="target",
            config={},
        )

        with (
            patch(
                "src.api.routes.resolve_filter_anchors",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "src.api.routes.audit_filters_present",
            ),
            patch(
                "src.api.routes.execute_routed_query",
                new_callable=AsyncMock,
                return_value=(
                    [{"revenue": 100}],  # rows
                    1024,                # bytes_processed
                    ["revenue"],         # columns
                    mock_target,         # chosen_source
                ),
            ),
            patch(
                "src.api.routes.audit_result_columns",
            ),
            patch(
                "src.api.routes.record_query_success",
                new_callable=AsyncMock,
            ),
            patch(
                "src.api.routes.record_aggregate_hit",
                new_callable=AsyncMock,
            ) as mock_credit,
        ):
            rows, _bp, _cols, _src, _ms, returned_decision = (
                await execute_with_observation(
                    bound=bq,
                    decision=decision,
                    db=db,
                    user_identity="user@test.com",
                    tenant_id="test-tenant",
                )
            )

        # Credited exactly once with the correct aggregate object
        mock_credit.assert_awaited_once_with(agg, db)

    async def test_source_route_does_not_credit(self):
        """A source-routed query (pending_hit_credit=None) must not credit."""
        from src.api.routes import execute_with_observation

        m = make_measure("revenue")
        bq = make_bound_query([make_dimension("country")], [m])

        decision = RouteDecision(
            route_type="source",
            rewritten_query="SELECT SUM(revenue) FROM sales",
            reason="No aggregate match",
            pending_hit_credit=None,
        )

        db = AsyncMock()
        db.execute = AsyncMock(return_value=None)
        db.get = AsyncMock(return_value=None)

        mock_source = types.SimpleNamespace(
            project_connection_id="conn-1",
            display_name="source",
            config={},
        )

        with (
            patch(
                "src.api.routes.resolve_filter_anchors",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch("src.api.routes.audit_filters_present"),
            patch(
                "src.api.routes.execute_routed_query",
                new_callable=AsyncMock,
                return_value=(
                    [{"revenue": 100}],
                    1024,
                    ["revenue"],
                    mock_source,
                ),
            ),
            patch("src.api.routes.audit_result_columns"),
            patch("src.api.routes.record_query_success", new_callable=AsyncMock),
            patch(
                "src.api.routes.record_aggregate_hit",
                new_callable=AsyncMock,
            ) as mock_credit,
        ):
            await execute_with_observation(
                bound=bq,
                decision=decision,
                db=db,
                user_identity="user@test.com",
                tenant_id="test-tenant",
            )

        mock_credit.assert_not_awaited()


# ---------------------------------------------------------------------------
# 3. execute_with_observation does NOT credit on failure
# ---------------------------------------------------------------------------

class TestExecuteWithObservationNoCreditsOnFailure:
    """A failed execution must never credit hit_count."""

    async def test_execution_failure_does_not_credit(self):
        """When execute_routed_query raises, no hit credit must be issued."""
        from src.api.routes import execute_with_observation

        m = make_measure("revenue")
        agg = make_aggregate(["country"], [make_agg_col(m)])
        bq = make_bound_query([make_dimension("country")], [m])

        decision = RouteDecision(
            route_type="aggregate",
            rewritten_query="SELECT SUM(revenue__sum) FROM agg_table",
            reason="Matched aggregate agg-1",
            aggregate_id="agg-1",
            pending_hit_credit=agg,
        )

        db = AsyncMock()
        db.execute = AsyncMock(return_value=None)
        db.get = AsyncMock(return_value=None)

        with (
            patch(
                "src.api.routes.resolve_filter_anchors",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch("src.api.routes.audit_filters_present"),
            patch(
                "src.api.routes.execute_routed_query",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Source DB unreachable"),
            ),
            patch(
                "src.api.routes._log_query_failure",
                new_callable=AsyncMock,
            ),
            patch(
                "src.api.routes.record_aggregate_hit",
                new_callable=AsyncMock,
            ) as mock_credit,
        ):
            with pytest.raises(Exception):
                await execute_with_observation(
                    bound=bq,
                    decision=decision,
                    db=db,
                    user_identity="user@test.com",
                    tenant_id="test-tenant",
                )

        # The execution failed — no hit credit must have been issued
        mock_credit.assert_not_awaited()

    async def test_security_audit_failure_does_not_credit(self):
        """When audit_result_columns raises, no hit credit must be issued."""
        from src.api.routes import execute_with_observation
        from src.security.query_audit import SecurityAuditError

        m = make_measure("revenue")
        agg = make_aggregate(["country"], [make_agg_col(m)])
        bq = make_bound_query([make_dimension("country")], [m])

        decision = RouteDecision(
            route_type="aggregate",
            rewritten_query="SELECT SUM(revenue__sum) FROM agg_table",
            reason="Matched aggregate agg-1",
            aggregate_id="agg-1",
            pending_hit_credit=agg,
        )

        db = AsyncMock()
        db.execute = AsyncMock(return_value=None)
        db.get = AsyncMock(return_value=None)
        db.commit = AsyncMock()

        mock_target = types.SimpleNamespace(
            project_connection_id="conn-1",
            display_name="target",
            config={},
        )

        with (
            patch(
                "src.api.routes.resolve_filter_anchors",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch("src.api.routes.audit_filters_present"),
            patch(
                "src.api.routes.execute_routed_query",
                new_callable=AsyncMock,
                return_value=(
                    [{"revenue": 100}],
                    1024,
                    ["revenue"],
                    mock_target,
                ),
            ),
            patch(
                "src.api.routes.audit_result_columns",
                side_effect=SecurityAuditError("Unauthorised column"),
            ),
            patch(
                "src.api.routes._log_query_failure",
                new_callable=AsyncMock,
            ),
            patch(
                "src.api.routes.audit",
                new_callable=AsyncMock,
            ),
            patch(
                "src.api.routes.record_aggregate_hit",
                new_callable=AsyncMock,
            ) as mock_credit,
        ):
            with pytest.raises(Exception):
                await execute_with_observation(
                    bound=bq,
                    decision=decision,
                    db=db,
                    user_identity="user@test.com",
                    tenant_id="test-tenant",
                )

        # Security audit failed — no hit credit
        mock_credit.assert_not_awaited()


# ---------------------------------------------------------------------------
# 4. No double-count across router + endpoint
# ---------------------------------------------------------------------------

class TestNoDoubleCount:
    """The router must not credit, and execute_with_observation must credit
    exactly once — so the full pipeline credits exactly once on success."""

    async def test_full_pipeline_credits_exactly_once(self):
        """route_query + execute_with_observation together must credit once."""
        from src.routing.router import route_query
        from src.api.routes import execute_with_observation
        from src.routing.aggregate_matcher import AggregateMatchResult

        m = make_measure("revenue")
        agg = make_aggregate(["country"], [make_agg_col(m)])
        bq = make_bound_query([make_dimension("country")], [m])

        db = AsyncMock()
        db.execute = AsyncMock(return_value=None)
        db.get = AsyncMock(return_value=None)
        db.commit = AsyncMock()

        mock_target = types.SimpleNamespace(
            project_connection_id="conn-1",
            display_name="target",
            config={},
        )

        # Step 1: route (should NOT credit)
        with (
            patch(
                "src.routing.router.find_best_aggregate",
                new_callable=AsyncMock,
                return_value=AggregateMatchResult(aggregate=agg),
            ),
            patch(
                "src.routing.router.find_best_pocket",
                new_callable=AsyncMock,
                return_value=types.SimpleNamespace(pocket=None, skipped_reason=None),
            ),
            patch(
                "src.routing.router.rewrite_for_aggregate",
                return_value="SELECT SUM(revenue__sum) FROM agg_table",
            ),
            patch(
                "src.routing.router.validate_aggregate_route",
                return_value=(True, "ok"),
            ),
            patch(
                "src.routing.router._resolve_aggregate_target_dialect",
                new_callable=AsyncMock,
                return_value="postgres",
            ),
            patch(
                "src.routing.router._resolve_aggregate_source_dialect",
                new_callable=AsyncMock,
                return_value="postgres",
            ),
            patch(
                "src.routing.router._ensure_valid_user_defined_attributes",
                new_callable=AsyncMock,
            ),
            patch(
                "src.routing.router.compile_row_security",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "src.routing.router._check_column_restrictions",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            decision = await route_query(bq, db)

        assert decision.pending_hit_credit is agg

        # Step 2: execute (should credit exactly once)
        with (
            patch(
                "src.api.routes.resolve_filter_anchors",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch("src.api.routes.audit_filters_present"),
            patch(
                "src.api.routes.execute_routed_query",
                new_callable=AsyncMock,
                return_value=(
                    [{"revenue": 100}],
                    1024,
                    ["revenue"],
                    mock_target,
                ),
            ),
            patch("src.api.routes.audit_result_columns"),
            patch("src.api.routes.record_query_success", new_callable=AsyncMock),
            patch(
                "src.api.routes.record_aggregate_hit",
                new_callable=AsyncMock,
            ) as mock_credit,
        ):
            await execute_with_observation(
                bound=bq,
                decision=decision,
                db=db,
                user_identity="user@test.com",
                tenant_id="test-tenant",
            )

        # Exactly one credit, with the correct aggregate
        mock_credit.assert_awaited_once_with(agg, db)


# ---------------------------------------------------------------------------
# 5. Bug-5325 — cross-project connection error maps to a clean 422 at the
#    execute_with_observation wrapper, NOT the generic 502 execution path, and
#    issues no hit credit. This pins the route-level error translation that the
#    /execute, /headless/query and /plugin/execute seams all share.
# ---------------------------------------------------------------------------

class TestCrossProjectConnectionMapping:
    """A CrossProjectConnectionError raised by execute_routed_query (the guard
    fired before any SQL ran) must surface as HTTP 422 — fail closed, not a
    masked 502 — and must never credit an aggregate hit."""

    async def test_cross_project_error_maps_to_422_no_credit(self):
        from fastapi import HTTPException

        from shared.connection_scope import CrossProjectConnectionError
        from src.api.routes import execute_with_observation

        m = make_measure("revenue")
        agg = make_aggregate(["country"], [make_agg_col(m)])
        bq = make_bound_query([make_dimension("country")], [m])

        decision = RouteDecision(
            route_type="aggregate",
            rewritten_query="SELECT SUM(revenue__sum) FROM agg_table",
            reason="Matched aggregate agg-1",
            aggregate_id="agg-1",
            pending_hit_credit=agg,
        )

        db = AsyncMock()
        db.execute = AsyncMock(return_value=None)
        db.get = AsyncMock(return_value=None)
        db.commit = AsyncMock()

        with (
            patch(
                "src.api.routes.resolve_filter_anchors",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch("src.api.routes.audit_filters_present"),
            patch(
                "src.api.routes.execute_routed_query",
                new_callable=AsyncMock,
                side_effect=CrossProjectConnectionError("different project"),
            ),
            patch(
                "src.api.routes._log_query_failure",
                new_callable=AsyncMock,
            ),
            patch(
                "src.api.routes.record_aggregate_hit",
                new_callable=AsyncMock,
            ) as mock_credit,
        ):
            with pytest.raises(HTTPException) as exc:
                await execute_with_observation(
                    bound=bq,
                    decision=decision,
                    db=db,
                    user_identity="user@test.com",
                    tenant_id="test-tenant",
                )

        assert exc.value.status_code == 422
        assert "different project" in exc.value.detail
        mock_credit.assert_not_awaited()
