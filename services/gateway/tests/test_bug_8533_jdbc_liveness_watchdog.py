"""Bug-8533 auto-recovery: a proven-dead accept loop must recycle the gateway.

Why this file exists
--------------------
Bug-8974 made ``/health`` truthful about the JDBC accept loop. Truthful is not
recovered: Docker Compose's ``restart: unless-stopped`` acts on process EXIT, not
on health, and the GCP db-vm gateway runs under a bare ``docker run
--restart=unless-stopped`` with no healthcheck at all. So the gateway watches its
own accept loop and exits non-zero once it is proven dead.

The whole risk of that mechanism is the FALSE NEGATIVE — restarting a gateway
that is actually working — so the tests below spend their effort on the three
things that prevent it: one failure is not enough, a success in between resets
the count, and the counter does not run before the listener has ever come up.
Plus the one live-load case the decision calls out by name: a probe refused by
the per-IP governor gets an ``E`` ErrorResponse, which is the application
ANSWERING and must never be counted as dead.

Nothing here mocks the boundary under test. Every probe below is the real
``shared.gateway_liveness`` primitive driven against a REAL bound socket whose
accept loop is genuinely stopped and restarted — the same runtime shape as the
wedge (connections complete into the kernel backlog and are never dequeued). The
test controls only the TIMELINE (which cycle the accept loop is running on) and
substitutes the process exit, because a real ``os._exit`` would take pytest with
it.

Execution scope: isolated (no DB, no live services, ephemeral loopback ports).
Gate tier: T2 (regression guard for a documented bug).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import threading
import time

import pytest

from shared.gateway_liveness import probe_jdbc_accept_loop_async
from src.jdbc.liveness_watchdog import (
    WATCHDOG_EXIT_CODE,
    run_jdbc_liveness_watchdog,
)

# Short enough that the whole file runs in seconds, long enough that a loopback
# exchange on a busy CI box is not mistaken for a wedge.
_PROBE_TIMEOUT = 0.5
_INTERVAL = 0.01
_TEST_DEADLINE = 30.0
_STATE_OBSERVATION_TIMEOUT = 1.0


class _ControllableListener:
    """A real listening socket whose accept loop can be stopped and restarted.

    Stopped is not closed: the socket stays bound and ``listen()``ing, so the
    kernel keeps completing the three-way handshake into the backlog and nothing
    ever dequeues it. That is exactly the failure this watchdog exists for, and
    the only shape in which a TCP-connect probe reports healthy.
    """

    def __init__(self, reply: bytes = b"N") -> None:
        self._reply = reply
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(100)  # asyncio's default backlog
        self._sock.settimeout(0.02)
        self.port: int = self._sock.getsockname()[1]
        self.accepting = True
        self._state_condition = threading.Condition()
        self._observed_accepting: bool | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def set_accepting(self, accepting: bool) -> None:
        self.accepting = accepting

    def wait_until_observed(self, accepting: bool) -> None:
        """Wait until the real accept loop has observed the requested state.

        Bug-8533's startup-grace test must not race a thread still blocked in
        ``accept()``.  A fixed sleep can let the first probe dequeue a queued
        connection before the thread notices ``accepting=False``.
        """
        deadline = time.monotonic() + _STATE_OBSERVATION_TIMEOUT
        with self._state_condition:
            while self._observed_accepting is not accepting:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(
                        "the listener accept loop did not acknowledge "
                        f"accepting={accepting} within "
                        f"{_STATE_OBSERVATION_TIMEOUT}s"
                    )
                self._state_condition.wait(timeout=remaining)

    def _serve(self) -> None:
        while not self._stop.is_set():
            accepting = self.accepting
            with self._state_condition:
                self._observed_accepting = accepting
                self._state_condition.notify_all()
            if not accepting:
                time.sleep(0.005)
                continue
            try:
                conn, _ = self._sock.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            try:
                conn.settimeout(1.0)
                conn.recv(8)          # the 8-byte SSLRequest
                conn.sendall(self._reply)
            except OSError:
                pass                  # a probe that already gave up and closed
            finally:
                with contextlib.suppress(OSError):
                    conn.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3.0)
        with contextlib.suppress(OSError):
            self._sock.close()


@pytest.fixture
def listener():
    node = _ControllableListener()
    try:
        yield node
    finally:
        node.close()


async def _drive(
    node: _ControllableListener,
    script: list[bool],
    *,
    failure_limit: int = 3,
) -> tuple[list[tuple[bool, str]], list[int], list[logging.LogRecord]]:
    """Run the watchdog through *script* (one entry per probe cycle: is the
    accept loop running?) and return (probe results, exit codes, log records).

    Stops when the watchdog exits or when the script is exhausted, whichever
    comes first — so "did not exit" is asserted after the full timeline ran,
    never before.
    """
    results: list[tuple[bool, str]] = []
    exits: list[int] = []

    async def scripted_probe(host: str, port: int, *, timeout: float):
        idx = len(results)
        accepting = script[idx] if idx < len(script) else script[-1]
        node.set_accepting(accepting)
        node.wait_until_observed(accepting)
        outcome = await probe_jdbc_accept_loop_async(host, port, timeout)
        results.append(outcome)
        return outcome

    handler = _RecordingHandler()
    watchdog_logger = logging.getLogger("src.jdbc.liveness_watchdog")
    previous_level = watchdog_logger.level
    # The suite's root level can be above INFO; the reset/arm lines are asserted
    # below, so this handler must see everything the watchdog emits.
    watchdog_logger.setLevel(logging.DEBUG)
    watchdog_logger.addHandler(handler)
    task = asyncio.create_task(
        run_jdbc_liveness_watchdog(
            "127.0.0.1",
            node.port,
            interval_seconds=_INTERVAL,
            failure_limit=failure_limit,
            probe_timeout_seconds=_PROBE_TIMEOUT,
            probe=scripted_probe,
            on_exit=exits.append,
        )
    )
    deadline = time.monotonic() + _TEST_DEADLINE
    try:
        while not exits and len(results) < len(script) and not task.done():
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"the watchdog did not finish the {len(script)}-cycle "
                    f"timeline within {_TEST_DEADLINE}s "
                    f"(probes so far: {results})"
                )
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        watchdog_logger.removeHandler(handler)
        watchdog_logger.setLevel(previous_level)
    return results, exits, handler.records


class _RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _messages(records, level: int) -> list[str]:
    return [r.getMessage() for r in records if r.levelno == level]


# ---------------------------------------------------------------------------
# The three behaviours the decision names
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_failed_probe_does_not_exit(listener):
    """A single miss must never recycle a working gateway.

    This is the false negative the whole three-strike shape exists to prevent:
    a scheduling stall, an accept-path retry or a governor refusal is a blip,
    not a wedge.

    Mutation: exit on the first failure (``>= 1`` instead of ``>= failure_limit``)
    -> red.
    """
    results, exits, records = await _drive(listener, [True, False, True, True])

    assert [alive for alive, _ in results] == [True, False, True, True], results
    assert exits == [], (
        "the gateway exited after a SINGLE failed probe — every transient blip "
        "is now a container restart"
    )
    assert any("strike 1 of 3" in m for m in _messages(records, logging.WARNING)), (
        "the failure was not logged at WARN naming the probe result, so an "
        "operator reading the log cannot tell a blip happened at all"
    )


@pytest.mark.asyncio
async def test_three_consecutive_failures_exit_non_zero(listener):
    """The mechanism must actually FIRE. An inert watchdog is worse than none.

    Mutation: drop the ``on_exit`` call, or raise ``failure_limit`` past the
    script length -> red.
    """
    results, exits, records = await _drive(listener, [True, False, False, False])

    assert [alive for alive, _ in results] == [True, False, False, False], results
    assert exits == [WATCHDOG_EXIT_CODE], (
        "three consecutive proven-dead probes did not exit the process — "
        "`restart: unless-stopped` acts on exit, so nothing recycles the "
        "wedged gateway and Bug-8533's silent wedge is unchanged"
    )
    assert WATCHDOG_EXIT_CODE != 0, (
        "the watchdog exited ZERO. `unless-stopped` recycles either way "
        "(measured), but a zero code is indistinguishable from a clean stop in "
        "`docker inspect` / Kubernetes `lastState.terminated`, and a "
        "`restart: on-failure` policy would not act on it at all"
    )

    warnings = _messages(records, logging.WARNING)
    for strike in (1, 2, 3):
        assert any(f"strike {strike} of 3" in m for m in warnings), (
            f"strike {strike} was not logged at WARN: {warnings}"
        )
    errors = _messages(records, logging.ERROR)
    assert any("proven dead" in m and "exiting with code" in m for m in errors), (
        "the exit was not logged at ERROR naming the reason — a silent exit is "
        f"indistinguishable from a crash: {errors}"
    )
    assert any("backlog" in m or "did not answer" in m for m in errors), (
        f"the ERROR line does not name the probe result: {errors}"
    )


@pytest.mark.asyncio
async def test_a_success_between_failures_resets_the_strike_count(listener):
    """CONSECUTIVE, not cumulative. Four total failures, never three in a row.

    Mutation: delete ``consecutive_failures = 0`` on the success branch -> the
    counter accumulates, the fourth failure below trips the limit -> red.
    """
    script = [True, False, False, True, False, False, True]
    results, exits, records = await _drive(listener, script)

    assert [alive for alive, _ in results] == script, results
    assert exits == [], (
        "failures spread across successful probes accumulated into an exit — "
        "a gateway that misses one probe an hour would restart itself"
    )
    infos = _messages(records, logging.INFO)
    assert any("reset to 0" in m for m in infos), (
        f"the reset was not logged, so nothing records that the blip cleared: {infos}"
    )


# ---------------------------------------------------------------------------
# Startup grace and the E-is-alive judgement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_counter_does_not_run_before_the_listener_is_ever_alive(listener):
    """A listener that never came up must not become a restart loop.

    A slow start, a port already in use or unreadable TLS material are not
    fixed by restarting, so the watchdog arms only once it has SEEN the accept
    loop answer. Six dead cycles against a limit of three and still no exit.

    Mutation: remove the ``if not observed_alive: continue`` guard -> red.
    """
    results, exits, records = await _drive(listener, [False] * 6)

    assert [alive for alive, _ in results] == [False] * 6, results
    assert exits == [], (
        "the watchdog exited on a listener it had never seen alive — a slow "
        "or misconfigured start is now an unbounded restart loop that never "
        "converges"
    )
    warnings = _messages(records, logging.WARNING)
    assert any("never been observed alive" in m for m in warnings), (
        f"the grace period was silent, so the failure is invisible: {warnings}"
    )
    assert not any("strike" in m for m in warnings), (
        f"the strike counter ran during the startup grace: {warnings}"
    )


@pytest.mark.asyncio
async def test_an_errorresponse_reply_counts_as_alive():
    """``E`` is the application ANSWERING, so it must never count as dead.

    The per-IP admission governor refuses a connection with a FATAL
    ErrorResponse BEFORE reading the startup frame, so a probe arriving while
    the loopback address is at its concurrency cap gets ``E``. Counting that as
    dead would restart a working gateway precisely when it is busiest — the
    exact false negative this decision is shaped to avoid.

    Mutation: drop ``b"E"`` from ``_SSL_REPLIES`` in
    ``shared/gateway_liveness.py`` -> every cycle below reads dead -> red.
    """
    node = _ControllableListener(reply=b"E")
    try:
        results, exits, records = await _drive(node, [True] * 5)
    finally:
        node.close()

    assert all(alive for alive, _ in results), (
        f"an ErrorResponse reply was read as a dead accept loop: {results}"
    )
    assert exits == [], "a gateway refusing at its connection cap was restarted"
    assert not any("strike" in m for m in _messages(records, logging.WARNING))


# ---------------------------------------------------------------------------
# Wiring — the mechanism must be reachable in the running service
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_lifespan_starts_the_watchdog_from_config_and_cancels_it_first(
    monkeypatch,
):
    """The watchdog must be WIRED, configured, and stopped before the listener.

    Three things at once, because each has been a real defect class here:
    (1) a watchdog defined but never started is the v4 inert-watchdog failure;
    (2) hard-coded 60/3 literals would make the settings decorative — the
        assertion uses deliberately non-default values, so a literal fails;
    (3) cancelling the watchdog AFTER the listener is closed would let an
        ORDERLY shutdown look exactly like a wedge and exit non-zero on the way
        down. Asserting ``is_serving()`` at cancellation time pins the order.
    """
    import src.main as gw

    started: dict = {}
    serving_at_cancel: dict = {}

    async def _fake_server_handler(reader, writer):  # pragma: no cover - unused
        writer.close()

    server = await asyncio.start_server(_fake_server_handler, "127.0.0.1", 0)

    async def _fake_watchdog(host, port, **kwargs):
        started["host"] = host
        started["port"] = port
        started.update(kwargs)
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            serving_at_cancel["value"] = server.is_serving()
            raise

    async def _fake_close_all_pools():
        return None

    monkeypatch.setattr(gw, "refresh_system_snapshot", lambda: asyncio.sleep(0))
    monkeypatch.setattr(
        gw, "start_jdbc_server", lambda: asyncio.sleep(0, result=server)
    )
    monkeypatch.setattr(gw, "run_jdbc_liveness_watchdog", _fake_watchdog)
    import shared.source_pool as sp
    monkeypatch.setattr(sp, "close_all_pools", _fake_close_all_pools)

    monkeypatch.setattr(gw.settings, "GATEWAY_JDBC_WATCHDOG_ENABLED", True, raising=False)
    monkeypatch.setattr(
        gw.settings, "GATEWAY_HEALTH_JDBC_PROBE_ENABLED", True, raising=False
    )
    monkeypatch.setattr(
        gw.settings, "GATEWAY_JDBC_WATCHDOG_INTERVAL_SECONDS", 7.5, raising=False
    )
    monkeypatch.setattr(
        gw.settings, "GATEWAY_JDBC_WATCHDOG_FAILURE_LIMIT", 5, raising=False
    )
    monkeypatch.setattr(
        gw.settings, "GATEWAY_HEALTH_JDBC_PROBE_TIMEOUT_SECONDS", 1.25, raising=False
    )

    async with gw.lifespan(gw.app):
        for _ in range(100):
            if started:
                break
            await asyncio.sleep(0.01)

    assert started, (
        "the gateway lifespan never started the JDBC liveness watchdog — the "
        "auto-recovery half of Bug-8533 is unwired product code"
    )
    assert started["interval_seconds"] == 7.5, started
    assert started["failure_limit"] == 5, started
    assert started["probe_timeout_seconds"] == 1.25, started
    assert started["host"] == "127.0.0.1", started
    assert serving_at_cancel.get("value") is True, (
        "the watchdog was cancelled AFTER the listener stopped serving — an "
        "orderly shutdown would look like a wedge and could exit non-zero"
    )


@pytest.mark.asyncio
async def test_the_lifespan_honours_the_watchdog_disable_switch(monkeypatch):
    """A deployment that does not serve JDBC must be able to turn this off."""
    import src.main as gw

    started: list = []

    async def _fake_watchdog(host, port, **kwargs):  # pragma: no cover - must not run
        started.append(port)
        await asyncio.sleep(3600)

    async def _fake_server_handler(reader, writer):  # pragma: no cover - unused
        writer.close()

    server = await asyncio.start_server(_fake_server_handler, "127.0.0.1", 0)

    monkeypatch.setattr(gw, "refresh_system_snapshot", lambda: asyncio.sleep(0))
    monkeypatch.setattr(
        gw, "start_jdbc_server", lambda: asyncio.sleep(0, result=server)
    )
    monkeypatch.setattr(gw, "run_jdbc_liveness_watchdog", _fake_watchdog)
    import shared.source_pool as sp
    monkeypatch.setattr(sp, "close_all_pools", lambda: asyncio.sleep(0))
    monkeypatch.setattr(
        gw.settings, "GATEWAY_JDBC_WATCHDOG_ENABLED", False, raising=False
    )

    async with gw.lifespan(gw.app):
        await asyncio.sleep(0.05)

    assert started == [], "the watchdog ran with GATEWAY_JDBC_WATCHDOG_ENABLED=False"


@pytest.mark.asyncio
async def test_a_raising_probe_does_not_kill_the_watchdog():
    """A watchdog that dies silently is the failure mode that makes it useless.

    If the probe raises (a thread-pool hiccup, an fd shortage) the task must not
    end: a gateway whose watchdog died at start-up has no auto-recovery for the
    rest of its life while everything reports that it has some. The raise counts
    as a failed probe — nothing was proven alive — and the three-strike damping
    then applies to it like any other failure.

    Mutation: let the exception propagate out of the loop -> the task ends after
    one raise, no exit is ever recorded -> red.
    """
    calls: list[int] = []
    exits: list[int] = []

    async def raising_probe(host, port, *, timeout):
        calls.append(1)
        if len(calls) == 1:
            return True, "alive"  # arm the watchdog
        raise RuntimeError("probe blew up")

    task = asyncio.create_task(
        run_jdbc_liveness_watchdog(
            "127.0.0.1",
            5433,
            interval_seconds=_INTERVAL,
            failure_limit=3,
            probe_timeout_seconds=_PROBE_TIMEOUT,
            probe=raising_probe,
            on_exit=exits.append,
        )
    )
    deadline = time.monotonic() + _TEST_DEADLINE
    try:
        while not exits and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert exits == [WATCHDOG_EXIT_CODE], (
        "a raising probe stopped the watchdog instead of counting as a failure "
        f"— it survived {len(calls)} call(s) and never exited"
    )


def test_the_shipped_defaults_are_the_decided_ones():
    """60 seconds apart, three strikes — the values the user fixed."""
    from shared.config.settings import Settings

    assert Settings.model_fields["GATEWAY_JDBC_WATCHDOG_INTERVAL_SECONDS"].default == 60.0
    assert Settings.model_fields["GATEWAY_JDBC_WATCHDOG_FAILURE_LIMIT"].default == 3
    assert Settings.model_fields["GATEWAY_JDBC_WATCHDOG_ENABLED"].default is True
