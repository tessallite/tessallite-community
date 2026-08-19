"""Bug-8285 rewriter-level guard: the projected caption column must reach BOTH
the emitted SELECT and the emitted GROUP BY.

The boundary test in ``test_bug8285_caption_augmentation.py`` proves the
augmentation appends the synthetic dim / passthrough / grain entry. That is
necessary but NOT sufficient: a display column that lands in SELECT but not
GROUP BY is an ungrouped, non-aggregated column under a GROUP BY — a hard
source-DB error (PostgreSQL 42803), which turns a working pivot into a SOAP
fault. This test drives the augmentation output through the REAL source rewriter
(``_build_source_sql``) OFFLINE (deterministic FakeDB, no live stack) and asserts
the emitted SQL structurally: display column in SELECT AS ``<dim>__caption`` AND
in GROUP BY. Reverting the grain append (or the SELECT passthrough) fails this.
"""
from __future__ import annotations

import types

import pytest

from conftest import attach_fixture_deployed_shape
import sqlglot
from sqlglot import exp

from src.api.routes import _augment_execute_with_caption_columns
from src.ir.logical_query import BoundQuery
from src.parsing.sql_parser import parse_sql_to_ir
from src.rewrite.query_rewriter import _build_source_sql

from shared.db.models import (
    DataSource,
    Join,
    Measure,
    ModelColumn,
    ModelTable,
    UserDefinedAttribute,
)


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
        self._cols_by_id = {c.id: c for c in columns}

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
        # The caption augmentation loads the DISPLAY ModelColumn by id.
        if model_cls is ModelColumn:
            return self._cols_by_id.get(pk)
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


def _dim(id_, name, source_column_id, display_column_id=None):
    return types.SimpleNamespace(
        id=id_, model_id="m1", name=name, display_name=name,
        source_column_id=source_column_id, user_defined_attribute_id=None,
        display_column_id=display_column_id,
    )


def _db():
    cols = [
        _col("c-pcode", "product_code"),
        _col("c-pname", "product_name"),
        _col("c-amount", "amount"),
    ]
    return FakeDB(
        tables=[_tbl()],
        columns=cols,
        measures=[_meas("m-amount", "amount", "c-amount")],
    )


def _bound(raw_sql, *, dims, measures):
    lq = parse_sql_to_ir(raw_sql, "m1")
    model = types.SimpleNamespace(
        id="m1", slug="modely", display_name="modely", deployed_version_id="v1",
    )
    return BoundQuery(
        logical_query=lq,
        model=model,
        resolved_measures=measures,
        resolved_dimensions=dims,
        resolved_filters=[],
    )


def _group_by_columns(sql: str) -> set[str]:
    ast = sqlglot.parse_one(sql, read="postgres")
    select = ast if isinstance(ast, exp.Select) else ast.find(exp.Select)
    group = select.args.get("group")
    if group is None:
        return set()
    return {c.name.lower() for c in group.find_all(exp.Column)}


@pytest.mark.asyncio
async def test_caption_reaches_select_and_group_by():
    pcode = _dim("d-pcode", "product_code", "c-pcode", display_column_id="c-pname")
    amount = _meas("m-amount", "amount", "c-amount")
    bq = _bound(
        'SELECT product_code, SUM(amount) FROM tx GROUP BY product_code',
        dims=[pcode], measures=[amount],
    )
    assert bq.logical_query.grain == ["product_code"]

    added = await _augment_execute_with_caption_columns(
        bq, _db(), ["product_code"]
    )
    assert added == ["product_code__caption"]
    # grain now carries the caption alias (so it enters GROUP BY).
    assert "product_code__caption" in bq.logical_query.grain

    await attach_fixture_deployed_shape(bq, _db())
    sql = await _build_source_sql(bq, _db(), target_dialect="postgres")

    # SELECT projects the DISPLAY column aliased as the companion caption column.
    assert '"product_name" AS "product_code__caption"' in sql, (
        f"caption column not projected into SELECT: {sql}"
    )
    # GROUP BY includes the display column (else the query faults at the source).
    gb = _group_by_columns(sql)
    assert "product_name" in gb, (
        f"display column missing from GROUP BY (would fault at source, "
        f"PostgreSQL 42803): {sql}"
    )
    assert "product_code" in gb, f"key column dropped from GROUP BY: {sql}"
    # The measure is still aggregated.
    assert "SUM(" in sql.upper(), f"measure aggregate missing: {sql}"


@pytest.mark.asyncio
async def test_no_caption_signal_leaves_sql_unchanged():
    pcode = _dim("d-pcode", "product_code", "c-pcode", display_column_id="c-pname")
    amount = _meas("m-amount", "amount", "c-amount")
    bq = _bound(
        'SELECT product_code, SUM(amount) FROM tx GROUP BY product_code',
        dims=[pcode], measures=[amount],
    )
    # No signal -> no augmentation -> no caption column.
    added = await _augment_execute_with_caption_columns(bq, _db(), None)
    assert added == []
    await attach_fixture_deployed_shape(bq, _db())
    sql = await _build_source_sql(bq, _db(), target_dialect="postgres")
    assert "__caption" not in sql, f"caption column leaked without a signal: {sql}"
