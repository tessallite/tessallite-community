"""Unit tests for ``shared.semantic.sql_builder.build_pocket_select_sql``.

Mocks the AsyncSession to return a small, hand-built model
(fact + one dim + one join) and asserts the emitted CTAS body
expands ``FROM <model_slug>`` into the joined physical SQL with
column references qualified by table alias.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

from shared.semantic.sql_builder import build_from_clause, build_pocket_select_sql


@dataclass
class _FakePocket:
    id: UUID
    model_id: UUID
    defining_sql: str


@dataclass
class _FakeTable:
    id: UUID
    physical_name: str
    table_type: str = "dim_aggregate"


@dataclass
class _FakeColumn:
    id: UUID
    model_table_id: UUID
    column_name: str


@dataclass
class _FakeJoin:
    left_table_id: UUID
    right_table_id: UUID
    left_column_id: UUID
    right_column_id: UUID
    join_type: str = "left"


@dataclass
class _FakeDim:
    id: UUID
    name: str
    source_column_id: Optional[UUID]
    user_defined_attribute_id: Optional[UUID] = None


@dataclass
class _FakeMeasure:
    id: UUID
    name: str
    source_column_id: Optional[UUID]
    user_defined_attribute_id: Optional[UUID] = None


def _build_db():
    """Construct a model: payment_transaction (fact) JOIN dim_auth_method."""
    fact_id = uuid4()
    dim_id = uuid4()
    fact = _FakeTable(
        id=fact_id,
        physical_name="demo_data.payment_transaction",
        table_type="fact",
    )
    dim = _FakeTable(
        id=dim_id,
        physical_name="demo_data.dim_auth_method",
        table_type="dim",
    )
    fact_amount = _FakeColumn(id=uuid4(), model_table_id=fact_id, column_name="base_amount")
    fact_country = _FakeColumn(id=uuid4(), model_table_id=fact_id, column_name="country")
    fact_auth_id = _FakeColumn(id=uuid4(), model_table_id=fact_id, column_name="auth_method_id")
    dim_id_col = _FakeColumn(id=uuid4(), model_table_id=dim_id, column_name="id")
    dim_name = _FakeColumn(id=uuid4(), model_table_id=dim_id, column_name="auth_method_name")
    join = _FakeJoin(
        left_table_id=fact_id,
        right_table_id=dim_id,
        left_column_id=fact_auth_id.id,
        right_column_id=dim_id_col.id,
    )
    dim_country = _FakeDim(id=uuid4(), name="country", source_column_id=fact_country.id)
    dim_auth = _FakeDim(id=uuid4(), name="auth_method", source_column_id=dim_name.id)
    measure_amount = _FakeMeasure(id=uuid4(), name="amount", source_column_id=fact_amount.id)

    tables = [fact, dim]
    columns = [fact_amount, fact_country, fact_auth_id, dim_id_col, dim_name]
    joins = [join]
    dims = [dim_country, dim_auth]
    measures = [measure_amount]

    from shared.db.models import Dimension, Join, Measure, ModelColumn, ModelTable

    def _result(items):
        r = MagicMock()
        r.scalars.return_value.all.return_value = items
        return r

    async def _execute(stmt):
        # Use the entity in column descriptions to dispatch.
        ent = None
        try:
            ent = stmt.column_descriptions[0]["entity"]
        except Exception:
            pass
        if ent is ModelTable:
            return _result(tables)
        if ent is Join:
            return _result(joins)
        if ent is ModelColumn:
            return _result(columns)
        if ent is Dimension:
            return _result(dims)
        if ent is Measure:
            return _result(measures)
        return _result([])

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_execute)
    return db, fact_id, dim_id


def _run(coro):
    return asyncio.run(coro)


def test_build_pocket_select_sql_expands_from_and_qualifies_where():
    db, _fact_id, _dim_id = _build_db()
    pocket = _FakePocket(
        id=uuid4(),
        model_id=uuid4(),
        defining_sql="SELECT * FROM modely WHERE country = 'GB'",
    )
    sql = _run(build_pocket_select_sql(pocket, db))

    # FROM is the joined physical SQL anchored on the fact table.
    assert '"demo_data"."payment_transaction" AS base' in sql
    assert "LEFT JOIN" in sql
    assert '"demo_data"."dim_auth_method"' in sql

    # WHERE column is qualified with the alias of the column's table.
    assert 'base."country"' in sql

    # Projection includes columns from both tables, alias-qualified.
    assert 'base."base_amount"' in sql
    assert '"auth_method_name"' in sql

    # The pocket's own ``FROM modely`` is no longer present.
    assert "FROM modely" not in sql.lower() or "FROM modely" not in sql


def test_build_pocket_select_sql_no_where_no_extra_clauses():
    db, _fact_id, _dim_id = _build_db()
    pocket = _FakePocket(
        id=uuid4(),
        model_id=uuid4(),
        defining_sql="SELECT * FROM modely",
    )
    sql = _run(build_pocket_select_sql(pocket, db))
    assert "WHERE" not in sql.upper()
    assert "ORDER BY" not in sql.upper()
    assert "LIMIT" not in sql.upper()


def test_build_pocket_select_sql_preserves_order_and_limit():
    db, _fact_id, _dim_id = _build_db()
    pocket = _FakePocket(
        id=uuid4(),
        model_id=uuid4(),
        defining_sql="SELECT * FROM modely ORDER BY country LIMIT 50",
    )
    sql = _run(build_pocket_select_sql(pocket, db))
    assert "ORDER BY" in sql.upper()
    assert "LIMIT 50" in sql.upper()
    assert 'base."country"' in sql


# ---------------------------------------------------------------------------
# build_from_clause — join closure / intermediate table tests
# ---------------------------------------------------------------------------

@dataclass
class _FakeModelTable:
    id: UUID
    model_id: UUID
    physical_name: str
    table_type: str = "dim"
    alias: str | None = None


def _build_chain_db():
    """Construct a 3-table chain: fact -> dim_customer -> dim_city.

    This is the snowflake / bridge scenario where dim_city is only
    reachable through dim_customer.
    """
    model_id = uuid4()
    fact_id, cust_id, city_id = uuid4(), uuid4(), uuid4()
    fact = _FakeModelTable(id=fact_id, model_id=model_id, physical_name="fact_sales", table_type="fact")
    cust = _FakeModelTable(id=cust_id, model_id=model_id, physical_name="dim_customer")
    city = _FakeModelTable(id=city_id, model_id=model_id, physical_name="dim_city")

    fc_left = _FakeColumn(id=uuid4(), model_table_id=fact_id, column_name="customer_id")
    fc_right = _FakeColumn(id=uuid4(), model_table_id=cust_id, column_name="id")
    cc_left = _FakeColumn(id=uuid4(), model_table_id=cust_id, column_name="city_id")
    cc_right = _FakeColumn(id=uuid4(), model_table_id=city_id, column_name="id")

    j1 = _FakeJoin(left_table_id=fact_id, right_table_id=cust_id,
                    left_column_id=fc_left.id, right_column_id=fc_right.id)
    j2 = _FakeJoin(left_table_id=cust_id, right_table_id=city_id,
                    left_column_id=cc_left.id, right_column_id=cc_right.id)

    tables = [fact, cust, city]
    columns = [fc_left, fc_right, cc_left, cc_right]
    joins = [j1, j2]

    from shared.db.models import Join as JoinModel, ModelColumn, ModelTable

    def _result(items):
        r = MagicMock()
        r.scalars.return_value.all.return_value = items
        return r

    async def _execute(stmt):
        ent = None
        try:
            ent = stmt.column_descriptions[0]["entity"]
        except Exception:
            pass
        if ent is ModelTable:
            return _result(tables)
        if ent is JoinModel:
            return _result(joins)
        if ent is ModelColumn:
            return _result(columns)
        return _result([])

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_execute)
    return db, model_id, fact_id, cust_id, city_id


def test_from_clause_includes_intermediate_join_table():
    """When only dim_city is needed, dim_customer must be included as
    an intermediate table on the path from fact to dim_city."""
    db, model_id, fact_id, cust_id, city_id = _build_chain_db()
    from_sql, aliases = _run(build_from_clause(db, model_id, needed_table_ids={city_id}))
    assert city_id in aliases, "dim_city must appear in alias map"
    assert cust_id in aliases, "dim_customer must appear as intermediate"
    assert '"dim_city"' in from_sql
    assert '"dim_customer"' in from_sql


def test_from_clause_prunes_unneeded_tables():
    """When only dim_customer is needed, dim_city should be pruned."""
    db, model_id, fact_id, cust_id, city_id = _build_chain_db()
    from_sql, aliases = _run(build_from_clause(db, model_id, needed_table_ids={cust_id}))
    assert cust_id in aliases
    assert city_id not in aliases
    assert '"dim_city"' not in from_sql


def test_from_clause_raises_on_unreachable_table():
    """A needed table with no join path raises ValueError."""
    db, model_id, fact_id, cust_id, city_id = _build_chain_db()
    orphan_id = uuid4()
    import pytest as _pt
    with _pt.raises(ValueError, match="Cannot reach"):
        _run(build_from_clause(db, model_id, needed_table_ids={orphan_id}))


# ---------------------------------------------------------------------------
# build_pocket_select_sql — FROM pruning via needed_table_ids
# ---------------------------------------------------------------------------

def _build_db_with_extra_table():
    """Model with fact + dim + a zero-column joined table (e.g. calendar alias).

    The extra table has a join but no ModelColumn rows exposed for
    projection, so it should be excluded from the pocket FROM via
    needed_table_ids derivation.
    """
    fact_id = uuid4()
    dim_id = uuid4()
    extra_id = uuid4()
    model_id = uuid4()

    fact = _FakeTable(id=fact_id, physical_name="demo_data.payment_transaction", table_type="fact")
    dim = _FakeTable(id=dim_id, physical_name="demo_data.dim_auth_method")
    extra = _FakeTable(id=extra_id, physical_name="demo_data.empty_calendar")

    fact_amount = _FakeColumn(id=uuid4(), model_table_id=fact_id, column_name="base_amount")
    fact_country = _FakeColumn(id=uuid4(), model_table_id=fact_id, column_name="country")
    fact_auth_id = _FakeColumn(id=uuid4(), model_table_id=fact_id, column_name="auth_method_id")
    fact_date_id = _FakeColumn(id=uuid4(), model_table_id=fact_id, column_name="date_id")
    dim_id_col = _FakeColumn(id=uuid4(), model_table_id=dim_id, column_name="id")
    dim_name = _FakeColumn(id=uuid4(), model_table_id=dim_id, column_name="auth_method_name")
    extra_join_col = _FakeColumn(id=uuid4(), model_table_id=extra_id, column_name="date_key")

    join1 = _FakeJoin(
        left_table_id=fact_id, right_table_id=dim_id,
        left_column_id=fact_auth_id.id, right_column_id=dim_id_col.id,
    )
    join2 = _FakeJoin(
        left_table_id=fact_id, right_table_id=extra_id,
        left_column_id=fact_date_id.id, right_column_id=extra_join_col.id,
    )

    dim_country = _FakeDim(id=uuid4(), name="country", source_column_id=fact_country.id)
    dim_auth = _FakeDim(id=uuid4(), name="auth_method", source_column_id=dim_name.id)
    measure_amount = _FakeMeasure(id=uuid4(), name="amount", source_column_id=fact_amount.id)

    tables = [fact, dim, extra]
    columns_with_projection = [fact_amount, fact_country, fact_auth_id, fact_date_id, dim_id_col, dim_name]
    all_join_columns = columns_with_projection + [extra_join_col]
    dims = [dim_country, dim_auth]
    measures = [measure_amount]

    from shared.db.models import Dimension, Join as JoinModel, Measure, ModelColumn, ModelTable

    def _result(items):
        r = MagicMock()
        r.scalars.return_value.all.return_value = items
        return r

    mc_call = [0]

    async def _execute(stmt):
        ent = None
        try:
            ent = stmt.column_descriptions[0]["entity"]
        except Exception:
            pass
        if ent is ModelTable:
            return _result(tables)
        if ent is JoinModel:
            return _result([join1, join2])
        if ent is ModelColumn:
            mc_call[0] += 1
            if mc_call[0] == 2:
                return _result(all_join_columns)
            return _result(columns_with_projection)
        if ent is Dimension:
            return _result(dims)
        if ent is Measure:
            return _result(measures)
        return _result([])

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_execute)
    return db, model_id, extra_id


def test_pocket_select_prunes_zero_column_table():
    """Pocket FROM should not include a table that has no projectable columns."""
    db, model_id, extra_id = _build_db_with_extra_table()
    pocket = _FakePocket(
        id=uuid4(),
        model_id=model_id,
        defining_sql="SELECT * FROM modely WHERE country = 'GB'",
    )
    sql = _run(build_pocket_select_sql(pocket, db))
    assert '"demo_data"."payment_transaction" AS base' in sql
    assert '"demo_data"."dim_auth_method"' in sql
    assert '"demo_data"."empty_calendar"' not in sql


# ---------------------------------------------------------------------------
# build_from_clause — physical_name_overrides
# ---------------------------------------------------------------------------

def test_from_clause_physical_name_overrides():
    """Calendar table override: FROM clause uses target-side name."""
    db, model_id, fact_id, cust_id, city_id = _build_chain_db()
    override_name = "acme_aggregates.tess_cal_standard_1"
    overrides = {city_id: override_name}
    from_sql, aliases = _run(
        build_from_clause(
            db, model_id,
            needed_table_ids={city_id},
            physical_name_overrides=overrides,
        )
    )
    assert '"acme_aggregates"."tess_cal_standard_1"' in from_sql
    assert '"dim_city"' not in from_sql
    assert city_id in aliases
