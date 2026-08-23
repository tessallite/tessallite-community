"""Tests for schema drift auto-remediation and acknowledgement API (Block A)."""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from src.main import app
from src.auth.middleware import CurrentUser, get_current_user, require_tenant_admin
from .conftest import (
    TEST_TENANT,
    TEST_USER_ID,
    TEST_PROJECT_ID,
    TEST_MODEL_ID,
    NOW,
    async_gen_from,
    make_mock_db,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user() -> CurrentUser:
    return CurrentUser(user_id=TEST_USER_ID, tenant_id=TEST_TENANT, email=TEST_USER_ID)


def _make_event(
    event_id: uuid.UUID | None = None,
    model_id: uuid.UUID = TEST_MODEL_ID,
    change_type: str = "column_removed",
    is_breaking: bool = True,
    acknowledged_at=None,
    detail: dict | None = None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=event_id or uuid.uuid4(),
        model_id=model_id,
        source_id=uuid.uuid4(),
        table_name="public.sales",
        change_type=change_type,
        is_breaking=is_breaking,
        detail=detail or {"column_name": "amount"},
        detected_at=NOW,
        acknowledged_at=acknowledged_at,
    )


@pytest.fixture
def auth():
    user = _make_user()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_tenant_admin] = lambda: user
    yield user
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(require_tenant_admin, None)


@pytest.fixture
async def client(auth):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# GET /admin/schema-drift — list events
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_schema_drift_empty(client):
    """Empty result returns 200 with items=[] and total=0."""
    mock_db = make_mock_db()
    count_result = MagicMock()
    count_result.scalar_one.return_value = 0
    list_result = MagicMock()
    list_result.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(side_effect=[count_result, list_result])

    with patch("src.api.schema_drift.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.get("/api/v1/admin/schema-drift")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["items"] == []


@pytest.mark.asyncio
async def test_list_schema_drift_returns_events(client):
    """Returns events when they exist."""
    event_id = uuid.uuid4()
    event = _make_event(event_id=event_id, change_type="column_removed")
    mock_db = make_mock_db()
    count_result = MagicMock()
    count_result.scalar_one.return_value = 1
    list_result = MagicMock()
    list_result.scalars.return_value.all.return_value = [event]
    mock_db.execute = AsyncMock(side_effect=[count_result, list_result])

    with patch("src.api.schema_drift.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.get("/api/v1/admin/schema-drift")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["change_type"] == "column_removed"
    assert data["items"][0]["is_breaking"] is True


@pytest.mark.asyncio
async def test_list_schema_drift_filter_by_model(client):
    """model_id query param is forwarded to the DB query."""
    mock_db = make_mock_db()
    count_result = MagicMock()
    count_result.scalar_one.return_value = 0
    list_result = MagicMock()
    list_result.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(side_effect=[count_result, list_result])

    with patch("src.api.schema_drift.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.get(f"/api/v1/admin/schema-drift?model_id={TEST_MODEL_ID}")

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# PATCH /admin/schema-drift/{event_id}/acknowledge
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_acknowledge_sets_timestamp(client):
    """Acknowledge sets acknowledged_at and returns the updated event."""
    event_id = uuid.uuid4()
    event = _make_event(event_id=event_id)

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=event)
    # No ModelAlert to clear
    alert_result = MagicMock()
    alert_result.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(return_value=alert_result)

    with patch("src.api.schema_drift.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.patch(f"/api/v1/admin/schema-drift/{event_id}/acknowledge")

    assert resp.status_code == 200
    assert mock_db.commit.called
    assert event.acknowledged_at is not None


@pytest.mark.asyncio
async def test_acknowledge_already_acknowledged_is_idempotent(client):
    """Acknowledging an already-acknowledged event is a no-op."""
    event_id = uuid.uuid4()
    event = _make_event(event_id=event_id, acknowledged_at=NOW)

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=event)

    with patch("src.api.schema_drift.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.patch(f"/api/v1/admin/schema-drift/{event_id}/acknowledge")

    assert resp.status_code == 200
    assert not mock_db.commit.called  # no-op, already acknowledged


@pytest.mark.asyncio
async def test_acknowledge_event_not_found(client):
    """Returns 404 when event does not exist."""
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=None)

    with patch("src.api.schema_drift.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.patch(f"/api/v1/admin/schema-drift/{uuid.uuid4()}/acknowledge")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_acknowledge_clears_model_alert(client):
    """Acknowledge resolves any associated ModelAlert."""
    event_id = uuid.uuid4()
    event = _make_event(event_id=event_id)
    alert = types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        related_object_type="schema_change_event",
        related_object_id=event_id,
        resolved_at=None,
        dismissed_at=None,
    )

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=event)
    alert_result = MagicMock()
    alert_result.scalars.return_value.all.return_value = [alert]
    mock_db.execute = AsyncMock(return_value=alert_result)

    with patch("src.api.schema_drift.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.patch(f"/api/v1/admin/schema-drift/{event_id}/acknowledge")

    assert resp.status_code == 200
    assert alert.resolved_at is not None


# ---------------------------------------------------------------------------
# Remediation unit tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_remediation_column_added_creates_hidden_column():
    """column_added event auto-creates a hidden ModelColumn."""
    from shared.schema_drift.remediation import apply_remediation

    table_id = uuid.uuid4()
    event = types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        source_id=uuid.uuid4(),
        table_name="public.sales",
        change_type="column_added",
        is_breaking=False,
        detail={"column_name": "new_col", "data_type": "integer"},
    )

    mock_table = types.SimpleNamespace(id=table_id, model_id=TEST_MODEL_ID)

    db = AsyncMock()
    # First execute: find table — returns table
    table_result = MagicMock()
    table_result.scalar_one_or_none.return_value = mock_table
    # Second execute: find existing column — returns None (not already catalogued)
    col_result = MagicMock()
    col_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(side_effect=[table_result, col_result])
    db.add = MagicMock()

    await apply_remediation(TEST_MODEL_ID, [event], db)

    assert db.add.called
    added = db.add.call_args[0][0]
    assert added.column_name == "new_col"
    assert added.is_hidden is True
    assert added.drift_removed is False


@pytest.mark.asyncio
async def test_remediation_column_removed_marks_drift_removed():
    """column_removed event marks ModelColumn drift_removed and invalidates dims."""
    from shared.schema_drift.remediation import apply_remediation

    col_id = uuid.uuid4()
    event = types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        source_id=uuid.uuid4(),
        table_name="public.sales",
        change_type="column_removed",
        is_breaking=True,
        detail={"column_name": "amount", "old_data_type": "numeric"},
    )

    mock_table = types.SimpleNamespace(id=uuid.uuid4(), model_id=TEST_MODEL_ID)
    mock_col = types.SimpleNamespace(
        id=col_id, column_name="amount", drift_removed=False
    )
    mock_dim = types.SimpleNamespace(
        id=uuid.uuid4(), model_id=TEST_MODEL_ID, name="amount_dim",
        is_invalid=False, invalid_reason=None,
        source_column_id=col_id,
    )

    db = AsyncMock()
    table_result = MagicMock()
    table_result.scalar_one_or_none.return_value = mock_table
    col_result = MagicMock()
    col_result.scalar_one_or_none.return_value = mock_col
    dims_result = MagicMock()
    dims_result.scalars.return_value.all.return_value = [mock_dim]
    meas_result = MagicMock()
    meas_result.scalars.return_value.all.return_value = []
    # F-012-01 / Fable R1: apply_remediation now also resolves hierarchy-level
    # grain names, dependent aggregates (SELECT, empty here), and — on a
    # breaking event — marks the model's fresh pockets stale (UPDATE).
    hlevel_result = MagicMock()
    hlevel_result.fetchall.return_value = []
    agg_result = MagicMock()
    agg_result.scalars.return_value.all.return_value = []
    pocket_update_result = MagicMock()
    pocket_update_result.rowcount = 0
    db.execute = AsyncMock(side_effect=[
        table_result, col_result, dims_result, meas_result,
        hlevel_result, agg_result, pocket_update_result,
    ])
    db.add = MagicMock()

    await apply_remediation(TEST_MODEL_ID, [event], db)

    assert mock_col.drift_removed is True
    assert mock_dim.is_invalid is True
    assert "amount" in mock_dim.invalid_reason
    assert db.add.called  # ModelAlert added


@pytest.mark.asyncio
async def test_remediation_type_changed_updates_data_type():
    """type_changed event updates ModelColumn.data_type and creates alert."""
    from shared.schema_drift.remediation import apply_remediation

    col_id = uuid.uuid4()
    event = types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        source_id=uuid.uuid4(),
        table_name="public.sales",
        change_type="type_changed",
        is_breaking=True,
        detail={
            "column_name": "amount",
            "old_data_type": "numeric",
            "new_data_type": "text",
        },
    )

    mock_table = types.SimpleNamespace(id=uuid.uuid4(), model_id=TEST_MODEL_ID)
    mock_col = types.SimpleNamespace(
        id=col_id, column_name="amount", data_type="numeric"
    )
    # measure using this column with a numeric aggregation
    mock_meas = types.SimpleNamespace(
        id=uuid.uuid4(), model_id=TEST_MODEL_ID,
        source_column_id=col_id,
        default_agg="sum",
        is_invalid=False, invalid_reason=None,
    )

    db = AsyncMock()
    table_result = MagicMock()
    table_result.scalar_one_or_none.return_value = mock_table
    col_result = MagicMock()
    col_result.scalar_one_or_none.return_value = mock_col
    meas_result = MagicMock()
    meas_result.scalars.return_value.all.return_value = [mock_meas]
    # F-012-01: an incompatibly-retyped measure column is a breaking event, so
    # apply_remediation now resolves dependent aggregates (SELECT, empty here)
    # and marks the model's fresh pockets stale (UPDATE). Script those calls.
    agg_result = MagicMock()
    agg_result.scalars.return_value.all.return_value = []
    pocket_update_result = MagicMock()
    pocket_update_result.rowcount = 0
    db.execute = AsyncMock(side_effect=[
        table_result, col_result, meas_result,
        agg_result, pocket_update_result,
    ])
    db.add = MagicMock()

    await apply_remediation(TEST_MODEL_ID, [event], db)

    assert mock_col.data_type == "text"
    assert mock_meas.is_invalid is True
    assert "text" in mock_meas.invalid_reason
    alert = db.add.call_args[0][0]
    assert alert.category == "schema_drift"
    assert alert.severity == "error"
