"""F-SAL-04: storage observations expose a truthful freshness signal."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from shared.system_logs import metrics, storage

_aio = pytest.mark.asyncio


class _Result:
    def one(self):
        return (4096, 3, None)


@pytest.fixture(autouse=True)
def reset_measurement_timestamp():
    previous = metrics.LOG_STORAGE_MEASURED_AT._value.get()
    metrics.LOG_STORAGE_MEASURED_AT.set(0)
    yield
    metrics.LOG_STORAGE_MEASURED_AT.set(previous)


def test_storage_measurement_starts_unreported():
    assert metrics.LOG_STORAGE_MEASURED_AT._value.get() == 0


@_aio
async def test_failed_storage_measurement_does_not_advance_timestamp():
    metrics.LOG_STORAGE_MEASURED_AT.set(1234)
    db = AsyncMock()
    db.execute.side_effect = RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        await storage.refresh_storage_metrics(db)

    assert metrics.LOG_STORAGE_MEASURED_AT._value.get() == 1234


@_aio
async def test_successful_storage_measurement_sets_a_fresh_timestamp(monkeypatch):
    db = AsyncMock()
    db.execute.return_value = _Result()
    monkeypatch.setattr(storage.time, "time", lambda: 9876)

    await storage.refresh_storage_metrics(db)

    assert metrics.LOG_BYTES._value.get() == 4096
    assert metrics.LOG_ROWS._value.get() == 3
    assert metrics.LOG_STORAGE_MEASURED_AT._value.get() == 9876
