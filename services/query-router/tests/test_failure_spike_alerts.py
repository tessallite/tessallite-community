import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.api import routes


@pytest.fixture(autouse=True)
def reset_failure_spike_state():
    routes._failure_timestamps.clear()
    routes._last_spike_alert.clear()
    yield
    routes._failure_timestamps.clear()
    routes._last_spike_alert.clear()


@pytest.mark.asyncio
async def test_failure_spike_preserves_tenant_window_and_dispatches_project_scopes():
    db = AsyncMock()
    project_a = uuid.uuid4()
    project_b = uuid.uuid4()

    with patch(
        "shared.alerting.dispatcher.dispatch_alert",
        new_callable=AsyncMock,
    ) as dispatch:
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

    with patch("src.api.routes.log_query_failure", new_callable=AsyncMock), patch(
        "shared.alerting.dispatcher.dispatch_alert",
        new_callable=AsyncMock,
    ) as dispatch:
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

    assert any(
        call.kwargs["event_type"] == "query_failure_spike"
        and call.kwargs["project_id"] == project_id
        for call in dispatch.await_args_list
    )


@pytest.mark.asyncio
async def test_failure_spike_without_project_keeps_tenant_global_dispatch():
    db = AsyncMock()

    with patch(
        "shared.alerting.dispatcher.dispatch_alert",
        new_callable=AsyncMock,
    ) as dispatch:
        for _ in range(routes._FAILURE_SPIKE_THRESHOLD):
            await routes._check_failure_spike(db, "tenant-1")

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

    with patch.object(routes.time, "monotonic", side_effect=mock_monotonic), patch(
        "shared.alerting.dispatcher.dispatch_alert",
        new_callable=AsyncMock,
    ) as dispatch:
        for _ in range(routes._FAILURE_SPIKE_THRESHOLD):
            await routes._check_failure_spike(db, "fresh-tenant")

    assert dispatch.await_count >= 1, (
        "first spike alert for a never-alerted tenant must dispatch even when "
        "time.monotonic() is below _FAILURE_SPIKE_WINDOW"
    )
