"""Phase 4A — rewriter tests for calculated-measure expansion.

Covers both ``calc_agg_mode`` modes:
  * ``expression_as_written``: each ``measure("m")`` expands inline to its
    aggregated form (``SUM(col)``), and the expression is emitted as-is.
  * ``per_row_then_aggregate``: each ``measure("m")`` expands to a raw
    column reference; the whole expression is wrapped in the calculated
    measure's ``default_agg``.

Tests wire a FakeDB stub into :func:`_build_source_sql` so the rewriter
sees the shape it would see against the real catalog.
"""
from __future__ import annotations

import types
from uuid import uuid4

import pytest

from src.ir.logical_query import LogicalQuery, BoundQuery
from src.rewrite.query_rewriter import _build_source_sql

from shared.db.models import (
    DataSource,
    Join,
    Measure,
    ModelColumn,
    ModelTable,
    ProjectConnection,
    UserDefinedAttribute,
)


# ---------------------------------------------------------------------------
# Fake DB scaffold — dispatches SQLAlchemy ``select(Model)`` calls to
# pre-seeded row lists, and supports ``db.get(Model, pk)`` lookups.
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class FakeDB:
    def __init__(self, *, tables=None, columns=None, joins=None,
                 udas=None, measures=None, sources=None, connections=None):
        self.tables = list(tables or [])
        self.columns = list(columns or [])
        self.joins = list(joins or [])
        self.udas = list(udas or [])
        self.measures = list(measures or [])
        self.sources = list(sources or [])
        self.connections = list(connections or [])

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity is ModelTable:
            return _Result(self.tables)
        if entity is ModelColumn:
            return _Result(self.columns)
        if entity is Join:
            return _Result(self.joins)
        if entity is UserDefinedAttribute:
            return _Result(self.udas)
        if entity is Measure:
            return _Result(self.measures)
        if entity is DataSource:
            return _Result(self.sources)
        raise AssertionError(f"Unexpected entity: {entity!r}")

    async def get(self, model_cls, pk):
        if model_cls is ProjectConnection:
            for c in self.connections:
                if getattr(c, "id", None) == pk:
                    return c
            return None
        if model_cls is DataSource:
            for s in self.sources:
                if getattr(s, "id", None) == pk:
                    return s
            return None
        return None


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _model_table(id_, physical_name, alias):
    return types.SimpleNamespace(
        id=id_,
        model_id="model-1",
        physical_name=physical_name,
        alias=alias,
        table_type="fact",
        source_id="source-1",
    )


def _model_col(id_, table_id, column_name):
    return types.SimpleNamespace(
        id=id_,
        model_table_id=table_id,
        column_name=column_name,
    )


def _measure(id_, name, *, measure_type="standard",
             default_agg="sum", source_column_id=None,
             expression=None, calc_agg_mode=None,
             user_defined_attribute_id=None):
    return types.SimpleNamespace(
        id=id_,
        model_id="model-1",
        name=name,
        display_name=name,
        measure_type=measure_type,
        default_agg=default_agg,
        is_additive=True,
        source_column_id=source_column_id,
        user_defined_attribute_id=user_defined_attribute_id,
        expression=expression,
        calc_agg_mode=calc_agg_mode,
        variant_kind=None,
        variant_of_measure_id=None,
        variant_n=None,
        is_invalid=False,
        invalid_reason=None,
    )


def _dimension(id_, name, source_column_id):
    return types.SimpleNamespace(
        id=id_,
        model_id="model-1",
        name=name,
        display_name=name,
        source_column_id=source_column_id,
        user_defined_attribute_id=None,
    )


def _bound_query(resolved_measures, resolved_dimensions=None, grain=None,
                 raw_query=None):
    mid = str(uuid4())
    # Bug-7803: these tests exercise calc-expansion RENDERING, seeding the
    # referenced base measures into the FakeDB. The calc-dependency load now
    # resolves base measures from the DEPLOYED SNAPSHOT for a deployed model
    # (fail-closed), and the FakeDB carries no ModelVersion snapshot. Model the
    # world as UNDEPLOYED so the FakeDB's live ``measures`` ARE the authority
    # (the correct source for an undeployed model) — keeping each rendering
    # assertion intact. Deploy-authority itself is covered by
    # test_bug_7803_calc_dependency_snapshot_authority.py.
    model = types.SimpleNamespace(
        id=mid, slug="test", display_name="test",
        deployed_version_id=None,
    )
    dim_names = [d.name for d in (resolved_dimensions or [])]
    lq = LogicalQuery(
        model_id=mid, protocol="jdbc",
        raw_query=raw_query or "SELECT margin_ratio FROM test",
        requested_measures=[m.name for m in resolved_measures],
        requested_dimensions=dim_names,
        filters=[], grain=grain or [], order_by=[],
        limit=None, offset=None,
        query_fingerprint="fp",
    )
    return BoundQuery(
        logical_query=lq,
        model=model,
        resolved_measures=resolved_measures,
        resolved_dimensions=resolved_dimensions or [],
        resolved_filters=[],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calc_measure_expression_as_written():
    fact = _model_table("t-fact", "demo.sales", "f")
    gm_col = _model_col("c-gm", "t-fact", "gross_margin")
    ns_col = _model_col("c-ns", "t-fact", "net_sales")

    gm = _measure("m-gm", "gm", source_column_id="c-gm")
    sales = _measure("m-sales", "sales", source_column_id="c-ns")
    ratio = _measure(
        "m-ratio", "margin_ratio",
        measure_type="calculated",
        default_agg="sum",
        expression='safe_div(measure("gm"), measure("sales"))',
        calc_agg_mode="expression_as_written",
    )

    db = FakeDB(
        tables=[fact],
        columns=[gm_col, ns_col],
        measures=[gm, sales],
    )

    sql = await _build_source_sql(_bound_query([ratio]), db)

    # Both base measures expand to SUM(...), safe_div rewrites to CASE WHEN.
    assert 'SUM("f"."gross_margin")' in sql
    assert 'SUM("f"."net_sales")' in sql
    assert "CASE WHEN" in sql
    assert '"margin_ratio"' in sql
    assert '"demo"."sales"' in sql


@pytest.mark.asyncio
async def test_calc_measure_per_row_then_aggregate():
    fact = _model_table("t-fact", "demo.line_items", "li")
    price_col = _model_col("c-price", "t-fact", "price")
    qty_col = _model_col("c-qty", "t-fact", "qty")

    price = _measure("m-price", "price", source_column_id="c-price")
    qty = _measure("m-qty", "qty", source_column_id="c-qty")
    line_total = _measure(
        "m-total", "line_total",
        measure_type="calculated",
        default_agg="sum",
        expression='measure("price") * measure("qty")',
        calc_agg_mode="per_row_then_aggregate",
    )

    db = FakeDB(
        tables=[fact],
        columns=[price_col, qty_col],
        measures=[price, qty],
    )

    sql = await _build_source_sql(_bound_query([line_total]), db)

    # Per-row mode: base measures become raw column refs, the full
    # expression is wrapped once in the outer SUM(...).
    assert 'SUM("li"."price" * "li"."qty")' in sql
    assert '"line_total"' in sql


@pytest.mark.asyncio
async def test_calc_measure_missing_ref_raises():
    fact = _model_table("t-fact", "demo.sales", "f")
    gm_col = _model_col("c-gm", "t-fact", "gross_margin")

    gm = _measure("m-gm", "gm", source_column_id="c-gm")
    # Reference 'sales' is NOT loaded in the DB.
    ratio = _measure(
        "m-ratio", "margin_ratio",
        measure_type="calculated",
        default_agg="sum",
        expression='measure("gm") / measure("sales")',
        calc_agg_mode="expression_as_written",
    )

    db = FakeDB(tables=[fact], columns=[gm_col], measures=[gm])

    from src.ir.logical_query import SemanticBindingError
    with pytest.raises(SemanticBindingError, match="unknown measure 'sales'"):
        await _build_source_sql(_bound_query([ratio]), db)


@pytest.mark.asyncio
async def test_bare_calc_measure_with_group_by():
    """Bug-140: bare column ref to a calculated measure must expand with GROUP BY."""
    fact = _model_table("t-fact", "demo.transactions", "t")
    country_col = _model_col("c-country", "t-fact", "country_code")
    gm_col = _model_col("c-gm", "t-fact", "gross_margin")
    ns_col = _model_col("c-ns", "t-fact", "net_sales")

    country_dim = _dimension("d-country", "country_code", "c-country")
    gm = _measure("m-gm", "gross_margin", source_column_id="c-gm")
    ns = _measure("m-ns", "net_sales", source_column_id="c-ns")
    pct = _measure(
        "m-pct", "gross_margin_pct",
        measure_type="calculated",
        default_agg="sum",
        expression='safe_div(measure("gross_margin"), measure("net_sales"))',
        calc_agg_mode="expression_as_written",
    )

    db = FakeDB(
        tables=[fact],
        columns=[country_col, gm_col, ns_col],
        measures=[gm, ns],
    )

    bq = _bound_query(
        resolved_measures=[pct],
        resolved_dimensions=[country_dim],
        grain=["country_code"],
        raw_query="SELECT country_code, gross_margin_pct FROM test GROUP BY country_code",
    )
    sql = await _build_source_sql(bq, db)

    assert 'SUM("t"."gross_margin")' in sql
    assert 'SUM("t"."net_sales")' in sql
    assert '"gross_margin_pct"' in sql
    assert "GROUP BY" in sql


@pytest.mark.asyncio
async def test_calc_ref_with_inconsistent_semi_additive_default_agg_raises():
    """F-015-12: a calculated measure that references a base measure whose
    ``default_agg`` is a semi-additive balance token (last_non_empty) but which
    carries NO ``semi_additive_behavior`` (a data inconsistency) must fail loud,
    not silently substitute SUM.  Summing a point-in-time balance is a wrong
    number; the row must be repaired instead."""
    fact = _model_table("t-fact", "demo.balances", "b")
    bal_col = _model_col("c-bal", "t-fact", "closing_balance")
    cnt_col = _model_col("c-cnt", "t-fact", "day_count")

    # balance measure: SA token in default_agg, but semi_additive_behavior unset.
    balance = _measure(
        "m-bal", "balance",
        default_agg="last_non_empty", source_column_id="c-bal",
    )
    days = _measure("m-days", "days", source_column_id="c-cnt")
    ratio = _measure(
        "m-ratio", "avg_balance",
        measure_type="calculated",
        default_agg="sum",
        expression='safe_div(measure("balance"), measure("days"))',
        calc_agg_mode="expression_as_written",
    )

    db = FakeDB(tables=[fact], columns=[bal_col, cnt_col],
                measures=[balance, days])

    from src.ir.logical_query import SemanticBindingError
    with pytest.raises(SemanticBindingError, match="semi-additive balance token"):
        await _build_source_sql(_bound_query([ratio]), db)


@pytest.mark.asyncio
async def test_calc_outer_with_inconsistent_semi_additive_default_agg_raises():
    """F-015-12: a per_row_then_aggregate calculated measure whose OWN
    ``default_agg`` is a semi-additive balance token with no
    ``semi_additive_behavior`` must fail loud on the outer aggregation step
    rather than silently wrapping the per-row expression in SUM."""
    fact = _model_table("t-fact", "demo.line_items", "li")
    price_col = _model_col("c-price", "t-fact", "price")
    qty_col = _model_col("c-qty", "t-fact", "qty")

    price = _measure("m-price", "price", source_column_id="c-price")
    qty = _measure("m-qty", "qty", source_column_id="c-qty")
    bad = _measure(
        "m-bad", "bad_total",
        measure_type="calculated",
        default_agg="last_non_empty",   # inconsistent SA token as outer agg
        expression='measure("price") * measure("qty")',
        calc_agg_mode="per_row_then_aggregate",
    )

    db = FakeDB(tables=[fact], columns=[price_col, qty_col],
                measures=[price, qty])

    from src.ir.logical_query import SemanticBindingError
    with pytest.raises(SemanticBindingError, match="semi-additive balance token"):
        await _build_source_sql(_bound_query([bad]), db)
