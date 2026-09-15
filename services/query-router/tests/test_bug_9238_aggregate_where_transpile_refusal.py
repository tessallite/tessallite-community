"""Bug-9238 [wrong numbers] — an aggregate WHERE fragment that fails to
transpile must REFUSE the aggregate route, never emit PostgreSQL SQL into a
non-PostgreSQL target.

The old fallback concatenated the postgres-canonical clause verbatim when
``render_expression_for_dialect`` raised. On BigQuery/Spark a double-quoted
identifier is a STRING LITERAL, so ``"country" = 'DE'`` became the constant
``'country' = 'DE'`` (always false -> empty result) with no error anywhere.

Test escape: every aggregate WHERE test used a fragment sqlglot renders
cleanly, so the ``except`` branch was never exercised on a non-PG dialect.
Guard: this file (rewriter-level refusal on bigquery, PG fallback intact,
router-level fall-through to source with the reason recorded and logged).
Tier: T2 (shared rewrite boundary, wrong-numbers class).
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest
from sqlglot import exp

from src.ir.logical_query import LogicalFilter
from src.rewrite import aggregate as aggregate_mod
from src.rewrite.aggregate import AggregateRewriteUnsupported, rewrite_for_aggregate

from conftest import make_agg_col, make_aggregate, make_bound_query, make_dimension, make_measure


def _failing_where_renderer(monkeypatch):
    """Make the render boundary raise for a WHERE fragment only, so the
    SELECT/GROUP BY items still render and the WHERE ``except`` is the only
    path exercised."""
    real = aggregate_mod.render_expression_for_dialect

    def _render(expression, target_dialect, **kw):
        if isinstance(expression, exp.Where):
            raise ValueError("simulated sqlglot generation failure")
        return real(expression, target_dialect, **kw)

    monkeypatch.setattr(aggregate_mod, "render_expression_for_dialect", _render)


def _filtered_query():
    m = make_measure("revenue")
    agg = make_aggregate(["country"], [make_agg_col(m)])
    agg.is_stale = False
    bq = make_bound_query(
        [make_dimension("country")], [m],
        filters=[LogicalFilter("country", "eq", "DE")],
        grain=["country"],
    )
    bq.resolved_dimensions_by_name = {"country": make_dimension("country")}
    return bq, agg


@pytest.mark.parametrize("dialect", ["bigquery", "spark"])
def test_bug_9238_where_transpile_failure_refuses_non_pg_aggregate(monkeypatch, dialect):
    _failing_where_renderer(monkeypatch)
    bq, agg = _filtered_query()
    with pytest.raises(AggregateRewriteUnsupported) as exc:
        rewrite_for_aggregate(bq, agg, target_dialect=dialect)
    assert dialect in str(exc.value)


def test_bug_9238_where_transpile_failure_on_postgres_keeps_the_canonical_clause(monkeypatch):
    """The canonical clause IS PostgreSQL, so a PG target may still emit it."""
    _failing_where_renderer(monkeypatch)
    bq, agg = _filtered_query()
    sql = rewrite_for_aggregate(bq, agg, target_dialect="postgres")
    assert "WHERE" in sql.upper()
    assert "'DE'" in sql


def test_bug_9238_healthy_bigquery_where_still_renders():
    """Without a render failure the bigquery WHERE is emitted target-native
    (backticks, never PG double quotes)."""
    bq, agg = _filtered_query()
    sql = rewrite_for_aggregate(bq, agg, target_dialect="bigquery")
    assert "WHERE" in sql.upper()
    assert '"country"' not in sql


async def test_bug_9238_router_falls_through_to_source_and_logs_the_refusal(monkeypatch, caplog):
    from src.routing.aggregate_matcher import AggregateMatchResult
    from src.routing.pocket_matcher import PocketMatchResult
    from src.routing.router import route_query

    _failing_where_renderer(monkeypatch)
    bq, agg = _filtered_query()

    with (
        patch("src.routing.router.find_best_pocket", new_callable=AsyncMock) as mock_pocket,
        patch("src.routing.router.find_best_aggregate", new_callable=AsyncMock) as mock_agg,
        patch("src.routing.router.validate_aggregate_route") as mock_validate,
        patch("src.routing.router.rewrite_for_source", new_callable=AsyncMock) as mock_source,
        patch("src.routing.router._resolve_aggregate_target_dialect", new_callable=AsyncMock) as mock_dialect,
        patch("src.routing.router._resolve_aggregate_source_dialect", new_callable=AsyncMock) as mock_src_dialect,
        patch("src.routing.router.record_aggregate_miss", new_callable=AsyncMock),
    ):
        mock_pocket.return_value = PocketMatchResult()
        mock_agg.return_value = AggregateMatchResult(
            aggregate=agg, logical_to_aggregate_grain={"country": "country"},
        )
        mock_validate.return_value = (True, "")
        mock_dialect.return_value = "bigquery"
        mock_src_dialect.return_value = "bigquery"
        mock_source.return_value = "SELECT `country`, SUM(`revenue`) FROM `src` WHERE `country` = 'DE' GROUP BY `country`"

        with caplog.at_level(logging.WARNING, logger="src.rewrite.aggregate"):
            decision = await route_query(bq, AsyncMock())

    assert decision.route_type == "source"
    assert decision.rewritten_query == mock_source.return_value
    assert "WHERE fragment could not be rendered" in (decision.reason or "")
    assert any("Bug-9238" in rec.getMessage() for rec in caplog.records)
