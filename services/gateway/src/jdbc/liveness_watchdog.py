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
import logging
import os
from typing import Awaitable, Callable

from shared.gateway_liveness import probe_jdbc_accept_loop_async

logger = logging.getLogger(__name__)

__all__ = ["WATCHDOG_EXIT_CODE", "run_jdbc_liveness_watchdog"]

# Deliberately a constant, not a setting. The interval and the strike limit are
# operational tunables; this is a diagnostic contract. It is what tells an
# operator reading `docker inspect` or a Kubernetes `lastState.terminated`
# whether the gateway killed itself over a dead accept loop or was stopped
# cleanly, and it is the only thing a `restart: on-failure` policy acts on.
# Making it settable would let a deployment set it to 0 and erase that signal.
WATCHDOG_EXIT_CODE = 3

ProbeFn = Callable[..., Awaitable[tuple[bool, str]]]


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
            on_exit(WATCHDOG_EXIT_CODE)
            return
