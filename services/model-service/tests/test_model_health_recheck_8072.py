from __future__ import annotations

import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    make_mock_db,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_recheck_composes_recorded_hierarchy_pocket_and_drift_signals(client):
    """Bug-8072: Re-check must report all persisted health domains while
    stating truthfully that it did not introspect the live source."""
    report = types.SimpleNamespace(
        invalid_dimensions=[("d1", "missing column")],
        invalid_measures=[],
        invalid_aggregates=[("a1", "stale")],
        newly_valid_dimensions=[],
        newly_valid_measures=["m1"],
        newly_valid_aggregates=[],
    )
    latest_drift = datetime(2026, 8, 1, 12, 30, tzinfo=timezone.utc)

    recorded = MagicMock()
    recorded.one.return_value = (2, 1, 3, latest_drift)
    no_rows = MagicMock()
    no_rows.scalars.return_value.all.return_value = []

    db = make_mock_db()
    db.add = MagicMock()
    db.execute = AsyncMock(side_effect=[recorded, no_rows, no_rows])

    with (
        patch("src.api.alerts.get_tenant_db", async_gen_from(db)),
        patch("src.api.alerts.ensure_model_in_project", new=AsyncMock()),
        patch("src.api.alerts.revalidate_model", new=AsyncMock(return_value=report)),
        # Bug-8484 (L17) added queue_model_source_check as a new collaborator of
        # revalidate_model_endpoint. It runs a live source probe (its own
        # db.execute calls) that this Bug-8072 recorded-signals test must not
        # exercise against the positional db.execute mock — leaving it unpatched
        # consumed the side_effect entries and misaligned the recorded 4-tuple
        # unpack. Patch it like the other collaborators; False matches the
        # asserted live_source_checked outcome.
        patch("src.api.alerts.queue_model_source_check", new=AsyncMock(return_value=False)),
        # Bug-8147 (L17) also added an audit() call to this endpoint, which
        # itself issues a db.execute — another consumer of the positional mock.
        # It is an audit-trail side effect, not this test's subject, so patch it.
        patch("src.api.alerts.audit", new=AsyncMock()),
    ):
        response = await client.post(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/revalidate"
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["invalid_dimension_count"] == 1
    assert body["invalid_aggregate_count"] == 1
    assert body["newly_valid_measure_count"] == 1
    assert body["unresolved_hierarchy_issue_count"] == 2
    assert body["failed_pocket_count"] == 1
    assert body["unacknowledged_schema_drift_count"] == 3
    assert body["latest_recorded_schema_drift_at"] == "2026-08-01T12:30:00Z"
    assert body["live_source_checked"] is False


@pytest.mark.asyncio
async def test_recheck_returns_zeroes_when_no_recorded_signals_exist(client):
    report = types.SimpleNamespace(
        invalid_dimensions=[], invalid_measures=[], invalid_aggregates=[],
        newly_valid_dimensions=[], newly_valid_measures=[],
        newly_valid_aggregates=[],
    )
    recorded = MagicMock()
    recorded.one.return_value = (0, 0, 0, None)
    no_rows = MagicMock()
    no_rows.scalars.return_value.all.return_value = []

    db = make_mock_db()
    db.add = MagicMock()
    db.execute = AsyncMock(side_effect=[recorded, no_rows, no_rows])

    with (
        patch("src.api.alerts.get_tenant_db", async_gen_from(db)),
        patch("src.api.alerts.ensure_model_in_project", new=AsyncMock()),
        patch("src.api.alerts.revalidate_model", new=AsyncMock(return_value=report)),
        # Bug-8484 (L17) added queue_model_source_check as a new collaborator of
        # revalidate_model_endpoint. It runs a live source probe (its own
        # db.execute calls) that this Bug-8072 recorded-signals test must not
        # exercise against the positional db.execute mock — leaving it unpatched
        # consumed the side_effect entries and misaligned the recorded 4-tuple
        # unpack. Patch it like the other collaborators; False matches the
        # asserted live_source_checked outcome.
        patch("src.api.alerts.queue_model_source_check", new=AsyncMock(return_value=False)),
        # Bug-8147 (L17) also added an audit() call to this endpoint, which
        # itself issues a db.execute — another consumer of the positional mock.
        # It is an audit-trail side effect, not this test's subject, so patch it.
        patch("src.api.alerts.audit", new=AsyncMock()),
    ):
        response = await client.post(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/revalidate"
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["unresolved_hierarchy_issue_count"] == 0
    assert body["failed_pocket_count"] == 0
    assert body["unacknowledged_schema_drift_count"] == 0
    assert body["latest_recorded_schema_drift_at"] is None


@pytest.mark.asyncio
async def test_recheck_resolves_measure_source_column_without_crash_f030_04(client):
    """F-030-04: Re-check used to read m.source_table_id / m.source_column_name,
    which do not exist on Measure — a 500 on any real model with measures. It now
    resolves the fact table + physical column name through source_column_id ->
    ModelColumn, so the dual-signal validation runs instead of crashing."""
    report = types.SimpleNamespace(
        invalid_dimensions=[], invalid_measures=[], invalid_aggregates=[],
        newly_valid_dimensions=[], newly_valid_measures=[], newly_valid_aggregates=[],
    )
    recorded = MagicMock()
    recorded.one.return_value = (0, 0, 0, None)

    table_id = "11111111-1111-1111-1111-111111111111"
    col_id = "22222222-2222-2222-2222-222222222222"

    fact_table = types.SimpleNamespace(id=table_id, columns=[])
    tables_res = MagicMock()
    tables_res.scalars.return_value.all.return_value = [fact_table]

    measure = types.SimpleNamespace(
        source_column_id=col_id, name="revenue",
    )
    measures_res = MagicMock()
    measures_res.scalars.return_value.all.return_value = [measure]

    model_col = types.SimpleNamespace(
        id=col_id, model_table_id=table_id, column_name="amount",
    )
    cols_res = MagicMock()
    cols_res.scalars.return_value.all.return_value = [model_col]

    db = make_mock_db()
    db.add = MagicMock()
    # order: recorded 4-tuple, fact tables, measures, ModelColumn lookup.
    db.execute = AsyncMock(side_effect=[recorded, tables_res, measures_res, cols_res])

    with (
        patch("src.api.alerts.get_tenant_db", async_gen_from(db)),
        patch("src.api.alerts.ensure_model_in_project", new=AsyncMock()),
        patch("src.api.alerts.revalidate_model", new=AsyncMock(return_value=report)),
        patch("src.api.alerts.queue_model_source_check", new=AsyncMock(return_value=False)),
        patch("src.api.alerts.audit", new=AsyncMock()),
        # validate_measures is the semantic analyzer; stub it so this test asserts
        # only the resolution wiring (fact table + column name) reached it.
        patch(
            "shared.semantic.table_analyzer.validate_measures",
            new=MagicMock(return_value=[]),
        ) as vm,
    ):
        response = await client.post(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/revalidate"
        )

    assert response.status_code == 200, response.text
    # The measure resolved to its fact table's column and reached validate_measures
    # with the physical column name — no AttributeError, no 500.
    vm.assert_called_once()
    _table_arg, col_names = vm.call_args.args
    assert col_names == ["amount"]
