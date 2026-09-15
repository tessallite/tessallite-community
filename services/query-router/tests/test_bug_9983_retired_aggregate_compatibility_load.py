"""Bug-9983: retired aggregate history stays out of request-time compatibility."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from result_fakes import ScalarResult
from shared.db.models import AggregateColumn, AggregateDefinition, ModelVersion
from src.api.routes import _load_field_compatibility_metadata


class _ScalarRows:
    """Result fake whose ``scalars()`` hands back a separate scalar view.

    Returning the receiver here would let this test keep passing if the code
    under test used a Result-only accessor after the scalar projection, which
    fails against a real SQLAlchemy Result (Bug-9012).
    """

    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return ScalarResult(self._rows)


class _CompatibilityDB:
    def __init__(self, active_aggregate, active_column):
        self.active_aggregate = active_aggregate
        self.active_column = active_column
        self.statements = []

    async def get(self, model, _row_id):
        assert model is ModelVersion
        return SimpleNamespace(snapshot_json={"measures": [{"id": str(uuid4())}]})

    async def execute(self, statement):
        self.statements.append(statement)
        entity = statement.column_descriptions[0]["entity"]
        if entity is AggregateDefinition:
            return _ScalarRows([self.active_aggregate])
        assert entity is AggregateColumn
        return _ScalarRows([self.active_column])


@pytest.mark.asyncio
async def test_retired_aggregate_columns_are_filtered_before_orm_loading():
    model_id = uuid4()
    version_id = uuid4()
    active_id = uuid4()
    active = SimpleNamespace(id=active_id, status="active", grain=[])
    active_column = SimpleNamespace(
        aggregate_definition_id=active_id,
        measure_id=uuid4(),
    )
    db = _CompatibilityDB(active, active_column)

    result = await _load_field_compatibility_metadata(model_id, version_id, db)

    aggregate_statement, column_statement = db.statements
    aggregate_params = aggregate_statement.compile().params
    status_values = next(
        value
        for name, value in aggregate_params.items()
        if "status" in name
    )
    assert set(status_values) == {"active", "ready"}

    column_params = column_statement.compile().params
    selected_ids = next(
        value
        for name, value in column_params.items()
        if "aggregate_definition_id" in name
    )
    assert selected_ids == [active_id]
    assert result[6] == [active]
    assert result[7] == [active_column]
