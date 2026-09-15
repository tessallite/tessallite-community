"""Auto-recovery for a proven-dead JDBC accept loop (Bug-8533, second half).

What this adds that ``/health`` does not
---------------------------------------
Bug-8974 made every liveness probe in the estate TRUTHFUL: ``/health`` runs a
credential-free PostgreSQL ``SSLRequest`` against the gateway's own listener and
answers 503 when the accept loop is dead. Truthful is not the same as recovered.
Docker Compose does not restart an unhealthy container — ``restart:
unless-stopped`` acts on process EXIT, not on health (only Swarm acts on health)
— and the GCP db-vm gateway runs under a bare ``docker run --restart=unless-stopped``
with no healthcheck at all. On both surfaces a wedged gateway stayed wedged, and
the only thing the 503 changed was that somebody could now see it.

So the gateway watches its own accept loop with the SAME probe and, once the
loop is proven dead, exits non-zero. The restart policy does the rest.

Why three strikes and not one
-----------------------------
The decision this implements (``docs/questions/questions_gateway-jdbc-listener-liveness.md``)
turns on one judgement: the false NEGATIVE is worse than the false positive being
fixed, because restarting a working gateway is a self-inflicted outage. An
immediate exit would make every transient miss — a scheduling stall, an
accept-path resource retry, a per-IP governor refusal under load — a container
restart. Three failures 60 seconds apart separate a blip from a wedge and cost at
most ~2 extra minutes on a real wedge, against the ~17 minutes of hanging BI
clients that backlog exhaustion took to surface anything at all.

The counter is CONSECUTIVE: any successful probe resets it to zero, so three
failures spread across a day never accumulate into an exit.

``/health`` is deliberately NOT coupled to this counter. It keeps reporting the
first failure immediately, because a monitor should see the blip; only the EXIT
waits for three, because only the exit is destructive.

Why the exit is ``os._exit`` and not a graceful shutdown
-------------------------------------------------------
The PROCESS must actually die, on every launcher, or the watchdog is inert. The
gateway is started two different ways in this repo: ``python -m src.main`` (both
compose files) and ``uvicorn src.main:app``
(``deploy/gcp/db-vm/db-vm-deploy-gateway.sh``). A graceful self-SIGTERM would
depend on the ``__main__`` block to finish the job, and that block does not run
under the uvicorn CLI — so the mechanism would be silently INERT on the one
deployment where it is the ONLY auto-recovery available. That is the exact
failure the v4 JDBC watchdog had (its ``except OSError`` arm was unreachable, so
196 lines of tests guarded a no-op), and an inert watchdog is worse than none,
because it is believed. ``os._exit`` also cannot be swallowed by the very
shutdown path a wedged process is least able to complete.

Measured, not assumed (2026-08-11, docker 29.6.1, the real ``infra-gateway``
image): ``--restart=unless-stopped`` recycles a container on exit 3 AND on exit
0 — that policy does not filter on the code. So the non-zero code is not what
makes the restart happen; it is what makes the restart READABLE. ``docker
inspect``'s ``State.ExitCode``, Kubernetes' ``lastState.terminated.exitCode``
and any ``restart: on-failure`` policy an operator uses all distinguish "this
gateway killed itself because its accept loop was dead" from "somebody stopped
it cleanly".

The abruptness costs little: file descriptors are closed by the kernel on exit,
so source connections are FIN'd normally and a container restart re-creates the
pools. The ERROR line is emitted and the log handlers flushed first, so the exit
is never mistaken for a crash.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable

from shared.gateway_liveness import probe_jdbc_accept_loop_async
from shared.metrics import JDBC_PROBE_FAILURES, JDBC_WATCHDOG_EXITS

logger = logging.getLogger(__name__)

__all__ = [
    "WATCHDOG_EXIT_CODE",
    "run_jdbc_liveness_watchdog",
    "seed_exit_counter_from_disk",
    "watchdog_state_path",
]

# Deliberately a constant, not a setting. The interval and the strike limit are
# operational tunables; this is a diagnostic contract. It is what tells an
# operator reading `docker inspect` or a Kubernetes `lastState.terminated`
# whether the gateway killed itself over a dead accept loop or was stopped
# cleanly, and it is the only thing a `restart: on-failure` policy acts on.
# Making it settable would let a deployment set it to 0 and erase that signal.
WATCHDOG_EXIT_CODE = 3

ProbeFn = Callable[..., Awaitable[tuple[bool, str]]]


def _open_file_descriptors() -> int | None:
    """Count this process's open file descriptors, or None when unavailable.

    Bug-9834: a wedged accept loop that is actually descriptor exhaustion looks
    identical, from the outside, to one that is not — and the exit erases the
    evidence. Best-effort by design: this runs on the path that is about to
    terminate the process, so it must never be the reason the exit does not
    happen.
    """
    try:
        return len(os.listdir("/proc/self/fd"))
    except Exception:  # pragma: no cover - not Linux, or /proc unavailable
        return None


def _active_jdbc_sessions() -> int | None:
    """Admitted JDBC connections held at this moment, or None if unknown.

    Read from the connection governor, which already maintains the count to
    enforce the per-IP cap. Best-effort for the same reason as above.
    """
    try:
        from src.jdbc.throttle import get_governor

        return get_governor().active_connection_total()
    except Exception:  # pragma: no cover - governor unavailable
        return None


# Bug-9834 (review F3): where the exit tally is kept so it OUTLIVES the process
# that records it.
#
# The in-process counter cannot carry recurrence on its own. The watchdog
# increments it and exits immediately; with a 15s scrape interval Prometheus
# almost never observes the incremented value, and the replacement process
# starts at zero. ``increase()`` over such a series can stay flat through any
# number of wedges — the alert that depended on it would simply never fire.
#
# The tally is therefore written to a file before the exit and read back at
# start-up to SEED the counter, so the series steps 0 -> 1 -> 2 across restarts
# instead of resetting. Prometheus sees the step on its next scrape, whenever
# that lands.
#
# No new dependency and no scrape-wait: the exit is never delayed for a metric.
# The file is best-effort — losing it degrades to the previous behaviour, it
# never blocks recovery. Set TESSALLITE_JDBC_WATCHDOG_STATE to place it on a
# volume that survives container RECREATION; the default survives a restart.
_STATE_ENV = "TESSALLITE_JDBC_WATCHDOG_STATE"
_DEFAULT_STATE_PATH = "/tmp/tessallite-jdbc-watchdog-exits"


def watchdog_state_path() -> str:
    return os.environ.get(_STATE_ENV) or _DEFAULT_STATE_PATH


def read_persisted_exit_count() -> int:
    """The exit tally recorded by previous incarnations of this process."""
    try:
        with open(watchdog_state_path(), encoding="utf-8") as fh:
            return max(0, int(fh.read().strip() or "0"))
    except Exception:
        return 0


def record_persisted_exit() -> int:
    """Increment the durable tally and return the new value.

    Written and flushed to disk before the process exits, because after the
    exit there is nothing left to ask.
    """
    total = read_persisted_exit_count() + 1
    try:
        with open(watchdog_state_path(), "w", encoding="utf-8") as fh:
            fh.write(str(total))
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:  # pragma: no cover - must never block the exit
        logger.warning(
            "could not persist the JDBC watchdog exit tally to %s; recurrence "
            "detection will under-report until it is writable",
            watchdog_state_path(), exc_info=True,
        )
    return total


def seed_exit_counter_from_disk() -> int:
    """Carry the previous tally into this process's counter, at start-up.

    This is what makes the Prometheus series monotonic across a watchdog
    restart rather than sawtoothing back to zero.
    """
    total = read_persisted_exit_count()
    if total:
        try:
            JDBC_WATCHDOG_EXITS.inc(total)
        except Exception:  # pragma: no cover
            pass
    return total


def _emit_watchdog_exit_event(payload: dict[str, Any]) -> None:
    """Emit the machine-readable record of a watchdog kill.

    This is the DURABLE signal, and the reason it is a log line rather than a
    metric: the in-process counter cannot survive the exit it is recording. A
    log-based or orchestration-level counter built on this event is what lets an
    operator distinguish "the wedge never came back" from "the wedge now happens
    every hour and the restart hides it" — the failure mode that made
    'investigate only on recurrence' unsafe until now.

    Serialised as one JSON object on a single line so a log pipeline can match
    and count it without parsing prose. Never raises: the exit must happen.
    """
    try:
        logger.critical("jdbc_watchdog_exit %s", json.dumps(payload, default=str))
    except Exception:  # pragma: no cover - logging must not block the exit
        pass


def _hard_exit(code: int) -> None:
    """Terminate this process with *code*, flushing logs first."""
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:  # pragma: no cover - flushing must never mask the exit
            pass
    os._exit(code)


async def run_jdbc_liveness_watchdog(
    host: str,
    port: int,
    *,
    interval_seconds: float,
    failure_limit: int,
    probe_timeout_seconds: float,
    probe: ProbeFn = probe_jdbc_accept_loop_async,
    on_exit: Callable[[int], None] = _hard_exit,
) -> None:
    """Probe the JDBC accept loop every *interval_seconds*; exit after
    *failure_limit* CONSECUTIVE failures.

    Returns only after ``on_exit`` has been called (the production ``on_exit``
    never returns). Cancel the task to stop it — that is what the gateway's
    lifespan does on shutdown.

    Startup grace: the counter does not run until the listener has been observed
    alive AT LEAST ONCE. A bounded startup window was the alternative; "observed
    alive once" is stronger for the same cost. It needs no tuning against how
    slow a particular host starts, and the case it deliberately declines to act
    on — a listener that never came up at all — is one a restart cannot fix
    (port already in use, unreadable TLS material, a bind failure), so acting on
    it would be a restart loop that never converges. That case is already loud
    without this: ``/health`` answers 503 from the very first probe and the
    compose healthcheck marks the container unhealthy. This watchdog exists for
    a listener that WAS serving and wedged.
    """
    observed_alive = False
    consecutive_failures = 0
    started_monotonic = time.monotonic()

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            alive, detail = await probe(host, port, timeout=probe_timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A probe that cannot even RUN has not proven the accept loop alive,
            # so it counts as a failure — but it must not kill this task. A
            # watchdog that died silently at start-up leaves a gateway with no
            # auto-recovery for its whole life while everyone believes it has
            # some, which is the worst outcome available here. The three-strike
            # damping already covers a transient (a thread-pool hiccup, a
            # momentary fd shortage) without restarting anything.
            alive, detail = False, f"the liveness probe itself raised {exc!r}"
            logger.warning(
                "JDBC liveness probe on port %d raised instead of returning; "
                "counting it as a failed probe", port, exc_info=True,
            )

        if alive:
            if not observed_alive:
                logger.info(
                    "JDBC liveness watchdog armed — the accept loop on port %d "
                    "has been observed alive (%s)", port, detail,
                )
            elif consecutive_failures:
                logger.info(
                    "JDBC accept loop on port %d answered again after %d "
                    "consecutive failure(s); strike count reset to 0 (%s)",
                    port, consecutive_failures, detail,
                )
            observed_alive = True
            consecutive_failures = 0
            continue

        if not observed_alive:
            # Startup grace. WARN, not silence: a listener that never comes up
            # is a real failure, it is simply not one a restart resolves.
            logger.warning(
                "JDBC accept loop on port %d has not answered yet and has "
                "never been observed alive, so the restart watchdog is not "
                "armed (a listener that never bound is not fixed by a "
                "restart — check the port, the TLS material and the startup "
                "log): %s", port, detail,
            )
            continue

        consecutive_failures += 1
        try:
            JDBC_PROBE_FAILURES.inc()
        except Exception:  # pragma: no cover - metrics must never break recovery
            pass
        logger.warning(
            "JDBC accept loop on port %d did not answer the liveness probe "
            "(strike %d of %d, %ss apart): %s",
            port, consecutive_failures, failure_limit, interval_seconds, detail,
        )

        if consecutive_failures >= failure_limit:
            logger.error(
                "JDBC accept loop on port %d proven dead after %d consecutive "
                "liveness failures; exiting with code %d so the container "
                "restart policy recycles this gateway. Last probe result: %s",
                port, consecutive_failures, WATCHDOG_EXIT_CODE, detail,
            )
            _exit_total = record_persisted_exit()
            try:
                JDBC_WATCHDOG_EXITS.inc()
            except Exception:  # pragma: no cover
                pass
            # Bug-9834: the machine-readable record, emitted BEFORE the exit
            # and carrying the state the exit is about to destroy. ``_hard_exit``
            # flushes the handlers, so this reaches the log even though the
            # process does not unwind.
            try:
                _emit_watchdog_exit_event({
                    "event": "jdbc_watchdog_exit",
                    "exit_code": WATCHDOG_EXIT_CODE,
                    "port": port,
                    "consecutive_failures": consecutive_failures,
                    "failure_limit": failure_limit,
                    "probe_interval_seconds": interval_seconds,
                    "last_probe_error": detail,
                    "process_uptime_seconds": round(
                        time.monotonic() - started_monotonic, 3
                    ),
                    "active_jdbc_sessions": _active_jdbc_sessions(),
                    "open_file_descriptors": _open_file_descriptors(),
                    "watchdog_exits_total": _exit_total,
                })
            except Exception:  # pragma: no cover
                # Collecting the diagnostics must never become the reason a
                # wedged gateway is NOT recycled. Losing the record is bad;
                # losing the recovery is worse. The individual collectors guard
                # themselves too — this is the backstop for a future one that
                # forgets, which is the failure this file exists to prevent
                # elsewhere.
                logger.exception(
                    "could not emit the jdbc_watchdog_exit record; exiting anyway"
                )
            on_exit(WATCHDOG_EXIT_CODE)
            return
