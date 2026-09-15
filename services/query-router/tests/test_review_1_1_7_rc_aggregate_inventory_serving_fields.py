"""F-RC-01 serving-field coverage for the frozen aggregate inventory.

The process-global inventory is consumed by the aggregate matcher, aggregate
rewriter, derived-grain adapter, and router generation/dialect guards. This
guard proves the session-free projection carries every field those readers
require, including the active refresh generation.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from shared.db.models import DataTarget, ProjectConnection
from src.routing.aggregate_generation_guard import (
    AggregateGenerationChangedError,
    aggregate_generation_of,
    assert_admitted_generation,
)
from src.routing.router import (
    _resolve_aggregate_source_dialect,
    _resolve_aggregate_target_dialect,
)
from src.semantic.binder import _freeze_aggregate_definition


def _aggregate_row() -> SimpleNamespace:
    return SimpleNamespace(
        id="aggregate-1",
        model_id="model-1",
        target_id="target-1",
        physical_table_name="agg_sales",
        target_schema="analytics",
        status="active",
        grain=["region"],
        grain_physical_cols=None,
        grain_keys=[
            {
                "key_id": "dim-region",
                "kind": "PHYSICAL_COLUMN",
                "physical_column": "region",
            }
        ],
        attribute_edges=[
            {"attribute_key": "attr-country", "relationship_id": "rel-1"}
        ],
        passenger_columns=[
            {"attribute_key": "attr-country", "physical_column": "country_name"}
        ],
        active_refresh_run_id="refresh-run-1",
        built_for_version_id=None,
        built_for_epoch=None,
        is_stale=False,
        invalid_reason=None,
        last_refreshed_at=None,
        persona_id=None,
        columns=[],
        refresh_policy=None,
    )


async def test_f_rc_01_projection_preserves_router_serving_identifiers():
    """The projection preserves every serving reader's required field."""
    aggregate = _freeze_aggregate_definition(_aggregate_row())
    assert aggregate.target_id == "target-1"
    assert aggregate.model_id == "model-1"
    assert aggregate.active_refresh_run_id == "refresh-run-1"
    assert aggregate.grain_keys[0]["physical_column"] == "region"
    assert aggregate.attribute_edges[0]["relationship_id"] == "rel-1"
    assert aggregate.passenger_columns[0]["physical_column"] == "country_name"

    target = SimpleNamespace(project_connection_id="connection-1")
    connection = SimpleNamespace(connection_type="bigquery")
    db = AsyncMock()

    async def get(model, key):
        if model is DataTarget:
            assert key == "target-1"
            return target
        if model is ProjectConnection:
            assert key == "connection-1"
            return connection
        raise AssertionError(f"unexpected lookup: {model!r}, {key!r}")

    db.get.side_effect = get
    with patch(
        "shared.aggregate_connection.resolve_source_connection",
        new=AsyncMock(
            return_value=SimpleNamespace(connection_type="postgresql"),
        ),
    ) as resolve_source:
        target_dialect = await _resolve_aggregate_target_dialect(aggregate, db)
        source_dialect = await _resolve_aggregate_source_dialect(aggregate, db)

    assert target_dialect == "bigquery"
    assert source_dialect == "postgres"
    resolve_source.assert_awaited_once_with("model-1", db)


def test_f_rc_01_cached_projection_revalidates_active_refresh_generation():
    """The cached projection supplies the same generation guard as live ORM data."""
    cached = _freeze_aggregate_definition(_aggregate_row())
    admitted = aggregate_generation_of(cached)
    assert admitted.active_refresh_run_id == "refresh-run-1"

    assert_admitted_generation(
        admitted,
        admitted,
        kind="aggregate",
        artifact_id=cached.id,
        error_cls=AggregateGenerationChangedError,
    )

    refreshed = SimpleNamespace(
        status=cached.status,
        active_refresh_run_id="refresh-run-2",
        physical_table_name=cached.physical_table_name,
        target_schema=cached.target_schema,
        target_id=cached.target_id,
    )
    with pytest.raises(AggregateGenerationChangedError):
        assert_admitted_generation(
            aggregate_generation_of(refreshed),
            admitted,
            kind="aggregate",
            artifact_id=cached.id,
            error_cls=AggregateGenerationChangedError,
        )
