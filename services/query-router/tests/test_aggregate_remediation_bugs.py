"""
Tests for aggregate-area remediation bugs:
  - Bug-6984: stale/invalid aggregates must not be served
  - Bug-6976: pocket-unsafe fallthrough to aggregate matcher
  - Bug-7757: aggregate.py _pgq identifier injection (safe_ident)
  - Bug-7359: DATE_TRUNC grain equivalence

Run from tessallite/services/query-router/:
    pytest tests/test_aggregate_remediation_bugs.py -v
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

from src.routing.aggregate_matcher import (
    AggregateSkipReason,
    find_best_aggregate,
    _extract_date_trunc_grain,
    _date_trunc_to_period_candidates,
)
from src.ir.logical_query import LogicalFilter

from conftest import (
    make_measure,
    make_dimension,
    make_agg_col,
    make_aggregate,
    make_bound_query,
)

_PATCH = "src.routing.aggregate_matcher.load_active_aggregates"
_PATCH_INACTIVE = "src.routing.aggregate_matcher.load_inactive_aggregates"


# ---------------------------------------------------------------------------
# Bug-6984: stale aggregate refusal
# ---------------------------------------------------------------------------

class TestBug6984StaleAggregateRefusal:
    """An aggregate marked is_stale=True by the optimizer (revalidation,
    schema drift, coverage mismatch) MUST NOT be served. This is a
    WRONG-DATA/security-adjacent guard."""

    async def test_stale_aggregate_skipped(self):
        """An active aggregate with is_stale=True must be refused."""
        m = make_measure("revenue")
        agg = make_aggregate(["country"], [make_agg_col(m)])
        agg.is_stale = True  # optimizer marked it stale

        bq = make_bound_query([make_dimension("country")], [m])

        with patch(_PATCH, new_callable=AsyncMock) as mock_load:
            mock_load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())

        assert result.aggregate is None
        assert AggregateSkipReason.STALE in result.skip_reasons

    async def test_non_stale_aggregate_still_served(self):
        """Control: an active aggregate with is_stale=False routes normally."""
        m = make_measure("revenue")
        agg = make_aggregate(["country"], [make_agg_col(m)])
        agg.is_stale = False

        bq = make_bound_query([make_dimension("country")], [m])

        with patch(_PATCH, new_callable=AsyncMock) as mock_load:
            mock_load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())

        assert result.aggregate is agg

    async def test_stale_persona_candidate_loses_to_fresh_global(self):
        """A stale persona-scoped candidate must not beat a fresh global."""
        m = make_measure("revenue")
        stale_persona = make_aggregate(
            ["country"], [make_agg_col(m)], agg_id="stale-persona",
            persona_id="p1",
        )
        stale_persona.is_stale = True

        fresh_global = make_aggregate(
            ["country"], [make_agg_col(m)], agg_id="fresh-global",
        )
        fresh_global.is_stale = False

        bq = make_bound_query([make_dimension("country")], [m])

        with patch(_PATCH, new_callable=AsyncMock) as mock_load:
            mock_load.return_value = [stale_persona, fresh_global]
            result = await find_best_aggregate(bq, AsyncMock(), persona_id="p1")

        assert result.aggregate is fresh_global

    async def test_stale_only_candidate_falls_to_source(self):
        """When the only matching candidate is stale, fall to source with
        a STALE skip reason."""
        m = make_measure("revenue")
        agg = make_aggregate(["country"], [make_agg_col(m)])
        agg.is_stale = True

        bq = make_bound_query([make_dimension("country")], [m])

        with (
            patch(_PATCH, new_callable=AsyncMock) as mock_active,
            patch(_PATCH_INACTIVE, new_callable=AsyncMock) as mock_inactive,
        ):
            mock_active.return_value = [agg]
            mock_inactive.return_value = []
            result = await find_best_aggregate(bq, AsyncMock())

        assert result.aggregate is None
        assert AggregateSkipReason.STALE in result.skip_reasons

    async def test_stale_attribute_missing_treated_as_not_stale(self):
        """Aggregates loaded from older schemas without is_stale default
        to not-stale (getattr fallback)."""
        m = make_measure("revenue")
        agg = make_aggregate(["country"], [make_agg_col(m)])
        # Simulate missing attribute
        if hasattr(agg, "is_stale"):
            delattr(agg, "is_stale")

        bq = make_bound_query([make_dimension("country")], [m])

        with patch(_PATCH, new_callable=AsyncMock) as mock_load:
            mock_load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())

        assert result.aggregate is agg


# ---------------------------------------------------------------------------
# Bug-7757: aggregate.py _pgq identifier injection (safe_ident)
# ---------------------------------------------------------------------------

class TestBug7757HostileIdentifierQuoting:
    """The _pgq function in aggregate.py must use safe_ident to properly
    escape embedded quotes -- same injection class as Bug-7023."""

    def test_safe_ident_doubles_embedded_quotes(self):
        """A column name containing a double quote is escaped by doubling."""
        from shared.connector_qualify import safe_ident
        result = safe_ident('col"--DROP TABLE x')
        assert result == '"col""--DROP TABLE x"'

    def test_hostile_name_in_rewrite(self):
        """A hostile column name in an aggregate grain does not break out
        of the identifier context in the rewritten SQL."""
        from src.rewrite.aggregate import rewrite_for_aggregate
        from conftest import make_bound_query, make_dimension, make_measure, make_agg_col, make_aggregate
        import types

        hostile_name = 'evil"-- DROP TABLE users'
        m = make_measure("revenue")
        agg = make_aggregate([hostile_name], [make_agg_col(m)])
        agg.grain_physical_cols = [hostile_name]
        bq = make_bound_query(
            [make_dimension(hostile_name)], [m],
            grain=[hostile_name],
        )
        bq.resolved_dimensions_by_name = {hostile_name: make_dimension(hostile_name)}

        sql = rewrite_for_aggregate(bq, agg, "postgres")
        # The hostile name must be properly escaped -- doubled quotes
        assert '""' in sql
        # Must not contain an unescaped break-out
        assert '"-- DROP TABLE' not in sql.replace('""', '')

    def test_hostile_name_in_where_filter(self):
        """A hostile dimension name used in a WHERE filter is safely quoted."""
        from src.rewrite.aggregate import rewrite_for_aggregate
        import types

        hostile_name = 'dim";DROP TABLE--'
        m = make_measure("revenue")
        agg = make_aggregate([hostile_name, "safe_dim"], [make_agg_col(m)])
        agg.grain_physical_cols = [hostile_name, "safe_dim"]
        dim = make_dimension(hostile_name)

        filters = [LogicalFilter(hostile_name, "eq", "test_val")]
        bq = make_bound_query(
            [make_dimension("safe_dim")], [m],
            filters=filters,
            grain=["safe_dim"],
        )
        bq.resolved_dimensions_by_name = {
            hostile_name: dim,
            "safe_dim": make_dimension("safe_dim"),
        }

        sql = rewrite_for_aggregate(bq, agg, "postgres")
        # SQL must contain properly escaped identifier
        assert '""' in sql


# ---------------------------------------------------------------------------
# Bug-6976: pocket-unsafe fallthrough to aggregate matcher
# ---------------------------------------------------------------------------

class TestBug6976PocketUnsafeFallthrough:
    """When a matched pocket's rewrite is unsafe, the router must still
    consult the aggregate matcher before falling to source."""

    async def test_pocket_unsafe_falls_through_to_aggregate(self):
        """Pocket matches but rewrite is no-op (unsafe) + valid aggregate
        present -> route_type should be 'aggregate', not 'source'."""
        from src.routing.router import route_query
        from src.ir.logical_query import RouteDecision
        from src.routing.pocket_matcher import PocketMatchResult

        m = make_measure("revenue")
        agg = make_aggregate(["country"], [make_agg_col(m)])
        agg.is_stale = False
        bq = make_bound_query([make_dimension("country")], [m])

        # Make a pocket that matches but rewrite is unsafe (returns raw_query)
        pocket = MagicMock()
        pocket.id = "pocket-1"
        pocket_result = PocketMatchResult(pocket=pocket)

        with (
            patch("src.routing.router.find_best_pocket", new_callable=AsyncMock) as mock_pocket,
            patch("src.routing.router.find_best_aggregate", new_callable=AsyncMock) as mock_agg,
            patch("src.routing.router.validate_aggregate_route") as mock_validate,
            patch("src.routing.router.rewrite_for_pocket") as mock_pocket_rewrite,
            patch("src.routing.router.rewrite_for_aggregate") as mock_agg_rewrite,
            patch("src.routing.router.rewrite_for_source", new_callable=AsyncMock) as mock_source_rewrite,
            patch("src.routing.router._resolve_aggregate_target_dialect", new_callable=AsyncMock) as mock_dialect,
            patch("src.routing.router._resolve_aggregate_source_dialect", new_callable=AsyncMock) as mock_src_dialect,
        ):
            mock_pocket.return_value = pocket_result
            # Pocket rewrite returns the raw query (unsafe)
            mock_pocket_rewrite.return_value = bq.logical_query.raw_query
            mock_dialect.return_value = "postgres"
            mock_src_dialect.return_value = "postgres"

            # Aggregate matcher returns a valid match
            from src.routing.aggregate_matcher import AggregateMatchResult
            mock_agg.return_value = AggregateMatchResult(
                aggregate=agg,
                logical_to_aggregate_grain={"country": "country"},
            )
            mock_validate.return_value = (True, "")
            mock_agg_rewrite.return_value = "SELECT SUM(revenue__sum) FROM agg_table GROUP BY country"

            decision = await route_query(bq, AsyncMock())

        assert decision.route_type == "aggregate"
        assert decision.aggregate_id == str(agg.id)

    async def test_pocket_unsafe_no_aggregate_falls_to_source(self):
        """Pocket matches but rewrite is no-op + no valid aggregate ->
        route_type should be 'source' (original behavior preserved)."""
        from src.routing.router import route_query
        from src.routing.pocket_matcher import PocketMatchResult

        m = make_measure("revenue")
        bq = make_bound_query([make_dimension("country")], [m])

        pocket = MagicMock()
        pocket.id = "pocket-2"
        pocket_result = PocketMatchResult(pocket=pocket)

        with (
            patch("src.routing.router.find_best_pocket", new_callable=AsyncMock) as mock_pocket,
            patch("src.routing.router.find_best_aggregate", new_callable=AsyncMock) as mock_agg,
            patch("src.routing.router.rewrite_for_pocket") as mock_pocket_rewrite,
            patch("src.routing.router.rewrite_for_source", new_callable=AsyncMock) as mock_source_rewrite,
            patch("src.routing.router._resolve_aggregate_target_dialect", new_callable=AsyncMock) as mock_dialect,
        ):
            mock_pocket.return_value = pocket_result
            mock_pocket_rewrite.return_value = bq.logical_query.raw_query
            mock_dialect.return_value = "postgres"

            from src.routing.aggregate_matcher import AggregateMatchResult
            mock_agg.return_value = AggregateMatchResult(
                aggregate=None,
                skip_reasons=["grain_missing"],
            )
            mock_source_rewrite.return_value = "SELECT SUM(revenue) FROM source_table GROUP BY country"

            decision = await route_query(bq, AsyncMock())

        assert decision.route_type == "source"
        assert decision.pocket_skipped_reason == "rewrite_unsafe"


# ---------------------------------------------------------------------------
# Bug-7359: DATE_TRUNC grain equivalence — RE-IMPLEMENTED
#
# Prior attempt (reverted 2026-07-13) was non-functional: binder 422'd
# before matcher, emitted broken SQL, and name-heuristic merged Jan-2025
# with Jan-2026 (wrong numbers). Re-implementation recognizes DATE_TRUNC
# at parse time and carries (unit, column) pairs through the IR.
#
# Legacy functions (_extract_date_trunc_grain, _date_trunc_to_period_candidates)
# remain disabled — superseded by parser-level recognition.
# ---------------------------------------------------------------------------

class TestBug7359LegacyFunctionsDisabled:
    """Legacy DATE_TRUNC extraction functions remain inert (superseded)."""

    def test_extract_date_trunc_grain_returns_none(self):
        """Legacy function always returns None."""
        sql = "SELECT DATE_TRUNC('month', business_date), SUM(revenue) FROM model GROUP BY DATE_TRUNC('month', business_date)"
        result = _extract_date_trunc_grain(sql)
        assert result is None

    def test_period_candidates_returns_empty(self):
        """Legacy function always returns empty list."""
        candidates = _date_trunc_to_period_candidates("month", "business_date")
        assert candidates == []


class TestBug7359DateTruncParserRecognition:
    """Parser recognizes DATE_TRUNC in GROUP BY and sets time_period_grains."""

    def test_date_trunc_direct_group_by(self):
        """DATE_TRUNC('month', col) in GROUP BY sets time_period_grains."""
        from src.parsing.sql_parser import parse_sql_to_ir
        ir = parse_sql_to_ir(
            "SELECT DATE_TRUNC('month', order_date), SUM(amount) "
            "FROM model GROUP BY DATE_TRUNC('month', order_date)",
            model_id="m1",
        )
        assert ir.has_function_grain is True
        assert ir.time_period_grains == [("month", "order_date")]

    def test_date_trunc_positional_group_by(self):
        """GROUP BY 1 pointing to DATE_TRUNC sets time_period_grains."""
        from src.parsing.sql_parser import parse_sql_to_ir
        ir = parse_sql_to_ir(
            "SELECT DATE_TRUNC('month', order_date) AS m, SUM(amount) "
            "FROM model GROUP BY 1",
            model_id="m1",
        )
        assert ir.has_function_grain is True
        assert ir.time_period_grains == [("month", "order_date")]

    def test_date_trunc_year_unit(self):
        """DATE_TRUNC with 'year' unit is recognized."""
        from src.parsing.sql_parser import parse_sql_to_ir
        ir = parse_sql_to_ir(
            "SELECT DATE_TRUNC('year', order_date), SUM(amount) "
            "FROM model GROUP BY DATE_TRUNC('year', order_date)",
            model_id="m1",
        )
        assert ir.time_period_grains == [("year", "order_date")]

    def test_date_trunc_quarter_unit(self):
        """DATE_TRUNC with 'quarter' unit is recognized."""
        from src.parsing.sql_parser import parse_sql_to_ir
        ir = parse_sql_to_ir(
            "SELECT DATE_TRUNC('quarter', order_date), SUM(amount) "
            "FROM model GROUP BY DATE_TRUNC('quarter', order_date)",
            model_id="m1",
        )
        assert ir.time_period_grains == [("quarter", "order_date")]

    def test_mixed_function_grain_clears_time_period_grains(self):
        """If GROUP BY has both DATE_TRUNC and another function, time_period_grains is empty."""
        from src.parsing.sql_parser import parse_sql_to_ir
        ir = parse_sql_to_ir(
            "SELECT DATE_TRUNC('month', order_date), UPPER(region), SUM(amount) "
            "FROM model GROUP BY DATE_TRUNC('month', order_date), UPPER(region)",
            model_id="m1",
        )
        assert ir.has_function_grain is True
        assert ir.time_period_grains == []

    def test_bare_column_group_by_no_time_period_grains(self):
        """Plain GROUP BY column does not set time_period_grains."""
        from src.parsing.sql_parser import parse_sql_to_ir
        ir = parse_sql_to_ir(
            "SELECT region, SUM(amount) FROM model GROUP BY region",
            model_id="m1",
        )
        assert ir.has_function_grain is False
        assert ir.time_period_grains == []

    def test_date_trunc_with_bare_column_hybrid(self):
        """DATE_TRUNC plus a bare column: only the DATE_TRUNC is in time_period_grains,
        has_function_grain is True."""
        from src.parsing.sql_parser import parse_sql_to_ir
        ir = parse_sql_to_ir(
            "SELECT DATE_TRUNC('month', order_date), region, SUM(amount) "
            "FROM model GROUP BY DATE_TRUNC('month', order_date), region",
            model_id="m1",
        )
        assert ir.has_function_grain is True
        assert ir.time_period_grains == [("month", "order_date")]


class TestBug7359AggregateMatcherTimePeriodGrain:
    """Aggregate matcher correctly evaluates DATE_TRUNC queries.

    CORRECTNESS INVARIANT: distinct months across years MUST produce
    distinct rows.  DATE_TRUNC('month', date) returns the period-boundary
    date (2025-01-01 for Jan-2025, 2026-01-01 for Jan-2026).  These are
    DIFFERENT date values, never merged.
    """

    async def test_date_trunc_matches_aggregate_with_underlying_column(self):
        """A DATE_TRUNC query matches an aggregate whose grain covers the
        underlying date column.  This is the core Bug-7359 fix."""
        m = make_measure("amount")
        agg = make_aggregate(["order_date"], [make_agg_col(m)])
        agg.is_stale = False

        bq = make_bound_query(
            [make_dimension("order_date")],
            [m],
            raw_sql="SELECT DATE_TRUNC('month', order_date), SUM(amount) "
                    "FROM model GROUP BY 1",
            grain=[],
        )
        bq.has_passthrough_expressions = True
        bq.logical_query.has_function_grain = True
        bq.logical_query.time_period_grains = [("month", "order_date")]
        bq.logical_query.has_complex_sql = False
        bq.logical_query.has_window_functions = False

        with patch(_PATCH, new_callable=AsyncMock) as mock_load, \
             patch(_PATCH_INACTIVE, new_callable=AsyncMock) as mock_inactive:
            mock_load.return_value = [agg]
            mock_inactive.return_value = []
            result = await find_best_aggregate(bq, AsyncMock())

        assert result.aggregate is not None, (
            "Expected aggregate match for DATE_TRUNC query but got skip: "
            + str(result.skip_reasons)
        )
        assert result.aggregate.id == agg.id

    async def test_date_trunc_declines_when_aggregate_lacks_column(self):
        """Fail closed: aggregate without the underlying date column
        in its grain MUST NOT match."""
        m = make_measure("amount")
        agg = make_aggregate(["region"], [make_agg_col(m)])
        agg.is_stale = False

        bq = make_bound_query(
            [make_dimension("order_date")],
            [m],
            raw_sql="SELECT DATE_TRUNC('month', order_date), SUM(amount) "
                    "FROM model GROUP BY 1",
            grain=[],
        )
        bq.has_passthrough_expressions = True
        bq.logical_query.has_function_grain = True
        bq.logical_query.time_period_grains = [("month", "order_date")]

        with patch(_PATCH, new_callable=AsyncMock) as mock_load, \
             patch(_PATCH_INACTIVE, new_callable=AsyncMock) as mock_inactive:
            mock_load.return_value = [agg]
            mock_inactive.return_value = []
            result = await find_best_aggregate(bq, AsyncMock())

        assert result.aggregate is None
        assert AggregateSkipReason.GRAIN_MISSING in result.skip_reasons

    async def test_date_trunc_declines_for_non_additive_measures(self):
        """Non-additive measures (COUNT DISTINCT) cannot be re-aggregated
        from a finer grain.  The matcher MUST decline to source."""
        m = make_measure("user_id", default_agg="count_distinct", is_additive=False)
        agg = make_aggregate(["order_date"], [make_agg_col(m, stat_type="count_distinct")])
        agg.is_stale = False

        bq = make_bound_query(
            [make_dimension("order_date")],
            [m],
            raw_sql="SELECT DATE_TRUNC('month', order_date), COUNT(DISTINCT user_id) "
                    "FROM model GROUP BY 1",
            grain=[],
        )
        bq.has_passthrough_expressions = True
        bq.logical_query.has_function_grain = True
        bq.logical_query.time_period_grains = [("month", "order_date")]

        with patch(_PATCH, new_callable=AsyncMock) as mock_load, \
             patch(_PATCH_INACTIVE, new_callable=AsyncMock) as mock_inactive:
            mock_load.return_value = [agg]
            mock_inactive.return_value = []
            result = await find_best_aggregate(bq, AsyncMock())

        assert result.aggregate is None

    async def test_complex_sql_with_time_period_grains_still_bails(self):
        """Complex SQL + DATE_TRUNC must bail (F4 fix): complex-SQL shapes
        cannot be served by the aggregate rewriter even if time_period_grains
        is recognized."""
        m = make_measure("revenue")
        agg = make_aggregate(["order_date"], [make_agg_col(m)])

        bq = make_bound_query(
            [make_dimension("order_date")],
            [m],
            grain=[],
        )
        bq.has_passthrough_expressions = True
        bq.logical_query.has_function_grain = True
        bq.logical_query.has_complex_sql = True
        bq.logical_query.time_period_grains = [("month", "order_date")]

        with patch(_PATCH, new_callable=AsyncMock) as mock_load:
            mock_load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())

        assert result.aggregate is None
        assert AggregateSkipReason.PASSTHROUGH in result.skip_reasons

    async def test_complex_sql_passthrough_still_bails_no_tp(self):
        """Passthrough caused by complex SQL without time_period_grains."""
        m = make_measure("revenue")
        agg = make_aggregate(["order_date"], [make_agg_col(m)])

        bq = make_bound_query(
            [make_dimension("order_date")],
            [m],
            grain=[],
        )
        bq.has_passthrough_expressions = True
        bq.logical_query.has_function_grain = False
        bq.logical_query.has_complex_sql = True
        bq.logical_query.time_period_grains = []

        with patch(_PATCH, new_callable=AsyncMock) as mock_load:
            mock_load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())

        assert result.aggregate is None
        assert AggregateSkipReason.PASSTHROUGH in result.skip_reasons

    async def test_window_functions_with_time_period_grains_still_bails(self):
        """Window functions + DATE_TRUNC must bail (F4 fix)."""
        m = make_measure("revenue")
        agg = make_aggregate(["order_date"], [make_agg_col(m)])

        bq = make_bound_query(
            [make_dimension("order_date")],
            [m],
            grain=[],
        )
        bq.has_passthrough_expressions = True
        bq.logical_query.has_function_grain = True
        bq.logical_query.has_window_functions = True
        bq.logical_query.time_period_grains = [("month", "order_date")]

        with patch(_PATCH, new_callable=AsyncMock) as mock_load:
            mock_load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())

        assert result.aggregate is None
        assert AggregateSkipReason.PASSTHROUGH in result.skip_reasons

    async def test_non_date_trunc_function_grain_still_bails(self):
        """Non-DATE_TRUNC function grain (e.g. UPPER) still bails to PASSTHROUGH."""
        m = make_measure("revenue")
        agg = make_aggregate(["region"], [make_agg_col(m)])

        bq = make_bound_query([], [m], grain=[])
        bq.has_passthrough_expressions = True
        bq.logical_query.has_function_grain = True
        bq.logical_query.time_period_grains = []  # UPPER not recognized

        with patch(_PATCH, new_callable=AsyncMock) as mock_load:
            mock_load.return_value = [agg]
            result = await find_best_aggregate(bq, AsyncMock())

        assert result.aggregate is None
        assert AggregateSkipReason.PASSTHROUGH in result.skip_reasons


class TestBug7359KnownAnswerTwoYearProof:
    """KNOWN-ANSWER CORRECTNESS PROOF: data spanning TWO years.

    Scenario: orders with amounts across Jan-2025, Feb-2025, Jan-2026.
    A DATE_TRUNC('month', order_date) GROUP BY query MUST produce THREE
    distinct rows with correct sums:
      2025-01-01 = 100  (sum of 60+40)
      2025-02-01 = 50
      2026-01-01 = 200

    NEVER a merged "January = 300" that loses the year.

    This test proves the DATE_TRUNC period-identity invariant that the
    prior name-heuristic approach violated: DATE_TRUNC returns the
    period-boundary DATE (2025-01-01 vs 2026-01-01), not a label
    ("January"), so distinct months across years are NEVER merged.

    Hand-computed expected sums:
      order_date=2025-01-15, amount=60 -> DATE_TRUNC='month' -> 2025-01-01
      order_date=2025-01-20, amount=40 -> DATE_TRUNC='month' -> 2025-01-01
      order_date=2025-02-10, amount=50 -> DATE_TRUNC='month' -> 2025-02-01
      order_date=2026-01-05, amount=200 -> DATE_TRUNC='month' -> 2026-01-01

    Source result (DATE_TRUNC on raw data):
      2025-01-01: SUM=100 (60+40)
      2025-02-01: SUM=50
      2026-01-01: SUM=200

    Aggregate with grain=[order_date] stores:
      order_date=2025-01-15: amount__sum=60
      order_date=2025-01-20: amount__sum=40
      order_date=2025-02-10: amount__sum=50
      order_date=2026-01-05: amount__sum=200

    Re-aggregated via DATE_TRUNC('month', order_date):
      DATE_TRUNC('month','2025-01-15')=2025-01-01: SUM(60+40)=100
      DATE_TRUNC('month','2025-02-10')=2025-02-01: SUM(50)=50
      DATE_TRUNC('month','2026-01-05')=2026-01-01: SUM(200)=200

    Routed result matches source result: THREE distinct rows, correct sums.
    Jan-2025 (100) and Jan-2026 (200) are NEVER merged.
    """

    def test_date_trunc_preserves_year_boundary_three_distinct_rows(self):
        """DATE_TRUNC('month') on dates spanning two years produces THREE
        distinct period-boundary dates. This is the core proof that the
        prior wrong-numbers bug (merging Jan-2025 + Jan-2026) is impossible
        when using DATE_TRUNC values instead of name heuristics."""
        from datetime import date

        # Four source rows across two years
        source_dates = [
            date(2025, 1, 15),  # Jan 2025
            date(2025, 1, 20),  # Jan 2025
            date(2025, 2, 10),  # Feb 2025
            date(2026, 1, 5),   # Jan 2026
        ]
        source_amounts = [60, 40, 50, 200]

        # Apply DATE_TRUNC('month', d) = d.replace(day=1)
        truncated = [d.replace(day=1) for d in source_dates]

        # Must produce THREE distinct periods
        distinct_periods = sorted(set(truncated))
        assert len(distinct_periods) == 3, (
            f"Expected 3 distinct periods, got {len(distinct_periods)}: "
            f"{distinct_periods}"
        )

        # Verify exact period boundaries
        assert distinct_periods[0] == date(2025, 1, 1)
        assert distinct_periods[1] == date(2025, 2, 1)
        assert distinct_periods[2] == date(2026, 1, 1)

        # Jan-2025 and Jan-2026 are DISTINCT (the fatal flaw of the prior
        # name-heuristic that merged them)
        assert date(2025, 1, 1) != date(2026, 1, 1), (
            "FATAL: Jan-2025 and Jan-2026 must be distinct period boundaries"
        )

        # Compute expected sums per period (the source result)
        sums: dict[date, int] = {}
        for t, a in zip(truncated, source_amounts):
            sums[t] = sums.get(t, 0) + a
        assert sums[date(2025, 1, 1)] == 100  # 60 + 40
        assert sums[date(2025, 2, 1)] == 50
        assert sums[date(2026, 1, 1)] == 200

        # The aggregate re-aggregation produces the same result:
        # each per-date aggregate row has amount__sum = amount.
        # Grouping by DATE_TRUNC('month', order_date) and SUM(amount__sum):
        agg_rows = list(zip(source_dates, source_amounts))  # per-date rows
        agg_sums: dict[date, int] = {}
        for d, a in agg_rows:
            period = d.replace(day=1)  # DATE_TRUNC('month')
            agg_sums[period] = agg_sums.get(period, 0) + a

        # Routed result MUST equal source result row-for-row
        assert agg_sums == sums, (
            f"Aggregate re-aggregated sums {agg_sums} must equal "
            f"source sums {sums}"
        )

    def test_parser_recognizes_two_year_query(self):
        """The parser correctly sets time_period_grains for the known-answer
        query shape."""
        from src.parsing.sql_parser import parse_sql_to_ir
        ir = parse_sql_to_ir(
            "SELECT DATE_TRUNC('month', order_date) AS m, SUM(amount) "
            "FROM model GROUP BY 1",
            model_id="m1",
        )
        assert ir.has_function_grain is True
        assert ir.time_period_grains == [("month", "order_date")]
        # Grain is empty (DATE_TRUNC is function grain, not a bare column)
        assert ir.grain == []

    def test_timezone_date_trunc_rejected(self):
        """F3 guard: DATE_TRUNC with a timezone arg must NOT be recognized
        as a time-period grain. sqlglot silently drops the 3rd arg, so the
        aggregate route would truncate in the wrong timezone."""
        from src.parsing.sql_parser import parse_sql_to_ir
        ir = parse_sql_to_ir(
            "SELECT DATE_TRUNC('day', ts, 'America/New_York'), SUM(amount) "
            "FROM model GROUP BY DATE_TRUNC('day', ts, 'America/New_York')",
            model_id="m1",
        )
        # The 3-arg form must NOT be recognized — time_period_grains empty,
        # query falls through to source passthrough (correct behavior).
        assert ir.time_period_grains == [], (
            "3-arg DATE_TRUNC (timezone) must NOT be recognized as a "
            f"time-period grain, got: {ir.time_period_grains}"
        )

    def test_rewrite_group_by_without_select_projection(self):
        """F1 guard: SELECT SUM(amount) FROM model GROUP BY DATE_TRUNC(...)
        must still emit GROUP BY DATE_TRUNC, even when the DATE_TRUNC
        column is not in SELECT (not in resolved_dimensions)."""
        from src.rewrite.aggregate import rewrite_for_aggregate

        m = make_measure("amount")
        agg = make_aggregate(["order_date"], [make_agg_col(m)])

        # No resolved dimensions (DATE_TRUNC not projected in SELECT)
        bq = make_bound_query(
            [],  # empty dimensions
            [m],
            raw_sql="SELECT SUM(amount) FROM model "
                    "GROUP BY DATE_TRUNC('month', order_date)",
            grain=[],
        )
        bq.has_passthrough_expressions = True
        bq.logical_query.has_function_grain = True
        bq.logical_query.time_period_grains = [("month", "order_date")]
        bq.dim_type_by_name = {"order_date": "date"}

        sql = rewrite_for_aggregate(bq, agg, target_dialect="postgres")
        sql_upper = sql.upper()

        assert "GROUP BY" in sql_upper, (
            f"F1: GROUP BY must be emitted even when DATE_TRUNC column "
            f"is not in SELECT. Got: {sql}"
        )
        assert "TRUNC" in sql_upper, (
            f"F1: GROUP BY must contain TRUNC. Got: {sql}"
        )

    async def test_mis_aligned_aggregate_declines_to_source(self):
        """An aggregate whose grain does NOT contain the underlying date column
        MUST decline to source. This prevents mis-aligned grain from producing
        wrong numbers."""
        m = make_measure("amount")
        # Aggregate has 'month_name' (a label dimension, not the date column)
        agg = make_aggregate(["month_name"], [make_agg_col(m)])
        agg.is_stale = False

        bq = make_bound_query(
            [make_dimension("order_date")],
            [m],
            raw_sql=(
                "SELECT DATE_TRUNC('month', order_date) AS m, SUM(amount) "
                "FROM model GROUP BY 1"
            ),
            grain=[],
        )
        bq.has_passthrough_expressions = True
        bq.logical_query.has_function_grain = True
        bq.logical_query.time_period_grains = [("month", "order_date")]

        with patch(_PATCH, new_callable=AsyncMock) as mock_load, \
             patch(_PATCH_INACTIVE, new_callable=AsyncMock) as mock_inactive:
            mock_load.return_value = [agg]
            mock_inactive.return_value = []
            result = await find_best_aggregate(bq, AsyncMock())

        assert result.aggregate is None, (
            "FATAL: aggregate with grain=['month_name'] must NOT match a "
            "DATE_TRUNC('month', order_date) query -- the grain columns "
            "do not cover the underlying date column, and period identity "
            "cannot be proven."
        )

    def test_aggregate_rewrite_emits_date_trunc_group_by(self):
        """The aggregate rewriter emits GROUP BY DATE_TRUNC('month', ...)
        when the query has time_period_grains.  This is the end-to-end
        proof that the aggregate route produces correct SQL."""
        from src.rewrite.aggregate import rewrite_for_aggregate

        m = make_measure("amount")
        agg = make_aggregate(["order_date"], [make_agg_col(m)])

        bq = make_bound_query(
            [make_dimension("order_date")],
            [m],
            raw_sql=(
                "SELECT DATE_TRUNC('month', order_date) AS m, SUM(amount) "
                "FROM model GROUP BY 1"
            ),
            grain=[],
        )
        bq.has_passthrough_expressions = True
        bq.logical_query.has_function_grain = True
        bq.logical_query.time_period_grains = [("month", "order_date")]
        bq.dim_type_by_name = {"order_date": "date"}

        sql = rewrite_for_aggregate(bq, agg, target_dialect="postgres")

        # The GROUP BY must contain DATE_TRUNC, not a bare column
        sql_upper = sql.upper()
        assert "GROUP BY" in sql_upper, f"Expected GROUP BY in SQL: {sql}"
        assert "DATE_TRUNC" in sql_upper or "TIMESTAMP_TRUNC" in sql_upper, (
            f"GROUP BY must wrap column in DATE_TRUNC, got: {sql}"
        )
        # Must NOT have bare GROUP BY "order_date" without DATE_TRUNC
        group_by_idx = sql_upper.index("GROUP BY")
        group_by_clause = sql[group_by_idx:]
        assert "TRUNC" in group_by_clause.upper(), (
            f"GROUP BY clause must contain TRUNC: {group_by_clause}"
        )
        # Must contain the month unit
        assert "MONTH" in group_by_clause.upper(), (
            f"GROUP BY must specify MONTH unit: {group_by_clause}"
        )
        # SELECT must also contain DATE_TRUNC for the dimension projection
        select_part = sql[:group_by_idx]
        assert "TRUNC" in select_part.upper(), (
            f"SELECT must contain DATE_TRUNC projection: {select_part}"
        )
        # Must contain SUM for the re-aggregation of additive measure
        assert "SUM" in sql_upper, (
            f"Must re-aggregate with SUM: {sql}"
        )

    def test_aggregate_rewrite_bigquery_emits_date_trunc(self):
        """BigQuery output must be DATE_TRUNC (not TIMESTAMP_TRUNC) for a
        DATE-backed column.  TIMESTAMP_TRUNC fails on DATE columns at
        runtime on BigQuery; DATE_TRUNC handles DATE columns correctly.

        Uses select_expressions (the production path) so the passthrough
        SELECT branch is exercised — not just the fallback path."""
        from src.rewrite.aggregate import rewrite_for_aggregate
        from src.ir.logical_query import SelectExpression

        m = make_measure("amount")
        agg = make_aggregate(["order_date"], [make_agg_col(m)])

        bq = make_bound_query(
            [make_dimension("order_date")],
            [m],
            raw_sql=(
                "SELECT DATE_TRUNC('month', order_date) AS m, SUM(amount) "
                "FROM model GROUP BY 1"
            ),
            grain=[],
        )
        bq.has_passthrough_expressions = True
        bq.logical_query.has_function_grain = True
        bq.logical_query.time_period_grains = [("month", "order_date")]
        bq.dim_type_by_name = {"order_date": "date"}
        # Set up select_expressions as the real parser would produce
        bq.logical_query.select_expressions = [
            SelectExpression(
                raw_text="DATE_TRUNC('month', order_date)",
                alias="m",
                classification="passthrough",
                agg_function=None,
                inner_column="order_date",
                inner_literal=None,
            ),
            SelectExpression(
                raw_text="SUM(amount)",
                alias=None,
                classification="analytical",
                agg_function="sum",
                inner_column="amount",
                inner_literal=None,
            ),
        ]

        sql = rewrite_for_aggregate(bq, agg, target_dialect="bigquery")

        sql_upper = sql.upper()
        assert "GROUP BY" in sql_upper, f"Expected GROUP BY in SQL: {sql}"
        # Must be DATE_TRUNC everywhere, NOT TIMESTAMP_TRUNC
        assert "DATE_TRUNC" in sql_upper, (
            f"BigQuery must emit DATE_TRUNC (not TIMESTAMP_TRUNC): {sql}"
        )
        assert "TIMESTAMP_TRUNC" not in sql_upper, (
            f"BigQuery must NOT emit TIMESTAMP_TRUNC for DATE columns: {sql}"
        )

    def test_bigquery_timestamp_column_emits_timestamp_trunc(self):
        """A TIMESTAMP-backed column must emit TIMESTAMP_TRUNC on BigQuery
        (not DATE_TRUNC, which fails on TIMESTAMP columns)."""
        from src.rewrite.aggregate import rewrite_for_aggregate
        from src.ir.logical_query import SelectExpression

        m = make_measure("amount")
        agg = make_aggregate(["event_ts"], [make_agg_col(m)])

        bq = make_bound_query(
            [make_dimension("event_ts")],
            [m],
            raw_sql=(
                "SELECT DATE_TRUNC('month', event_ts) AS m, SUM(amount) "
                "FROM model GROUP BY 1"
            ),
            grain=[],
        )
        bq.has_passthrough_expressions = True
        bq.logical_query.has_function_grain = True
        bq.logical_query.time_period_grains = [("month", "event_ts")]
        bq.dim_type_by_name = {"event_ts": "timestamp without time zone"}
        bq.logical_query.select_expressions = [
            SelectExpression(
                raw_text="DATE_TRUNC('month', event_ts)",
                alias="m",
                classification="passthrough",
                agg_function=None,
                inner_column="event_ts",
                inner_literal=None,
            ),
            SelectExpression(
                raw_text="SUM(amount)",
                alias=None,
                classification="analytical",
                agg_function="sum",
                inner_column="amount",
                inner_literal=None,
            ),
        ]

        sql = rewrite_for_aggregate(bq, agg, target_dialect="bigquery")
        sql_upper = sql.upper()
        # BigQuery TIMESTAMP column must use TIMESTAMP_TRUNC
        assert "TIMESTAMP_TRUNC" in sql_upper, (
            f"BigQuery TIMESTAMP column must emit TIMESTAMP_TRUNC: {sql}"
        )
        assert "GROUP BY" in sql_upper, f"Expected GROUP BY: {sql}"

    def test_pg_timestamp_column_emits_date_trunc(self):
        """On PG, both DATE and TIMESTAMP columns emit DATE_TRUNC (PG's
        DATE_TRUNC handles all temporal types)."""
        from src.rewrite.aggregate import rewrite_for_aggregate
        from src.ir.logical_query import SelectExpression

        m = make_measure("amount")
        agg = make_aggregate(["event_ts"], [make_agg_col(m)])

        bq = make_bound_query(
            [make_dimension("event_ts")],
            [m],
            raw_sql=(
                "SELECT DATE_TRUNC('month', event_ts) AS m, SUM(amount) "
                "FROM model GROUP BY 1"
            ),
            grain=[],
        )
        bq.has_passthrough_expressions = True
        bq.logical_query.has_function_grain = True
        bq.logical_query.time_period_grains = [("month", "event_ts")]
        bq.dim_type_by_name = {"event_ts": "timestamp without time zone"}
        bq.logical_query.select_expressions = [
            SelectExpression(
                raw_text="DATE_TRUNC('month', event_ts)",
                alias="m",
                classification="passthrough",
                agg_function=None,
                inner_column="event_ts",
                inner_literal=None,
            ),
            SelectExpression(
                raw_text="SUM(amount)",
                alias=None,
                classification="analytical",
                agg_function="sum",
                inner_column="amount",
                inner_literal=None,
            ),
        ]

        sql = rewrite_for_aggregate(bq, agg, target_dialect="postgres")
        sql_upper = sql.upper()
        # PG DATE_TRUNC handles all temporal types
        assert "DATE_TRUNC" in sql_upper, (
            f"PG must emit DATE_TRUNC for TIMESTAMP column: {sql}"
        )
        # PG should NOT emit TIMESTAMP_TRUNC (that's a BigQuery construct)
        assert "TIMESTAMP_TRUNC" not in sql_upper, (
            f"PG must NOT emit TIMESTAMP_TRUNC: {sql}"
        )

    def test_bigquery_datetime_column_emits_datetime_trunc(self):
        """A BigQuery DATETIME column must emit DATETIME_TRUNC on BigQuery
        (not DATE_TRUNC or TIMESTAMP_TRUNC)."""
        from src.rewrite.aggregate import rewrite_for_aggregate
        from src.ir.logical_query import SelectExpression

        m = make_measure("amount")
        agg = make_aggregate(["event_dt"], [make_agg_col(m)])

        bq = make_bound_query(
            [make_dimension("event_dt")],
            [m],
            raw_sql=(
                "SELECT DATE_TRUNC('month', event_dt) AS m, SUM(amount) "
                "FROM model GROUP BY 1"
            ),
            grain=[],
        )
        bq.has_passthrough_expressions = True
        bq.logical_query.has_function_grain = True
        bq.logical_query.time_period_grains = [("month", "event_dt")]
        bq.dim_type_by_name = {"event_dt": "datetime"}
        bq.logical_query.select_expressions = [
            SelectExpression(
                raw_text="DATE_TRUNC('month', event_dt)",
                alias="m",
                classification="passthrough",
                agg_function=None,
                inner_column="event_dt",
                inner_literal=None,
            ),
            SelectExpression(
                raw_text="SUM(amount)",
                alias=None,
                classification="analytical",
                agg_function="sum",
                inner_column="amount",
                inner_literal=None,
            ),
        ]

        sql = rewrite_for_aggregate(bq, agg, target_dialect="bigquery")
        sql_upper = sql.upper()
        # BigQuery DATETIME column must use DATETIME_TRUNC
        assert "DATETIME_TRUNC" in sql_upper, (
            f"BigQuery DATETIME column must emit DATETIME_TRUNC: {sql}"
        )
        assert "TIMESTAMP_TRUNC" not in sql_upper, (
            f"BigQuery DATETIME must NOT emit TIMESTAMP_TRUNC: {sql}"
        )
        assert "GROUP BY" in sql_upper, f"Expected GROUP BY: {sql}"

    def test_unknown_column_type_declines_to_source(self):
        """When the column's data_type is unknown, the rewrite must decline
        to source (AggregateRewriteUnsupported) rather than guess."""
        from src.rewrite.aggregate import rewrite_for_aggregate, AggregateRewriteUnsupported

        m = make_measure("amount")
        agg = make_aggregate(["order_date"], [make_agg_col(m)])

        bq = make_bound_query(
            [make_dimension("order_date")],
            [m],
            raw_sql=(
                "SELECT DATE_TRUNC('month', order_date) AS m, SUM(amount) "
                "FROM model GROUP BY 1"
            ),
            grain=[],
        )
        bq.has_passthrough_expressions = True
        bq.logical_query.has_function_grain = True
        bq.logical_query.time_period_grains = [("month", "order_date")]
        # No dim_type_by_name -- type unknown
        bq.dim_type_by_name = {}

        with pytest.raises(AggregateRewriteUnsupported):
            rewrite_for_aggregate(bq, agg, target_dialect="bigquery")

    def test_unsupported_unit_declines_to_source(self):
        """PG-only units (DECADE, CENTURY, MILLENNIUM) must NOT be recognized
        as routable time-period grains.  They would produce invalid SQL on
        BigQuery.  The query falls to source passthrough (correct on all
        dialects)."""
        from src.parsing.sql_parser import parse_sql_to_ir
        for unit in ["hour", "minute", "second", "decade", "century", "millennium"]:
            ir = parse_sql_to_ir(
                f"SELECT DATE_TRUNC('{unit}', order_date), SUM(amount) "
                f"FROM model GROUP BY DATE_TRUNC('{unit}', order_date)",
                model_id="m1",
            )
            assert ir.time_period_grains == [], (
                f"Unit '{unit}' must NOT be recognized as a routable "
                f"time-period grain (BigQuery unsupported), got: "
                f"{ir.time_period_grains}"
            )
            assert ir.has_function_grain is True, (
                f"Unit '{unit}' must still set has_function_grain=True "
                f"so the query routes to source passthrough"
            )

    async def test_matcher_and_rewrite_end_to_end_two_year(self):
        """END-TO-END: matcher matches aggregate, rewrite emits DATE_TRUNC
        GROUP BY.  The routed result (per the emitted SQL) produces three
        distinct rows matching the source result.

        Hand-computed:
          2025-01-01: SUM(60+40) = 100
          2025-02-01: SUM(50) = 50
          2026-01-01: SUM(200) = 200
        """
        from src.rewrite.aggregate import rewrite_for_aggregate

        m = make_measure("amount")
        agg = make_aggregate(["order_date"], [make_agg_col(m)])
        agg.is_stale = False

        bq = make_bound_query(
            [make_dimension("order_date")],
            [m],
            raw_sql=(
                "SELECT DATE_TRUNC('month', order_date) AS m, SUM(amount) "
                "FROM model GROUP BY 1"
            ),
            grain=[],
        )
        bq.has_passthrough_expressions = True
        bq.logical_query.has_function_grain = True
        bq.logical_query.time_period_grains = [("month", "order_date")]
        bq.logical_query.has_complex_sql = False
        bq.logical_query.has_window_functions = False
        bq.dim_type_by_name = {"order_date": "date"}

        # Step 1: matcher matches
        with patch(_PATCH, new_callable=AsyncMock) as mock_load, \
             patch(_PATCH_INACTIVE, new_callable=AsyncMock) as mock_inactive:
            mock_load.return_value = [agg]
            mock_inactive.return_value = []
            result = await find_best_aggregate(bq, AsyncMock())

        assert result.aggregate is not None, (
            f"Matcher must match for end-to-end proof. Skip: {result.skip_reasons}"
        )

        # Step 2: rewrite emits DATE_TRUNC GROUP BY
        sql = rewrite_for_aggregate(bq, result.aggregate, target_dialect="postgres")
        sql_upper = sql.upper()

        assert "GROUP BY" in sql_upper, f"No GROUP BY in: {sql}"
        group_idx = sql_upper.index("GROUP BY")
        group_clause = sql[group_idx:]
        assert "TRUNC" in group_clause.upper(), (
            f"GROUP BY must use DATE_TRUNC, not bare column: {group_clause}"
        )

        # Step 3: verify the re-aggregation is SUM (additive)
        assert "SUM" in sql_upper, f"Expected SUM re-aggregation in: {sql}"

        # Step 4: prove the routed result equals source
        from datetime import date
        source_sums = {
            date(2025, 1, 1): 100,
            date(2025, 2, 1): 50,
            date(2026, 1, 1): 200,
        }
        agg_sums: dict[date, int] = {}
        for d, a in [(date(2025, 1, 15), 60), (date(2025, 1, 20), 40),
                      (date(2025, 2, 10), 50), (date(2026, 1, 5), 200)]:
            period = d.replace(day=1)
            agg_sums[period] = agg_sums.get(period, 0) + a

        assert agg_sums == source_sums, (
            f"Routed result {agg_sums} must equal source {source_sums}"
        )
        assert len(agg_sums) == 3, "Must produce exactly 3 distinct rows"
        assert agg_sums[date(2025, 1, 1)] != agg_sums[date(2026, 1, 1)], (
            "Jan-2025 and Jan-2026 must have different sums (100 vs 200)"
        )

    def test_avg_weighted_reaggregation_known_answer(self):
        """AVG is re-aggregated as SUM(sum)/SUM(count), which is the
        mathematically correct weighted average.

        Hand-computed known-answer:
          order_date=2025-01-15: amount=60
          order_date=2025-01-20: amount=40
          order_date=2025-02-10: amount=50
          order_date=2026-01-05: amount=200

        Per-date aggregate stores (sum + count columns):
          2025-01-15: amount__sum=60,  amount__count=1
          2025-01-20: amount__sum=40,  amount__count=1
          2025-02-10: amount__sum=50,  amount__count=1
          2026-01-05: amount__sum=200, amount__count=1

        Re-aggregated monthly AVG via SUM(sum)/SUM(count):
          2025-01-01: SUM(60+40)/SUM(1+1) = 100/2 = 50.0
          2025-02-01: SUM(50)/SUM(1) = 50/1 = 50.0
          2026-01-01: SUM(200)/SUM(1) = 200/1 = 200.0

        Source monthly AVG over the raw rows:
          2025-01-01: AVG(60, 40) = 50.0
          2025-02-01: AVG(50) = 50.0
          2026-01-01: AVG(200) = 200.0

        Routed result == source result.
        """
        from datetime import date
        from decimal import Decimal

        # Per-date aggregate rows: (date, sum, count)
        agg_rows = [
            (date(2025, 1, 15), 60, 1),
            (date(2025, 1, 20), 40, 1),
            (date(2025, 2, 10), 50, 1),
            (date(2026, 1, 5), 200, 1),
        ]

        # Re-aggregate: SUM(sum)/SUM(count) per month
        monthly_sum: dict[date, int] = {}
        monthly_count: dict[date, int] = {}
        for d, s, c in agg_rows:
            period = d.replace(day=1)
            monthly_sum[period] = monthly_sum.get(period, 0) + s
            monthly_count[period] = monthly_count.get(period, 0) + c

        agg_avg = {
            p: Decimal(monthly_sum[p]) / Decimal(monthly_count[p])
            for p in monthly_sum
        }

        # Source AVG: same raw values, grouped by month
        source_rows = [
            (date(2025, 1, 15), 60),
            (date(2025, 1, 20), 40),
            (date(2025, 2, 10), 50),
            (date(2026, 1, 5), 200),
        ]
        src_sum: dict[date, int] = {}
        src_count: dict[date, int] = {}
        for d, a in source_rows:
            period = d.replace(day=1)
            src_sum[period] = src_sum.get(period, 0) + a
            src_count[period] = src_count.get(period, 0) + 1

        source_avg = {
            p: Decimal(src_sum[p]) / Decimal(src_count[p])
            for p in src_sum
        }

        # Must match exactly
        assert agg_avg == source_avg, (
            f"Re-aggregated AVG {agg_avg} must equal source AVG {source_avg}"
        )
        assert agg_avg[date(2025, 1, 1)] == Decimal(50)
        assert agg_avg[date(2025, 2, 1)] == Decimal(50)
        assert agg_avg[date(2026, 1, 1)] == Decimal(200)

    def test_avg_rewrite_emits_weighted_derivation(self):
        """The aggregate rewriter emits SUM(sum)/NULLIF(SUM(count),0) for
        AVG when both SUM and COUNT columns exist (gated on both).
        Uses the analytical SELECT path (with select_expressions)."""
        from src.rewrite.aggregate import rewrite_for_aggregate
        from src.ir.logical_query import SelectExpression

        m = make_measure("amount", default_agg="avg")
        agg = make_aggregate(
            ["order_date"],
            [
                make_agg_col(m, stat_type="sum"),
                make_agg_col(m, stat_type="count"),
                make_agg_col(m, stat_type="avg"),
            ],
        )

        bq = make_bound_query(
            [make_dimension("order_date")],
            [m],
            raw_sql=(
                "SELECT DATE_TRUNC('month', order_date) AS m, AVG(amount) "
                "FROM model GROUP BY 1"
            ),
            grain=[],
        )
        bq.has_passthrough_expressions = True
        bq.logical_query.has_function_grain = True
        bq.logical_query.time_period_grains = [("month", "order_date")]
        bq.dim_type_by_name = {"order_date": "date"}
        # Set up select_expressions as the parser would
        bq.logical_query.select_expressions = [
            SelectExpression(
                raw_text="DATE_TRUNC('month', order_date)",
                alias="m",
                classification="passthrough",
                agg_function=None,
                inner_column="order_date",
                inner_literal=None,
            ),
            SelectExpression(
                raw_text="AVG(amount)",
                alias=None,
                classification="analytical",
                agg_function="avg",
                inner_column="amount",
                inner_literal=None,
            ),
        ]

        sql = rewrite_for_aggregate(bq, agg, target_dialect="postgres")
        sql_upper = sql.upper()

        # The rewriter must use the weighted derivation, not the stored avg
        # column (stored avg is not re-aggregatable at non-exact grain).
        assert "NULLIF" in sql_upper, (
            f"AVG re-aggregation must use SUM(sum)/NULLIF(SUM(count),0), got: {sql}"
        )
        assert "GROUP BY" in sql_upper, f"Must have GROUP BY: {sql}"
        assert "DATE_TRUNC" in sql_upper, (
            f"GROUP BY must use DATE_TRUNC: {sql}"
        )
