"""Tests for query-router failure-spike alert dispatch.

Bug-7331: dispatch_alert is no longer called inline on the request path.
Instead, _check_failure_spike spawns a background task via _spawn_background
that opens its own DB session. Tests must await the background task before
asserting on dispatch_alert.
"""
import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api import routes


@pytest.fixture(autouse=True)
def reset_failure_spike_state():
    routes._failure_timestamps.clear()
    routes._last_spike_alert.clear()
    yield
    routes._failure_timestamps.clear()
    routes._last_spike_alert.clear()


async def _drain_background_tasks():
    """Wait for all background tasks spawned by _spawn_background to complete."""
    if routes._background_tasks:
        await asyncio.gather(*routes._background_tasks, return_exceptions=True)


def _mock_get_tenant_db():
    """Create a mock for get_tenant_db that yields an AsyncMock db session."""
    mock_db = AsyncMock()

    async def fake_get_tenant_db(tenant_id):
        yield mock_db

    return fake_get_tenant_db, mock_db


@pytest.mark.asyncio
async def test_failure_spike_preserves_tenant_window_and_dispatches_project_scopes():
    db = AsyncMock()
    project_a = uuid.uuid4()
    project_b = uuid.uuid4()

    fake_get_db, bg_db = _mock_get_tenant_db()

    with patch(
        "shared.alerting.dispatcher.dispatch_alert",
        new_callable=AsyncMock,
    ) as dispatch, patch(
        "src.api.routes.get_tenant_db",
        side_effect=fake_get_db,
    ):
        for _ in range(routes._FAILURE_SPIKE_THRESHOLD - 1):
            await routes._check_failure_spike(
                db,
                "tenant-1",
                project_id=project_a,
            )
        await routes._check_failure_spike(
            db,
            "tenant-1",
            project_id=project_b,
        )
        await _drain_background_tasks()

    assert dispatch.await_count == 3
    assert {call.kwargs["project_id"] for call in dispatch.await_args_list} == {
        None,
        project_a,
        project_b,
    }
    assert all(
        call.kwargs["event_type"] == "query_failure_spike"
        for call in dispatch.await_args_list
    )


@pytest.mark.asyncio
async def test_log_query_failure_dispatches_spike_with_failed_model_project():
    db = AsyncMock()
    project_id = uuid.uuid4()
    bound = SimpleNamespace(
        model=SimpleNamespace(
            id=uuid.uuid4(),
            project_id=project_id,
            project=SimpleNamespace(display_name="Project"),
            display_name="Model",
        ),
        logical_query=SimpleNamespace(query_fingerprint="abc123", protocol="jdbc"),
    )
    decision = SimpleNamespace(route_type="source")

    fake_get_db, bg_db = _mock_get_tenant_db()

    with patch("src.api.routes.log_query_failure", new_callable=AsyncMock), patch(
        "shared.alerting.dispatcher.dispatch_alert",
        new_callable=AsyncMock,
    ) as dispatch, patch(
        "src.api.routes.get_tenant_db",
        side_effect=fake_get_db,
    ):
        for _ in range(routes._FAILURE_SPIKE_THRESHOLD):
            await routes._log_query_failure(
                db,
                "analyst@example.com",
                "tenant-1",
                bound,
                decision,
                routes.time.monotonic(),
                "execution_error",
                "boom",
            )
        await _drain_background_tasks()

    assert any(
        call.kwargs["event_type"] == "query_failure_spike"
        and call.kwargs["project_id"] == project_id
        for call in dispatch.await_args_list
    )


@pytest.mark.asyncio
async def test_failure_spike_without_project_keeps_tenant_global_dispatch():
    db = AsyncMock()

    fake_get_db, bg_db = _mock_get_tenant_db()

    with patch(
        "shared.alerting.dispatcher.dispatch_alert",
        new_callable=AsyncMock,
    ) as dispatch, patch(
        "src.api.routes.get_tenant_db",
        side_effect=fake_get_db,
    ):
        for _ in range(routes._FAILURE_SPIKE_THRESHOLD):
            await routes._check_failure_spike(db, "tenant-1")
        await _drain_background_tasks()

    dispatch.assert_awaited_once()
    assert dispatch.await_args.kwargs["event_type"] == "query_failure_spike"
    assert dispatch.await_args.kwargs["project_id"] is None


@pytest.mark.asyncio
async def test_first_spike_dispatches_on_low_monotonic_clock():
    """Regression: Bug-5429.

    With the old ``defaultdict(float)`` sentinel (0.0), a never-alerted tenant
    would have ``now - 0.0 < 300`` evaluate True whenever
    ``time.monotonic() < 300`` (e.g. freshly-booted container / CI runner),
    silently suppressing the FIRST failure-spike alert.

    The fix uses ``defaultdict(lambda: float("-inf"))`` so the dedup guard
    always sees the never-alerted case as "long enough ago".
    """
    db = AsyncMock()
    low_clock = 10.0  # monotonic value well below _FAILURE_SPIKE_WINDOW (300)

    # Prove the old sentinel (0.0) would have suppressed the alert:
    assert low_clock - 0.0 < routes._FAILURE_SPIKE_WINDOW, (
        "precondition: old sentinel 0.0 would suppress at low monotonic"
    )
    # Prove the new sentinel (-inf) does not suppress:
    assert low_clock - float("-inf") >= routes._FAILURE_SPIKE_WINDOW, (
        "precondition: new sentinel -inf never suppresses"
    )

    fake_clock = [low_clock]

    def mock_monotonic():
        return fake_clock[0]

    fake_get_db, bg_db = _mock_get_tenant_db()

    with patch.object(routes.time, "monotonic", side_effect=mock_monotonic), patch(
        "shared.alerting.dispatcher.dispatch_alert",
        new_callable=AsyncMock,
    ) as dispatch, patch(
        "src.api.routes.get_tenant_db",
        side_effect=fake_get_db,
    ):
        for _ in range(routes._FAILURE_SPIKE_THRESHOLD):
            await routes._check_failure_spike(db, "fresh-tenant")
        await _drain_background_tasks()

    assert dispatch.await_count >= 1, (
        "first spike alert for a never-alerted tenant must dispatch even when "
        "time.monotonic() is below _FAILURE_SPIKE_WINDOW"
    )


@pytest.mark.asyncio
async def test_check_failure_spike_returns_immediately_without_awaiting_dispatch():
    """Bug-7331: the error response must return immediately; the dispatch
    runs as a background task that never blocks the request path."""
    db = AsyncMock()

    # Create a dispatch_alert that would block for a long time if awaited
    # inline. The test asserts that _check_failure_spike returns before the
    # background task finishes.
    dispatch_started = asyncio.Event()
    dispatch_gate = asyncio.Event()

    async def slow_dispatch(*a, **kw):
        dispatch_started.set()
        await dispatch_gate.wait()

    fake_get_db, bg_db = _mock_get_tenant_db()

    with patch(
        "shared.alerting.dispatcher.dispatch_alert",
        side_effect=slow_dispatch,
    ), patch(
        "src.api.routes.get_tenant_db",
        side_effect=fake_get_db,
    ):
        for _ in range(routes._FAILURE_SPIKE_THRESHOLD):
            await routes._check_failure_spike(db, "tenant-1")

        # _check_failure_spike returned already; the background task is
        # running but blocked on dispatch_gate. Give a tiny window for the
        # task to start, then verify it hasn't completed.
        await asyncio.sleep(0.01)

        # Unblock the background dispatch so the test can clean up.
        dispatch_gate.set()
        await _drain_background_tasks()
