"""Result-level render guards for Bug-6081/6082/6083 (Fable F-003-15/16/17).

The parser unit tests in ``test_sql_parser.py`` assert the LogicalQuery IR
(``grain`` / ``limit`` / ``has_unresolvable_where``). Those are necessary but
not sufficient: a BI user only gets correct numbers if the parser's IR actually
reaches the SQL the source database executes. These tests close that gap OFFLINE
(deterministic FakeDB, no live stack) by driving the real parser output through
the SOURCE rewriter (``_build_source_sql`` / ``rewrite_for_source``) and
asserting the emitted SQL:

  * Bug-6082 — ``GROUP BY 1`` resolves so the rebuilt query GROUPS BY the region
    column (one row per region, not duplicate rows).
  * Bug-6083 — ``FETCH FIRST n ROWS ONLY`` bounds the rebuilt query to ``LIMIT n``.
  * Bug-6081 — a bare-boolean WHERE conjunct (``WHERE is_active``) is PRESERVED
    in the rebuilt query's WHERE (predicate not silently dropped), and a mixed
    ``is_active AND region = 'EU'`` keeps BOTH predicates.

Full live-row execution remains the SQL e2e suite's job (Docker); this proves
the rewrite boundary the parser fix feeds.
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
    UserDefinedAttribute,
)


# ---------------------------------------------------------------------------
# AST assertion helpers — inspect the EMITTED SQL structurally (a substring
# check for "LIMIT 5" would also pass on "LIMIT 50"; parse the emitted SQL and
# assert the exact node instead).
# ---------------------------------------------------------------------------


def _emitted_select(sql: str) -> exp.Select:
    ast = sqlglot.parse_one(sql, read="postgres")
    select = ast if isinstance(ast, exp.Select) else ast.find(exp.Select)
    assert select is not None, f"no SELECT in emitted SQL: {sql}"
    return select


def _emitted_limit(sql: str) -> int | None:
    limit = _emitted_select(sql).args.get("limit")
    if limit is None or limit.expression is None:
        return None
    return int(limit.expression.this)


def _where_column_names(sql: str) -> set[str]:
    where = _emitted_select(sql).args.get("where")
    if where is None:
        return set()
    return {c.name.lower() for c in where.find_all(exp.Column)}


def _where_has_eq(sql: str, column: str, literal: str) -> bool:
    """True when the emitted WHERE contains ``column = 'literal'`` as a real
    equality comparison (column node + literal node), either operand order."""
    where = _emitted_select(sql).args.get("where")
    if where is None:
        return False
    for node in where.find_all(exp.EQ):
        col, lit = node.this, node.expression
        if isinstance(col, exp.Literal) and isinstance(lit, exp.Column):
            col, lit = lit, col
        if (
            isinstance(col, exp.Column)
            and col.name.lower() == column.lower()
            and isinstance(lit, exp.Literal)
            and str(lit.this) == literal
        ):
            return True
    return False


def _where_has_literal_eq(sql: str, left: str, right: str) -> bool:
    where = _emitted_select(sql).args.get("where")
    if where is None:
        return False
    for node in where.find_all(exp.EQ):
        lhs, rhs = node.this, node.expression
        if isinstance(lhs, exp.Literal) and isinstance(rhs, exp.Literal):
            if str(lhs.this) == left and str(rhs.this) == right:
                return True
    return False


# ---------------------------------------------------------------------------
# Deterministic offline FakeDB (mirrors test_calculated_rewrite.py).
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


def _dim(id_, name, source_column_id):
    return types.SimpleNamespace(
        id=id_, model_id="m1", name=name, display_name=name,
        source_column_id=source_column_id, user_defined_attribute_id=None,
    )


def _db():
    cols = [
        _col("c-region", "region"),
        _col("c-amount", "amount"),
        _col("c-active", "is_active"),
    ]
    return FakeDB(
        tables=[_tbl()],
        columns=cols,
        measures=[_meas("m-amount", "amount", "c-amount")],
    )


def _bound(raw_sql, *, dims, measures, filters=None):
    lq = parse_sql_to_ir(raw_sql, "m1")
    model = types.SimpleNamespace(
        id="m1", slug="modely", display_name="modely", deployed_version_id="v1",
    )
    return BoundQuery(
        logical_query=lq,
        model=model,
        resolved_measures=measures,
        resolved_dimensions=dims,
        resolved_filters=filters or [],
    )


_REGION = _dim("d-region", "region", "c-region")
_AMOUNT = _meas("m-amount", "amount", "c-amount")


# ---------------------------------------------------------------------------
# Bug-6082 — positional GROUP BY reaches the emitted GROUP BY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_by_positional_renders_group_by_region():
    bq = _bound(
        "SELECT region, SUM(amount) FROM tx GROUP BY 1",
        dims=[_REGION], measures=[_AMOUNT],
    )
    await attach_fixture_deployed_shape(bq, _db())
    sql = await _build_source_sql(bq, _db(), target_dialect="postgres")
    up = sql.upper()
    assert "GROUP BY" in up, f"no GROUP BY emitted (duplicate rows): {sql}"
    # The region column must be the grouping key (one row per region).
    group_part = up.split("GROUP BY", 1)[1]
    assert "REGION" in group_part, f"GROUP BY does not group by region: {sql}"
    assert "SUM(" in up, f"aggregate missing: {sql}"


# ---------------------------------------------------------------------------
# Bug-6083 — FETCH FIRST n bounds the emitted query to LIMIT n
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_by_output_alias_renders_group_by_underlying_column():
    bq = _bound(
        "SELECT region AS r, SUM(amount) FROM tx GROUP BY r",
        dims=[_REGION], measures=[_AMOUNT],
    )
    assert bq.logical_query.grain == ["region"]
    await attach_fixture_deployed_shape(bq, _db())
    sql = await _build_source_sql(bq, _db(), target_dialect="postgres")
    up = sql.upper()
    assert '"region" AS "r"' in sql, f"SELECT output alias not preserved: {sql}"
    assert "GROUP BY" in up, f"GROUP BY output alias dropped during rendering: {sql}"
    group_part = up.split("GROUP BY", 1)[1]
    assert "REGION" in group_part, f"GROUP BY does not use underlying region column: {sql}"
    assert '"R"' not in group_part, f"GROUP BY must not emit SELECT alias r: {sql}"


@pytest.mark.asyncio
async def test_fetch_first_renders_limit():
    bq = _bound(
        "SELECT region, SUM(amount) FROM tx GROUP BY 1 FETCH FIRST 5 ROWS ONLY",
        dims=[_REGION], measures=[_AMOUNT],
    )
    await attach_fixture_deployed_shape(bq, _db())
    sql = await _build_source_sql(bq, _db(), target_dialect="postgres")
    assert _emitted_limit(sql) == 5, f"FETCH FIRST 5 not bounded to exactly LIMIT 5: {sql}"


@pytest.mark.asyncio
async def test_fetch_first_row_only_renders_limit_1():
    bq = _bound(
        "SELECT region, SUM(amount) FROM tx GROUP BY 1 FETCH FIRST ROW ONLY",
        dims=[_REGION], measures=[_AMOUNT],
    )
    await attach_fixture_deployed_shape(bq, _db())
    sql = await _build_source_sql(bq, _db(), target_dialect="postgres")
    assert _emitted_limit(sql) == 1, f"FETCH FIRST ROW ONLY not bounded to exactly LIMIT 1: {sql}"


# ---------------------------------------------------------------------------
# Bug-6081 — bare-boolean WHERE conjunct is preserved, not dropped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bare_boolean_where_predicate_preserved_in_rebuild():
    bq = _bound(
        "SELECT region, SUM(amount) FROM tx WHERE is_active GROUP BY region",
        dims=[_REGION], measures=[_AMOUNT],
    )
    await attach_fixture_deployed_shape(bq, _db())
    sql = await rewrite_for_source(bq, _db(), target_dialect="postgres")
    assert _emitted_select(sql).args.get("where") is not None, (
        f"WHERE clause dropped entirely (unfiltered): {sql}"
    )
    assert "is_active" in _where_column_names(sql), (
        f"is_active predicate silently dropped: {sql}"
    )


@pytest.mark.asyncio
async def test_boolean_literal_comparison_where_preserved_in_rebuild():
    bq = _bound(
        "SELECT region, SUM(amount) FROM tx WHERE 1=0 GROUP BY region",
        dims=[_REGION], measures=[_AMOUNT],
    )
    await attach_fixture_deployed_shape(bq, _db())
    sql = await rewrite_for_source(bq, _db(), target_dialect="postgres")
    assert _emitted_select(sql).args.get("where") is not None, (
        f"WHERE 1=0 clause dropped entirely (unfiltered): {sql}"
    )
    assert _where_has_literal_eq(sql, "1", "0"), (
        f"WHERE 1=0 predicate not structurally preserved: {sql}"
    )


@pytest.mark.asyncio
async def test_bare_boolean_and_comparison_both_predicates_preserved():
    bq = _bound(
        "SELECT region, SUM(amount) FROM tx WHERE is_active AND region = 'EU' GROUP BY region",
        dims=[_REGION], measures=[_AMOUNT],
    )
    await attach_fixture_deployed_shape(bq, _db())
    sql = await rewrite_for_source(bq, _db(), target_dialect="postgres")
    # The bare boolean conjunct survives as a column predicate...
    assert "is_active" in _where_column_names(sql), f"is_active predicate dropped: {sql}"
    # ...AND the region = 'EU' comparison survives structurally (column +
    # operator + literal), not merely the 'EU' literal text somewhere.
    assert _where_has_eq(sql, "region", "EU"), (
        f"region = 'EU' comparison not structurally preserved: {sql}"
    )
