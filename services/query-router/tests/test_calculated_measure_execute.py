"""Phase 5.3.B — end-to-end execute regression for calculated measures.

Complements ``test_calculated_rewrite.py`` (which pokes the rewriter in
isolation) by exercising the full ``route_query`` path — the same code
the ``/execute`` endpoint wraps — so the response envelope the
frontend and Phase 11 MCP will consume is locked against regression.

Invariants under test:
  * A query whose only measure is ``measure_type="calculated"`` rewrites
    through the source path (calculated measures cannot be served from
    a bare aggregate column in v1) and emits the expanded expression
    rather than a literal ``measure("x")`` token.
  * The ``RouteDecision`` returned to the API layer carries
    ``route_type="source"``, no aggregate / pocket id, and a reason
    string — all fields the ``ExecuteResponse`` contract promises.
  * Both ``calc_agg_mode`` variants flow through the same routing path.
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from src.ir.logical_query import BoundQuery, LogicalQuery
from src.routing.router import route_query

from shared.db.models import (
    DataSource,
    Join,
    Measure,
    ModelColumn,
    ModelTable,
    ProjectConnection,
    UserDefinedAttribute,
)

pytestmark = pytest.mark.integration

_PATCH_LOAD = "src.routing.aggregate_matcher.load_active_aggregates"


# ---------------------------------------------------------------------------
# FakeDB — same shape as test_calculated_rewrite but scoped to this test
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


class _FakeDB:
    def __init__(self, *, tables, columns, measures, sources=None, connections=None, udas=None, joins=None):
        self.tables = list(tables)
        self.columns = list(columns)
        self.measures = list(measures)
        self.sources = list(sources or [])
        self.connections = list(connections or [])
        self.udas = list(udas or [])
        self.joins = list(joins or [])

    async def execute(self, stmt):
        desc = getattr(stmt, "column_descriptions", None)
        if not desc:
            return _Result([])
        entity = desc[0]["entity"]
        if entity is ModelTable:
            return _Result(self.tables)
        if entity is ModelColumn:
            return _Result(self.columns)
        if entity is Measure:
            return _Result(self.measures)
        if entity is DataSource:
            return _Result(self.sources)
        if entity is Join:
            return _Result(self.joins)
        if entity is UserDefinedAttribute:
            return _Result(self.udas)
        return _Result([])

    async def get(self, model_cls, pk):
        if model_cls is ProjectConnection:
            for c in self.connections:
                if getattr(c, "id", None) == pk:
                    return c
        if model_cls is DataSource:
            for s in self.sources:
                if getattr(s, "id", None) == pk:
                    return s
        return None


# ---------------------------------------------------------------------------
# Namespace builders
# ---------------------------------------------------------------------------


def _table(id_, physical_name, alias):
    return types.SimpleNamespace(
        id=id_,
        model_id="model-1",
        physical_name=physical_name,
        alias=alias,
        table_type="fact",
        source_id="source-1",
    )


def _col(id_, table_id, column_name):
    return types.SimpleNamespace(
        id=id_,
        model_table_id=table_id,
        column_name=column_name,
    )


def _measure(id_, name, *, measure_type="standard", default_agg="sum",
             source_column_id=None, expression=None, calc_agg_mode=None):
    return types.SimpleNamespace(
        id=id_,
        model_id="model-1",
        name=name,
        display_name=name,
        measure_type=measure_type,
        default_agg=default_agg,
        is_additive=True,
        source_column_id=source_column_id,
        user_defined_attribute_id=None,
        expression=expression,
        calc_agg_mode=calc_agg_mode,
        variant_kind=None,
        variant_of_measure_id=None,
        variant_n=None,
        is_invalid=False,
        invalid_reason=None,
    )


def _bound_query(resolved_measures, *, raw_sql):
    mid = str(uuid4())
    model = types.SimpleNamespace(
        id=mid, slug="sales", display_name="sales",
        deployed_version_id="v1", status="active", aggregations_enabled=True,
    )
    lq = LogicalQuery(
        model_id=mid, protocol="jdbc",
        raw_query=raw_sql,
        requested_measures=[m.name for m in resolved_measures],
        requested_dimensions=[],
        filters=[], grain=[], order_by=[],
        limit=None, offset=None,
        query_fingerprint="fp",
    )
    return BoundQuery(
        logical_query=lq,
        model=model,
        resolved_measures=resolved_measures,
        resolved_dimensions=[],
        resolved_filters=[],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_calculated_measure_routes_to_source_with_expanded_expression():
    """Expression-as-written mode: base measures expand to SUM(...) and the
    full expression is emitted inline. Route decision must say source, with
    no aggregate/pocket id, and a reason the badge tooltip can show."""
    fact = _table("t-fact", "public.sales", "s")
    gm_col = _col("c-gm", "t-fact", "gross_margin")
    ns_col = _col("c-ns", "t-fact", "net_sales")

    gm = _measure("m-gm", "gm", source_column_id="c-gm")
    sales = _measure("m-sales", "sales", source_column_id="c-ns")
    ratio = _measure(
        "m-ratio", "margin_ratio",
        measure_type="calculated",
        default_agg="sum",
        expression='safe_div(measure("gm"), measure("sales"))',
        calc_agg_mode="expression_as_written",
    )

    db = _FakeDB(tables=[fact], columns=[gm_col, ns_col], measures=[gm, sales])
    bq = _bound_query([ratio], raw_sql="SELECT margin_ratio FROM sales")

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = []
        decision = await route_query(bq, db)

    assert decision.route_type == "source"
    assert decision.aggregate_id is None
    assert decision.pocket_id is None
    assert isinstance(decision.reason, str) and decision.reason
    # Expanded: base measures aggregated, safe_div rendered as CASE WHEN.
    assert 'SUM("s"."gross_margin")' in decision.rewritten_query
    assert 'SUM("s"."net_sales")' in decision.rewritten_query
    assert "CASE WHEN" in decision.rewritten_query
    # No unexpanded ``measure("...")`` tokens leak through.
    assert 'measure("' not in decision.rewritten_query


async def test_calculated_measure_per_row_then_aggregate_routes_to_source():
    """Per-row mode: base measures become raw column refs; the full expression
    is wrapped once in the calculated measure's default_agg."""
    fact = _table("t-fact", "public.line_items", "li")
    price_col = _col("c-price", "t-fact", "price")
    qty_col = _col("c-qty", "t-fact", "qty")

    price = _measure("m-price", "price", source_column_id="c-price")
    qty = _measure("m-qty", "qty", source_column_id="c-qty")
    line_total = _measure(
        "m-total", "line_total",
        measure_type="calculated",
        default_agg="sum",
        expression='measure("price") * measure("qty")',
        calc_agg_mode="per_row_then_aggregate",
    )

    db = _FakeDB(tables=[fact], columns=[price_col, qty_col], measures=[price, qty])
    bq = _bound_query([line_total], raw_sql="SELECT line_total FROM line_items")

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = []
        decision = await route_query(bq, db)

    assert decision.route_type == "source"
    assert decision.aggregate_id is None
    assert 'SUM("li"."price" * "li"."qty")' in decision.rewritten_query
    assert 'measure("' not in decision.rewritten_query


async def test_calculated_measure_response_envelope_fields_present():
    """The RouteDecision carries every field the ExecuteResponse serialises
    — route_type, aggregate_id, pocket_id, reason, rewritten_query — so the
    gateway / MCP can rely on the envelope shape without a second round-trip
    to /explain."""
    fact = _table("t-fact", "public.sales", "s")
    gm_col = _col("c-gm", "t-fact", "gross_margin")
    ns_col = _col("c-ns", "t-fact", "net_sales")

    gm = _measure("m-gm", "gm", source_column_id="c-gm")
    sales = _measure("m-sales", "sales", source_column_id="c-ns")
    ratio = _measure(
        "m-ratio", "margin_ratio",
        measure_type="calculated",
        default_agg="sum",
        expression='measure("gm") / measure("sales")',
        calc_agg_mode="expression_as_written",
    )

    db = _FakeDB(tables=[fact], columns=[gm_col, ns_col], measures=[gm, sales])
    bq = _bound_query([ratio], raw_sql="SELECT margin_ratio FROM sales")

    with patch(_PATCH_LOAD, new_callable=AsyncMock):
        decision = await route_query(bq, db)

    # Every field the API envelope needs.
    for field in ("route_type", "aggregate_id", "pocket_id", "reason", "rewritten_query"):
        assert hasattr(decision, field), f"RouteDecision missing {field}"
    assert decision.route_type in {"source", "aggregate", "pocket"}
    assert decision.rewritten_query.strip(), "rewritten_query must not be empty"
