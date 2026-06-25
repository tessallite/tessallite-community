"""Auto-create the implicit DrillThroughSet row on measure creation.

Covers:
  * Standard measure (with a source column) → drill-through row added,
    ``source_table_id`` resolved from the source column's ``model_table_id``.
  * Variant measure (with a source column) → drill-through row added.
  * Calculated measure → no drill-through row added (no single source fact).
  * Measure with neither source column nor UDA → drill-through row added
    with ``source_table_id=None`` (implicit default, resolved at query time).
  * UDA-backed measure → ``source_table_id`` resolved from UDA.table_id.

Phase 4C.1 of the drill-through + calculated-members plan.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.db.models import DrillThroughSet
from src.api.measures import _ensure_drill_through_set

pytestmark = pytest.mark.unit


def _measure(
    *,
    measure_type: str = "standard",
    source_column_id: uuid.UUID | None = None,
    user_defined_attribute_id: uuid.UUID | None = None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        measure_type=measure_type,
        source_column_id=source_column_id,
        user_defined_attribute_id=user_defined_attribute_id,
    )


def _db_with_lookup(*, column=None, uda=None) -> AsyncMock:
    db = AsyncMock()
    db.add = MagicMock()

    async def _get(cls, key):
        name = cls.__name__
        if name == "ModelColumn":
            return column
        if name == "UserDefinedAttribute":
            return uda
        return None

    db.get = AsyncMock(side_effect=_get)
    return db


def _added_drill_sets(db: AsyncMock) -> list[DrillThroughSet]:
    return [
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], DrillThroughSet)
    ]


@pytest.mark.asyncio
async def test_standard_measure_autocreates_row_with_source_table():
    table_id = uuid.uuid4()
    col_id = uuid.uuid4()
    column = types.SimpleNamespace(id=col_id, model_table_id=table_id)
    m = _measure(source_column_id=col_id)
    db = _db_with_lookup(column=column)

    await _ensure_drill_through_set(db, m)

    rows = _added_drill_sets(db)
    assert len(rows) == 1
    assert rows[0].measure_id == m.id
    assert rows[0].source_table_id == table_id


@pytest.mark.asyncio
async def test_variant_measure_autocreates_row():
    table_id = uuid.uuid4()
    col_id = uuid.uuid4()
    column = types.SimpleNamespace(id=col_id, model_table_id=table_id)
    m = _measure(source_column_id=col_id)
    m.measure_type = "standard"  # variants carry measure_type="standard"
    db = _db_with_lookup(column=column)

    await _ensure_drill_through_set(db, m)

    assert len(_added_drill_sets(db)) == 1


@pytest.mark.asyncio
async def test_calculated_measure_does_not_autocreate_row():
    m = _measure(measure_type="calculated")
    db = _db_with_lookup()

    await _ensure_drill_through_set(db, m)

    assert _added_drill_sets(db) == []


@pytest.mark.asyncio
async def test_unresolved_source_falls_back_to_null_source_table():
    m = _measure()  # no source column, no UDA
    db = _db_with_lookup()

    await _ensure_drill_through_set(db, m)

    rows = _added_drill_sets(db)
    assert len(rows) == 1
    assert rows[0].source_table_id is None


@pytest.mark.asyncio
async def test_uda_backed_measure_resolves_source_table_from_uda():
    table_id = uuid.uuid4()
    uda_id = uuid.uuid4()
    uda = types.SimpleNamespace(id=uda_id, table_id=table_id)
    m = _measure(user_defined_attribute_id=uda_id)
    db = _db_with_lookup(uda=uda)

    await _ensure_drill_through_set(db, m)

    rows = _added_drill_sets(db)
    assert len(rows) == 1
    assert rows[0].source_table_id == table_id
