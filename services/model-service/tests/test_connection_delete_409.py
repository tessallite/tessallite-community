"""Test connection DELETE returns 409 when models depend on it."""
import pytest
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock

from src.api.connections import _get_connection_dependents


@pytest.mark.asyncio
async def test_no_dependents_returns_empty_list():
    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=execute_result)

    result = await _get_connection_dependents(db, uuid4())
    assert result == []


@pytest.mark.asyncio
async def test_datasource_dependent_returns_model_name():
    db = AsyncMock()
    source_result = MagicMock()
    source_result.scalars.return_value.all.return_value = [uuid4()]
    target_result = MagicMock()
    target_result.scalars.return_value.all.return_value = []
    name_result = MagicMock()
    name_result.scalars.return_value.all.return_value = ["Sales Model"]
    db.execute = AsyncMock(side_effect=[source_result, target_result, name_result])

    result = await _get_connection_dependents(db, uuid4())
    assert result == ["Sales Model"]


@pytest.mark.asyncio
async def test_datatarget_dependent_included():
    db = AsyncMock()
    source_result = MagicMock()
    source_result.scalars.return_value.all.return_value = []
    target_result = MagicMock()
    mid = uuid4()
    target_result.scalars.return_value.all.return_value = [mid]
    name_result = MagicMock()
    name_result.scalars.return_value.all.return_value = ["Finance Model"]
    db.execute = AsyncMock(side_effect=[source_result, target_result, name_result])

    result = await _get_connection_dependents(db, uuid4())
    assert result == ["Finance Model"]
