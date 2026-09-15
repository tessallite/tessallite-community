"""Bug-9837 source-route owner-join regressions.

The live failure was an RLS-protected grouped query whose rolled-up dimension
was omitted from the source ``FROM``/``JOIN`` plan.  The security injector
correctly refused to bind the predicate, but the source planner had discarded
the relation that owned the predicate column.

Test escape: the existing source-render tests exercised selected dimensions
and measures, but none supplied compiled RLS owner metadata for an omitted
dimension, so they could not observe the missing owner relation.
Guard: the grouped source AST must include the proven owner relation while
keeping the security column out of projection and grouping; an unjoinable
owner must reject the rewrite.
Tier: T3 (security and grouped-result correctness).
"""
from __future__ import annotations

from dataclasses import replace

import sqlglot
from sqlglot import exp
import pytest

from conftest import attach_fixture_deployed_shape
from src.ir.logical_query import SemanticBindingError
from src.rewrite.query_rewriter import rewrite_for_source
from src.routing.router import _inject_security_where
from src.security import CompiledPredicate

from test_render_golden import (
    _bound,
    _col,
    _dim,
    _join_db,
    _meas,
    _se,
    _tbl,
)


def _grouped_query_with_owner_table():
    account_type = _dim("account_type", source_column_id="c-account")
    amount = _meas("amount", source_column_id="c-amount")
    bound = _bound(
        measures=[amount],
        dimensions=[account_type],
        grain=["account_type"],
        raw_query=(
            "SELECT account_type, SUM(amount) FROM sales "
            "JOIN country ON sales.country_id = country.id "
            "GROUP BY account_type"
        ),
        select_expressions=[
            _se(
                "account_type",
                classification="passthrough",
                inner_column="account_type",
            ),
            _se(
                "SUM(amount)",
                classification="analytical",
                agg_function="sum",
                inner_column="amount",
            ),
        ],
    )
    db = _join_db(dimensions=[account_type], measures=[amount])
    db.columns.append(_col("c-account", "t-fact", "account_type"))
    db.columns.append(_col("c-owner", "t-dim", "region_code"))
    return bound, db


def _rls_predicate() -> CompiledPredicate:
    return CompiledPredicate(
        sql_expression="region_code = 'EMEA'",
        active_rule_ids=("bug-9837",),
        security_dimension_columns=("region_code",),
        security_column_owners=(("region_code", "country"),),
    )


def _select(sql: str) -> exp.Select:
    tree = sqlglot.parse_one(sql, read="postgres")
    selected = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    assert selected is not None, sql
    return selected


@pytest.mark.asyncio
async def test_bug_9837_grouped_source_keeps_rls_owner_outside_result_shape():
    bound, db = _grouped_query_with_owner_table()
    await attach_fixture_deployed_shape(bound, db)
    compiled = _rls_predicate()

    rewritten = await rewrite_for_source(
        bound,
        db,
        target_dialect="postgres",
        security_column_owners=compiled.security_column_owners,
    )
    sql = _inject_security_where(rewritten, compiled)
    select = _select(sql)

    tables = list(select.find_all(exp.Table))
    assert {table.name.lower() for table in tables} == {"sales", "country"}
    assert {table.alias_or_name.lower() for table in tables} >= {"f", "d"}

    projected_names = {
        column.name.lower()
        for expression in select.expressions
        for column in expression.find_all(exp.Column)
    }
    grouped_names = {
        column.name.lower()
        for expression in (select.args["group"].expressions if select.args.get("group") else [])
        for column in expression.find_all(exp.Column)
    }
    assert "region_code" not in projected_names
    assert "region_code" not in grouped_names

    where_columns = [column for column in select.args["where"].find_all(exp.Column)]
    assert any(
        column.name.lower() == "region_code"
        and column.table.lower() == "d"
        for column in where_columns
    )
    assert "GROUP BY" in sql.upper()
    assert "SUM(" in sql.upper()


@pytest.mark.asyncio
async def test_bug_9837_grouped_source_uses_next_present_owner_candidate():
    bound, db = _grouped_query_with_owner_table()
    await attach_fixture_deployed_shape(bound, db)
    compiled = replace(
        _rls_predicate(),
        security_column_owners=(
            ("region_code", "country_not_in_this_graph"),
            ("region_code", "country"),
        ),
    )

    rewritten = await rewrite_for_source(
        bound,
        db,
        target_dialect="postgres",
        security_column_owners=compiled.security_column_owners,
    )
    sql = _inject_security_where(rewritten, compiled)

    assert '"demo"."country"' in sql
    assert '"demo"."country_not_in_this_graph"' not in sql
    assert any(
        column.name.lower() == "region_code" and column.table.lower() == "d"
        for column in _select(sql).args["where"].find_all(exp.Column)
    )


@pytest.mark.asyncio
async def test_bug_9978_dimension_only_group_starts_from_security_owner_fact():
    country_name = _dim("country_name", source_column_id="c-cname")
    bound = _bound(
        measures=[],
        dimensions=[country_name],
        grain=["country_name"],
        raw_query="SELECT country_name FROM golden GROUP BY country_name",
        select_expressions=[
            _se(
                "country_name",
                classification="passthrough",
                inner_column="country_name",
            ),
        ],
    )
    db = _join_db(dimensions=[country_name])
    await attach_fixture_deployed_shape(bound, db)
    compiled = replace(
        _rls_predicate(),
        security_column_owners=(("region_code", "sales"),),
    )

    rewritten = await rewrite_for_source(
        bound,
        db,
        target_dialect="postgres",
        security_column_owners=compiled.security_column_owners,
    )
    sql = _inject_security_where(rewritten, compiled)
    select = _select(sql)

    assert select.args["from_"].this.alias_or_name == "f"
    assert {table.name.lower() for table in select.find_all(exp.Table)} == {
        "sales",
        "country",
    }
    assert [column.name for column in select.expressions[0].find_all(exp.Column)] == [
        "country_name",
    ]
    assert any(
        column.name.lower() == "region_code" and column.table.lower() == "f"
        for column in select.args["where"].find_all(exp.Column)
    )
    assert "GROUP BY" in sql.upper()


@pytest.mark.asyncio
async def test_bug_9837_unrestricted_select_star_keeps_rls_owner_join():
    bound = _bound(
        measures=[],
        dimensions=[],
        grain=[],
        from_tables=["golden"],
        select_star=True,
        raw_query="SELECT * FROM golden",
    )
    db = _join_db()
    db.columns.append(_col("c-owner", "t-dim", "region_code"))
    await attach_fixture_deployed_shape(bound, db)
    compiled = _rls_predicate()

    rewritten = await rewrite_for_source(
        bound,
        db,
        target_dialect="postgres",
        security_column_owners=compiled.security_column_owners,
    )
    sql = _inject_security_where(rewritten, compiled)
    select = _select(sql)

    assert {table.name.lower() for table in select.find_all(exp.Table)} == {
        "sales",
        "country",
    }
    assert "JOIN" in sql.upper()
    assert any(
        column.name.lower() == "region_code" and column.table.lower() == "d"
        for column in select.args["where"].find_all(exp.Column)
    )


@pytest.mark.asyncio
async def test_bug_9837_count_source_keeps_rls_owner_join():
    row_count = _meas("__row_count", default_agg="count")
    bound = _bound(
        measures=[row_count],
        dimensions=[],
        grain=[],
        from_tables=["golden"],
        raw_query="SELECT COUNT(*) FROM golden",
        select_expressions=[
            _se("COUNT(*)", classification="literal", agg_function="count")
        ],
    )
    db = _join_db(measures=[row_count])
    db.columns.append(_col("c-owner", "t-dim", "region_code"))
    await attach_fixture_deployed_shape(bound, db)
    compiled = _rls_predicate()

    rewritten = await rewrite_for_source(
        bound,
        db,
        target_dialect="postgres",
        security_column_owners=compiled.security_column_owners,
    )
    sql = _inject_security_where(rewritten, compiled)

    assert "COUNT(*)" in sql.upper()
    assert '"demo"."country"' in sql
    assert any(
        column.name.lower() == "region_code" and column.table.lower() == "d"
        for column in _select(sql).args["where"].find_all(exp.Column)
    )


@pytest.mark.asyncio
async def test_bug_9837_no_column_source_without_relation_fails_closed():
    bound = _bound(
        measures=[],
        dimensions=[],
        grain=[],
        raw_query="SELECT 1",
    )
    db = _join_db()
    await attach_fixture_deployed_shape(bound, db)

    with pytest.raises(SemanticBindingError, match="RLS"):
        await rewrite_for_source(
            bound,
            db,
            target_dialect="postgres",
            security_column_owners=(("region_code", "country"),),
        )


@pytest.mark.asyncio
async def test_bug_9837_unreachable_rls_owner_fails_closed():
    bound, db = _grouped_query_with_owner_table()
    db.tables.append(_tbl("t-orphan", "demo.orphan", "o", table_type="dimension"))
    db.columns.append(_col("c-orphan-owner", "t-orphan", "region_code"))
    await attach_fixture_deployed_shape(bound, db)

    with pytest.raises(SemanticBindingError, match="RLS security owner") as exc_info:
        await rewrite_for_source(
            bound,
            db,
            target_dialect="postgres",
            security_column_owners=(("region_code", "orphan"),),
        )

    assert "orphan" not in str(exc_info.value)
