"""Regression proof for Bug-8637 aggregate-build/model-query identity."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from shared.semantic.aggregate_model_query import (
    compile_aggregate_model_query,
)
from shared.semantic.grain_resolver import (
    ResolvedAggregateLayout,
    ResolvedGrainCol,
    ResolvedMeasureCol,
)


@pytest.mark.asyncio
async def test_bug8637_build_sql_is_the_model_compile_join_closure(monkeypatch):
    """Test escape / Guard / Tier: T3.

    The returned build SQL must be byte-identical to the query-router model
    compile, including its join closure.  This fails against the pre-fix
    aggregate builder because no model-query compile exists on that path.
    """
    model_id = uuid4()
    expected_model_compile = (
        'SELECT "region" AS "region", SUM("amount") AS "amount__sum", '
        'COUNT(*) AS "__row_count__count" FROM "modely" '
        'JOIN "customers" ON "orders"."customer_id" = "customers"."id" '
        'GROUP BY "region"'
    )
    layout = ResolvedAggregateLayout(
        grain_cols=[
            ResolvedGrainCol(
                logical_name="region",
                dimension_id=uuid4(),
                source_table_id=uuid4(),
                source_column_name="region",
                physical_col_name="region",
            )
        ],
        measure_cols=[
            ResolvedMeasureCol(
                measure_id=uuid4(),
                measure_name="amount",
                stat_type="sum",
                aggregation_function="sum",
                source_table_id=uuid4(),
                source_column_name="amount",
                physical_col_name="amount__sum",
            )
        ],
    )

    async def fake_compile(model_id_arg, definition_sql, bearer_token):
        assert model_id_arg == model_id
        assert 'FROM "modely"' in definition_sql
        assert 'SELECT\n  "region",' in definition_sql
        assert 'GROUP BY "region"' in definition_sql
        assert bearer_token == "service-token"
        return expected_model_compile

    monkeypatch.setattr(
        "shared.semantic.aggregate_model_query._get_rewritten_sql",
        fake_compile,
    )
    monkeypatch.setattr(
        "shared.semantic.aggregate_model_query._mint_service_token",
        lambda tenant_id: "service-token" if tenant_id == "tenant-a" else "bad",
    )

    compiled = await compile_aggregate_model_query(
        db=SimpleNamespace(info={"tenant_id": "tenant-a"}),
        model_id=model_id,
        model_slug="modely",
        layout=layout,
        measures=[SimpleNamespace(id=layout.measure_cols[0].measure_id, name="amount")],
    )

    assert compiled.sql == expected_model_compile
    assert compiled.output_columns == ("region", "amount__sum", "__row_count__count")


@pytest.mark.asyncio
async def test_bug8637_build_uses_resolved_source_column_inside_aggregate(monkeypatch):
    """Test escape / Guard / Tier: T3.

    A semantic measure alias can differ from its physical source column.  The
    build query must hand the source column to the model compiler inside AVG,
    otherwise the source database sees the alias as a physical column.
    """
    model_id = uuid4()
    measure_id = uuid4()
    layout = ResolvedAggregateLayout(
        grain_cols=[],
        measure_cols=[
            ResolvedMeasureCol(
                measure_id=measure_id,
                measure_name="avg_base_amount",
                stat_type="avg",
                aggregation_function="avg",
                source_table_id=uuid4(),
                source_column_name="base_amount",
                physical_col_name="avg_base_amount__avg",
            )
        ],
    )
    measure = SimpleNamespace(
        id=measure_id,
        name="avg_base_amount",
        measure_type="standard",
        default_agg="avg",
    )
    captured: dict[str, str] = {}

    async def fake_compile(_model_id, definition_sql, _bearer_token):
        captured["raw_query"] = definition_sql
        return definition_sql

    monkeypatch.setattr(
        "shared.semantic.aggregate_model_query._get_rewritten_sql",
        fake_compile,
    )
    monkeypatch.setattr(
        "shared.semantic.aggregate_model_query._mint_service_token",
        lambda _tenant_id: "service-token",
    )

    compiled = await compile_aggregate_model_query(
        db=SimpleNamespace(info={"tenant_id": "tenant-a"}),
        model_id=model_id,
        model_slug="modely",
        layout=layout,
        measures=[measure],
    )

    assert 'AVG("base_amount")' in captured["raw_query"]
    assert 'AVG("avg_base_amount")' not in captured["raw_query"]
    assert compiled.raw_query == captured["raw_query"]
