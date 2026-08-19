"""Bug-3635 (AKA Bug-1066) durability lock — source-route compound/scalar
aggregate rendering.

The defect: ``_build_source_sql`` (the SOURCE rewrite path) once leaked the
component measures of a compound aggregate as extra result columns, and dropped
the outer scalar wrapper (CAST / ROUND / SQRT / POWER) over an aggregate. The
live verification of the fix runs on ``modely`` *only while that model carries
no covering active aggregate* — a future aggregate over these shapes would move
them off the source path and could silently hide a regression in the source
renderer.

This test pins the SOURCE renderer (``_build_source_sql`` / ``rewrite_for_source``)
directly and OFFLINE, with a deterministic FakeDB, so the assertion holds
regardless of the live aggregate inventory:

  1. A compound ratio (``SUM(a) / SUM(b)``) renders as a SINGLE projection —
     no trailing ``, SUM(a) AS "a", SUM(b) AS "b"`` component columns.
  2. A scalar wrapper over a compound aggregate (``CAST(SUM(a)/SUM(b) AS …)``)
     is preserved end to end.
  3. Scalar wrappers over a single aggregate (``ROUND`` / ``SQRT`` / ``POWER``)
     are preserved, not stripped to the bare aggregate.

Mirrors the proven FakeDB scaffold from ``test_calculated_rewrite.py``.
"""
from __future__ import annotations

import types

import pytest

from conftest import attach_fixture_deployed_shape
import sqlglot
from sqlglot import exp

from src.ir.logical_query import BoundQuery
from src.parsing.sql_parser import parse_sql_to_ir
from src.rewrite.query_rewriter import _build_source_sql, rewrite_for_source

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
# FakeDB scaffold (dispatches select(Model) to seeded rows; supports db.get).
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
    def __init__(self, *, tables, columns, measures):
        self.tables = list(tables)
        self.columns = list(columns)
        self.measures = list(measures)

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity is ModelTable:
            return _Result(self.tables)
        if entity is ModelColumn:
            return _Result(self.columns)
        if entity is Join:
            return _Result([])
        if entity is UserDefinedAttribute:
            return _Result([])
        if entity is Measure:
            return _Result(self.measures)
        if entity is DataSource:
            return _Result([])
        raise AssertionError(f"Unexpected entity: {entity!r}")

    async def get(self, model_cls, pk):
        return None


def _tbl():
    return types.SimpleNamespace(
        id="t-fact", model_id="m1", physical_name="demo.tx", alias="tx",
        table_type="fact", source_id="s1",
    )


def _col(id_, name):
    return types.SimpleNamespace(id=id_, model_table_id="t-fact", column_name=name)


def _meas(id_, name, source_column_id):
    return types.SimpleNamespace(
        id=id_, model_id="m1", name=name, display_name=name,
        measure_type="standard", default_agg="sum", is_additive=True,
        source_column_id=source_column_id, user_defined_attribute_id=None,
        expression=None, calc_agg_mode=None, variant_kind=None,
        variant_of_measure_id=None, variant_n=None, is_invalid=False,
        invalid_reason=None, semi_additive_behavior=None,
    )


def _bound(sql, measures):
    lq = parse_sql_to_ir(sql, "m1")
    model = types.SimpleNamespace(
        id="m1", slug="modely", display_name="modely", deployed_version_id="v1",
    )
    return BoundQuery(
        logical_query=lq, model=model, resolved_measures=measures,
        resolved_dimensions=[], resolved_filters=[],
    )


def _db(measures):
    cols = [_col("c-fee", "fee_amount"), _col("c-base", "base_amount")]
    return FakeDB(tables=[_tbl()], columns=cols, measures=measures)


def _projection_count(sql: str) -> int:
    """Count top-level SELECT projections in the rendered SQL via AST."""
    ast = sqlglot.parse_one(sql, read="postgres")
    select = ast if isinstance(ast, exp.Select) else ast.find(exp.Select)
    assert select is not None, f"no SELECT in: {sql}"
    return len(select.expressions)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compound_ratio_renders_single_projection():
    """SUM(a)/SUM(b) must render as ONE projection, NOT leak the component
    measures as trailing ``, SUM(a) AS "a", SUM(b) AS "b"`` columns."""
    fee = _meas("m-fee", "fee_amount", "c-fee")
    base = _meas("m-base", "base_amount", "c-base")
    sql = await _build_source_sql(
        await attach_fixture_deployed_shape(_bound("SELECT SUM(fee_amount) / SUM(base_amount) FROM tx", [fee, base]), _db([fee, base])),
        _db([fee, base]), target_dialect="postgres",
    )

    assert _projection_count(sql) == 1, f"component columns leaked: {sql}"
    # The single projection is the ratio of the two aggregates.
    assert 'SUM("tx"."fee_amount") / SUM("tx"."base_amount")' in sql
    # No trailing component-column aliases.
    assert 'AS "fee_amount"' not in sql, f"fee_amount leaked: {sql}"
    assert 'AS "base_amount"' not in sql, f"base_amount leaked: {sql}"


@pytest.mark.asyncio
async def test_cast_over_compound_aggregate_preserved():
    """CAST(SUM(a)/SUM(b) AS NUMERIC) — the outer CAST must survive (it once
    got dropped, returning the raw ratio) and stay a single projection."""
    fee = _meas("m-fee", "fee_amount", "c-fee")
    base = _meas("m-base", "base_amount", "c-base")
    sql = await _build_source_sql(
        await attach_fixture_deployed_shape(_bound( "SELECT CAST(SUM(fee_amount) / SUM(base_amount) AS NUMERIC(18,4)) FROM tx", [fee, base], ), _db([fee, base])),
        _db([fee, base]), target_dialect="postgres",
    )

    assert _projection_count(sql) == 1, f"component columns leaked: {sql}"
    upper = sql.upper()
    assert "CAST(" in upper, f"CAST wrapper dropped: {sql}"
    assert "DECIMAL(18, 4)" in upper or "NUMERIC(18, 4)" in upper, sql
    assert 'SUM("tx"."fee_amount") / SUM("tx"."base_amount")' in sql


@pytest.mark.parametrize(
    "raw_sql, wrapper_token",
    [
        ("SELECT ROUND(SUM(fee_amount), 2) FROM tx", "ROUND("),
        ("SELECT SQRT(SUM(base_amount)) FROM tx", "SQRT("),
        ("SELECT POWER(SUM(base_amount), 2) FROM tx", "POWER("),
    ],
)
@pytest.mark.asyncio
async def test_scalar_wrapper_over_single_aggregate_preserved(raw_sql, wrapper_token):
    """ROUND / SQRT / POWER over a single aggregate must keep the wrapper —
    the bug returned the RAW aggregate value (wrong number), not just an
    extra column."""
    fee = _meas("m-fee", "fee_amount", "c-fee")
    base = _meas("m-base", "base_amount", "c-base")
    measures = [fee] if "fee_amount" in raw_sql else [base]
    sql = await _build_source_sql(
        await attach_fixture_deployed_shape(_bound(raw_sql, measures), _db([fee, base])), _db([fee, base]), target_dialect="postgres",
    )

    assert _projection_count(sql) == 1, f"unexpected extra columns: {sql}"
    assert wrapper_token in sql.upper(), f"{wrapper_token} wrapper dropped: {sql}"
    # The wrapper sits around the aggregate, not beside a leaked bare aggregate.
    assert sql.upper().count("SUM(") == 1, f"aggregate duplicated/leaked: {sql}"


@pytest.mark.asyncio
async def test_compound_ratio_via_public_rewrite_for_source_entry():
    """Same single-projection guarantee through the public ``rewrite_for_source``
    entry point (not just the internal ``_build_source_sql``)."""
    fee = _meas("m-fee", "fee_amount", "c-fee")
    base = _meas("m-base", "base_amount", "c-base")
    sql = await rewrite_for_source(
        await attach_fixture_deployed_shape(_bound("SELECT SUM(fee_amount) / SUM(base_amount) FROM tx", [fee, base]), _db([fee, base])),
        _db([fee, base]), target_dialect="postgres",
    )
    assert _projection_count(sql) == 1, f"component columns leaked: {sql}"
