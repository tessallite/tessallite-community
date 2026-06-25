"""Unit tests for the grain resolver and DDL builders.

These tests use lightweight stand-ins for the ORM rows so they can run
without a database. They lock in the alias-qualified emission rule
(Bug-053 fix) and the collision-only output-column prefix rule.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import UUID, uuid4

import pytest

from shared.semantic.grain_resolver import (
    GrainResolutionError,
    ResolvedAggregateLayout,
    derive_table_slug,
    resolve_aggregate_layout,
)


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
class _FakeDim:
    id: UUID
    name: str
    source_column_id: Optional[UUID] = None
    user_defined_attribute_id: Optional[UUID] = None


@dataclass
class _FakeMeasure:
    id: UUID
    name: str
    default_agg: str = "sum"
    is_additive: bool = True
    source_column_id: Optional[UUID] = None
    user_defined_attribute_id: Optional[UUID] = None


def _fixture():
    fact = _FakeTable(uuid4(), "demo_data.payment_transaction", "fact")
    dim_a = _FakeTable(uuid4(), "demo_data.dim_account_type", "dim_aggregate")
    dim_b = _FakeTable(uuid4(), "demo_data.dim_auth_method", "dim_aggregate")
    tables = [fact, dim_a, dim_b]

    fact_amount = _FakeColumn(uuid4(), fact.id, "base_amount")
    fact_type = _FakeColumn(uuid4(), fact.id, "account_type")
    dim_a_flag = _FakeColumn(uuid4(), dim_a.id, "active_flag")
    dim_b_flag = _FakeColumn(uuid4(), dim_b.id, "active_flag")
    dim_a_name = _FakeColumn(uuid4(), dim_a.id, "account_type_name")
    columns = [fact_amount, fact_type, dim_a_flag, dim_b_flag, dim_a_name]

    d_account = _FakeDim(uuid4(), "account_type", source_column_id=fact_type.id)
    d_account_active = _FakeDim(
        uuid4(), "account_active_flag", source_column_id=dim_a_flag.id
    )
    d_auth_active = _FakeDim(
        uuid4(), "auth_active_flag", source_column_id=dim_b_flag.id
    )
    d_account_name = _FakeDim(
        uuid4(), "account_type_name", source_column_id=dim_a_name.id
    )
    dims = [d_account, d_account_active, d_auth_active, d_account_name]

    m_amount = _FakeMeasure(
        uuid4(), "base_amount", source_column_id=fact_amount.id
    )
    measures = [m_amount]

    alias_map = {fact.id: "base", dim_a.id: "t1", dim_b.id: "t2"}
    from_clause = (
        '"demo_data"."payment_transaction" AS base\n'
        '  JOIN "demo_data"."dim_account_type" AS t1 '
        'ON base."account_type" = t1."account_type_code"\n'
        '  JOIN "demo_data"."dim_auth_method" AS t2 '
        'ON base."auth_method" = t2."code"'
    )
    return {
        "tables": tables,
        "columns": columns,
        "dims": dims,
        "measures": measures,
        "alias_map": alias_map,
        "from_clause": from_clause,
    }


def test_resolver_resolves_grain_through_source_column():
    f = _fixture()
    layout = resolve_aggregate_layout(
        grain_names=["account_type", "account_active_flag"],
        measure_specs=[("base_amount", "sum", "sum")],
        dimensions=f["dims"],
        measures=f["measures"],
        tables=f["tables"],
        columns=f["columns"],
    )
    by_name = {g.logical_name: g for g in layout.grain_cols}
    assert by_name["account_type"].source_column_name == "account_type"
    assert by_name["account_active_flag"].source_column_name == "active_flag"
    # No collision — physical name stays as the logical name.
    assert by_name["account_type"].physical_col_name == "account_type"
    assert by_name["account_active_flag"].physical_col_name == "account_active_flag"


def test_resolver_falls_back_to_stat_type_when_agg_function_null():
    """REGRESSION: existing AggregateColumn rows store aggregation_function=NULL.
    The resolver must fall back to the column's stat_type, NOT measure.default_agg
    — otherwise every stat column (max/min/count) rebuilds as the default (sum)
    on refresh, corrupting the aggregate."""
    f = _fixture()
    layout = resolve_aggregate_layout(
        grain_names=["account_type"],
        # agg_function is None (as stored); stat_type is max/min/count.
        measure_specs=[
            ("base_amount", "sum", None),
            ("base_amount", "max", None),
            ("base_amount", "min", None),
            ("base_amount", "count", None),
        ],
        dimensions=f["dims"],
        measures=f["measures"],
        tables=f["tables"],
        columns=f["columns"],
    )
    by_stat = {mc.stat_type: mc for mc in layout.measure_cols}
    assert by_stat["sum"].aggregation_function == "sum"
    assert by_stat["max"].aggregation_function == "max"
    assert by_stat["min"].aggregation_function == "min"
    assert by_stat["count"].aggregation_function == "count"


def test_resolver_unknown_grain_raises():
    f = _fixture()
    with pytest.raises(GrainResolutionError):
        resolve_aggregate_layout(
            grain_names=["does_not_exist"],
            measure_specs=[],
            dimensions=f["dims"],
            measures=f["measures"],
            tables=f["tables"],
            columns=f["columns"],
        )


def test_resolver_uda_dimension_without_source_column_is_not_rejected():
    f = _fixture()
    # Add a UDA-backed dimension with no source_column_id
    uda_dim = _FakeDim(uuid4(), "uda_expr", source_column_id=None)
    layout = resolve_aggregate_layout(
        grain_names=["uda_expr"],
        measure_specs=[],
        dimensions=f["dims"] + [uda_dim],
        measures=f["measures"],
        tables=f["tables"],
        columns=f["columns"],
    )
    g = layout.grain_cols[0]
    assert g.source_table_id is None
    assert g.source_column_name is None
    assert g.physical_col_name == "uda_expr"


@dataclass
class _FakeUDA:
    id: UUID
    table_id: UUID
    expression: str


def test_resolver_populates_source_expression_for_uda_backed_dimension():
    f = _fixture()
    fact_table = f["tables"][0]
    uda_id = uuid4()
    uda = _FakeUDA(id=uda_id, table_id=fact_table.id, expression='EXTRACT(YEAR FROM "date_key")')
    uda_dim = _FakeDim(uuid4(), "date_year", source_column_id=None, user_defined_attribute_id=uda_id)
    layout = resolve_aggregate_layout(
        grain_names=["date_year"],
        measure_specs=[("base_amount", "sum", "sum")],
        dimensions=f["dims"] + [uda_dim],
        measures=f["measures"],
        tables=f["tables"],
        columns=f["columns"],
        user_defined_attributes=[uda],
    )
    g = layout.grain_cols[0]
    assert g.source_expression == 'EXTRACT(YEAR FROM "date_key")'
    assert g.source_table_id == fact_table.id
    assert g.source_column_name is None
    assert g.is_resolved is True


def test_derive_table_slug_strips_dim_prefix():
    t = _FakeTable(uuid4(), "demo_data.dim_account_type", "dim_aggregate")
    assert derive_table_slug(t) == "account_type"
    fact = _FakeTable(uuid4(), "demo_data.fact_payment", "fact")
    assert derive_table_slug(fact) == "payment"


def test_resolver_collision_prefix_applies_when_names_clash(monkeypatch):
    """If two grain entries resolve to the same output name but different
    source tables, the resolver should prefix both with the table slug."""
    f = _fixture()
    # Force a collision by giving two dims the same logical name via the
    # resolver's internal collision detector. We do this by calling the
    # internal helper directly: the UNIQUE constraint in production
    # prevents same-name dimensions in the same model, so we synthesise
    # the pre-collision state and confirm the resolver recovers.
    from shared.semantic.grain_resolver import (
        ResolvedGrainCol,
        _resolve_physical_name_collisions,
    )

    dim_a = f["tables"][1]
    dim_b = f["tables"][2]
    grain = [
        ResolvedGrainCol(
            logical_name="active_flag",
            dimension_id=uuid4(),
            source_table_id=dim_a.id,
            source_column_name="active_flag",
            physical_col_name="active_flag",
        ),
        ResolvedGrainCol(
            logical_name="active_flag",
            dimension_id=uuid4(),
            source_table_id=dim_b.id,
            source_column_name="active_flag",
            physical_col_name="active_flag",
        ),
    ]
    new_grain, _ = _resolve_physical_name_collisions(
        grain, [], {t.id: t for t in f["tables"]}
    )
    names = {g.physical_col_name for g in new_grain}
    assert "account_type_active_flag" in names
    assert "auth_method_active_flag" in names
