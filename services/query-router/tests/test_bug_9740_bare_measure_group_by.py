"""Bug-9740 — a bare measure name projected by a GROUPED query must bind as a
MEASURE at its ``default_agg``, not as a raw value column.

Live symptom (JDBC, model ``modely``)::

    SELECT account_type, avg_base_amount FROM modely GROUP BY account_type

returned HTTP 502 with ``column "payment_transaction.base_amount" must appear
in the GROUP BY clause or be used in an aggregate function``. The binder
resolved the bare measure through its "measure used outside an aggregate"
fallback, which wraps the measure as a virtual dimension
(``is_measure_as_dimension``) projecting the raw physical column. With a GROUP
BY in the query that projection is invalid SQL, and on a source that tolerated
it the number would be wrong.

The same model's ``SELECT *`` expansion already resolves ``avg_base_amount`` as
a MEASURE at ``default_agg=avg`` — so the star projection and the named
projection disagreed about what the same object is. These tests pin the
agreement.

The UNGROUPED bare projection must keep the raw-value shape: drill-through leaf
detail (``drill/semantic_builder.py``, leaf mode) emits no GROUP BY and relies
on the bare measure projecting one raw value per contributing fact row.

Run from tessallite/services/query-router/:
    pytest tests/test_bug_9740_bare_measure_group_by.py -v
"""
from __future__ import annotations

import types
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
import sqlglot
from sqlglot import exp

from conftest import attach_fixture_deployed_shape
from result_fakes import ScalarResult

from shared.db.models import (
    DataSource,
    Join,
    Measure,
    ModelColumn,
    ModelTable,
    UserDefinedAttribute,
)
from src.parsing.sql_parser import parse_sql_to_ir
from src.rewrite.query_rewriter import _build_source_sql
from src.semantic.binder import bind_query_to_model


# ---------------------------------------------------------------------------
# Fixture model: one fact table with a dimension and two measures, one of them
# ``default_agg=avg`` (the reported shape).
# ---------------------------------------------------------------------------

COL_ACCOUNT_TYPE = "c-account-type"
COL_BASE_AMOUNT = "c-base-amount"
COL_TXN_AMOUNT = "c-txn-amount"


def _model():
    return types.SimpleNamespace(
        id="m1", slug="modely", display_name="modely",
        deployed_version_id="v1", project=None,
    )


def _dim(id_, name, source_column_id):
    return types.SimpleNamespace(
        id=id_, model_id="m1", name=name, display_name=name,
        source_column_id=source_column_id, user_defined_attribute_id=None,
    )


def _meas(id_, name, source_column_id, default_agg):
    return types.SimpleNamespace(
        id=id_, model_id="m1", name=name, display_name=name,
        measure_type="standard", default_agg=default_agg, is_additive=True,
        source_column_id=source_column_id, user_defined_attribute_id=None,
        expression=None, calc_agg_mode=None, variant_kind=None,
        variant_of_measure_id=None, variant_n=None, is_invalid=False,
        invalid_reason=None, semi_additive_behavior=None,
    )


ACCOUNT_TYPE = _dim("d-account-type", "account_type", COL_ACCOUNT_TYPE)
AVG_BASE_AMOUNT = _meas("m-avg-base", "avg_base_amount", COL_BASE_AMOUNT, "avg")
TXN_AMOUNT = _meas("m-txn-amount", "transaction_amount", COL_TXN_AMOUNT, "sum")


def _binder_patches():
    """Patch the binder's model + deployed-shape loaders with the fixture model."""
    from src.semantic.snapshot_resolver import DeployedShape

    measures = [AVG_BASE_AMOUNT, TXN_AMOUNT]
    dimensions = [ACCOUNT_TYPE]
    columns_by_id = {
        COL_ACCOUNT_TYPE: {
            "id": COL_ACCOUNT_TYPE, "column_name": "account_type",
            "data_type": "text",
        },
        COL_BASE_AMOUNT: {
            "id": COL_BASE_AMOUNT, "column_name": "base_amount",
            "data_type": "numeric",
        },
        COL_TXN_AMOUNT: {
            "id": COL_TXN_AMOUNT, "column_name": "transaction_amount",
            "data_type": "numeric",
        },
    }
    shape = DeployedShape(
        measures=list(measures),
        dimensions=list(dimensions),
        hidden_column_ids=set(),
        physical_columns_all={"account_type", "base_amount", "transaction_amount"},
        physical_columns_visible={"account_type", "base_amount", "transaction_amount"},
        hierarchy_rows=[],
        columns_by_id=columns_by_id,
    )
    stack = ExitStack()
    stack.enter_context(patch(
        "src.semantic.binder._load_model", new=AsyncMock(return_value=_model()),
    ))
    stack.enter_context(patch(
        "src.semantic.binder.resolve_deployed_shape",
        new=AsyncMock(return_value=shape),
    ))
    return stack


async def _bind(raw_sql: str):
    lq = parse_sql_to_ir(raw_sql, "m1")
    db = AsyncMock()
    with _binder_patches():
        return await bind_query_to_model(lq, db, include_hidden=False)


# ---------------------------------------------------------------------------
# Offline FakeDB for the source rewriter (physical graph only).
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return ScalarResult(self._rows)

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


def _col(id_, name):
    return types.SimpleNamespace(
        id=id_, model_table_id="t-fact", column_name=name,
    )


def _render_db():
    return FakeDB(
        tables=[types.SimpleNamespace(
            id="t-fact", model_id="m1",
            physical_name="demo_data.payment_transaction",
            alias="payment_transaction", table_type="fact", source_id="s1",
        )],
        columns=[
            _col(COL_ACCOUNT_TYPE, "account_type"),
            _col(COL_BASE_AMOUNT, "base_amount"),
            _col(COL_TXN_AMOUNT, "transaction_amount"),
        ],
        measures=[AVG_BASE_AMOUNT, TXN_AMOUNT],
    )


async def _source_sql(raw_sql: str) -> str:
    bound = await _bind(raw_sql)
    db = _render_db()
    await attach_fixture_deployed_shape(bound, db)
    return await _build_source_sql(bound, db, target_dialect="postgres")


def _select(sql: str) -> exp.Select:
    ast = sqlglot.parse_one(sql, read="postgres")
    select = ast if isinstance(ast, exp.Select) else ast.find(exp.Select)
    assert select is not None, f"no SELECT in emitted SQL: {sql}"
    return select


def _ungrouped_bare_columns(sql: str) -> set[str]:
    """Physical columns projected bare while a GROUP BY is present.

    PostgreSQL rejects exactly these; the 502 in the report was this set being
    non-empty. Columns inside an aggregate call do not count.
    """
    select = _select(sql)
    group = select.args.get("group")
    if group is None:
        return set()
    grouped = {c.name.lower() for c in group.find_all(exp.Column)}
    bare: set[str] = set()
    for item in select.expressions:
        inner = item.this if isinstance(item, exp.Alias) else item
        if list(inner.find_all(exp.AggFunc)):
            continue
        for col in inner.find_all(exp.Column):
            if col.name.lower() not in grouped:
                bare.add(col.name.lower())
    return bare


# ---------------------------------------------------------------------------
# Binder classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bare_avg_measure_with_group_by_binds_as_measure():
    """The reported query. Before the fix the measure landed in
    ``resolved_dimensions`` as a virtual dimension and the source DB rejected
    the emitted SQL."""
    bound = await _bind(
        "SELECT account_type, avg_base_amount FROM modely GROUP BY account_type"
    )
    measure_names = {m.name for m in bound.resolved_measures}
    dim_names = {d.name for d in bound.resolved_dimensions}

    assert "avg_base_amount" in measure_names, (
        "a bare measure in a GROUPED query bound as a raw column, so the "
        "source database rejects the query (Bug-9740)"
    )
    assert "avg_base_amount" not in dim_names
    assert "account_type" in dim_names


@pytest.mark.asyncio
async def test_bare_sum_measure_with_group_by_binds_as_measure():
    """Not avg-specific: any bare measure in a grouped query is the measure at
    its own ``default_agg``."""
    bound = await _bind(
        "SELECT account_type, transaction_amount FROM modely GROUP BY account_type"
    )
    assert "transaction_amount" in {m.name for m in bound.resolved_measures}
    assert "transaction_amount" not in {d.name for d in bound.resolved_dimensions}


@pytest.mark.asyncio
async def test_named_projection_agrees_with_select_star():
    """Catalogue/executor agreement at the model level: ``SELECT *`` already
    resolves this object as a measure, so naming it must not turn it into a
    dimension."""
    star = await _bind("SELECT * FROM modely")
    named = await _bind(
        "SELECT account_type, avg_base_amount FROM modely GROUP BY account_type"
    )
    assert "avg_base_amount" in {m.name for m in star.resolved_measures}
    assert "avg_base_amount" in {m.name for m in named.resolved_measures}


# ---------------------------------------------------------------------------
# Shapes that must NOT change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ungrouped_bare_measure_still_projects_raw_value():
    """Drill-through LEAF detail emits no GROUP BY and needs the raw value
    column once per contributing fact row. That shape must be untouched."""
    bound = await _bind(
        "SELECT account_type, avg_base_amount FROM modely ORDER BY avg_base_amount DESC"
    )
    dim_names = {d.name for d in bound.resolved_dimensions}
    assert "avg_base_amount" in dim_names, (
        "an UNGROUPED bare measure must still project its raw value column — "
        "drill-through leaf detail depends on it"
    )
    assert "avg_base_amount" not in {m.name for m in bound.resolved_measures}
    measure_as_dim = next(
        d for d in bound.resolved_dimensions if d.name == "avg_base_amount"
    )
    assert getattr(measure_as_dim, "is_measure_as_dimension", False) is True, (
        "the persona measure allow-list gates this projection through "
        "is_measure_as_dimension (persona_gate.enforce_persona)"
    )


@pytest.mark.asyncio
async def test_measure_named_in_group_by_stays_a_grouping_column():
    """``GROUP BY <measure column>`` is valid SQL that groups by the raw
    column. It must not be turned into an aggregate."""
    bound = await _bind(
        "SELECT account_type, avg_base_amount FROM modely "
        "GROUP BY account_type, avg_base_amount"
    )
    assert "avg_base_amount" in {d.name for d in bound.resolved_dimensions}
    assert "avg_base_amount" not in {m.name for m in bound.resolved_measures}


@pytest.mark.asyncio
async def test_measure_inside_expression_keeps_passthrough_binding():
    """A measure column referenced inside an arithmetic expression is not a
    bare projection, so its passthrough rendering is unchanged."""
    bound = await _bind(
        "SELECT account_type, avg_base_amount * 2 AS doubled FROM modely "
        "GROUP BY account_type"
    )
    assert "avg_base_amount" not in {m.name for m in bound.resolved_measures}
    assert bound.has_passthrough_expressions is True


# ---------------------------------------------------------------------------
# Emitted source SQL — the boundary the source database actually rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emitted_sql_aggregates_the_bare_avg_measure():
    sql = await _source_sql(
        "SELECT account_type, avg_base_amount FROM modely GROUP BY account_type"
    )
    assert _ungrouped_bare_columns(sql) == set(), (
        f"emitted SQL still projects an ungrouped bare column, which the "
        f"source database rejects: {sql}"
    )
    select = _select(sql)
    aggs = [a for a in select.find_all(exp.Avg)]
    assert aggs, f"the avg measure was not aggregated with AVG: {sql}"
    assert any(
        c.name.lower() == "base_amount"
        for a in aggs for c in a.find_all(exp.Column)
    ), f"AVG() does not wrap the measure's physical column: {sql}"
    assert '"avg_base_amount"' in sql, (
        f"the measure's semantic name must remain the output column: {sql}"
    )


@pytest.mark.asyncio
async def test_each_bare_measure_gets_its_own_default_agg():
    """Two bare measures in one grouped query are aggregated independently —
    the avg measure with AVG, the sum measure with SUM."""
    sql = await _source_sql(
        "SELECT account_type, avg_base_amount, transaction_amount FROM modely "
        "GROUP BY account_type"
    )
    assert _ungrouped_bare_columns(sql) == set(), sql
    assert 'AVG("payment_transaction"."base_amount") AS "avg_base_amount"' in sql, sql
    assert (
        'SUM("payment_transaction"."transaction_amount") AS "transaction_amount"'
        in sql
    ), sql


@pytest.mark.asyncio
async def test_order_by_a_bare_measure_references_its_output_alias():
    """A BI tool sorting by the measure it selected must still get valid SQL."""
    sql = await _source_sql(
        "SELECT account_type, avg_base_amount FROM modely "
        "GROUP BY account_type ORDER BY avg_base_amount DESC"
    )
    assert _ungrouped_bare_columns(sql) == set(), sql
    assert 'ORDER BY "avg_base_amount" DESC' in sql, sql


@pytest.mark.asyncio
async def test_emitted_sql_for_ungrouped_leaf_projection_is_unchanged():
    """No GROUP BY -> no aggregate; the leaf detail projection still reads the
    raw value column."""
    sql = await _source_sql("SELECT account_type, avg_base_amount FROM modely")
    select = _select(sql)
    assert select.args.get("group") is None, f"unexpected GROUP BY: {sql}"
    assert not list(select.find_all(exp.Avg)), (
        f"an ungrouped bare measure must not be aggregated: {sql}"
    )
    assert "base_amount" in sql
