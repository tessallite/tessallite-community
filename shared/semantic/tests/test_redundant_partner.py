"""Unit tests for the redundant-partner helper."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import UUID, uuid4

from shared.semantic.redundant_partner import compute_redundant_partners


@dataclass
class _FakeTable:
    id: UUID
    physical_name: str
    table_type: str


@dataclass
class _FakeColumn:
    id: UUID
    model_table_id: UUID
    column_name: str


@dataclass
class _FakeJoin:
    id: UUID
    left_table_id: UUID
    right_table_id: UUID
    left_column_id: UUID
    right_column_id: UUID
    join_type: str


def _build_inner_join_fixture():
    fact = _FakeTable(uuid4(), "demo_data.fact_payment", "fact")
    dim = _FakeTable(uuid4(), "demo_data.dim_account", "dim_aggregate")
    fact_col = _FakeColumn(uuid4(), fact.id, "account_type")
    dim_col = _FakeColumn(uuid4(), dim.id, "account_type_code")
    j = _FakeJoin(uuid4(), dim.id, fact.id, dim_col.id, fact_col.id, "inner")
    tables = {fact.id: fact, dim.id: dim}
    cols = {fact_col.id: fact_col, dim_col.id: dim_col}
    return j, fact, dim, fact_col, dim_col, tables, cols


def test_inner_join_marks_dim_side_redundant():
    j, fact, dim, fact_col, dim_col, tables, cols = _build_inner_join_fixture()
    hints = compute_redundant_partners([j], tables, cols)
    assert dim_col.id in hints
    assert fact_col.id not in hints
    h = hints[dim_col.id]
    assert h.partner_column_name == "account_type"
    assert h.join_type == "inner"


def test_full_outer_join_never_marks_redundant():
    j, fact, dim, fact_col, dim_col, tables, cols = _build_inner_join_fixture()
    j.join_type = "full"
    hints = compute_redundant_partners([j], tables, cols)
    assert hints == {}


def test_left_join_with_fact_on_left_marks_right_redundant():
    fact = _FakeTable(uuid4(), "fact", "fact")
    dim = _FakeTable(uuid4(), "dim", "dim_aggregate")
    fact_col = _FakeColumn(uuid4(), fact.id, "country_code")
    dim_col = _FakeColumn(uuid4(), dim.id, "iso_code")
    j = _FakeJoin(uuid4(), fact.id, dim.id, fact_col.id, dim_col.id, "left")
    hints = compute_redundant_partners(
        [j],
        {fact.id: fact, dim.id: dim},
        {fact_col.id: fact_col, dim_col.id: dim_col},
    )
    assert dim_col.id in hints
    assert fact_col.id not in hints


def test_left_join_with_fact_on_right_not_redundant():
    fact = _FakeTable(uuid4(), "fact", "fact")
    dim = _FakeTable(uuid4(), "dim", "dim_aggregate")
    fact_col = _FakeColumn(uuid4(), fact.id, "country_code")
    dim_col = _FakeColumn(uuid4(), dim.id, "iso_code")
    j = _FakeJoin(uuid4(), dim.id, fact.id, dim_col.id, fact_col.id, "left")
    hints = compute_redundant_partners(
        [j],
        {fact.id: fact, dim.id: dim},
        {fact_col.id: fact_col, dim_col.id: dim_col},
    )
    assert hints == {}  # fact is on the nullable side; dim isn't redundant


def test_fact_to_fact_join_not_redundant():
    a = _FakeTable(uuid4(), "fact_a", "fact")
    b = _FakeTable(uuid4(), "fact_b", "fact")
    ca = _FakeColumn(uuid4(), a.id, "id")
    cb = _FakeColumn(uuid4(), b.id, "a_id")
    j = _FakeJoin(uuid4(), a.id, b.id, ca.id, cb.id, "inner")
    hints = compute_redundant_partners(
        [j],
        {a.id: a, b.id: b},
        {ca.id: ca, cb.id: cb},
    )
    assert hints == {}


def test_legacy_cardinality_join_type_treated_as_inner():
    j, fact, dim, fact_col, dim_col, tables, cols = _build_inner_join_fixture()
    j.join_type = "many_to_one"
    hints = compute_redundant_partners([j], tables, cols)
    assert dim_col.id in hints
