"""Bug-9914 — ``force_route="raw"`` must carry the joined row-security owner.

Bug-9837 taught the SOURCE rewriter to keep the relation that owns the
row-security column in its join plan. The RAW rewriter was never handed the
same owner metadata, so a raw plan that did not happen to join the owner
dimension reached ``_inject_security_where`` with the owner unscanned and was
refused ``security_column_owner_not_scanned`` -- for a query the product
answers correctly one route over.

Test escape: the raw rewriter tests never supplied compiled owner metadata,
and the RLS routing tests for the raw route used a fact-owned security
column, so the joined-owner shape was never exercised on the raw route.
Guard: this file. Tier: T3 (row-level security route parity).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import sqlglot
from sqlglot import exp

from src.rewrite.raw_sql import RawRouteUnsupported, rewrite_for_raw
from src.routing.router import _inject_security_where, route_query
from src.security import CompiledPredicate, Principal

from conftest import make_dimension, make_measure
from test_query_flow import _bind
from test_rewrite_raw import (
    _MockDB,
    _bound,
    _column,
    _join,
    _measure,
    _patch_graph,
    _table,
)
from test_row_security_routing import _PATCH_LOAD, _PATCH_OWNERS, _db_returning, _role_rule


def _graph(monkeypatch, *, with_join=True):
    fact = _table("payment_transaction", "demo.payment_transaction", table_type="fact")
    dim = _table("dim_channel_code", "demo.dim_channel_code")
    amount = _column("amount", fact, "numeric")
    fact_channel = _column("channel_code", fact, "text")
    dim_channel = _column("channel_code", dim, "text")
    dim_name = _column("channel_name", dim, "text")
    joins = [_join(fact, dim, fact_channel, dim_channel, join_type="left")] if with_join else []
    _patch_graph(monkeypatch, [fact, dim], joins, [amount, fact_channel, dim_channel, dim_name])
    return fact, dim, amount


def _scanned_tables(sql: str) -> set[str]:
    return {t.name for t in sqlglot.parse_one(sql, read="postgres").find_all(exp.Table)}


async def test_bug_9914_owner_dimension_is_joined_into_the_raw_plan(monkeypatch):
    """A measure-only raw query does not reference the owner dimension; the
    owner metadata alone must pull it into the plan so the predicate binds."""
    fact, dim, amount = _graph(monkeypatch)
    bound = _bound([_measure("amount", amount)], [])
    db = _MockDB({}, [], {}, {})

    without = await rewrite_for_raw(bound, db, target_dialect="postgres")
    assert "dim_channel_code" not in _scanned_tables(without)

    with_owner = await rewrite_for_raw(
        bound, db, target_dialect="postgres",
        security_column_owners=(("channel_code", "dim_channel_code"),),
    )
    assert _scanned_tables(with_owner) == {"payment_transaction", "dim_channel_code"}

    compiled = CompiledPredicate(
        sql_expression="\"channel_code\" IN ('WEB', 'APP')",
        active_rule_ids=("r1",),
        security_dimension_columns=("channel_code",),
        security_column_owners=(("channel_code", "dim_channel_code"),),
    )
    secured = _inject_security_where(with_owner, compiled, force_route="raw")
    ast = sqlglot.parse_one(secured, read="postgres")
    bound_cols = {
        col.table for col in ast.args["where"].find_all(exp.Column)
        if col.name == "channel_code"
    }
    assert bound_cols == {"dim_channel_code"}, secured


async def test_bug_9914_owner_absent_from_the_graph_declines_to_source(monkeypatch):
    _, _, amount = _graph(monkeypatch)
    bound = _bound([_measure("amount", amount)], [])
    with pytest.raises(RawRouteUnsupported):
        await rewrite_for_raw(
            bound, _MockDB({}, [], {}, {}), target_dialect="postgres",
            security_column_owners=(("channel_code", "dim_missing"),),
        )


async def test_bug_9914_unjoinable_owner_declines_to_source(monkeypatch):
    """Owner present in the graph but with no join path: never a typed-NULL
    placeholder, never a bare predicate -- hand off to the source route."""
    _, _, amount = _graph(monkeypatch, with_join=False)
    bound = _bound([_measure("amount", amount)], [])
    with pytest.raises(RawRouteUnsupported):
        await rewrite_for_raw(
            bound, _MockDB({}, [], {}, {}), target_dialect="postgres",
            security_column_owners=(("channel_code", "dim_channel_code"),),
        )


async def test_bug_9914_router_hands_the_raw_rewriter_the_compiled_owners():
    """The RLS raw branch of ``route_query`` must pass ``compiled.security_
    column_owners`` to ``rewrite_for_raw`` (it passed nothing), and the
    resulting raw SQL is then secured on the owner scan and served as raw."""
    m = make_measure("amount")
    d = make_dimension("channel_code")
    bq = _bind("SELECT channel_code, amount FROM payment_transaction", [m], [d])

    rule = _role_rule(
        "channel.channel_code",
        "dimension_equals('channel.channel_code', 'WEB')",
        ["account_manager"],
    )
    principal = Principal(user_identity="am@x", roles=frozenset({"account_manager"}))
    owners = (("channel_code", "dim_channel_code"),)
    raw_sql = (
        'SELECT "dim_channel_code"."channel_code" AS "channel_code", '
        '"payment_transaction"."amount" AS "amount" '
        'FROM "demo"."payment_transaction" AS "payment_transaction" '
        'LEFT JOIN "demo"."dim_channel_code" AS "dim_channel_code" '
        'ON "payment_transaction"."channel_code" = "dim_channel_code"."channel_code"'
    )

    with (
        patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load,
        patch(_PATCH_OWNERS, new_callable=AsyncMock) as mock_owners,
        patch("src.routing.router.rewrite_for_raw", new_callable=AsyncMock) as mock_raw,
    ):
        mock_load.return_value = []
        mock_owners.return_value = owners
        mock_raw.return_value = raw_sql
        decision = await route_query(
            bq, _db_returning([rule]), principal=principal, force_route="raw",
        )

    mock_raw.assert_awaited_once()
    assert mock_raw.await_args.kwargs["security_column_owners"] == owners
    assert decision.route_type == "raw"
    where = sqlglot.parse_one(decision.rewritten_query, read="postgres").args["where"]
    bound_cols = {
        (col.table, col.name) for col in where.find_all(exp.Column)
        if col.name == "channel_code"
    }
    assert bound_cols == {("dim_channel_code", "channel_code")}, decision.rewritten_query
    assert "'WEB'" in where.sql(dialect="postgres")
