"""Test connection DELETE returns 409 when models depend on it.

Bug-7161: _get_connection_dependents now uses a single UNION ALL query
to check DataSource and DataTarget references atomically, so the test
mocks reflect 2 db.execute calls (combined check, then Model names)
instead of the previous 3 (DataSource, DataTarget, Model names).
"""
import pytest
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock

from src.api.connections import _get_connection_dependents


@pytest.mark.asyncio
async def test_no_dependents_returns_empty_list():
    db = AsyncMock()
    # Bug-7161: single combined query returns no model_ids.
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=execute_result)

    result = await _get_connection_dependents(db, uuid4())
    assert result == []


@pytest.mark.asyncio
async def test_datasource_dependent_returns_model_name():
    db = AsyncMock()
    mid = uuid4()
    # Bug-7161: first call is the combined UNION ALL query returning model_ids.
    combined_result = MagicMock()
    combined_result.scalars.return_value.all.return_value = [mid]
    # Second call fetches model display names.
    name_result = MagicMock()
    name_result.scalars.return_value.all.return_value = ["Sales Model"]
    db.execute = AsyncMock(side_effect=[combined_result, name_result])

    result = await _get_connection_dependents(db, uuid4())
    assert result == ["Sales Model"]


@pytest.mark.asyncio
async def test_datatarget_dependent_included():
    db = AsyncMock()
    mid = uuid4()
    # Bug-7161: combined query picks up both DataSource and DataTarget refs.
    combined_result = MagicMock()
    combined_result.scalars.return_value.all.return_value = [mid]
    name_result = MagicMock()
    name_result.scalars.return_value.all.return_value = ["Finance Model"]
    db.execute = AsyncMock(side_effect=[combined_result, name_result])

    result = await _get_connection_dependents(db, uuid4())
    assert result == ["Finance Model"]
