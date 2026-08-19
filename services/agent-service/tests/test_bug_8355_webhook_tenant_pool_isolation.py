"""Bug-8355 — one tenant's slow receiver must not starve every other tenant.

The defect
----------
Outbound agent-webhook delivery is multi-tenant; the httpx connection pool is
not. One process-wide ``httpx.AsyncClient`` with ``max_connections=20`` served
every tenant, and nothing bounded how much of it any single tenant could hold.

Bug-8349 R2 added ``_MAX_RESPONSE_BYTES = 8 KB`` so a hostile receiver
drip-feeding a huge body could not exhaust memory. That cap bounds BYTES, not
TIME. httpx's read timeout is per read, and every arriving chunk resets it, so
a receiver that replies 500 and then sends one byte every three seconds stays
under both the 10s read timeout and the 8 KB cap indefinitely while holding its
pooled connection the whole time. Live-reproduced against the real
dispatcher/httpx/httpcore stack with a raw socket server (2026-07-29). Enough
such receivers in ONE tenant and every other tenant's deliveries fail — and
they fail as an indistinguishable transport error, retried, backed off and
DLQ'd exactly like a genuine receiver-side failure.

The fix has three parts, and this file pins each one separately because each
closes a different half of the problem:

1. a per-tenant concurrency budget in front of the shared pool, so a tenant
   can only ever hold its own slice;
2. an absolute wall-clock deadline per attempt, so a trickling receiver
   releases its connection even within its own tenant's budget;
3. distinguishable failure reasons, so an operator can tell "we throttled
   you" and "we stopped waiting" from "your receiver broke".

Test escape: every prior dispatcher test either mocked ``_post_once`` outright
or drove exactly one delivery, so no test ever had two deliveries in flight at
once and the shared-pool property was untestable by construction.
Guard: this file. Tier: T2 (multi-tenancy isolation regression).
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from src.webhooks import dispatcher

pytestmark = pytest.mark.unit


class _Gate:
    """A send that blocks until released — stands in for a trickling receiver
    without needing a real socket (the live socket reproduction is recorded in
    the issue; this pins the CODE property deterministically and fast)."""

    def __init__(self):
        self.release = asyncio.Event()
        self.in_flight = 0
        self.peak_in_flight = 0
        self.started = asyncio.Event()

    async def send(self, client, target_url, body_bytes, headers):
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        self.started.set()
        try:
            await self.release.wait()
            return False, 500, "HTTP 500"
        finally:
            self.in_flight -= 1


async def _post(tenant: str):
    return await dispatcher._post_once(
        tenant, "https://receiver.example/hook", b"{}", "", "turn.completed",
    )


class TestPerTenantBudget:
    @pytest.mark.asyncio
    async def test_one_tenant_cannot_hold_more_than_its_budget(self, monkeypatch):
        """The isolation guarantee itself: N+1 concurrent deliveries for ONE
        tenant never put more than N on the wire at the same time."""
        budget = dispatcher._MAX_CONNECTIONS_PER_TENANT
        gate = _Gate()
        monkeypatch.setattr(dispatcher, "_send_and_read_status", gate.send)
        monkeypatch.setattr(dispatcher, "_POOL_ACQUIRE_TIMEOUT_SEC", 0.05)
        monkeypatch.setattr(dispatcher, "_tenant_budgets", {})

        tasks = [
            asyncio.create_task(_post("noisy-tenant"))
            for _ in range(budget + 3)
        ]
        await gate.started.wait()
        # Let the over-budget callers reach (and fail) their acquire timeout.
        await asyncio.sleep(0.2)
        gate.release.set()
        results = await asyncio.gather(*tasks)

        assert gate.peak_in_flight <= budget, (
            f"{gate.peak_in_flight} concurrent sends for one tenant with a "
            f"budget of {budget} — the tenant can still monopolise the pool"
        )
        throttled = [
            r for r in results
            if r[2] == dispatcher.TENANT_BUDGET_EXHAUSTED_REASON
        ]
        assert throttled, (
            "no delivery was throttled, so the budget was never actually "
            "contended and this test proves nothing"
        )

    @pytest.mark.asyncio
    async def test_a_saturated_tenant_does_not_block_another_tenant(
        self, monkeypatch
    ):
        """The business outcome. Tenant A saturates its own budget with
        never-completing sends; tenant B's delivery must still go out."""
        budget = dispatcher._MAX_CONNECTIONS_PER_TENANT
        gate = _Gate()
        monkeypatch.setattr(dispatcher, "_send_and_read_status", gate.send)
        monkeypatch.setattr(dispatcher, "_POOL_ACQUIRE_TIMEOUT_SEC", 0.05)
        monkeypatch.setattr(dispatcher, "_tenant_budgets", {})

        hogs = [
            asyncio.create_task(_post("tenant-a")) for _ in range(budget)
        ]
        await gate.started.wait()
        await asyncio.sleep(0.05)

        b_task = asyncio.create_task(_post("tenant-b"))
        await asyncio.sleep(0.2)
        assert b_task.done() is False, (
            "sanity: tenant B should be mid-send, not finished"
        )

        gate.release.set()
        ok, status, error = await b_task
        await asyncio.gather(*hogs)

        assert error != dispatcher.TENANT_BUDGET_EXHAUSTED_REASON, (
            "tenant B was throttled by tenant A's traffic — the budget is "
            "not actually per-tenant"
        )
        assert status == 500, "tenant B's delivery never reached the receiver"

    @pytest.mark.asyncio
    async def test_budget_is_released_when_the_send_raises(self, monkeypatch):
        """A leaked permit shrinks the tenant's budget permanently, which is
        the same starvation this fix exists to prevent, just slower."""
        monkeypatch.setattr(dispatcher, "_tenant_budgets", {})

        async def _boom(*_a, **_kw):
            raise RuntimeError("transport exploded")

        monkeypatch.setattr(dispatcher, "_send_and_read_status", _boom)

        for _ in range(dispatcher._MAX_CONNECTIONS_PER_TENANT + 2):
            ok, status, error = await _post("tenant-c")
            assert ok is False
            assert error != dispatcher.TENANT_BUDGET_EXHAUSTED_REASON, (
                "the budget was exhausted by permits that were never "
                "released after a failing send"
            )


class TestAttemptDeadline:
    @pytest.mark.asyncio
    async def test_a_trickling_receiver_is_cut_off_at_the_deadline(
        self, monkeypatch
    ):
        """A send that never completes must not hold its connection forever,
        even when the tenant is well inside its budget. This is the half the
        byte cap cannot cover: bytes are bounded, time was not."""
        monkeypatch.setattr(dispatcher, "_tenant_budgets", {})
        monkeypatch.setattr(dispatcher, "_ATTEMPT_DEADLINE_SEC", 0.1)

        async def _never_finishes(*_a, **_kw):
            await asyncio.sleep(3600)

        monkeypatch.setattr(dispatcher, "_send_and_read_status", _never_finishes)

        ok, status, error = await asyncio.wait_for(_post("tenant-d"), timeout=5)

        assert ok is False
        assert status is None
        assert error == dispatcher.ATTEMPT_DEADLINE_REASON

    @pytest.mark.asyncio
    async def test_the_deadline_releases_the_tenant_budget(self, monkeypatch):
        """A deadline that abandons the coroutine without releasing the permit
        would convert a slow receiver into permanent self-starvation."""
        monkeypatch.setattr(dispatcher, "_tenant_budgets", {})
        monkeypatch.setattr(dispatcher, "_ATTEMPT_DEADLINE_SEC", 0.05)

        async def _never_finishes(*_a, **_kw):
            await asyncio.sleep(3600)

        monkeypatch.setattr(dispatcher, "_send_and_read_status", _never_finishes)

        for _ in range(dispatcher._MAX_CONNECTIONS_PER_TENANT + 2):
            # The outer wait_for is what makes a MISSING deadline fail this
            # test in a second rather than hang the suite for an hour. A guard
            # whose failure mode is "the run never finishes" is not a guard —
            # mutation-verified: removing the deadline made an earlier draft of
            # this test hang instead of go red.
            _ok, _status, error = await asyncio.wait_for(
                _post("tenant-e"), timeout=5,
            )
            assert error == dispatcher.ATTEMPT_DEADLINE_REASON, (
                f"expected the deadline reason, got {error!r} — a permit "
                "leaked on the deadline path"
            )


class TestFailureReasonsAreDistinguishable:
    def test_our_throttle_and_deadline_reasons_are_not_receiver_errors(self):
        """The original report's second half: a starved delivery was recorded
        the same way as a genuine receiver failure, so an operator could not
        tell them apart. Both reasons must be explicit about whose problem it
        is and what happens next."""
        for reason in (
            dispatcher.TENANT_BUDGET_EXHAUSTED_REASON,
            dispatcher.ATTEMPT_DEADLINE_REASON,
        ):
            assert "retried" in reason.lower()
            assert not reason.startswith("HTTP ")


class TestPoolConfiguration:
    def test_per_tenant_budget_is_meaningfully_below_the_pool_size(self):
        """A per-tenant cap at or above the pool size guarantees nothing: one
        tenant could still take every connection."""
        assert dispatcher._MAX_CONNECTIONS_PER_TENANT < dispatcher._MAX_POOL_CONNECTIONS
        assert (
            dispatcher._MAX_CONNECTIONS_PER_TENANT
            <= dispatcher._MAX_POOL_CONNECTIONS // 2
        ), (
            "one tenant may hold more than half the pool; two such tenants "
            "still starve everyone else"
        )

    def test_pool_acquisition_has_its_own_short_timeout(self):
        """The old single-float timeout applied the full request timeout to
        pool acquisition, so a caller blocked purely by another tenant waited
        the whole 10s and then reported a generic transport error."""
        timeout = dispatcher._client_timeout()
        assert timeout.pool == dispatcher._POOL_ACQUIRE_TIMEOUT_SEC
        assert timeout.pool < timeout.read

    def test_attempt_deadline_exceeds_the_per_phase_timeout(self):
        """The deadline is the backstop for the trickle case only. If it were
        shorter than the per-phase timeout it would pre-empt ordinary
        slow-but-honest receivers and mask their real errors."""
        assert dispatcher._ATTEMPT_DEADLINE_SEC > dispatcher._REQUEST_TIMEOUT_SEC

    def test_post_once_receives_the_tenant(self):
        """The budget can only be per-tenant if the call site says which
        tenant. Pinned because a signature drift here fails silently: the
        TypeError is swallowed by dispatch_event's broad handler and becomes a
        six-minute retry loop rather than an error anyone sees."""
        params = list(inspect.signature(dispatcher._post_once).parameters)
        assert params[0] == "tenant_id"

    def test_dispatch_event_passes_the_tenant_to_post_once(self):
        source = inspect.getsource(dispatcher.dispatch_event)
        assert "_post_once(\n                tenant_id," in source, (
            "dispatch_event no longer passes tenant_id to _post_once; every "
            "delivery would then share one budget bucket"
        )


class TestTheDeadlineSettingIsClampedNotTrusted:
    """R2 reviewer finding 4, guarded (R3).

    The R2 round clamped ``_ATTEMPT_DEADLINE_SEC`` to
    ``max(setting, _REQUEST_TIMEOUT_SEC + 1)`` in BOTH dispatcher families,
    because the knob is shared and an operator tightening one family could
    otherwise drop the other's deadline below its own per-phase request
    timeout — at which point every slow-but-honest receiver's real error is
    replaced by ``ATTEMPT_DEADLINE_REASON``.

    R3 test escape: the clamp shipped with no guard.
    ``TestPoolConfiguration::test_attempt_deadline_exceeds_the_per_phase_
    timeout`` asserts the RESOLVED value, and the shipped default (30) is
    already above the 10s per-phase timeout — so it stays green with the
    clamp removed and proves nothing about it. This loads each dispatcher
    module fresh against a deliberately hostile setting, which is the only
    way the clamp is observable. Deleting either ``max(...)`` turns it red.
    Tier: T2 (operational-safety regression).
    """

    @staticmethod
    def _load_with_deadline(rel_path: str, module_name: str, deadline: int):
        """Execute a dispatcher module fresh with a patched setting.

        A throwaway module object, registered in ``sys.modules`` only for the
        duration of ``exec_module`` (``@dataclass`` resolves its own module
        during class creation) and popped immediately, so the already-imported
        production modules and their pool/budget state are untouched.
        """
        import importlib.util
        import pathlib
        import sys

        from shared.config import settings as settings_mod

        real = settings_mod.get_settings()

        class _Patched:
            AGENT_WEBHOOK_ATTEMPT_DEADLINE_SEC = deadline

            def __getattr__(self, name):
                return getattr(real, name)

        original = settings_mod.get_settings
        settings_mod.get_settings = lambda: _Patched()
        try:
            path = pathlib.Path(__file__).resolve().parents[3] / rel_path
            spec = importlib.util.spec_from_file_location(module_name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            finally:
                sys.modules.pop(module_name, None)
            return module
        finally:
            settings_mod.get_settings = original

    @pytest.mark.parametrize(
        "rel_path,module_name",
        [
            (
                "services/agent-service/src/webhooks/dispatcher.py",
                "_r3_probe_agent_dispatcher",
            ),
            ("shared/webhooks/dispatcher.py", "_r3_probe_shared_dispatcher"),
        ],
        ids=["agent-service", "shared"],
    )
    def test_a_too_low_setting_cannot_pre_empt_the_per_phase_timeout(
        self, rel_path, module_name
    ):
        module = self._load_with_deadline(rel_path, module_name, 1)
        assert module._ATTEMPT_DEADLINE_SEC == module._REQUEST_TIMEOUT_SEC + 1, (
            f"{rel_path}: a 1s AGENT_WEBHOOK_ATTEMPT_DEADLINE_SEC was taken "
            f"raw ({module._ATTEMPT_DEADLINE_SEC}s) instead of being clamped "
            f"above the {module._REQUEST_TIMEOUT_SEC}s per-phase request "
            "timeout — every slow-but-honest receiver's real error would be "
            "replaced by ATTEMPT_DEADLINE_REASON"
        )

    @pytest.mark.parametrize(
        "rel_path,module_name",
        [
            (
                "services/agent-service/src/webhooks/dispatcher.py",
                "_r3_probe_agent_dispatcher_high",
            ),
            ("shared/webhooks/dispatcher.py", "_r3_probe_shared_dispatcher_high"),
        ],
        ids=["agent-service", "shared"],
    )
    def test_the_clamp_does_not_overwrite_a_legitimate_setting(
        self, rel_path, module_name
    ):
        """Fail-safe must not become fail-always: an operator who deliberately
        raises the deadline must still get the value they asked for."""
        module = self._load_with_deadline(rel_path, module_name, 120)
        assert module._ATTEMPT_DEADLINE_SEC == 120
