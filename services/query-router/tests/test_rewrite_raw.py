"""Tests for the raw-route SQL rewriter (rewrite_for_raw).

Invariants under test:
  * No GROUP BY clause in output.
  * No aggregation wrappers (SUM/AVG/COUNT) around measures.
  * Orientation-aware mandatory population joins; optional projection-only
    edges remain LEFT JOINs.
  * Disconnected tables produce typed-NULL projections.
  * Time-variant measures and __row_count emit typed NULLs.
  * Calculated measures with resolvable refs render raw arithmetic.
  * Calculated measures with unreachable refs render typed NULLs.
  * UDA expressions render per-row when reachable.
  * Dialect translation uses _translate_raw_sql.
"""
from __future__ import annotations

import sqlite3
import types
from uuid import uuid4

import pytest
import sqlglot

from src.rewrite.raw_sql import RawRouteUnsupported, rewrite_for_raw
from src.rewrite.joins import _build_joined_from_clause
from src.ir.logical_query import BoundQuery, LogicalQuery
from shared.db.models import UserDefinedAttribute
from shared.semantic.graph_order import pick_anchor_table


pytestmark = pytest.mark.integration


def _uid():
    return uuid4()


def _table(name, physical_name=None, table_type="dim_aggregate", alias=None):
    return types.SimpleNamespace(
        id=_uid(),
        name=name,
        physical_name=physical_name or name,
        table_type=table_type,
        alias=alias or name,
    )


def _column(name, table, data_type="text"):
    return types.SimpleNamespace(
        id=_uid(),
        column_name=name,
        model_table_id=table.id,
        data_type=data_type,
    )


def _join(left_table, right_table, left_col, right_col, join_type="left"):
    return types.SimpleNamespace(
        id=_uid(),
        left_table_id=left_table.id,
        right_table_id=right_table.id,
        left_column_id=left_col.id,
        right_column_id=right_col.id,
        join_type=join_type,
        model_id=None,
    )


def _measure(name, col=None, measure_type="standard", expression=None,
             variant_of_measure_id=None, default_agg="sum", data_type=None,
             user_defined_attribute_id=None):
    return types.SimpleNamespace(
        id=_uid(),
        name=name,
        source_column_id=col.id if col else None,
        measure_type=measure_type,
        expression=expression,
        variant_of_measure_id=variant_of_measure_id,
        default_agg=default_agg,
        is_additive=True,
        data_type=data_type,
        user_defined_attribute_id=user_defined_attribute_id,
    )


def _dimension(name, col=None, user_defined_attribute_id=None,
               calc_expression=None):
    return types.SimpleNamespace(
        id=_uid(),
        name=name,
        source_column_id=col.id if col else None,
        user_defined_attribute_id=user_defined_attribute_id,
        calc_expression=calc_expression,
    )


def _orm_column_names(model_cls) -> set[str]:
    return {column.name for column in model_cls.__table__.columns}


def _orm_stub(model_cls, **values):
    column_names = _orm_column_names(model_cls)
    unknown = set(values) - column_names
    assert not unknown, (
        f"{model_cls.__name__} test stub uses non-ORM attribute(s): "
        f"{', '.join(sorted(unknown))}"
    )
    attrs = {name: None for name in column_names}
    attrs.update(values)
    return types.SimpleNamespace(**attrs)


def _uda(expression, table, output_data_type="text"):
    # Bug-6121 (F-006-01): the ORM ``UserDefinedAttribute`` attribute is
    # ``table_id``, not ``model_table_id``.  The original fixture used the
    # wrong name, masking the production mismatch — the code and fixture
    # agreed on a name the ORM does not have, so tests passed while live
    # queries emitted NULL for every UDA-backed column.
    return _orm_stub(
        UserDefinedAttribute,
        id=_uid(),
        expression=expression,
        table_id=table.id,
        model_id=None,
        output_data_type=output_data_type,
    )


def _bound(measures, dimensions, model_id=None, filters=None):
    mid = model_id or _uid()
    lq = LogicalQuery(
        model_id=str(mid),
        protocol="jdbc",
        raw_query="SELECT * FROM model",
        requested_measures=[m.name for m in measures],
        requested_dimensions=[d.name for d in dimensions],
        filters=filters or [],
        grain=[],
        order_by=[],
        limit=None,
        offset=None,
        query_fingerprint="raw-test",
    )
    model = types.SimpleNamespace(
        id=mid,
        # Bug-7803: the calc-dependency load now resolves referenced base
        # measures from the DEPLOYED SNAPSHOT (fail-closed) for a deployed
        # model. These raw-rewrite tests seed the referenced base measures into
        # a fake DB (``_CalcRefDB``) with no ModelVersion snapshot, so model the
        # world as UNDEPLOYED — the live measures ARE the authority for an
        # undeployed model. Deploy-authority is covered separately by
        # test_bug_7803_calc_dependency_snapshot_authority.py.
        deployed_version_id=None,
        display_name="Test Model",
        slug="test_model",
    )
    return BoundQuery(
        logical_query=lq,
        model=model,
        resolved_measures=measures,
        resolved_dimensions=dimensions,
        resolved_filters=filters or [],
        resolved_dimensions_by_name={d.name: d for d in dimensions},
    )


class _MockDB:
    def __init__(self, tables_by_id, joins, columns_by_id, uda_by_id):
        self._tables_by_id = tables_by_id
        self._joins = joins
        self._columns_by_id = columns_by_id
        self._uda_by_id = uda_by_id

    async def execute(self, *args, **kwargs):
        return _EmptyResult()


class _EmptyResult:
    def scalars(self):
        return self

    def all(self):
        return []


def _patch_graph(monkeypatch, tables, joins_list, columns, udas=None):
    from src.rewrite import raw_sql as raw_sql_mod
    tables_by_id = {t.id: t for t in tables}
    columns_by_id = {c.id: c for c in columns}
    uda_by_id = {u.id: u for u in (udas or [])}

    async def _mock_load(*args, **kwargs):
        return tables_by_id, joins_list, columns_by_id, uda_by_id

    monkeypatch.setattr(raw_sql_mod, "_load_model_graph", _mock_load)

    async def _mock_dialect(*args, **kwargs):
        return "postgres"

    monkeypatch.setattr(raw_sql_mod, "_resolve_target_dialect", _mock_dialect)


@pytest.mark.parametrize("reverse_declared_orientation", [False, True])
async def test_population_defining_inner_raw_route_matches_source_population(
    monkeypatch, reverse_declared_orientation,
):
    """B01: raw serving must preserve an INNER population edge.

    The fact has keys ``{1, 2, 3}`` and the dimension has ``{1, 2}``.  A
    population-defining INNER edge therefore returns ``{1, 2}``, irrespective
    of whether the modeler declared the edge fact -> dimension or the reversed
    dimension -> fact orientation.  The old raw route unconditionally emitted
    LEFT JOIN and returned the extra fact key ``3``.
    """
    fact = _table("fact_sales", table_type="fact")
    dim = _table("dim_region")
    fact_key = _column("id", fact, "int4")
    dim_key = _column("id", dim, "int4")
    amount = _column("amount", fact, "numeric")
    if reverse_declared_orientation:
        join = _join(dim, fact, dim_key, fact_key, join_type="inner")
    else:
        join = _join(fact, dim, fact_key, dim_key, join_type="inner")
    join.population_participation = "population_defining"

    bound = _bound(
        [_measure("amount", amount)],
        [_dimension("fact_id", fact_key)],
    )
    _patch_graph(
        monkeypatch,
        [fact, dim],
        [join],
        [fact_key, dim_key, amount],
    )

    raw_sql = await rewrite_for_raw(bound, _MockDB({}, [], {}, {}), target_dialect="postgres")
    assert "INNER JOIN" in raw_sql.upper()
    assert "LEFT JOIN" not in raw_sql.upper()

    source_from = _build_joined_from_clause(
        base_table_id=fact.id,
        required_table_ids={fact.id, dim.id},
        joins=[join],
        tables_by_id={fact.id: fact, dim.id: dim},
        columns_by_id={fact_key.id: fact_key, dim_key.id: dim_key},
        alias_by_table_id={fact.id: fact.alias, dim.id: dim.alias},
        connector="postgresql",
    )
    source_sql = (
        f'SELECT "{fact.alias}"."id" AS "fact_id" FROM {source_from}'
    )

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            'CREATE TABLE "fact_sales" ("id" INTEGER, "amount" NUMERIC)'
        )
        connection.execute('CREATE TABLE "dim_region" ("id" INTEGER)')
        connection.executemany(
            'INSERT INTO "fact_sales" VALUES (?, ?)',
            [(1, 10), (2, 20), (3, 30)],
        )
        connection.executemany(
            'INSERT INTO "dim_region" VALUES (?)', [(1,), (2,)]
        )
        raw_rows = {
            row[0]
            for row in connection.execute(raw_sql).fetchall()
        }
        source_rows = {
            row[0] for row in connection.execute(source_sql).fetchall()
        }
    finally:
        connection.close()

    assert raw_rows == {1, 2}
    assert raw_rows == source_rows


async def test_population_defining_unknown_orientation_falls_back(monkeypatch):
    """B01: a mandatory edge without a provable orientation is fail-closed."""
    fact = _table("fact_sales", table_type="fact")
    dim = _table("dim_region")
    fact_key = _column("id", fact, "int4")
    dim_key = _column("id", dim, "int4")
    amount = _column("amount", fact, "numeric")
    join = _join(fact, dim, fact_key, dim_key, join_type="many_to_one")
    join.population_participation = "population_defining"
    bound = _bound(
        [_measure("amount", amount)],
        [_dimension("fact_id", fact_key)],
    )
    _patch_graph(monkeypatch, [fact, dim], [join], [fact_key, dim_key, amount])

    with pytest.raises(RawRouteUnsupported, match="orientation"):
        await rewrite_for_raw(bound, _MockDB({}, [], {}, {}), target_dialect="postgres")


async def test_simple_model_no_group_by(monkeypatch):
    fact = _table("fact_sales", table_type="fact")
    dim_region = _table("dim_region")
    fact_region_fk = _column("region_id", fact, "int4")
    region_pk = _column("region_id", dim_region, "int4")
    region_name = _column("region_name", dim_region)
    amount = _column("amount", fact, "numeric")
    j = _join(fact, dim_region, fact_region_fk, region_pk)

    m = _measure("revenue", amount)
    d = _dimension("region_name", region_name)
    bq = _bound([m], [d])

    _patch_graph(monkeypatch, [fact, dim_region],
                 [j], [fact_region_fk, region_pk, region_name, amount])

    sql = await rewrite_for_raw(bq, _MockDB({}, [], {}, {}))

    assert "GROUP BY" not in sql.upper()
    assert "SUM" not in sql.upper()
    assert "AVG" not in sql.upper()
    assert "COUNT" not in sql.upper()
    assert '"region_name"' in sql
    assert '"revenue"' in sql
    assert "LEFT JOIN" in sql.upper()


@pytest.mark.parametrize("reverse_input_order", [False, True])
async def test_bug_8626_raw_base_matches_shared_anchor_for_deployable_model(
    monkeypatch, reverse_input_order,
):
    """Bug-8626: raw and source serving share the L3 fact-anchor contract.

    The dimension deliberately has the lower canonical id and is the only
    projected table.  For a deployable multi-table model, neither input order
    nor required-table preference may move the raw FROM away from the one
    declared fact selected by ``pick_anchor_table``.
    """
    fact = _table("fact_sales", table_type="fact", alias="f")
    dim = _table("dim_region", table_type="dim_detail", alias="d")
    dim.id = uuid4()
    fact.id = uuid4()
    if str(fact.id) < str(dim.id):
        fact.id, dim.id = dim.id, fact.id

    fact_key = _column("region_id", fact, "int4")
    dim_key = _column("region_id", dim, "int4")
    region_name = _column("region_name", dim)
    join = _join(fact, dim, fact_key, dim_key)
    bound = _bound([], [_dimension("region_name", region_name)])
    tables = [dim, fact] if reverse_input_order else [fact, dim]
    _patch_graph(
        monkeypatch,
        tables,
        [join],
        [fact_key, dim_key, region_name],
    )

    assert pick_anchor_table(tables) is fact
    sql = await rewrite_for_raw(bound, _MockDB({}, [], {}, {}))

    from_clause = sql.upper().split(" FROM ", 1)[1]
    assert from_clause.startswith('"FACT_SALES" AS "F"')
    assert 'LEFT JOIN "DIM_REGION" AS "D"' in from_clause


async def test_disconnected_table_typed_null(monkeypatch):
    fact = _table("fact_sales", table_type="fact")
    dim_region = _table("dim_region")
    dim_product = _table("dim_product")
    fact_region_fk = _column("region_id", fact, "int4")
    region_pk = _column("region_id", dim_region, "int4")
    region_name = _column("region_name", dim_region)
    product_name = _column("product_name", dim_product)
    amount = _column("amount", fact, "numeric")
    j = _join(fact, dim_region, fact_region_fk, region_pk)

    m = _measure("revenue", amount)
    d_region = _dimension("region_name", region_name)
    d_product = _dimension("product_name", product_name)
    bq = _bound([m], [d_region, d_product])

    _patch_graph(monkeypatch, [fact, dim_region, dim_product],
                 [j], [fact_region_fk, region_pk, region_name,
                       product_name, amount])

    sql = await rewrite_for_raw(bq, _MockDB({}, [], {}, {}))

    assert 'CAST(NULL AS TEXT) AS "product_name"' in sql
    assert '"region_name"' in sql
    assert "dim_product" not in sql


async def test_time_variant_measure_typed_null(monkeypatch):
    fact = _table("fact_sales", table_type="fact")
    amount = _column("amount", fact, "numeric")
    # Bug-7024: the dimension's source column must be the SAME object passed
    # to _patch_graph so columns_by_id resolves correctly; previously the
    # test created a separate _column with a different uuid, which silently
    # fell through to _pg_null -- masked by the old silent-NULL behavior.
    dummy_col = _column("dummy_col", fact)
    base_m = _measure("revenue", amount)
    variant = _measure(
        "revenue_ytd", amount,
        variant_of_measure_id=base_m.id,
    )
    d = _dimension("dummy", dummy_col)
    bq = _bound([variant], [d])

    _patch_graph(monkeypatch, [fact], [],
                 [amount, dummy_col])

    sql = await rewrite_for_raw(bq, _MockDB({}, [], {}, {}))

    assert 'CAST(NULL' in sql
    assert '"revenue_ytd"' in sql


async def test_count_star_typed_null(monkeypatch):
    fact = _table("fact_sales", table_type="fact")
    dummy_col = _column("dummy_col", fact)
    d = _dimension("dummy", dummy_col)
    count_m = _measure("__row_count", None, measure_type="count_star")
    bq = _bound([count_m], [d])

    _patch_graph(monkeypatch, [fact], [],
                 [dummy_col])

    sql = await rewrite_for_raw(bq, _MockDB({}, [], {}, {}))

    assert 'CAST(NULL AS BIGINT) AS "__row_count"' in sql


async def test_left_join_forced_even_for_inner(monkeypatch):
    fact = _table("fact_sales", table_type="fact")
    dim_region = _table("dim_region")
    fact_fk = _column("region_id", fact, "int4")
    region_pk = _column("region_id", dim_region, "int4")
    region_name = _column("region_name", dim_region)
    amount = _column("amount", fact, "numeric")
    j = _join(fact, dim_region, fact_fk, region_pk, join_type="inner")

    m = _measure("revenue", amount)
    d = _dimension("region_name", region_name)
    bq = _bound([m], [d])

    _patch_graph(monkeypatch, [fact, dim_region],
                 [j], [fact_fk, region_pk, region_name, amount])

    sql = await rewrite_for_raw(bq, _MockDB({}, [], {}, {}))

    assert "INNER JOIN" not in sql.upper()
    assert "LEFT JOIN" in sql.upper()


async def test_uda_dimension_renders_expression(monkeypatch):
    fact = _table("fact_sales", table_type="fact")
    full_date = _column("full_date", fact, "date")
    amount_col = _column("amount", fact, "numeric")
    uda = _uda('EXTRACT(YEAR FROM "full_date")', fact, "int4")
    d = _dimension("sale_year", None, user_defined_attribute_id=uda.id)
    m = _measure("revenue", amount_col)
    bq = _bound([m], [d])

    _patch_graph(monkeypatch, [fact], [],
                 [full_date, amount_col],
                 udas=[uda])

    sql = await rewrite_for_raw(bq, _MockDB({}, [], {}, {}))

    assert '"sale_year"' in sql
    assert "EXTRACT" in sql.upper()


async def test_schema_preservation_all_columns_present(monkeypatch):
    fact = _table("fact_sales", table_type="fact")
    dim_a = _table("dim_a")
    dim_b = _table("dim_b")
    fact_fk_a = _column("a_id", fact, "int4")
    a_pk = _column("a_id", dim_a, "int4")
    a_name = _column("a_name", dim_a)
    b_name = _column("b_name", dim_b)
    amount = _column("amount", fact, "numeric")
    j = _join(fact, dim_a, fact_fk_a, a_pk)

    m = _measure("revenue", amount)
    d_a = _dimension("a_name", a_name)
    d_b = _dimension("b_name", b_name)
    bq = _bound([m], [d_a, d_b])

    _patch_graph(monkeypatch, [fact, dim_a, dim_b],
                 [j], [fact_fk_a, a_pk, a_name, b_name, amount])

    sql = await rewrite_for_raw(bq, _MockDB({}, [], {}, {}))

    assert '"a_name"' in sql
    assert '"b_name"' in sql
    assert '"revenue"' in sql
    parts = sql.split("SELECT")[1].split("FROM")[0]
    assert parts.count(",") == 2


async def test_calculated_measure_resolvable_raw_arithmetic(monkeypatch):
    fact = _table("fact_sales", table_type="fact")
    price = _column("price", fact, "numeric")
    qty = _column("qty", fact, "int4")
    dummy_col = _column("dummy_col", fact)
    m_price = _measure("price", price)
    m_qty = _measure("qty", qty)
    m_calc = _measure(
        "total_value", None, measure_type="calculated",
        expression='measure("price") * measure("qty")',
    )
    d = _dimension("dummy", dummy_col)
    bq = _bound([m_price, m_qty, m_calc], [d])

    _patch_graph(monkeypatch, [fact], [],
                 [price, qty, dummy_col])

    sql = await rewrite_for_raw(bq, _MockDB({}, [], {}, {}))

    assert '"total_value"' in sql
    assert 'CAST(NULL' not in sql.split('"total_value"')[0].split(",")[-1]


class _MeasureResult:
    """Result stub that returns a fixed list of Measure objects for the
    calc-referenced-measure dependency load in rewrite_for_raw."""
    def __init__(self, measures):
        self._measures = measures

    def scalars(self):
        return self

    def all(self):
        return self._measures


class _CalcRefDB:
    """DB mock whose execute() returns the supplied referenced measures for
    the Measure.name.in_(...) dependency query, and empty for everything else.
    The model graph is patched separately via _patch_graph, so only the
    referenced-measure load actually hits execute()."""
    def __init__(self, ref_measures):
        self._ref_measures = ref_measures

    async def execute(self, *args, **kwargs):
        return _MeasureResult(self._ref_measures)


async def test_calc_measure_selected_alone_loads_referenced_bases(monkeypatch):
    """R2 Codex Finding 1: a calculated measure selected ALONE (its referenced
    base measures NOT co-selected) must still render — rewrite_for_raw loads the
    referenced base measures from the DB (mirroring the non-raw Phase-4A load)
    and their tables drive join planning. Before the fix this raised
    SemanticBindingError('references unknown measure') on a valid query.

    Revert-sensitive: removing the dependency-load block makes resolved_measures
    contain only the calc measure, so measure("price")/measure("qty") miss the
    lookup and the calc renderer raises."""
    fact = _table("fact_sales", table_type="fact")
    price = _column("price", fact, "numeric")
    qty = _column("qty", fact, "int4")
    dummy_col = _column("dummy_col", fact)
    # Base measures exist in the model but are NOT selected.
    m_price = _measure("price", price)
    m_qty = _measure("qty", qty)
    m_calc = _measure(
        "total_value", None, measure_type="calculated",
        expression='measure("price") * measure("qty")',
    )
    d = _dimension("dummy", dummy_col)
    # Only the calc measure is in resolved_measures (the normal binder shape).
    bq = _bound([m_calc], [d])

    _patch_graph(monkeypatch, [fact], [], [price, qty, dummy_col])

    # The DB returns the referenced base measures for the dependency query.
    sql = await rewrite_for_raw(bq, _CalcRefDB([m_price, m_qty]))

    assert '"total_value"' in sql
    # The calc must render its physical arithmetic, not a typed NULL.
    calc_piece = sql.split('"total_value"')[0].split(",")[-1]
    assert 'CAST(NULL' not in calc_piece
    assert "price" in sql and "qty" in sql


async def test_calc_measure_selected_alone_referencing_row_count(monkeypatch):
    """R3 Codex Finding 2: a calc measure selected ALONE that references the
    synthetic __row_count (COUNT(*)) sentinel must render a typed NULL for that
    reference, NOT raise 'unknown measure'. __row_count has no persisted Measure
    row, so it is never in resolved_measures or the DB dependency map — the
    dependency load must exclude it and the renderer must special-case it before
    the unknown-measure raise. Revert-sensitive: without the exclusion +
    pre-raise sentinel check this raises SemanticBindingError."""
    fact = _table("fact_sales", table_type="fact")
    dummy_col = _column("dummy_col", fact)
    m_calc = _measure(
        "rc_calc", None, measure_type="calculated",
        expression='measure("__row_count") * 2',
    )
    d = _dimension("dummy", dummy_col)
    bq = _bound([m_calc], [d])
    _patch_graph(monkeypatch, [fact], [], [dummy_col])

    # No referenced measures are loadable (the DB returns none); the calc must
    # still render (as a typed NULL for the __row_count ref), not raise.
    sql = await rewrite_for_raw(bq, _CalcRefDB([]))
    assert '"rc_calc"' in sql
    assert "NULL" in sql.upper()


class _UdaResult:
    def __init__(self, udas):
        self._udas = udas

    def scalars(self):
        return self

    def all(self):
        return self._udas


class _CalcRefUdaDB:
    """DB mock that distinguishes the Measure dependency query (first) from the
    subsequent UDA refill query (cache-hit path in _load_model_graph). Returns
    the referenced UDA-backed Measure for the first call, then the UDA row for
    the refill call. Proves rewrite_for_raw folds the dependency UDA id into
    uda_ids BEFORE the graph load so the cache-hit refill includes it."""
    def __init__(self, ref_measures, ref_udas):
        self._ref_measures = ref_measures
        self._ref_udas = ref_udas
        self._calls = 0

    async def execute(self, *args, **kwargs):
        self._calls += 1
        if self._calls == 1:
            return _MeasureResult(self._ref_measures)
        return _UdaResult(self._ref_udas)


async def test_calc_ref_uda_dependency_refills_on_cache_hit(monkeypatch):
    """R3 Codex Finding 3: when the model graph is a CACHE HIT, _load_model_graph
    only refills UDAs whose ids are passed in via uda_ids. A calc measure
    selected alone whose referenced base measure is UDA-backed — and whose UDA
    was NOT already in the cache — must still resolve, because rewrite_for_raw
    now loads the dependency measures and folds their UDA ids into uda_ids
    BEFORE the graph load. Revert-sensitive: if the dep-UDA id is not added to
    uda_ids before the graph load, the cached graph lacks the UDA and the calc
    raises 'user_defined_attribute_id ... not found in model'."""
    from src.rewrite.join_graph_cache import (
        _put_join_graph, invalidate_join_graph_cache,
    )

    fact = _table("fact_sales", table_type="fact")
    dummy_col = _column("dummy_col", fact)
    uda_id = _uid()
    uda = _uda("amount * 1.2", fact, output_data_type="numeric")
    # Force the fixture UDA's id to a known value for the refill query.
    uda.id = uda_id
    uda.table_id = fact.id
    base = _measure("uda_base", None)
    base.user_defined_attribute_id = uda_id
    m_calc = _measure(
        "uda_calc", None, measure_type="calculated",
        expression='measure("uda_base") * 2',
    )
    d = _dimension("dummy", dummy_col)
    bq = _bound([m_calc], [d])

    # Prime the join-graph CACHE with a graph that does NOT contain the
    # dependency UDA (simulating a graph cached before the UDA was relevant).
    invalidate_join_graph_cache(bq.model.id)
    _put_join_graph(
        bq.model.id,
        {fact.id: fact}, [],
        {dummy_col.id: dummy_col},
        {},  # uda_by_id: dependency UDA deliberately ABSENT
    )
    try:
        # DB returns the UDA-backed base measure (call 1 = Measure dependency
        # query), then the UDA row on refill (call 2 = cache-hit UDA refill).
        # target_dialect is passed explicitly so no _resolve_target_dialect
        # query precedes the dependency load and offsets the call ordering.
        sql = await rewrite_for_raw(
            bq, _CalcRefUdaDB([base], [uda]), target_dialect="postgres",
        )
        assert '"uda_calc"' in sql
        # The UDA body must render (not a typed NULL, not a raise).
        assert "amount" in sql
    finally:
        invalidate_join_graph_cache(bq.model.id)


# ---------------------------------------------------------------------------
# Bug-6121 (F-006-01) — contract: fixture attribute names match the ORM
# ---------------------------------------------------------------------------

def test_uda_fixture_attribute_matches_orm():
    """The _uda fixture must use the ORM's real attribute name (``table_id``),
    not the fabricated ``model_table_id`` that masked the production mismatch.
    This guard prevents a future rename from re-introducing the F-006-01 class
    (fixture and code agree on a name the ORM does not have)."""
    uda_columns = _orm_column_names(UserDefinedAttribute)
    # The fixture uses ``table_id`` — verify the ORM has it.
    assert "table_id" in uda_columns, (
        "UserDefinedAttribute ORM model no longer has 'table_id'; "
        "update _uda fixture and raw_sql.py accordingly"
    )
    # The old wrong name must NOT be used.
    fact = _table("fact_sales", table_type="fact")
    stub = _uda("1+1", fact)
    assert hasattr(stub, "table_id"), (
        "_uda fixture missing 'table_id' — F-006-01 regression"
    )
    assert not hasattr(stub, "model_table_id"), (
        "_uda fixture still uses the wrong attribute 'model_table_id'"
    )
    assert set(vars(stub)) == uda_columns


def test_uda_fixture_rejects_non_orm_attributes():
    with pytest.raises(AssertionError, match="non-ORM attribute"):
        _orm_stub(
            UserDefinedAttribute,
            id=_uid(),
            expression="1+1",
            model_table_id=_uid(),
        )


# ---------------------------------------------------------------------------
# Bug-6121 (F-006-01) — UDA-backed measure renders expression, not NULL
# ---------------------------------------------------------------------------

async def test_uda_measure_renders_expression(monkeypatch):
    """A UDA-backed measure must render the UDA expression, not CAST(NULL)."""
    fact = _table("fact_sales", table_type="fact")
    full_date = _column("full_date", fact, "date")
    amount = _column("amount", fact, "numeric")
    uda = _uda('("amount" * 2)', fact, "numeric")
    d = _dimension("sale_date", full_date)
    m = _measure("doubled_amount", None,
                 user_defined_attribute_id=uda.id)
    bq = _bound([m], [d])

    _patch_graph(monkeypatch, [fact], [],
                 [full_date, amount],
                 udas=[uda])

    sql = await rewrite_for_raw(bq, _MockDB({}, [], {}, {}))

    # The UDA expression must be rendered, not a typed NULL.
    assert '"doubled_amount"' in sql
    assert 'CAST(NULL' not in sql.lower().split('"doubled_amount"')[0].rsplit(",", 1)[-1]
    # The expression content must appear in the output.
    assert '"amount"' in sql


# ---------------------------------------------------------------------------
# Bug-6121-B — UDA measure on a non-fact table must trigger a JOIN
# ---------------------------------------------------------------------------

async def test_uda_measure_on_dim_table_triggers_join(monkeypatch):
    """A UDA-backed measure whose UDA lives on a dimension table (not the
    fact/base table) must register that table in required_table_ids so it
    gets joined; without this the measure renders referencing a table alias
    absent from FROM (SQL error). This gap was dormant before Bug-6121
    because all UDA measures emitted NULL; the fix exposed it."""
    fact = _table("fact_sales", table_type="fact")
    dim_product = _table("dim_product")
    fact_fk = _column("product_id", fact, "int4")
    prod_pk = _column("product_id", dim_product, "int4")
    price = _column("price", dim_product, "numeric")
    j = _join(fact, dim_product, fact_fk, prod_pk)

    # UDA lives on dim_product, not fact
    uda = _uda('("price" * 1.1)', dim_product, "numeric")
    dummy_col = _column("dummy_col", fact)
    d = _dimension("dummy", dummy_col)
    m = _measure("marked_up_price", None,
                 user_defined_attribute_id=uda.id)
    bq = _bound([m], [d])

    _patch_graph(monkeypatch, [fact, dim_product],
                 [j], [fact_fk, prod_pk, price, dummy_col],
                 udas=[uda])

    sql = await rewrite_for_raw(bq, _MockDB({}, [], {}, {}))

    # The dim_product table must be joined so the UDA expression resolves.
    assert "LEFT JOIN" in sql.upper()
    assert '"dim_product"' in sql
    assert '"marked_up_price"' in sql
    # The expression must render, not NULL.
    assert "CAST(NULL" not in sql.upper().split('"marked_up_price"')[0].rsplit(",", 1)[-1]


# ---------------------------------------------------------------------------
# Bug-5903 (F-006-04) — raw route non-Postgres target invariant
# ---------------------------------------------------------------------------

async def test_raw_route_bigquery_target_keeps_identifiers_not_string_literals(monkeypatch):
    """Raw-route SQL is generated internally in Postgres-canonical quoting and
    then transpiled to the target dialect. Reading that generated SQL as the
    user input dialect would corrupt `"table"."column"` into dotted string
    literals on non-Postgres targets."""
    fact = _table("fact_sales", table_type="fact", physical_name="dataset.fact_sales")
    region = _column("region", fact, "text")
    amount = _column("amount", fact, "numeric")
    m = _measure("revenue", amount)
    d = _dimension("region", region)
    bq = _bound([m], [d])

    _patch_graph(monkeypatch, [fact], [], [region, amount])

    sql = await rewrite_for_raw(bq, _MockDB({}, [], {}, {}), target_dialect="bigquery")

    parsed = sqlglot.parse_one(sql, read="bigquery")
    columns = {(col.table, col.name) for col in parsed.find_all(sqlglot.exp.Column)}
    assert ("fact_sales", "region") in columns
    assert ("fact_sales", "amount") in columns
    literals = [lit.this for lit in parsed.find_all(sqlglot.exp.Literal) if lit.is_string]
    assert "fact_sales" not in literals
    assert "region" not in literals
    assert "amount" not in literals


async def test_raw_route_tsql_contains_filter_escapes_brackets(monkeypatch):
    """Bug-6900 (call-site guard for Bug-6894/6895): a contains-filter value
    containing T-SQL LIKE metacharacters ('[', ']') must be bracket-escaped in
    the raw route's emitted WHERE when the target is SQL Server — proving the
    raw route actually passes ``like_target_connector`` down to _render_where,
    not silently defaulting to None.

    Without the call-site wiring the raw WHERE would render an unescaped
    ``LIKE '%test[1]%'`` on tsql, which matches ``test1`` (a character class),
    returning silently-wrong rows.
    """
    from src.ir.logical_query import LogicalFilter

    fact = _table("fact_sales", table_type="fact")
    name_col = _column("name", fact, "text")
    d = _dimension("name", name_col)
    # ``contains`` renders as a LIKE with a backslash escape char + escaped
    # wildcards; the raw route resolves the dimension to a reachable column.
    # R1 Finding 2: seed a BARE-bracket value (no pre-escaping) so the test is
    # revert-sensitive — the '\[' can only appear if the raw route actually
    # propagated the tsql target to _tsql_bracket_escape. A pre-escaped input
    # would already contain '\[' and pass even with the wiring removed.
    f = LogicalFilter(
        dimension_name="name",
        operator="like",
        value="%test[1]%",
        like_escape="\\",
    )
    bq = _bound([], [d], filters=[f])
    _patch_graph(monkeypatch, [fact], [], [name_col])

    sql = await rewrite_for_raw(
        bq, _MockDB({}, [], {}, {}), target_dialect="tsql",
    )

    # The raw route must have propagated the tsql target to the LIKE renderer:
    # the bare brackets GAINED a backslash escape, and an ESCAPE clause emitted.
    assert "\\[" in sql
    assert "\\]" in sql
    assert "ESCAPE" in sql.upper()


async def test_raw_route_postgres_contains_filter_no_bracket_escape(monkeypatch):
    """Counterpart to the tsql test: on a Postgres target the same contains
    filter must NOT bracket-escape ([ is not a LIKE metacharacter there),
    confirming the escaping is target-driven and not unconditional."""
    from src.ir.logical_query import LogicalFilter

    fact = _table("fact_sales", table_type="fact")
    name_col = _column("name", fact, "text")
    d = _dimension("name", name_col)
    f = LogicalFilter(
        dimension_name="name",
        operator="like",
        value="%test[1]%",
        like_escape="\\",
    )
    bq = _bound([], [d], filters=[f])
    _patch_graph(monkeypatch, [fact], [], [name_col])

    sql = await rewrite_for_raw(
        bq, _MockDB({}, [], {}, {}), target_dialect="postgres",
    )
    assert "\\[" not in sql
    assert "[1]" in sql


# ---------------------------------------------------------------------------
# Bug-7801: calc measure with 11+ references — prefix collision guard
# ---------------------------------------------------------------------------


async def test_calc_measure_11_refs_no_prefix_collision_bug7801(monkeypatch):
    """Bug-7801: __tessallite_measure_ref__1 is a prefix of
    __tessallite_measure_ref__10.  A naive str.replace loop substitutes ref 1
    inside ref 10's placeholder, corrupting the SQL.  The fix sorts
    substitutions longest-first.

    Hand-computed expected: each ref resolves to its own column
    (m0..m10), so the expression is (m0 + m1 + ... + m10) and the SQL must
    contain every physical column name exactly once, with no mangled
    placeholder fragments.
    """
    fact = _table("fact_sales", table_type="fact")
    cols = [_column(f"m{i}", fact, "numeric") for i in range(11)]
    measures = [_measure(f"m{i}", cols[i]) for i in range(11)]
    dummy_col = _column("dummy_col", fact)

    # Build a calc expression with 11 measure() references: m0 through m10.
    expr_parts = [f'measure("m{i}")' for i in range(11)]
    expr = " + ".join(expr_parts)
    m_calc = _measure(
        "big_calc", None, measure_type="calculated",
        expression=expr,
    )
    d = _dimension("dummy", dummy_col)
    bq = _bound(measures + [m_calc], [d])
    _patch_graph(monkeypatch, [fact], [], cols + [dummy_col])

    sql = await rewrite_for_raw(bq, _MockDB({}, [], {}, {}))

    # Each physical column name must appear in the output exactly once inside
    # the calc expression (between the opening paren and the closing AS alias).
    calc_section = sql.split('"big_calc"')[0].rsplit(",", 1)[-1]
    for i in range(11):
        col_name = f'"m{i}"'
        assert col_name in calc_section, (
            f"Physical column {col_name} missing from calc expression. "
            f"Prefix collision likely corrupted placeholder substitution. "
            f"SQL fragment: {calc_section}"
        )
    # The placeholder prefix must not survive in the final SQL.
    assert "__tessallite_measure_ref__" not in sql, (
        "Residual placeholder found in SQL — substitution incomplete."
    )
