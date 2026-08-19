"""Phase 5.2 contract regression — ExecuteResponse and ExplainResponse.

These tests lock the response envelope so the frontend badge (and
Phase 11 MCP) can rely on the fields being present on every response.
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.routes import (
    ExecuteRequest,
    ExecuteResponse,
    ExplainResponse,
    PipelineTrace,
    ResultFreshness,
    _cached_response_with_current_freshness,
    _handle_execute,
    _result_freshness,
)


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


class _Savepoint:
    """Stand-in for the ``AsyncSession.begin_nested()`` async context manager."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _freshness_db(row):
    """An async-session double that supports the SAVEPOINT the producer takes.

    ``_result_freshness`` reads the artifact inside ``db.begin_nested()`` so a
    DB-level fault cannot abort the caller's transaction. A bare ``AsyncMock``
    does not implement the async-context-manager protocol, so a double that
    omitted ``begin_nested`` would make every freshness assertion below pass
    for the WRONG reason: the producer would fail closed to ``None`` on a
    TypeError before it ever read the artifact.
    """
    db = AsyncMock()
    db.execute.return_value = types.SimpleNamespace(one_or_none=lambda: row)
    db.begin_nested = MagicMock(side_effect=lambda: _Savepoint())
    return db


@pytest.mark.asyncio
async def test_source_result_freshness_is_live_without_artifact_lookup():
    decision = RouteDecision(
        route_type="source", rewritten_query="SELECT 1", reason="source"
    )
    db = AsyncMock()
    freshness = await _result_freshness(decision, db, model_id=uuid.uuid4())
    assert freshness is not None
    assert freshness.model_dump() == {
        "last_refreshed_at": None,
        "is_live": True,
        "is_stale": False,
    }
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_aggregate_result_freshness_comes_from_scoped_served_artifact():
    refreshed = datetime(2026, 8, 3, 9, 30, tzinfo=timezone.utc)
    row = types.SimpleNamespace(
        last_refreshed_at=refreshed,
        status="active",
        is_stale=False,
        cron_expression="0 * * * *",
        is_enabled=True,
    )
    db = _freshness_db(row)
    decision = RouteDecision(
        route_type="aggregate",
        rewritten_query="SELECT 1",
        reason="aggregate",
        aggregate_id=str(uuid.uuid4()),
    )
    with (
        patch(
            # Bug-8528: _result_freshness resolves the grace window through
            # the SHARED shared.staleness_gate.resolve_overdue_grace_seconds,
            # so the config read to intercept lives in that resolver, not on
            # this module. Patching the removed local import would silently
            # stop controlling the threshold under test.
            "shared.config.bootstrap.system_snapshot_get", return_value=1
        ),
        patch.object(_routes, "artifact_overdue", return_value=False),
    ):
        freshness = await _result_freshness(
            decision, db, model_id=uuid.uuid4()
        )
    assert freshness is not None
    assert freshness.last_refreshed_at == refreshed
    assert freshness.is_live is False
    assert freshness.is_stale is False


@pytest.mark.asyncio
async def test_pocket_result_freshness_reports_current_stale_verdict():
    refreshed = datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc)
    row = types.SimpleNamespace(
        last_refresh_at=refreshed,
        status="fresh",
        cron_expression="0 * * * *",
        is_enabled=True,
    )
    db = _freshness_db(row)
    decision = RouteDecision(
        route_type="pocket",
        rewritten_query="SELECT 1",
        reason="pocket",
        pocket_id=str(uuid.uuid4()),
    )
    with (
        patch(
            # Bug-8528: _result_freshness resolves the grace window through
            # the SHARED shared.staleness_gate.resolve_overdue_grace_seconds,
            # so the config read to intercept lives in that resolver, not on
            # this module. Patching the removed local import would silently
            # stop controlling the threshold under test.
            "shared.config.bootstrap.system_snapshot_get", return_value=0
        ),
        patch.object(_routes, "artifact_overdue", return_value=True),
    ):
        freshness = await _result_freshness(
            decision, db, model_id=uuid.uuid4()
        )
    assert freshness is not None
    assert freshness.last_refreshed_at == refreshed
    assert freshness.is_live is False
    assert freshness.is_stale is True


@pytest.mark.asyncio
async def test_accelerated_freshness_fails_closed_when_artifact_lookup_misses():
    db = _freshness_db(None)
    decision = RouteDecision(
        route_type="aggregate",
        rewritten_query="SELECT 1",
        reason="aggregate",
        aggregate_id=str(uuid.uuid4()),
    )
    assert await _result_freshness(
        decision, db, model_id=uuid.uuid4()
    ) is None


def _cached_accelerated_response(
    route_type: str, refreshed: datetime,
) -> ExecuteResponse:
    artifact_id = str(uuid.uuid4())
    return ExecuteResponse(
        rows=[{"account_id": 7, "value": 42}],
        columns=["account_id", "value"],
        route_type=route_type,
        reason=f"served by {route_type}",
        aggregate_id=artifact_id if route_type == "aggregate" else None,
        pocket_id=artifact_id if route_type == "pocket" else None,
        execution_ms=8,
        bytes_processed=128,
        rows_returned=1,
        freshness=ResultFreshness(
            last_refreshed_at=refreshed,
            is_live=False,
            is_stale=False,
        ),
    )


@pytest.mark.asyncio
async def test_aggregate_cache_hit_preserves_timestamp_and_crosses_stale_deadline():
    original_refreshed = datetime(2026, 8, 3, 7, 0, tzinfo=timezone.utc)
    current_artifact_refresh = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    cached = _cached_accelerated_response("aggregate", original_refreshed)
    row = types.SimpleNamespace(
        last_refreshed_at=current_artifact_refresh,
        status="active",
        is_stale=False,
        cron_expression="0 * * * *",
        is_enabled=True,
    )
    db = _freshness_db(row)

    with (
        patch(
            # Bug-8528: _result_freshness resolves the grace window through
            # the SHARED shared.staleness_gate.resolve_overdue_grace_seconds,
            # so the config read to intercept lives in that resolver, not on
            # this module. Patching the removed local import would silently
            # stop controlling the threshold under test.
            "shared.config.bootstrap.system_snapshot_get", return_value=1
        ),
        patch.object(_routes, "artifact_overdue", side_effect=[False, True]) as overdue,
    ):
        before_deadline = await _cached_response_with_current_freshness(
            cached, db, model_id=uuid.uuid4(),
        )
        after_deadline = await _cached_response_with_current_freshness(
            cached, db, model_id=uuid.uuid4(),
        )

    assert before_deadline.freshness is not None
    assert before_deadline.freshness.is_stale is False
    assert after_deadline.freshness is not None
    assert after_deadline.freshness.is_stale is True
    assert before_deadline.freshness.last_refreshed_at == original_refreshed
    assert after_deadline.freshness.last_refreshed_at == original_refreshed
    assert all(call.args[1] == original_refreshed for call in overdue.call_args_list)
    assert before_deadline.rows == after_deadline.rows == cached.rows
    assert before_deadline.rows is not cached.rows
    assert cached.freshness is not None and cached.freshness.is_stale is False
    assert cached.freshness.last_refreshed_at == original_refreshed


@pytest.mark.asyncio
async def test_pocket_cache_hit_preserves_timestamp_and_crosses_stale_deadline():
    original_refreshed = datetime(2026, 8, 3, 7, 0, tzinfo=timezone.utc)
    cached = _cached_accelerated_response("pocket", original_refreshed)
    row = types.SimpleNamespace(
        last_refresh_at=datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc),
        status="fresh",
        cron_expression="0 * * * *",
        is_enabled=True,
    )
    db = _freshness_db(row)

    with (
        patch(
            # Bug-8528: _result_freshness resolves the grace window through
            # the SHARED shared.staleness_gate.resolve_overdue_grace_seconds,
            # so the config read to intercept lives in that resolver, not on
            # this module. Patching the removed local import would silently
            # stop controlling the threshold under test.
            "shared.config.bootstrap.system_snapshot_get", return_value=1
        ),
        patch.object(_routes, "artifact_overdue", side_effect=[False, True]) as overdue,
    ):
        before_deadline = await _cached_response_with_current_freshness(
            cached, db, model_id=uuid.uuid4(),
        )
        after_deadline = await _cached_response_with_current_freshness(
            cached, db, model_id=uuid.uuid4(),
        )

    assert before_deadline.freshness is not None
    assert before_deadline.freshness.is_stale is False
    assert after_deadline.freshness is not None
    assert after_deadline.freshness.is_stale is True
    assert before_deadline.freshness.last_refreshed_at == original_refreshed
    assert after_deadline.freshness.last_refreshed_at == original_refreshed
    assert all(call.args[1] == original_refreshed for call in overdue.call_args_list)
    assert before_deadline.rows == after_deadline.rows == cached.rows
    assert before_deadline.rows is not cached.rows
    assert cached.freshness is not None and cached.freshness.is_stale is False


@pytest.mark.asyncio
@pytest.mark.parametrize("route_type", ["aggregate", "pocket"])
async def test_cache_hit_freshness_lookup_failure_omits_only_diagnostic(route_type):
    """A lookup that FAILS is undiagnosable, so the rows are still served with
    the freshness block omitted.

    Bug-8581 split this from the case below. The test used to model "failure" as
    an artifact row that is ABSENT — but an absent row from a query scoped by
    (artifact id, model id) is not an unprovable lookup, it is proof the artifact
    was DELETED, and serving on it is the defect. A genuine failure is an
    exception (a DB fault), which is what this now models; that path still fails
    open, because turning a transient blip into a cache stampede against the
    source would be worse than a missing diagnostic.
    """
    refreshed = datetime(2026, 8, 3, 7, 0, tzinfo=timezone.utc)
    cached = _cached_accelerated_response(route_type, refreshed)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=RuntimeError("connection reset"))
    db.begin_nested = MagicMock(side_effect=lambda: _Savepoint())

    served = await _cached_response_with_current_freshness(
        cached, db, model_id=uuid.uuid4(),
    )

    assert served is not None
    assert served.rows == cached.rows
    assert served.rows is not cached.rows
    assert served.freshness is None
    assert cached.freshness is not None
    assert cached.freshness.last_refreshed_at == refreshed
    assert cached.freshness.is_stale is False


@pytest.mark.asyncio
@pytest.mark.parametrize("route_type", ["aggregate", "pocket"])
async def test_bug_8581_cached_response_is_refused_when_its_artifact_is_gone(
    route_type,
):
    """The artifact this cached response NAMES no longer exists.

    Deleting a pocket (or an aggregate) changes nothing in the cache key — an
    artifact is not part of the model snapshot, so neither deployed_version_id
    nor deploy_epoch moves — and the cache fast path returns before route_query,
    so the matcher's built_for gate never runs. Observed live
    (LIVE-POCKET-RLS-001, 2026-08-04): a pocket was DELETEd, its physical table
    verified gone, and the same query kept returning route_type=pocket with the
    deleted pocket's id and a routed SQL naming the dropped table for 15+
    seconds. ``None`` tells the caller to treat the entry as a MISS.
    """
    refreshed = datetime(2026, 8, 3, 7, 0, tzinfo=timezone.utc)
    cached = _cached_accelerated_response(route_type, refreshed)
    db = _freshness_db(None)  # artifact row absent = deleted

    served = await _cached_response_with_current_freshness(
        cached, db, model_id=uuid.uuid4(),
    )

    assert served is None


@pytest.mark.asyncio
async def test_bug_8581_cached_response_is_refused_when_its_pocket_is_retired(
    route_type="pocket",
):
    """Retired is as unservable as deleted: the row survives but the matcher
    refuses it and the retirement sweep drops its physical table."""
    refreshed = datetime(2026, 8, 3, 7, 0, tzinfo=timezone.utc)
    cached = _cached_accelerated_response(route_type, refreshed)
    db = _freshness_db(
        types.SimpleNamespace(
            status="stale",
            retired_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
            last_refresh_at=refreshed,
            cron_expression=None,
            is_enabled=False,
        )
    )

    served = await _cached_response_with_current_freshness(
        cached, db, model_id=uuid.uuid4(),
    )

    assert served is None


@pytest.mark.asyncio
async def test_bug_8581_cached_response_is_refused_when_its_aggregate_is_retired():
    refreshed = datetime(2026, 8, 3, 7, 0, tzinfo=timezone.utc)
    cached = _cached_accelerated_response("aggregate", refreshed)
    db = _freshness_db(
        types.SimpleNamespace(
            status="retired",
            last_refreshed_at=refreshed,
            is_stale=False,
            cron_expression=None,
            is_enabled=False,
        )
    )

    served = await _cached_response_with_current_freshness(
        cached, db, model_id=uuid.uuid4(),
    )

    assert served is None


def _cache_hit_logical_query(model_id: uuid.UUID):
    from src.ir.logical_query import LogicalQuery

    return LogicalQuery(
        model_id=str(model_id),
        protocol="jdbc",
        raw_query='SELECT "amount" FROM "modely"',
        requested_measures=[],
        requested_dimensions=[],
        filters=[],
        grain=[],
        order_by=[],
        limit=None,
        offset=None,
        query_fingerprint="cache-hit-freshness",
        select_star=False,
        from_tables=["modely"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("route_type", ["aggregate", "pocket"])
async def test_handle_execute_cache_hit_revalidates_freshness_before_early_return(
    route_type,
):
    """R2 integration guard: deleting the cache-hit helper call must fail.

    The real ``_handle_execute`` early return receives a cached response, skips
    route/source execution, reads current scoped artifact policy, preserves the
    rows and serving timestamp, and returns a detached response.
    """
    model_id = uuid.uuid4()
    original_refreshed = datetime(2026, 8, 3, 7, 0, tzinfo=timezone.utc)
    cached = _cached_accelerated_response(route_type, original_refreshed)
    current_row = types.SimpleNamespace(
        last_refreshed_at=datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc),
        last_refresh_at=datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc),
        status="active" if route_type == "aggregate" else "fresh",
        is_stale=False,
        cron_expression="0 * * * *",
        is_enabled=True,
    )
    db = _freshness_db(current_row)
    logical = _cache_hit_logical_query(model_id)
    bound = types.SimpleNamespace(
        model=types.SimpleNamespace(
            id=model_id,
            deployed_version_id=uuid.uuid4(),
        ),
        logical_query=logical,
        resolved_measures=[],
        resolved_dimensions=[],
        resolved_filters=[],
        has_passthrough_expressions=False,
    )
    route_mock = AsyncMock(side_effect=AssertionError("router must be skipped"))
    execute_mock = AsyncMock(side_effect=AssertionError("source must be skipped"))
    cache_hit_log = AsyncMock()

    with (
        patch.object(_routes, "_bind_query_parameters", new=AsyncMock()),
        patch.object(_routes, "_parse", return_value=logical),
        patch.object(_routes, "_detect_kpi_table", return_value=False),
        patch.object(
            _routes, "bind_query_to_model", new=AsyncMock(return_value=bound),
        ),
        patch.object(
            _routes, "apply_persona_gate", new=AsyncMock(return_value=None),
        ),
        patch.object(
            _routes,
            "_evaluate_bound_field_compatibility",
            new=AsyncMock(return_value=None),
        ),
        patch.object(_routes, "audit_result_columns"),
        patch.object(_routes._cache, "get", return_value=cached),
        patch.object(_routes, "record_query_cache_hit", new=cache_hit_log),
        patch.object(_routes, "route_query", new=route_mock),
        patch.object(_routes, "execute_with_observation", new=execute_mock),
        patch(
            # Bug-8528: _result_freshness resolves the grace window through
            # the SHARED shared.staleness_gate.resolve_overdue_grace_seconds,
            # so the config read to intercept lives in that resolver, not on
            # this module. Patching the removed local import would silently
            # stop controlling the threshold under test.
            "shared.config.bootstrap.system_snapshot_get", return_value=1
        ),
        patch.object(_routes, "artifact_overdue", return_value=True) as overdue,
    ):
        served = await _handle_execute(
            ExecuteRequest(
                model_id=str(model_id),
                raw_query='SELECT "amount" FROM "modely"',
                protocol="jdbc",
            ),
            db,
            user_identity="analyst@example.com",
            tenant_id="acme-demo",
        )

    route_mock.assert_not_awaited()
    execute_mock.assert_not_awaited()
    # Bug-8581: two statements now, both indexed primary-key reads inside their
    # own SAVEPOINT — the servability check that proves the cached response's
    # artifact still exists, then the freshness read. Still no route and no
    # source execution, which is what this count exists to pin.
    assert db.execute.await_count == 2
    params = db.execute.await_args.args[0].compile().params
    assert model_id in params.values()
    assert uuid.UUID(cached.aggregate_id or cached.pocket_id) in params.values()
    assert served is not cached
    assert served.rows == cached.rows
    assert served.rows is not cached.rows
    assert served.freshness is not None and served.freshness.is_stale is True
    assert served.freshness.last_refreshed_at == original_refreshed
    assert cached.freshness is not None and cached.freshness.is_stale is False
    assert cached.freshness.last_refreshed_at == original_refreshed
    assert overdue.call_args.args[1] == original_refreshed
    assert cache_hit_log.await_args.kwargs["cached"] is served


@pytest.mark.asyncio
@pytest.mark.parametrize("route_type", ["aggregate", "pocket"])
async def test_handle_execute_cache_hit_lookup_failure_omits_freshness_only(
    route_type,
):
    model_id = uuid.uuid4()
    original_refreshed = datetime(2026, 8, 3, 7, 0, tzinfo=timezone.utc)
    cached = _cached_accelerated_response(route_type, original_refreshed)
    # Bug-8581: an ABSENT artifact row is now proof of deletion and makes the
    # entry a cache MISS (asserted in
    # ``test_bug_8581_cached_response_is_refused_when_its_artifact_is_gone``).
    # The contract this test owns is the other one: a lookup that FAILS must not
    # break an otherwise-successful cached query, so model a DB fault.
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=RuntimeError("connection reset"))
    db.begin_nested = MagicMock(side_effect=lambda: _Savepoint())
    logical = _cache_hit_logical_query(model_id)
    bound = types.SimpleNamespace(
        model=types.SimpleNamespace(
            id=model_id, deployed_version_id=uuid.uuid4(),
        ),
        logical_query=logical,
        resolved_measures=[],
        resolved_dimensions=[],
        resolved_filters=[],
        has_passthrough_expressions=False,
    )
    route_mock = AsyncMock(side_effect=AssertionError("router must be skipped"))
    execute_mock = AsyncMock(side_effect=AssertionError("source must be skipped"))

    with (
        patch.object(_routes, "_bind_query_parameters", new=AsyncMock()),
        patch.object(_routes, "_parse", return_value=logical),
        patch.object(_routes, "_detect_kpi_table", return_value=False),
        patch.object(
            _routes, "bind_query_to_model", new=AsyncMock(return_value=bound),
        ),
        patch.object(
            _routes, "apply_persona_gate", new=AsyncMock(return_value=None),
        ),
        patch.object(
            _routes,
            "_evaluate_bound_field_compatibility",
            new=AsyncMock(return_value=None),
        ),
        patch.object(_routes, "audit_result_columns"),
        patch.object(_routes._cache, "get", return_value=cached),
        patch.object(_routes, "record_query_cache_hit", new=AsyncMock()),
        patch.object(_routes, "route_query", new=route_mock),
        patch.object(_routes, "execute_with_observation", new=execute_mock),
    ):
        served = await _handle_execute(
            ExecuteRequest(
                model_id=str(model_id),
                raw_query='SELECT "amount" FROM "modely"',
                protocol="jdbc",
            ),
            db,
            user_identity="analyst@example.com",
            tenant_id="acme-demo",
        )

    route_mock.assert_not_awaited()
    execute_mock.assert_not_awaited()
    assert served is not cached
    assert served.rows == cached.rows
    assert served.freshness is None
    assert cached.freshness is not None
    assert cached.freshness.last_refreshed_at == original_refreshed
    assert cached.freshness.is_stale is False


# ---------------------------------------------------------------------------
# Bug-8449 / Bug-8427 — the execute path must publish the applied row-security
# rule ids, exactly as the explain path always has.
#
# Without this field a caller cannot tell a result set emptied by a row-security
# predicate from a genuinely empty one, and the KPI builder renders a governance
# denial as "N/A" / "Insufficient Data" (live-reproduced 2026-07-29 against
# acme-demo/modelx, where the tenant admin matches no role_predicate rule and the
# router rewrites to `... WHERE 0 = 1`).
# ---------------------------------------------------------------------------

from src.api.routes import (  # noqa: E402
    DENY_ALL_RULE_ID,
    RouteDecision,
    _security_rule_ids,
)


def test_execute_response_defaults_security_rules_to_empty_list():
    """A legacy caller that never sets the field must still deserialise, and an
    empty list must mean "no rule fired" — never "unknown"."""
    resp = ExecuteResponse(
        rows=[],
        columns=[],
        route_type="source",
        aggregate_id=None,
        execution_ms=0,
        bytes_processed=0,
        rows_returned=0,
    )
    assert resp.security_rules_applied == []
    assert "security_rules_applied" in resp.model_dump()


def test_execute_response_carries_the_deny_all_sentinel():
    resp = ExecuteResponse(
        rows=[{"value": None}],
        columns=["value"],
        route_type="source",
        reason="Row security active (1 rule(s): __deny_all__)",
        aggregate_id=None,
        execution_ms=1,
        bytes_processed=0,
        rows_returned=1,
        security_rules_applied=[DENY_ALL_RULE_ID],
    )
    assert resp.model_dump()["security_rules_applied"] == ["__deny_all__"]


def test_deny_all_rule_id_matches_the_compiler_sentinel():
    """The execute-contract constant and the compiler's sentinel must be the same
    string, or a consumer branching on one silently never fires."""
    from shared.security.predicate_compiler import _deny_all_predicate

    assert DENY_ALL_RULE_ID in _deny_all_predicate().active_rule_ids


def test_security_rule_ids_is_shared_by_execute_and_explain():
    """Both endpoints must publish the identical value for the same decision —
    a consumer must not have to know which endpoint it called."""
    decision = RouteDecision(
        route_type="source",
        rewritten_query="SELECT 1 WHERE 0 = 1",
        reason="Row security active",
        security_rules_applied=[
            {"rule_id": DENY_ALL_RULE_ID, "rule_name": "coverage deny-all",
             "predicate_sql": "0 = 1"},
        ],
    )
    ids = _security_rule_ids(decision)
    assert ids == [DENY_ALL_RULE_ID]

    execute = ExecuteResponse(
        rows=[], columns=[], route_type="source", aggregate_id=None,
        execution_ms=0, bytes_processed=0, rows_returned=0,
        security_rules_applied=ids,
    )
    explain = ExplainResponse(
        route_type="source", aggregate_id=None, reason="Row security active",
        rewritten_query="SELECT 1 WHERE 0 = 1", requested_measures=[],
        requested_dimensions=[], grain=[], query_fingerprint="fp",
        security_rules_applied=ids,
    )
    assert execute.security_rules_applied == explain.security_rules_applied


def test_security_rule_ids_never_leaks_predicate_sql_or_rule_names():
    """The field discloses THAT a policy applied, never WHAT it filters on."""
    decision = RouteDecision(
        route_type="source",
        rewritten_query="SELECT 1",
        reason="Row security active",
        security_rules_applied=[
            {"rule_id": "rule-1", "rule_name": "EMEA manager scope",
             "predicate_sql": "\"country_code\" IN ('DE', 'FR')"},
        ],
    )
    ids = _security_rule_ids(decision)
    assert ids == ["rule-1"]
    blob = " ".join(ids)
    assert "country_code" not in blob
    assert "EMEA" not in blob


def test_security_rule_ids_tolerates_a_missing_or_malformed_entry():
    decision = RouteDecision(
        route_type="source",
        rewritten_query="SELECT 1",
        reason="",
        security_rules_applied=[{"rule_name": "no id"}, "not-a-dict", {"rule_id": "ok"}],
    )
    assert _security_rule_ids(decision) == ["ok"]


def test_security_rule_ids_on_a_decision_with_no_rules():
    decision = RouteDecision(
        route_type="aggregate", rewritten_query="SELECT 1", reason="Matched aggregate",
    )
    assert _security_rule_ids(decision) == []


# ---------------------------------------------------------------------------
# Bug-8449 — HANDLER wiring guard (producer-fixed-consumer-unwired class).
#
# The model-shape tests above all stay green if someone deletes
# ``security_rules_applied=_security_rule_ids(decision)`` from the /execute
# response builder — verified by mutation while writing them. This drives the
# real ``_handle_execute`` so the field must actually be populated by the
# handler, not merely be declarable on the model.
# ---------------------------------------------------------------------------

from src.api import routes as _routes  # noqa: E402


def _deny_all_decision():
    return RouteDecision(
        route_type="source",
        rewritten_query='SELECT SUM("Revenue") AS "value" FROM "t" WHERE 0 = 1',
        reason=(
            "Row security active (1 rule(s): __deny_all__); no RLS-safe aggregate "
            "matched; source route with predicate injection"
        ),
        security_rules_applied=[
            {"rule_id": DENY_ALL_RULE_ID, "rule_name": "row-security coverage deny-all",
             "predicate_sql": "0 = 1"},
        ],
    )


async def _run_handle_execute(decision):
    """Drive _handle_execute with every heavy collaborator stubbed."""
    body = _routes.ExecuteRequest(
        model_id="model-1", raw_query='SELECT SUM("Revenue") FROM "t"', protocol="jdbc",
    )
    bound = types.SimpleNamespace(
        model=types.SimpleNamespace(id="model-1", deployed_version_id="v1"),
        logical_query=types.SimpleNamespace(
            protocol="jdbc", raw_query=body.raw_query, query_fingerprint="fp",
            limit=None, grain=[],
        ),
        resolved_measures=[],
        resolved_dimensions=[],
        resolved_filters=[],
    )
    # The response cache is module-level and survives between tests; a stale hit
    # would short-circuit the handler and make this guard assert nothing.
    _routes._cache.clear()

    async def _observed(**kwargs):
        return ([{"value": None}], 0, ["value"], None, 3, decision)

    with (
        patch.object(_routes, "_bind_query_parameters", new=AsyncMock()),
        patch.object(_routes, "bind_query_to_model", new=AsyncMock(return_value=bound)),
        patch.object(
            _routes, "_evaluate_bound_field_compatibility",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            _routes, "resolve_target_dialect_for_bound",
            new=AsyncMock(return_value="postgres"),
        ),
        patch.object(_routes, "compile_row_security", new=AsyncMock(return_value=None)),
        patch.object(_routes, "route_query", new=AsyncMock(return_value=decision)),
        patch.object(_routes, "execute_with_observation", new=_observed),
        patch.object(
            _routes, "_build_trace", new=AsyncMock(return_value=PipelineTrace()),
        ),
    ):
        return await _routes._handle_execute(
            body,
            db=AsyncMock(),
            user_identity="admin@acme-demo.com",
            principal=None,
            tenant_id="acme-demo",
        )


@pytest.mark.asyncio
async def test_handle_execute_publishes_the_deny_all_rule_id():
    """The Bug-8427 shape: rows came back but every value is NULL because the
    coverage predicate rewrote to ``WHERE 0 = 1``. A consumer must be able to
    see that structurally, not by parsing ``reason``."""
    resp = await _run_handle_execute(_deny_all_decision())
    assert resp.rows == [{"value": None}]
    assert resp.security_rules_applied == [DENY_ALL_RULE_ID], (
        "the /execute handler must publish the applied row-security rule ids"
    )
    assert resp.freshness is not None
    assert resp.freshness.is_live is True


@pytest.mark.asyncio
async def test_handle_execute_reports_no_rules_when_none_applied():
    """The negative half: an unrestricted query must report an EMPTY list, so a
    consumer can trust empty to mean "no policy fired"."""
    decision = RouteDecision(
        route_type="aggregate",
        rewritten_query="SELECT 1",
        reason="Matched aggregate agg-1",
        aggregate_id="agg-1",
    )
    resp = await _run_handle_execute(decision)
    assert resp.security_rules_applied == []


def test_security_rule_ids_tolerates_a_decision_without_the_attribute():
    """Several internal callers build a duck-typed decision object. A DIAGNOSTIC
    field must never be able to break the execution path it describes — this
    exact AttributeError took out 9 tests across three modules while Bug-8449
    was being written, so it gets its own guard rather than an incidental one."""
    class _DuckDecision:
        route_type = "source"
        rewritten_query = "SELECT 1"
        reason = ""
        aggregate_id = None
        pocket_id = None

    assert _security_rule_ids(_DuckDecision()) == []


# ---------------------------------------------------------------------------
# Bug-8449 — $KPIs HANDLER wiring guard (round-1 deep review, finding 5).
#
# $KPIs withholds the WHOLE scorecard for a row-restricted principal
# (Bug-6930). An empty ``security_rules_applied`` on that response reads as "no
# policy applied", i.e. "this model simply has no deployed KPIs" — the same
# conflation Bug-8449 fixes for the KPI value, one layer up. The earlier
# revision of this test re-implemented the handler's expression locally and so
# stayed green when the wiring was deleted; this one drives the real handler.
# ---------------------------------------------------------------------------

def _kpi_logical_query():
    from src.ir.logical_query import LogicalQuery

    return LogicalQuery(
        model_id="11111111-1111-4111-8111-111111111111",
        protocol="jdbc",
        raw_query="SELECT * FROM $KPIs",
        requested_measures=[],
        requested_dimensions=[],
        filters=[],
        grain=[],
        order_by=[],
        limit=None,
        offset=None,
        query_fingerprint="fp-kpis",
        select_star=True,
        from_tables=["$KPIs"],
    )


async def _run_kpi_table_query(compiled, *, bypass=False):
    lq = _kpi_logical_query()
    persona = types.SimpleNamespace(bypass_row_security=bypass) if bypass else None
    db = AsyncMock()
    # The RLS-withhold path deliberately skips every KPI value lookup, but its
    # audit tail still resolves the model.  Return a normal synchronous
    # SQLAlchemy-result shape after the awaited ``execute`` call; leaving the
    # nested method as an AsyncMock hides production misuse behind an unawaited
    # coroutine warning.
    db.execute.return_value = types.SimpleNamespace(
        scalar_one_or_none=lambda: types.SimpleNamespace(
            id=uuid.UUID(lq.model_id),
            project=types.SimpleNamespace(display_name="Project 1"),
        ),
    )
    compile_mock = (
        AsyncMock(side_effect=compiled)
        if isinstance(compiled, Exception)
        else AsyncMock(return_value=compiled)
    )
    with (
        patch.object(
            _routes, "compile_row_security", new=compile_mock,
        ),
        patch.object(_routes, "record_query_success", new=AsyncMock()),
    ):
        return await _routes._handle_kpi_table_query(
            db,
            "11111111-1111-4111-8111-111111111111",
            lq,
            persona=persona,
            principal=types.SimpleNamespace(user_identity="admin@acme-demo.com"),
            user_identity="admin@acme-demo.com",
            tenant_id="acme-demo",
        )


@pytest.mark.asyncio
async def test_kpi_table_withhold_publishes_the_applied_rule_ids():
    from shared.security.predicate_compiler import CompiledPredicate

    rule_id = "9e97a9b5-27b0-4b83-95b5-45b16bc21706"
    resp = await _run_kpi_table_query(
        CompiledPredicate(
            sql_expression='"country_code" IN (\'DE\')',
            active_rule_ids=(rule_id,),
        ),
    )
    assert resp.rows == []
    assert resp.security_rules_applied == [rule_id], (
        "a withheld $KPIs scorecard must say a row-security policy withheld it, "
        "not present as 'this model has no deployed KPIs'"
    )


@pytest.mark.asyncio
async def test_kpi_table_withhold_on_a_compile_failure_reports_the_sentinel():
    """The compile failed, so no rule id is knowable — but the list must still
    be NON-empty, because empty is the contract's 'no policy applied'."""
    from shared.security.predicate_compiler import RowSecurityCompileError

    resp = await _run_kpi_table_query(RowSecurityCompileError("bad rule"))
    assert resp.rows == []
    assert resp.security_rules_applied == [DENY_ALL_RULE_ID]


# ---------------------------------------------------------------------------
# R1 finding 1 — a FAILING artifact read must not poison the caller's
# transaction. Swallowing the exception in Python is not enough: a DB-level
# fault aborts the whole transaction, and on the cache-hit path this SELECT is
# the request's FIRST statement, so the abort would then take down
# record_query_cache_hit -> log_query (unguarded add/flush/commit) and turn a
# query already served from cache into a 500.
#
# Test escape: every existing freshness test drives a SUCCESSFUL or
# empty-result read, so none of them exercises what a faulting read does to
# the surrounding transaction. Guard: the two tests below. Tier: T1.
# ---------------------------------------------------------------------------


class _AbortingSession:
    """Models PostgreSQL's aborted-transaction rule closely enough to test it.

    A statement that raises marks the transaction aborted, and every later
    statement then fails with the driver's "current transaction is aborted"
    error — unless a SAVEPOINT taken before the failing statement is rolled
    back, which is exactly what ``db.begin_nested()`` does.
    """

    def __init__(self, *, fail_times: int = 1):
        self._fail_times = fail_times
        self.aborted = False
        self._savepoint_depth = 0
        self.executed = 0

    def begin_nested(self):
        session = self

        class _SavepointCtx:
            async def __aenter__(self_inner):
                session._savepoint_depth += 1
                return self_inner

            async def __aexit__(self_inner, exc_type, exc, tb):
                session._savepoint_depth -= 1
                if exc_type is not None:
                    # ROLLBACK TO SAVEPOINT clears the aborted state.
                    session.aborted = False
                return False

        return _SavepointCtx()

    async def execute(self, _stmt):
        if self.aborted:
            raise RuntimeError(
                "current transaction is aborted, commands ignored until "
                "end of transaction block"
            )
        self.executed += 1
        if self._fail_times > 0:
            self._fail_times -= 1
            self.aborted = True
            raise RuntimeError("canceling statement due to statement timeout")
        return types.SimpleNamespace(one_or_none=lambda: None)


@pytest.mark.asyncio
@pytest.mark.parametrize("route_type", ["aggregate", "pocket"])
async def test_failing_artifact_read_leaves_the_transaction_usable(route_type):
    """A faulting freshness read fails closed AND leaves the caller's
    transaction able to write. Without the SAVEPOINT the caller's next
    statement — the QueryLog write — raises and the request 500s."""
    db = _AbortingSession()
    decision = RouteDecision(
        route_type=route_type,
        rewritten_query="SELECT 1",
        reason=route_type,
        aggregate_id=str(uuid.uuid4()) if route_type == "aggregate" else None,
        pocket_id=str(uuid.uuid4()) if route_type == "pocket" else None,
    )

    assert await _result_freshness(decision, db, model_id=uuid.uuid4()) is None
    assert db.executed == 1, "the artifact read must actually have been attempted"
    assert db.aborted is False, (
        "the freshness read must not leave the caller's transaction aborted"
    )
    # The caller's very next statement (in production: the QueryLog write on
    # the cache-hit path) must still succeed.
    await db.execute("INSERT INTO query_logs ...")


@pytest.mark.asyncio
async def test_cache_hit_survives_a_failing_freshness_read():
    """The end-to-end shape of the same property: rows were already in the
    result cache, the freshness revalidation faults, and the caller still
    serves the cached rows (with freshness omitted) instead of 500ing on the
    subsequent QueryLog write."""
    cached = _cached_accelerated_response(
        "aggregate", datetime(2026, 8, 3, 7, 0, tzinfo=timezone.utc),
    )
    db = _AbortingSession()

    served = await _cached_response_with_current_freshness(
        cached, db, model_id=uuid.uuid4(),
    )

    assert served.rows == cached.rows
    assert served.freshness is None, "unprovable freshness must be omitted"
    assert db.aborted is False
    await db.execute("INSERT INTO query_logs ...")


# ---------------------------------------------------------------------------
# Bug-8250 (finding 5) — the cache fast path must discriminate deploy_epoch
# ---------------------------------------------------------------------------
#
# The key builder's own contract is proven in
# tessallite/shared/tests/test_result_cache_key.py. What is proven HERE is the
# WIRING: that ``_handle_execute`` actually reads ``bound.model.deploy_epoch``
# and passes it. A correct builder that the production path never feeds is the
# exact "unit test passes, production path fails" shape this bug is a case of --
# ``Model.deploy_epoch``'s docstring claimed the router cache used it for months
# while the inline key never mentioned it.


async def _capture_cache_key(model_id, deployed_version_id, deploy_epoch):
    """Run the real ``_handle_execute`` and return the key it looked up."""
    logical = _cache_hit_logical_query(model_id)
    bound = types.SimpleNamespace(
        model=types.SimpleNamespace(
            id=model_id,
            deployed_version_id=deployed_version_id,
            deploy_epoch=deploy_epoch,
        ),
        logical_query=logical,
        resolved_measures=[],
        resolved_dimensions=[],
        resolved_filters=[],
        has_passthrough_expressions=False,
    )
    seen: list[tuple] = []

    def _capturing_get(key):
        seen.append(key)
        return None  # force a miss so the routing path below is exercised

    decision = types.SimpleNamespace(
        route_type="source", reason="no aggregate", aggregate_id=None,
        pocket_id=None, rewritten_query="SELECT 1", matched_aggregate=None,
        matched_pocket=None, skip_reasons=[], trace=None,
    )

    with (
        patch.object(_routes, "_bind_query_parameters", new=AsyncMock()),
        patch.object(_routes, "_parse", return_value=logical),
        patch.object(_routes, "_detect_kpi_table", return_value=False),
        patch.object(
            _routes, "bind_query_to_model", new=AsyncMock(return_value=bound),
        ),
        patch.object(
            _routes, "apply_persona_gate", new=AsyncMock(return_value=None),
        ),
        patch.object(
            _routes,
            "_evaluate_bound_field_compatibility",
            new=AsyncMock(return_value=None),
        ),
        patch.object(_routes._cache, "get", side_effect=_capturing_get),
        patch.object(
            _routes, "route_query",
            new=AsyncMock(side_effect=RuntimeError("stop after the cache lookup")),
        ),
    ):
        try:
            await _handle_execute(
                ExecuteRequest(
                    model_id=str(model_id),
                    raw_query='SELECT "amount" FROM "modely"',
                    protocol="jdbc",
                ),
                AsyncMock(),
                user_identity="analyst@example.com",
                tenant_id="acme-demo",
            )
        except Exception:
            # Routing is deliberately aborted; the cache lookup already happened.
            pass

    assert seen, "_handle_execute never consulted the result cache"
    return seen[0]


@pytest.mark.asyncio
async def test_cache_key_discriminates_a_same_version_epoch_bump():
    """A revert to the currently-deployed version must miss the cached result.

    ``deployed_version_id`` is unchanged by such a revert, so before this fix the
    pre-revert result still matched its key and was replayed -- with neither
    compatibility gate running, because the fast path returns before
    ``route_query``.
    """
    model_id = uuid.uuid4()
    version_id = uuid.uuid4()
    before = await _capture_cache_key(model_id, version_id, 3)
    after = await _capture_cache_key(model_id, version_id, 4)
    assert before != after, (
        "a deploy_epoch bump on the SAME version must change the cache key"
    )


@pytest.mark.asyncio
async def test_cache_key_is_stable_when_nothing_moves():
    """No false invalidation: an unchanged pointer keeps the same key."""
    model_id = uuid.uuid4()
    version_id = uuid.uuid4()
    first = await _capture_cache_key(model_id, version_id, 3)
    second = await _capture_cache_key(model_id, version_id, 3)
    assert first == second
