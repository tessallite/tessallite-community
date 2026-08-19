"""Unit tests for the model validator's decision logic.

Covers ``validate_aggregate``, ``validate_dimension``, and
``validate_measure`` using lightweight stand-ins for the ORM rows so
the tests do not need a real database. The aggregate tests still go
through ``validate_aggregate`` with an AsyncMock session; the
dim/measure tests use the pure synchronous functions with a
hand-built ``_ModelStructure``.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

from shared.semantic.model_validator import (
    _ModelStructure,
    validate_aggregate,
    validate_dimension,
    validate_measure,
)


@dataclass
class _FakeAgg:
    id: UUID
    model_id: UUID
    grain: list
    status: str = "active"
    invalid_reason: Optional[str] = None


@dataclass
class _FakeDim:
    id: UUID
    name: str
    source_column_id: Optional[UUID] = None
    user_defined_attribute_id: Optional[UUID] = None
    is_invalid: bool = False
    invalid_reason: Optional[str] = None


@dataclass
class _FakeMeasure:
    id: UUID
    name: str
    source_column_id: Optional[UUID] = None
    user_defined_attribute_id: Optional[UUID] = None
    expression: Optional[str] = None
    measure_type: str = "standard"
    is_invalid: bool = False
    invalid_reason: Optional[str] = None


@dataclass
class _FakeTable:
    id: UUID
    physical_name: str
    table_type: str = "dim_aggregate"
    display_name: Optional[str] = None
    alias: Optional[str] = None


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
    join_type: str = "inner"


@dataclass
class _FakeAggColumn:
    id: UUID
    measure_id: Optional[UUID]
    measure: object = None


def _mock_db(dims, tables, columns, joins, measures, agg_cols):
    """Return an AsyncMock session whose ``execute`` yields the ORM rows
    in the exact order the validator asks for them."""
    call_count = [0]
    fetches = [dims, tables, columns, joins, measures, agg_cols]

    async def _execute(stmt):
        result = MagicMock()
        rows = fetches[call_count[0]] if call_count[0] < len(fetches) else []
        call_count[0] += 1
        result.scalars.return_value.all.return_value = rows
        return result

    db = AsyncMock()
    db.execute = _execute
    return db


def _run(coro):
    return asyncio.run(coro)


def test_valid_aggregate_returns_none():
    model_id = uuid4()
    fact = _FakeTable(uuid4(), "demo.fact", "fact")
    dim = _FakeTable(uuid4(), "demo.dim_country", "dim_aggregate")
    fact_col = _FakeColumn(uuid4(), fact.id, "country_code")
    dim_col = _FakeColumn(uuid4(), dim.id, "code")
    d = _FakeDim(uuid4(), "country_code", source_column_id=fact_col.id)
    j = _FakeJoin(uuid4(), fact.id, dim.id)
    agg = _FakeAgg(id=uuid4(), model_id=model_id, grain=["country_code"])

    db = _mock_db([d], [fact, dim], [fact_col, dim_col], [j], [], [])
    reason = _run(validate_aggregate(agg, db))
    assert reason is None


def test_missing_dimension_returns_reason():
    model_id = uuid4()
    fact = _FakeTable(uuid4(), "demo.fact", "fact")
    agg = _FakeAgg(id=uuid4(), model_id=model_id, grain=["deleted_dim"])

    db = _mock_db([], [fact], [], [], [], [])
    reason = _run(validate_aggregate(agg, db))
    assert reason is not None
    assert "deleted_dim" in reason


def test_unreachable_grain_table_returns_reason():
    """A dim table whose join to the fact was deleted is no longer
    reachable and the aggregate is marked invalid."""
    model_id = uuid4()
    fact = _FakeTable(uuid4(), "demo.fact", "fact")
    dim = _FakeTable(uuid4(), "demo.dim_orphan", "dim_aggregate")
    dim_col = _FakeColumn(uuid4(), dim.id, "orphan_name")
    d = _FakeDim(uuid4(), "orphan_name", source_column_id=dim_col.id)
    agg = _FakeAgg(id=uuid4(), model_id=model_id, grain=["orphan_name"])
    # no joins → dim table is not reachable from the fact anchor
    db = _mock_db([d], [fact, dim], [dim_col], [], [], [])
    reason = _run(validate_aggregate(agg, db))
    assert reason is not None
    assert "reachable" in reason.lower()


def test_empty_model_returns_reason():
    model_id = uuid4()
    agg = _FakeAgg(id=uuid4(), model_id=model_id, grain=[])
    db = _mock_db([], [], [], [], [], [])
    reason = _run(validate_aggregate(agg, db))
    assert reason is not None


# ---------------------------------------------------------------------------
# validate_dimension / validate_measure — pure function tests
# ---------------------------------------------------------------------------

def _structure(fact, dims, columns, reachable_ids):
    tables = {fact.id: fact}
    for d in dims:
        tables[d.id] = d
    return _ModelStructure(
        dim_by_name={},
        tables=tables,
        columns={c.id: c for c in columns},
        joins=[],
        measure_ids=set(),
        measures_by_name={},
        anchor_id=fact.id,
        reachable_from_anchor=frozenset(reachable_ids),
    )


def test_validate_dimension_valid_on_reachable_fact_column():
    fact = _FakeTable(uuid4(), "demo.fact", "fact")
    col = _FakeColumn(uuid4(), fact.id, "country")
    structure = _structure(fact, [], [col], [fact.id])
    dim = _FakeDim(uuid4(), "country", source_column_id=col.id)
    assert validate_dimension(dim, structure) is None


def test_validate_dimension_missing_source_column():
    fact = _FakeTable(uuid4(), "demo.fact", "fact")
    structure = _structure(fact, [], [], [fact.id])
    dim = _FakeDim(uuid4(), "ghost", source_column_id=uuid4())
    reason = validate_dimension(dim, structure)
    assert reason is not None
    assert "no longer exists" in reason


def test_validate_dimension_unreachable_source_table():
    fact = _FakeTable(uuid4(), "demo.fact", "fact")
    orphan = _FakeTable(uuid4(), "demo.dim_orphan", "dim_aggregate")
    col = _FakeColumn(uuid4(), orphan.id, "name")
    # orphan is in the model but not in reachable set
    structure = _structure(fact, [orphan], [col], [fact.id])
    dim = _FakeDim(uuid4(), "orphan_name", source_column_id=col.id)
    reason = validate_dimension(dim, structure)
    assert reason is not None
    assert "reachable" in reason.lower()


def test_validate_dimension_uda_no_source_column_is_valid():
    fact = _FakeTable(uuid4(), "demo.fact", "fact")
    structure = _structure(fact, [], [], [fact.id])
    dim = _FakeDim(
        uuid4(),
        "computed",
        source_column_id=None,
        user_defined_attribute_id=uuid4(),
    )
    assert validate_dimension(dim, structure) is None


def test_validate_dimension_no_binding_is_valid_placeholder():
    """A dimension with neither source_column_id nor UDA is a pure
    semantic label — valid but unqueryable. Don't mark it invalid."""
    fact = _FakeTable(uuid4(), "demo.fact", "fact")
    structure = _structure(fact, [], [], [fact.id])
    dim = _FakeDim(uuid4(), "placeholder")
    assert validate_dimension(dim, structure) is None


def test_validate_measure_valid_on_fact_column():
    fact = _FakeTable(uuid4(), "demo.fact", "fact")
    col = _FakeColumn(uuid4(), fact.id, "amount")
    structure = _structure(fact, [], [col], [fact.id])
    m = _FakeMeasure(uuid4(), "amount", source_column_id=col.id)
    assert validate_measure(m, structure) is None


def test_validate_measure_missing_source_column():
    fact = _FakeTable(uuid4(), "demo.fact", "fact")
    structure = _structure(fact, [], [], [fact.id])
    m = _FakeMeasure(uuid4(), "ghost", source_column_id=uuid4())
    reason = validate_measure(m, structure)
    assert reason is not None


def test_validate_measure_unreachable_table():
    fact = _FakeTable(uuid4(), "demo.fact", "fact")
    orphan = _FakeTable(uuid4(), "demo.dim_orphan", "dim_aggregate")
    col = _FakeColumn(uuid4(), orphan.id, "amount")
    structure = _structure(fact, [orphan], [col], [fact.id])
    m = _FakeMeasure(uuid4(), "orphan_amount", source_column_id=col.id)
    reason = validate_measure(m, structure)
    assert reason is not None
    assert "reachable" in reason.lower()


def test_validate_measure_expression_only_is_valid():
    fact = _FakeTable(uuid4(), "demo.fact", "fact")
    base_a = _FakeMeasure(uuid4(), "a", source_column_id=uuid4())
    base_b = _FakeMeasure(uuid4(), "b", source_column_id=uuid4())
    structure = _structure(fact, [], [], [fact.id])
    # Populate measures_by_name so the expression references resolve
    structure.measures_by_name["a"] = base_a
    structure.measures_by_name["b"] = base_b
    m = _FakeMeasure(uuid4(), "computed",
                     expression='measure("a") - measure("b")',
                     measure_type="calculated")
    assert validate_measure(m, structure) is None


# ---------------------------------------------------------------------------
# Bug-7903 (Fable R2 #2): structural revalidation must NOT resurrect a
# REFRESH-owned invalid aggregate to active.
# ---------------------------------------------------------------------------

def test_revalidate_does_not_resurrect_refresh_owned_invalid():
    """A structurally-VALID aggregate whose invalid state is REFRESH-owned (its
    durable refresh_prior_status is set by the refresh pending-guard) must stay
    invalid — flipping it to active would serve incomplete/understated data over a
    physical table the failed refresh left in doubt. Only a purely STRUCTURAL
    invalid self-heals to active."""
    from unittest.mock import patch
    from shared.semantic import model_validator as mv

    mid = uuid4()

    # Two invalid aggregates that both pass the structural check now:
    #  - refresh-owned (durable prior set) -> must STAY invalid
    #  - purely structural (no durable prior) -> self-heals to active
    refresh_owned = _FakeAgg(uuid4(), mid, grain=[], status="invalid")
    refresh_owned.refresh_prior_status = "active"
    structural = _FakeAgg(uuid4(), mid, grain=[], status="invalid")
    structural.refresh_prior_status = None

    async def _body():
        db = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [refresh_owned, structural]
        db.execute = AsyncMock(return_value=result)
        # validate_aggregate passes (structurally valid) for both.
        with patch.object(mv, "validate_aggregate", new=AsyncMock(return_value=None)):
            currently_invalid, newly_valid = await mv._revalidate_aggregates_with_structure(
                mid, db, structure=None,
            )
        return currently_invalid, newly_valid

    currently_invalid, newly_valid = asyncio.run(_body())

    # Refresh-owned invalid was NOT resurrected.
    assert refresh_owned.status == "invalid"
    assert refresh_owned.id not in newly_valid
    # Purely-structural invalid self-healed to active.
    assert structural.status == "active"
    assert structural.id in newly_valid


def test_revalidate_reactivates_disabled_refresh_owned_only_via_refresh():
    """A refresh-owned invalid whose durable prior is "disabled" must also stay
    invalid here (the refresh engine restores it to disabled, not the validator)."""
    from unittest.mock import patch
    from shared.semantic import model_validator as mv

    mid = uuid4()
    agg = _FakeAgg(uuid4(), mid, grain=[], status="invalid")
    agg.refresh_prior_status = "disabled"

    async def _body():
        db = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [agg]
        db.execute = AsyncMock(return_value=result)
        with patch.object(mv, "validate_aggregate", new=AsyncMock(return_value=None)):
            return await mv._revalidate_aggregates_with_structure(mid, db, structure=None)

    _ci, newly_valid = asyncio.run(_body())
    assert agg.status == "invalid"
    assert agg.id not in newly_valid
