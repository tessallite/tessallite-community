"""Tests for model-scoped schema change API (Block B)."""
import pytest
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_list_schema_changes_returns_events():
    from src.api.schema_changes import _list_schema_changes

    db = AsyncMock()
    event = MagicMock()
    event.id = uuid4()
    event.model_id = uuid4()
    event.table_name = "orders"
    event.change_type = "column_removed"
    event.is_breaking = True
    event.detail = {"column": "old_col"}
    event.detected_at = None
    event.acknowledged_at = None
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [event]
    db.execute = AsyncMock(return_value=result_mock)
    events = await _list_schema_changes(db, model_id=event.model_id)
    assert len(events) == 1
    assert events[0].table_name == "orders"


@pytest.mark.asyncio
async def test_acknowledge_sets_timestamp():
    from src.api.schema_changes import _acknowledge_schema_change

    db = AsyncMock()
    event = MagicMock()
    event.id = uuid4()
    event.acknowledged_at = None
    db.get = AsyncMock(return_value=event)
    await _acknowledge_schema_change(db, event_id=event.id)
    assert event.acknowledged_at is not None


@pytest.mark.asyncio
async def test_acknowledge_missing_event_raises():
    from src.api.schema_changes import _acknowledge_schema_change
    from fastapi import HTTPException

    db = AsyncMock()
    db.get = AsyncMock(return_value=None)
    with pytest.raises(HTTPException) as exc_info:
        await _acknowledge_schema_change(db, event_id=uuid4())
    assert exc_info.value.status_code == 404
