from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.dimensions import _glossary_texts_for_targets as dimension_glossary_texts
from src.api.measures import _glossary_texts_for_targets as measure_glossary_texts

pytestmark = pytest.mark.unit


def _result(rows):
    result = MagicMock()
    result.all.return_value = rows
    return result


@pytest.mark.asyncio
async def test_dimension_glossary_texts_batches_and_keeps_latest_order():
    model_id = uuid.uuid4()
    target_a = uuid.uuid4()
    target_b = uuid.uuid4()
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=_result([
            (target_a, "latest country definition"),
            (target_a, "older country definition"),
            (target_b, "city definition"),
        ])
    )

    out = await dimension_glossary_texts(
        db, model_id, "dimension", [target_a, target_b]
    )

    assert out == {
        target_a: "latest country definition",
        target_b: "city definition",
    }
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_measure_glossary_texts_skips_empty_target_list():
    db = AsyncMock()

    out = await measure_glossary_texts(db, uuid.uuid4(), "measure", [])

    assert out == {}
    db.execute.assert_not_awaited()
