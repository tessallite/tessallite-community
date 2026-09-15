"""Bug-9054: the shared metadata readiness probe is bounded and read-only."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import shared.service_readiness as readiness


class _Session:
    def __init__(self):
        self.invalidated = False
        self.exited = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.exited = True
        return False

    async def execute(self, _statement):
        return None

    async def invalidate(self):
        self.invalidated = True


class _HangingSession(_Session):
    async def execute(self, _statement):
        await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_probe_metadata_database_returns_ok_for_a_successful_select(monkeypatch):
    monkeypatch.setattr("shared.db.session.SystemSessionLocal", lambda: _Session())
    monkeypatch.setattr(
        readiness,
        "get_settings",
        lambda: SimpleNamespace(READINESS_PROBE_TIMEOUT_SECONDS=0.2),
    )

    assert await readiness.probe_metadata_database() == (True, "ok")


@pytest.mark.asyncio
async def test_probe_metadata_database_fails_closed_on_timeout(monkeypatch):
    hanging_session = _HangingSession()
    monkeypatch.setattr("shared.db.session.SystemSessionLocal", lambda: hanging_session)
    monkeypatch.setattr(
        readiness,
        "get_settings",
        lambda: SimpleNamespace(READINESS_PROBE_TIMEOUT_SECONDS=0.001),
    )

    ready, detail = await readiness.probe_metadata_database()

    assert ready is False
    assert detail == "metadata database probe timed out"
    assert hanging_session.invalidated is True
    assert hanging_session.exited is True


@pytest.mark.asyncio
async def test_probe_recovers_after_a_timed_out_connection_is_discarded(monkeypatch):
    sessions = iter((_HangingSession(), _Session()))
    monkeypatch.setattr("shared.db.session.SystemSessionLocal", lambda: next(sessions))
    monkeypatch.setattr(
        readiness,
        "get_settings",
        lambda: SimpleNamespace(READINESS_PROBE_TIMEOUT_SECONDS=0.001),
    )

    assert await readiness.probe_metadata_database() == (
        False,
        "metadata database probe timed out",
    )
    assert await readiness.probe_metadata_database() == (True, "ok")
