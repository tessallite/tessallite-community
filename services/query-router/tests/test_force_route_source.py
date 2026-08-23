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
_PATCH_RAW_REWRITE = "src.routing.router.rewrite_for_raw"

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


def test_validate_force_route_accepts_raw():
    _validate_force_route("raw")


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


async def test_force_aggregate_failure_preserves_skip_reason():
    """A forced aggregate miss must expose the aggregate-specific rejection
    reason, not only a generic no-match message."""
    from src.ir.logical_query import NoAggregateMatchError

    m = make_measure("revenue")
    d = make_dimension("region_code")
    wrong_grain_agg = make_aggregate(["country_code"], [make_agg_col(m)])
    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])
    db = _db_returning([])

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [wrong_grain_agg]
        with pytest.raises(NoAggregateMatchError) as exc:
            await route_query(bq, db, force_route="aggregate")

    msg = str(exc.value)
    assert "Route-specific reason" in msg
    assert "aggregate skip reasons" in msg
    assert "grain_missing" in msg


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


async def test_force_aggregate_with_row_security_raises_when_no_safe_agg():
    """Bug-7033: force_route='aggregate' with active row-security raises
    when no RLS-safe aggregate is available (the security columns are not
    in any aggregate's grain)."""
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


# ---------------------------------------------------------------------------
# Bug-6916 — force_route="raw" now tries aggregate/pocket matching BEFORE
# falling back to the raw rewriter, maximising aggregate hit rate.
# ---------------------------------------------------------------------------


async def test_force_route_raw_uses_aggregate_when_available():
    """Bug-6916: force_route='raw' should prefer an aggregate when one covers
    the query, rather than unconditionally bypassing aggregate routing."""
    m = make_measure("revenue")
    d = make_dimension("region_code")
    agg = make_aggregate(["region_code"], [make_agg_col(m)])

    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])
    db = _db_returning([])

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load, \
            patch(_PATCH_RAW_REWRITE, new_callable=AsyncMock) as mock_raw:
        mock_load.return_value = [agg]
        mock_raw.return_value = 'SELECT "region_code", "amount" FROM fact_sales'
        decision = await route_query(bq, db, force_route="raw")

    # Aggregate should win over raw when it covers the query.
    assert decision.route_type == "aggregate"
    assert decision.aggregate_id == str(agg.id)
    # The raw rewriter should NOT have been called.
    mock_raw.assert_not_called()


async def test_force_route_raw_falls_back_to_raw_when_no_aggregate():
    """Bug-6916: when no aggregate covers the query, force_route='raw' still
    produces the raw-route flat-row output.  Uses a dimension-only query
    (no measures) which is the canonical raw-route shape — an ungrouped
    detail-row SELECT that the aggregate matcher naturally skips
    (NO_MEASURES_NO_GRAIN)."""
    d = make_dimension("region_code")

    sql = "SELECT region_code FROM sales"
    bq = _bind(sql, [], [d])
    db = _db_returning([])

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load, \
            patch(_PATCH_RAW_REWRITE, new_callable=AsyncMock) as mock_raw:
        mock_load.return_value = []  # no aggregates
        mock_raw.return_value = 'SELECT "region_code" FROM fact_sales'
        decision = await route_query(bq, db, force_route="raw")

    assert decision.route_type == "raw"
    assert decision.aggregate_id is None
    assert "raw route" in decision.reason.lower() or "force_route=raw" in decision.reason


async def test_force_route_raw_ungrouped_skips_aggregate_to_prevent_cardinality_collapse():
    """Bug-6916 safety: an ungrouped SELECT with force_route='raw' must NOT
    match an aggregate even when one covers the projected columns, because
    the user expects detail rows and the aggregate would collapse cardinality.
    The aggregate matcher is skipped when force_route='raw' AND no GROUP BY
    grain exists; the raw rewriter handles the detail-row query instead."""
    m = make_measure("revenue")
    d = make_dimension("region_code")
    agg = make_aggregate(["region_code"], [make_agg_col(m)])

    # Ungrouped query (no GROUP BY) -- canonical raw-route shape.
    sql = "SELECT region_code FROM sales"
    bq = _bind(sql, [], [d])
    db = _db_returning([])

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load, \
            patch(_PATCH_RAW_REWRITE, new_callable=AsyncMock) as mock_raw:
        mock_load.return_value = [agg]
        mock_raw.return_value = 'SELECT "region_code" FROM fact_sales'
        decision = await route_query(bq, db, force_route="raw")

    # The aggregate matcher must NOT be invoked for ungrouped raw queries.
    assert decision.route_type == "raw"
    assert decision.aggregate_id is None
    mock_load.assert_not_called()
    mock_raw.assert_called_once()


async def test_force_route_raw_with_row_security():
    """force_route='raw' with active RLS uses rewrite_for_raw then injects
    security WHERE -- route_type must be 'raw', not 'source'."""
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

    with patch(_PATCH_LOAD, new_callable=AsyncMock), \
            patch(_PATCH_RAW_REWRITE, new_callable=AsyncMock) as mock_raw:
        mock_raw.return_value = 'SELECT "region_code", "amount" FROM fact_sales'
        decision = await route_query(
            bq, db, principal=principal, force_route="raw"
        )

    assert decision.route_type == "raw"
    assert "Row security active" in decision.reason
    mock_raw.assert_called_once()


# ---------------------------------------------------------------------------
# Bug-8800 / Bug-8788 — the RouteDecision telemetry contract A2 consumes
# ---------------------------------------------------------------------------


async def test_force_route_source_suppresses_the_miss_log():
    """Bug-8788: ``force_route="source"`` is a deliberate bypass of
    acceleration, not an acceleration miss.

    Logging it feeds the optimizer false BUILD evidence and spends a CTAS plus
    a refresh cadence forever on a shape the user explicitly asked to run live.
    ``log_miss=False`` is the producer half of that contract; the consumer is
    ``api/routes.py``.
    """
    m = make_measure("revenue")
    d = make_dimension("region_code")
    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])

    decision = await route_query(_db_returning([]) and bq, _db_returning([]),
                                 force_route="source")

    assert decision.route_type == "source"
    assert decision.log_miss is False, (
        "force_route=source must not be logged as an acceleration miss"
    )


async def test_required_grain_survives_the_raw_to_source_fallback():
    """Bug-8800: EVERY source decision reached after the matcher ran must carry
    the grain the MATCHER required — including the raw->source fallback.

    Witness matters here. ``force_route="raw"`` downgrades to source by two
    routes, and only ONE of them loses information:

    * ``has_unresolvable_where`` / ``has_complex_sql`` — the MATCHER itself
      early-returns (UNRESOLVABLE_WHERE) before it computes ``required_grain``,
      so there is nothing to carry and ``None`` is the correct, documented
      fallback to ``log_query_miss``'s own derivation.
    * ``RawRouteUnsupported`` from ``rewrite_for_raw`` — the matcher ran to
      completion and DID compute ``required_grain``; the raw builder simply
      could not render the query. This is the path that silently dropped it.

    So this test drives the second path. Omitting ``required_grain`` there
    reverts that ONE route to ``log_query_miss``'s independent
    ``sorted(set(lq.grain) | filter_dims)`` re-derivation — the exact divergence
    Bug-8800 exists to close — so the optimizer's auto-create sweep receives a
    different grain for raw-fallback misses than for every other source miss,
    losing the DISTINCT fallback and DATE_TRUNC substitutions the matcher
    applies.
    """
    from src.rewrite.raw_sql import RawRouteUnsupported

    m = make_measure("revenue")
    d = make_dimension("region_code")
    # An aggregate that cannot serve, so the matcher runs fully, records a skip
    # reason, and still computes required_grain.
    agg = make_aggregate(["other_dim"], [make_agg_col(m)])

    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load, patch(
        _PATCH_RAW_REWRITE,
        new=AsyncMock(side_effect=RawRouteUnsupported("flat-row shape unsupported")),
    ):
        mock_load.return_value = [agg]
        decision = await route_query(bq, _db_returning([]), force_route="raw")

    assert decision.route_type == "source", (
        f"expected the raw->source fallback, got {decision.route_type!r}"
    )
    assert "fell back to source" in decision.reason
    assert decision.aggregate_skipped_reasons, (
        "fixture is not exercising the post-matcher path; without matcher skip "
        "reasons this test cannot detect the dropped grain"
    )
    assert decision.required_grain == ["region_code"], (
        "the raw->source fallback dropped the matcher's required_grain "
        f"(got {decision.required_grain!r}); the miss-log silently falls back "
        "to its own divergent derivation (Bug-8800)"
    )


async def test_matcher_early_return_leaves_required_grain_unset_on_purpose():
    """The other half of the contract, so the guard above is not read as
    "required_grain must always be set".

    When the matcher early-returns BEFORE computing the grain (here:
    ``has_unresolvable_where``), ``required_grain`` is legitimately ``None`` and
    ``log_query_miss`` correctly falls back to deriving it. Pinning this stops a
    future "fix" from inventing a grain the matcher never computed.
    """
    m = make_measure("revenue")
    d = make_dimension("region_code")
    agg = make_aggregate(["region_code"], [make_agg_col(m)])

    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])
    bq.logical_query.has_unresolvable_where = True

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        decision = await route_query(bq, _db_returning([]), force_route="raw")

    assert decision.route_type == "source"
    assert decision.required_grain is None, (
        "the matcher never computed a grain on this path; reporting one would "
        "be fabricated telemetry"
    )


async def test_genuinely_empty_grain_reaches_the_miss_log_as_empty_not_none():
    """Bug-8800: `[]` (computed, genuinely empty) and `None` (never computed)
    are DIFFERENT facts and must stay distinguishable end to end.

    A measure-only query -- no GROUP BY, no filter dimensions -- has a required
    grain that is legitimately EMPTY. The matcher computes `[]`. If that is
    collapsed to `None` on the way to the miss log, the logger silently falls
    back to its own `sorted(set(lq.grain) | filter_dim_names)` derivation for
    exactly this query shape while every other shape uses the matcher's value —
    a divergence that is invisible until the optimizer builds at the wrong grain
    for measure-only workloads.

    The collapse is easy to reintroduce: `AggregateMatchResult.required_grain`
    used to default to `field(default_factory=list)`, which made "not computed"
    and "empty" both `[]` and forced the router to guess with `or None`.
    """
    m = make_measure("revenue")
    # The aggregate carries a DIFFERENT measure, so it fails measure coverage
    # and the query falls to source with the matcher having run to completion.
    other = make_measure("other_revenue")
    agg = make_aggregate(["region_code"], [make_agg_col(other)])

    # No dimensions, no GROUP BY, no filters -> required grain is empty.
    bq = _bind("SELECT SUM(revenue) FROM sales", [m], [])

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        decision = await route_query(bq, _db_returning([]))

    assert decision.route_type == "source", (
        f"fixture routed to {decision.route_type!r}; it must reach the source "
        "path with the matcher having run, or it proves nothing"
    )
    assert decision.aggregate_skipped_reasons, (
        "the matcher did not record a skip reason, so it may not have run to "
        "the point of computing required_grain"
    )
    assert decision.required_grain == [], (
        "a measure-only query's genuinely-empty required grain was collapsed to "
        f"{decision.required_grain!r}; the miss log will silently use the "
        "logger's own derivation for this shape only (Bug-8800)"
    )
    assert decision.required_grain is not None
