"""
Integration test: SQL in → route_query → RouteDecision out.

Mocks:
  - load_active_aggregates (DB call inside aggregate_matcher)
  - validate_aggregate_route (exactness validator) — let it run naturally

Run from tessallite/services/query-router/:
    pytest tests/test_query_flow.py -m integration
"""
import pytest
import types
from unittest.mock import AsyncMock, MagicMock, patch

from src.parsing.sql_parser import parse_sql_to_ir
from src.routing.router import route_query
from src.ir.logical_query import BoundQuery, LogicalFilter, LogicalQuery

from conftest import make_measure, make_dimension, make_agg_col, make_aggregate, make_bound_query

_PATCH_LOAD = "src.routing.aggregate_matcher.load_active_aggregates"
_PATCH_SA_SELECT = "src.routing.router.select"

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Full flow helpers
# ---------------------------------------------------------------------------

def _bind(sql: str, measures: list, dimensions: list) -> BoundQuery:
    """Build a BoundQuery from raw SQL + pre-resolved measures/dimensions."""
    lq = parse_sql_to_ir(sql, "model-1")
    model = types.SimpleNamespace(id="model-1", slug="test")
    return BoundQuery(
        logical_query=lq,
        model=model,
        resolved_measures=measures,
        resolved_dimensions=dimensions,
        resolved_filters=[],
    )


# ---------------------------------------------------------------------------
# Route to aggregate
# ---------------------------------------------------------------------------

async def test_matching_aggregate_produces_aggregate_route():
    m = make_measure("revenue")
    d = make_dimension("country")
    agg = make_aggregate(["country"], [make_agg_col(m)])

    sql = "SELECT country, SUM(revenue) FROM sales GROUP BY country"
    bq = _bind(sql, [m], [d])

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        decision = await route_query(bq, AsyncMock())

    assert decision.route_type == "aggregate"
    assert decision.aggregate_id == str(agg.id)
    assert '"country"' in decision.rewritten_query
    assert '"revenue__sum"' in decision.rewritten_query


# ---------------------------------------------------------------------------
# Fall back to source when no aggregate matches
# ---------------------------------------------------------------------------

async def test_no_aggregate_produces_source_route():
    m = make_measure("revenue")
    d = make_dimension("country")

    sql = "SELECT SUM(revenue) FROM sales GROUP BY country"
    bq = _bind(sql, [m], [d])

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = []   # no aggregates
        decision = await route_query(bq, AsyncMock())

    assert decision.route_type == "source"
    assert decision.aggregate_id is None
    assert decision.rewritten_query == sql   # pass-through


async def test_disabled_model_forces_source_route_without_aggregate_lookup():
    m = make_measure("revenue")
    d = make_dimension("country")
    sql = "SELECT SUM(revenue) FROM sales GROUP BY country"
    bq = _bind(sql, [m], [d])
    bq.model.status = "disabled"

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        decision = await route_query(bq, AsyncMock())

    assert decision.route_type == "source"
    assert decision.aggregate_id is None
    assert "Model is disabled" in decision.reason
    mock_load.assert_not_called()


async def test_aggregations_disabled_forces_source_route_without_aggregate_lookup():
    m = make_measure("revenue")
    d = make_dimension("country")
    sql = "SELECT SUM(revenue) FROM sales GROUP BY country"
    bq = _bind(sql, [m], [d])
    bq.model.aggregations_enabled = False

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        decision = await route_query(bq, AsyncMock())

    assert decision.route_type == "source"
    assert decision.aggregate_id is None
    assert "Aggregations are disabled" in decision.reason
    mock_load.assert_not_called()


# ---------------------------------------------------------------------------
# Grain mismatch forces source fallback
# ---------------------------------------------------------------------------

async def test_grain_mismatch_falls_back_to_source():
    m = make_measure("revenue")
    d = make_dimension("country")
    agg_wrong_grain = make_aggregate(["region"], [make_agg_col(m)])   # region, not country

    sql = "SELECT SUM(revenue) FROM sales GROUP BY country"
    bq = _bind(sql, [m], [d])

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg_wrong_grain]
        decision = await route_query(bq, AsyncMock())

    assert decision.route_type == "source"


# ---------------------------------------------------------------------------
# SQL parsing is included in the flow
# ---------------------------------------------------------------------------

async def test_sql_parsing_integrated_in_route():
    m = make_measure("orders")
    d = make_dimension("month")
    agg = make_aggregate(["month"], [make_agg_col(m, "count")])

    sql = "SELECT COUNT(orders) FROM sales GROUP BY month LIMIT 10"
    bq = _bind(sql, [m], [d])

    # Verify the parsed query has limit
    assert bq.logical_query.limit == 10

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        decision = await route_query(bq, AsyncMock())

    assert decision.route_type == "aggregate"
    assert "LIMIT 10" in decision.rewritten_query


async def test_invalid_referenced_uda_raises_hard_error():
    m = make_measure("revenue")
    d = types.SimpleNamespace(id="d-fx_country", name="fx_country", user_defined_attribute_id="uda-1")

    sql = "SELECT SUM(revenue) FROM sales GROUP BY fx_country"
    bq = _bind(sql, [m], [d])

    invalid_uda = types.SimpleNamespace(
        id="uda-1",
        name="fx_country",
        validated=False,
        validation_error="Source column 'country' no longer exists",
    )

    db = AsyncMock()
    exec_result = types.SimpleNamespace(
        scalars=lambda: types.SimpleNamespace(all=lambda: [invalid_uda])
    )
    db.execute.return_value = exec_result

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load, \
         patch(_PATCH_SA_SELECT, return_value=MagicMock()):
        mock_load.return_value = []
        with pytest.raises(ValueError, match="User-defined attribute 'fx_country' is invalid"):
            await route_query(bq, db)


async def test_invalid_filter_only_hierarchy_level_uda_raises_hard_error():
    bq = make_bound_query([], [], filters=[LogicalFilter("fx_segment", "eq", "A")])
    bq.resolved_dimensions_by_name = {
        "fx_segment": types.SimpleNamespace(
            name="fx_segment",
            user_defined_attribute_id="uda-segment",
        )
    }

    invalid_uda = types.SimpleNamespace(
        id="uda-segment",
        name="fx_segment",
        validated=False,
        validation_error="Referenced source column no longer exists",
    )
    exec_result = types.SimpleNamespace(
        scalars=lambda: types.SimpleNamespace(all=lambda: [invalid_uda])
    )
    db = AsyncMock()
    db.execute.return_value = exec_result

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load, \
         patch(_PATCH_SA_SELECT, return_value=MagicMock()):
        mock_load.return_value = []
        with pytest.raises(ValueError, match="User-defined attribute 'fx_segment' is invalid"):
            await route_query(bq, db)


async def test_hierarchy_level_grain_missing_in_aggregate_routes_to_source():
    m = make_measure("revenue")
    hierarchy_dim = make_dimension("region_level")
    agg = make_aggregate(["country_level"], [make_agg_col(m)])

    sql = "SELECT SUM(revenue) FROM model_table GROUP BY region_level"
    bq = _bind(sql, [m], [hierarchy_dim])

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        decision = await route_query(bq, AsyncMock())

    assert decision.route_type == "source"
    assert decision.aggregate_id is None

async def test_pocket_wins_over_aggregate_when_both_match():
    """Invariant (docs/architecture/architecture_pocket-tables.md §5): when a query is satisfiable
    by BOTH a fresh pocket and a valid aggregate, the pocket path wins.
    This test guards against a future refactor swapping the matcher order
    in router.py.
    """
    from datetime import datetime, timezone
    from src.ir.logical_query import PocketMatchResult

    m = make_measure("revenue")
    d = make_dimension("country")
    agg = make_aggregate(["country"], [make_agg_col(m)])

    pocket = types.SimpleNamespace(
        id="pocket-priority",
        model_id="model-1",
        status="fresh",
        query_fingerprint="fp-priority",
        last_refresh_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        predicates=[],
        physical_table_name="pocket_tbl",
        target_schema="public",
    )

    sql = "SELECT country, SUM(revenue) FROM sales GROUP BY country"
    bq = _bind(sql, [m], [d])

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load, \
         patch("src.routing.router.find_best_pocket", new_callable=AsyncMock) as mock_pocket, \
         patch("src.routing.router.rewrite_for_pocket") as mock_rewrite:
        mock_load.return_value = [agg]
        mock_pocket.return_value = PocketMatchResult(pocket=pocket)
        mock_rewrite.return_value = "SELECT country, SUM(revenue) FROM pocket_tbl GROUP BY country"
        decision = await route_query(bq, AsyncMock())

    assert decision.route_type == "pocket", (
        "Pocket must win over aggregate when both match (priority invariant)"
    )
    assert decision.pocket_id == str(pocket.id)
    assert decision.aggregate_id is None


async def test_specific_query_with_count_100_routes_to_aggregate():
    # The specific query: SELECT account_type_code, sum(base_amount), count(100) FROM modelx group by account_type_code
    m1 = make_measure("base_amount", "sum")
    m2 = make_measure("__row_count", "count")
    d = make_dimension("account_type_code")
    
    agg = make_aggregate(
        ["account_type_code"], 
        [make_agg_col(m1), make_agg_col(m2, "__row_count__count")]
    )

    sql = "SELECT account_type_code, sum(base_amount), count(100) FROM modelx group by account_type_code"
    bq = _bind(sql, [m1, m2], [d])

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        decision = await route_query(bq, AsyncMock())

    assert decision.route_type == "aggregate"
    assert decision.aggregate_id == str(agg.id)
    assert '"base_amount__sum"' in decision.rewritten_query
    assert '"__row_count__count"' in decision.rewritten_query
