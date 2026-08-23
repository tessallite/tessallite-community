"""``joins[].population_participation`` survives the snapshot round trip.

Bug-8615 phase G1. The flag is pinned model content: deploy, Save, revert,
export and import all go through ``serialiser._row_to_dict`` ->
``rehydrator._insert_joins``. If either end dropped it, a modeller's
deliberate ``population_defining`` declaration would silently revert to the
elidable default on the next revert or import — a wrong-numbers path the
moment phase G3 wires the flag into serving.

The three cases that matter are the two ends of the migration and the
tampered-bundle case:

* a snapshot saved AFTER this field exists carries and restores the value;
* a snapshot saved BEFORE it omits the key entirely, so the column's server
  default (``preserve_base_rows``) applies and the model rehydrates exactly as
  it always did;
* a value outside the vocabulary is coerced, not persisted and not raised on.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.db.models import Join
from shared.model_snapshot.rehydrator import _insert_joins
from shared.model_snapshot.serialiser import _row_to_dict

pytestmark = pytest.mark.unit


def _capture_db():
    db = AsyncMock()
    captured: list[dict] = []

    async def _execute(stmt):
        captured.append(dict(stmt.compile().params))
        return MagicMock()

    db.execute = AsyncMock(side_effect=_execute)
    db._captured = captured
    return db


def _snapshot_join(**overrides) -> dict:
    row = {
        "id": str(uuid.uuid4()),
        "model_id": str(uuid.uuid4()),
        "left_table_id": str(uuid.uuid4()),
        "right_table_id": str(uuid.uuid4()),
        "left_column_id": str(uuid.uuid4()),
        "right_column_id": str(uuid.uuid4()),
        "join_type": "inner",
    }
    row.update(overrides)
    return row


def test_the_serialiser_emits_the_field():
    """``_row_to_dict`` is derived from the ORM columns, so the guard here is
    that the COLUMN exists on the mapped table — the thing a future refactor
    could break."""
    join = Join(
        id=uuid.uuid4(), model_id=uuid.uuid4(),
        left_table_id=uuid.uuid4(), right_table_id=uuid.uuid4(),
        left_column_id=uuid.uuid4(), right_column_id=uuid.uuid4(),
        join_type="inner", population_participation="enrichment_only",
    )
    row = _row_to_dict(join, exclude=("created_at", "updated_at"))
    assert row["population_participation"] == "enrichment_only"


def test_g4_sol_r1_b01_serialiser_emits_participation_provenance():
    join = Join(
        id=uuid.uuid4(), model_id=uuid.uuid4(),
        left_table_id=uuid.uuid4(), right_table_id=uuid.uuid4(),
        left_column_id=uuid.uuid4(), right_column_id=uuid.uuid4(),
        join_type="left", population_participation="preserve_base_rows",
        population_participation_source="manual",
    )
    row = _row_to_dict(join, exclude=("created_at", "updated_at"))
    assert row["population_participation_source"] == "manual"


@pytest.mark.asyncio
async def test_a_declared_value_is_restored_verbatim():
    db = _capture_db()
    model_id = uuid.uuid4()
    await _insert_joins(
        model_id,
        {"joins": [_snapshot_join(population_participation="population_defining")]},
        db,
    )
    assert db._captured[0]["population_participation"] == "population_defining"


@pytest.mark.asyncio
async def test_g4_sol_r1_b01_manual_provenance_is_restored_verbatim():
    db = _capture_db()
    await _insert_joins(
        uuid.uuid4(),
        {"joins": [_snapshot_join(
            population_participation="preserve_base_rows",
            population_participation_source="manual",
        )]},
        db,
    )
    assert db._captured[0]["population_participation_source"] == "manual"


@pytest.mark.asyncio
async def test_a_pre_g1_snapshot_supplies_no_explicit_value():
    """The zero-behaviour-change path.

    ``_insert_joins`` must NOT invent a value for a snapshot that predates the
    field: supplying nothing is what lets the column's own default apply and
    reproduce the historical, elidable join. SQLAlchemy still NAMES the column
    in the compiled INSERT (that is how it delivers the Python-side default),
    so the observable here is that the bound value is not something this code
    chose — a mutation that injected ``undeclared`` or any other token for a
    missing key turns this red.

    The end result was measured against a real PostgreSQL 15 (throwaway schema,
    both insert shapes): key absent via ``insert(Join).values(**row)`` -> stored
    ``preserve_base_rows``; raw INSERT that never names the column -> stored
    ``preserve_base_rows``; zero NULL rows.
    """
    db = _capture_db()
    await _insert_joins(uuid.uuid4(), {"joins": [_snapshot_join()]}, db)
    assert db._captured[0].get("population_participation") is None


@pytest.mark.asyncio
async def test_an_out_of_enum_value_is_coerced_to_undeclared_not_persisted():
    """A hand-edited or tampered bundle bypasses the API's Literal validation.
    Coercing to ``undeclared`` surfaces it as a validator warning instead of
    letting it read as an affirmative declaration; both states are equally
    elidable, so no served number changes either way."""
    db = _capture_db()
    await _insert_joins(
        uuid.uuid4(),
        {"joins": [_snapshot_join(population_participation="anything_at_all")]},
        db,
    )
    assert db._captured[0]["population_participation"] == "undeclared"


@pytest.mark.asyncio
async def test_every_declared_value_survives_rehydrate():
    from shared.schemas.domains.aggregates_security import (
        POPULATION_PARTICIPATION_VALUES,
    )

    for value in POPULATION_PARTICIPATION_VALUES:
        db = _capture_db()
        await _insert_joins(
            uuid.uuid4(),
            {"joins": [_snapshot_join(population_participation=value)]},
            db,
        )
        assert db._captured[0]["population_participation"] == value
