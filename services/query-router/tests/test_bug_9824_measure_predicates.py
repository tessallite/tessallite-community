"""Bug-9824 regressions for typed grouped measure predicates.

These tests cover the producer/schema contract, binding authority, route
admission, both SQL renderers, and one executable source/aggregate oracle. A
measure predicate must never be represented as a row-level ``LogicalFilter``.
"""
from __future__ import annotations

import sqlite3
import types
from unittest.mock import AsyncMock, patch

import pytest

from src.api.filter_contract import (
    SemanticMeasureFilter,
    build_logical_measure_filters,
)
from src.api.plugin import PluginExecuteRequest
from src.ir.logical_query import (
    BoundMeasurePredicate,
    LogicalFilter,
    LogicalMeasurePredicate,
    LogicalQuery,
    UnsupportedMeasurePredicateError,
)
from src.rewrite.aggregate import rewrite_for_aggregate
from src.rewrite.query_rewriter import rewrite_for_source
from src.routing.aggregate_matcher import find_best_aggregate
from src.semantic.binder import _bind_measure_predicates

from conftest import (
    make_agg_col,
    make_aggregate,
    make_bound_query,
    make_dimension,
    make_measure,
)
from test_render_golden import (
    FakeDB,
    _agg_col,
    _aggregate,
    _attach_deployed_shape,
    _bound,
    _col,
    _dim,
    _meas,
    _se,
    _tbl,
)


def _logical_measure_query(predicate: LogicalMeasurePredicate) -> LogicalQuery:
    return LogicalQuery(
        model_id="model-9824",
        protocol="plugin",
        raw_query="{}",
        requested_measures=["fee_amount"],
        requested_dimensions=["account_type"],
        filters=[],
        grain=["account_type"],
        order_by=[],
        limit=None,
        offset=None,
        query_fingerprint="bug-9824",
        measure_filters=[predicate],
    )


def _bound_predicate(measure, *, value=10000, aggregation="sum"):
    return BoundMeasurePredicate(
        measure_id=measure.id,
        measure_name=measure.name,
        operator="gt",
        value=value,
        effective_aggregation=aggregation,
        value_type="numeric",
        measure=measure,
    )


def _known_answer_fixture():
    account = _dim("account_type", source_column_id="c-account")
    fee = _meas("fee_amount", source_column_id="c-fee")
    dimension_filter = LogicalFilter(
        dimension_name="account_type", operator="eq", value="CREDIT",
    )
    bound = _bound(
        measures=[fee],
        dimensions=[account],
        filters=[dimension_filter],
        grain=["account_type"],
        raw_query=(
            "SELECT account_type, SUM(fee_amount) AS fee_amount "
            "FROM sales GROUP BY account_type"
        ),
        select_expressions=[
            _se(
                "account_type",
                classification="passthrough",
                inner_column="account_type",
            ),
            _se(
                "SUM(fee_amount) AS fee_amount",
                classification="analytical",
                agg_function="sum",
                inner_column="fee_amount",
                alias="fee_amount",
            ),
        ],
    )
    bound.resolved_measure_filters = [_bound_predicate(fee)]
    db = FakeDB(
        tables=[_tbl("t-fact", "demo.sales", "f")],
        columns=[
            _col("c-account", "t-fact", "account_type"),
            _col("c-fee", "t-fact", "fee_amount", data_type="numeric"),
        ],
        dimensions=[account],
        measures=[fee],
    )
    _attach_deployed_shape(bound, db)
    aggregate = _aggregate(
        ["account_type", "day"],
        [_agg_col("fee_amount", "sum")],
    )
    return bound, db, aggregate


def _sqlite_known_answer_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("ATTACH DATABASE ':memory:' AS demo")
    connection.execute("ATTACH DATABASE ':memory:' AS aggregates")
    connection.execute(
        "CREATE TABLE demo.sales (account_type TEXT, fee_amount NUMERIC)"
    )
    # Neither row individually passes 10,000, but their grouped value does.
    connection.executemany(
        "INSERT INTO demo.sales VALUES (?, ?)",
        [("CREDIT", 6000), ("CREDIT", 6000), ("DEBIT", 20000)],
    )
    connection.execute(
        "CREATE TABLE aggregates.agg_golden "
        "(account_type TEXT, day TEXT, fee_amount__sum NUMERIC)"
    )
    connection.executemany(
        "INSERT INTO aggregates.agg_golden VALUES (?, ?, ?)",
        [("CREDIT", "2026-01-01", 6000), ("CREDIT", "2026-01-02", 6000)],
    )
    return connection


class _MatcherDB:
    """Async setting-read seam without AsyncMock coroutine leakage."""

    info = {}

    async def execute(self, _statement):
        return types.SimpleNamespace(scalar_one_or_none=lambda: None)


def test_bug_9824_plugin_parsing_preserves_typed_measure_identity_and_value():
    request = PluginExecuteRequest(
        project_id="project-9824",
        model_id="model-9824",
        measures=["fee_amount"],
        dimensions=["account_type"],
        filters=[{"dimension": "account_type", "operator": "eq", "values": ["CREDIT"]}],
        measure_filters=[{
            "measure_id": "measure-fee",
            "operator": "gt",
            "values": [10000],
            "effective_aggregation": "sum",
        }],
    )

    logical_measure_filters = build_logical_measure_filters(request.measure_filters)

    assert logical_measure_filters == [
        LogicalMeasurePredicate(
            measure_id="measure-fee",
            operator="gt",
            value=10000,
            effective_aggregation="sum",
        ),
    ]
    assert request.filters[0].dimension == "account_type"


def test_bug_9824_binder_uses_deployed_aggregation_not_producer_hint():
    measure = types.SimpleNamespace(
        id="measure-fee",
        name="fee_amount",
        measure_type="standard",
        default_agg="avg",
        source_column_id="column-fee",
        user_defined_attribute_id=None,
        semi_additive_behavior=None,
    )
    predicate = LogicalMeasurePredicate(
        measure_id="measure-fee",
        operator="gt",
        value=10000,
        effective_aggregation="sum",
    )
    deployed_shape = types.SimpleNamespace(
        columns_by_id={"column-fee": {"data_type": "numeric"}},
    )

    [bound] = _bind_measure_predicates(
        _logical_measure_query(predicate), [measure], deployed_shape,
    )

    assert bound.measure_id == "measure-fee"
    assert bound.measure_name == "fee_amount"
    assert bound.effective_aggregation == "avg"
    assert bound.value == 10000
    assert bound.value_type == "numeric"


@pytest.mark.parametrize(
    ("measure_type", "semi_additive_behavior"),
    [
        pytest.param("calculated", None, id="calculated"),
        pytest.param("standard", "last_non_empty", id="semi-additive"),
    ],
)
def test_bug_9824_unsupported_calculated_and_semi_additive_fail_typed(
    measure_type, semi_additive_behavior,
):
    measure = types.SimpleNamespace(
        id="measure-unsupported",
        name="ending_balance",
        measure_type=measure_type,
        default_agg="sum",
        source_column_id="column-balance",
        user_defined_attribute_id=None,
        semi_additive_behavior=semi_additive_behavior,
    )
    predicate = LogicalMeasurePredicate(
        measure_id=measure.id,
        operator="gt",
        value=1,
        effective_aggregation="sum",
    )

    with pytest.raises(UnsupportedMeasurePredicateError):
        _bind_measure_predicates(
            _logical_measure_query(predicate), [measure], None,
        )


def test_bug_9824_ambiguous_measure_identity_fails_typed():
    first = types.SimpleNamespace(
        id="measure-duplicate", name="fee_amount", measure_type="standard",
        default_agg="sum", source_column_id="column-fee-1",
        user_defined_attribute_id=None, semi_additive_behavior=None,
    )
    second = types.SimpleNamespace(
        id="measure-duplicate", name="fee_amount_copy", measure_type="standard",
        default_agg="sum", source_column_id="column-fee-2",
        user_defined_attribute_id=None, semi_additive_behavior=None,
    )
    predicate = LogicalMeasurePredicate(
        measure_id="measure-duplicate", operator="gt", value=1,
        effective_aggregation="sum",
    )

    with pytest.raises(UnsupportedMeasurePredicateError, match="ambiguous"):
        _bind_measure_predicates(
            _logical_measure_query(predicate), [first, second], None,
        )


@pytest.mark.asyncio
async def test_bug_9824_route_identity_requires_predicate_measure_stat():
    dimension = make_dimension("account_type")
    selected = make_measure("revenue")
    predicate_measure = make_measure("fee_amount")
    bound = make_bound_query([dimension], [selected])
    bound.resolved_measure_filters = [_bound_predicate(predicate_measure)]

    aggregate_without_predicate_stat = make_aggregate(
        ["account_type"], [make_agg_col(selected)], agg_id="agg-without-fee",
    )
    matcher_db = _MatcherDB()
    with patch(
        "src.routing.aggregate_matcher.load_active_aggregates",
        new=AsyncMock(return_value=[aggregate_without_predicate_stat]),
    ):
        result = await find_best_aggregate(bound, matcher_db)
    assert result.aggregate is None

    aggregate_with_predicate_stat = make_aggregate(
        ["account_type"],
        [make_agg_col(selected), make_agg_col(predicate_measure)],
        agg_id="agg-with-fee",
    )
    with patch(
        "src.routing.aggregate_matcher.load_active_aggregates",
        new=AsyncMock(return_value=[aggregate_with_predicate_stat]),
    ):
        result = await find_best_aggregate(bound, matcher_db)
    assert result.aggregate is aggregate_with_predicate_stat


@pytest.mark.asyncio
async def test_bug_9824_source_having_keeps_dimension_where_row_level():
    bound, db, _ = _known_answer_fixture()

    sql = await rewrite_for_source(bound, db, target_dialect="postgres")
    upper = sql.upper()
    assert "GROUP BY" in upper and "HAVING" in upper
    assert upper.index("GROUP BY") < upper.index("HAVING")
    where = upper.split("WHERE", 1)[1].split("GROUP BY", 1)[0]
    assert "ACCOUNT_TYPE" in where
    assert "FEE_AMOUNT" not in where
    having = upper.split("HAVING", 1)[1]
    assert "SUM" in having and "FEE_AMOUNT" in having and "> 10000" in having


def test_bug_9824_aggregate_having_is_after_group_by():
    bound, _, aggregate = _known_answer_fixture()

    sql = rewrite_for_aggregate(bound, aggregate, target_dialect="postgres")
    upper = sql.upper()
    assert "GROUP BY" in upper and "HAVING" in upper
    assert upper.index("GROUP BY") < upper.index("HAVING")
    where = upper.split("WHERE", 1)[1].split("GROUP BY", 1)[0]
    assert "ACCOUNT_TYPE" in where
    assert "FEE_AMOUNT" not in where
    having = upper.split("HAVING", 1)[1]
    assert "SUM" in having and "FEE_AMOUNT__SUM" in having and "> 10000" in having


@pytest.mark.asyncio
async def test_bug_9824_source_and_aggregate_known_answer_parity():
    bound, db, aggregate = _known_answer_fixture()
    connection = _sqlite_known_answer_connection()

    source_sql = await rewrite_for_source(bound, db, target_dialect="postgres")
    aggregate_sql = rewrite_for_aggregate(
        bound, aggregate, target_dialect="postgres",
    )
    source_rows = connection.execute(source_sql).fetchall()
    aggregate_rows = connection.execute(aggregate_sql).fetchall()
    old_row_filter_rows = connection.execute(
        "SELECT account_type, SUM(fee_amount) FROM demo.sales "
        "WHERE account_type = 'CREDIT' AND fee_amount > 10000 "
        "GROUP BY account_type"
    ).fetchall()

    assert source_rows == [("CREDIT", 12000)]
    assert aggregate_rows == source_rows
    assert old_row_filter_rows == []
