"""Single source of truth for the process shutdown time budget (Bug-8041 R7).

Graceful shutdown is NOT a property of any one module. Between the platform
sending ``SIGTERM`` and sending ``SIGKILL`` there is a fixed wall-clock window,
and several independently-configured things have to fit inside it, in this order:

    SIGTERM
      |
      |-- uvicorn drains in-flight HTTP requests/connections
      |     bounded by  --timeout-graceful-shutdown   (request_drain_seconds)
      |     ** with NO flag uvicorn waits FOREVER here and the lifespan
      |        shutdown hook below is never even reached **
      |
      |-- FastAPI lifespan shutdown runs:
      |     * gateway only: drain the JDBC asyncio.Server
      |         bounded by (pre_close_drain_seconds) -- unbounded, this was the
      |         same never-reach-the-pool-close defect one layer further in
      |     * shared.source_pool.close_all_pools()
      |         bounded by SOURCE_POOL_SHUTDOWN_GRACE_SECONDS (pool_grace_seconds)
      |         plus one second for the force-terminate step (terminate_margin)
      |
      |-- interpreter exit / log flush            (platform_margin_seconds)
      v
    SIGKILL  (docker-compose ``stop_grace_period`` / Cloud Run's fixed ~10s,
              or ``docker run --stop-timeout`` for the VM gateway)

Every one of those numbers is DERIVED here from a single operator-facing knob,
``TESSALLITE_SHUTDOWN_BUDGET_SECONDS``, so they can never drift apart. Prior
rounds of Bug-8041 configured them independently -- a hardcoded
``stop_grace_period: 15s`` in compose, a separately-defaulted
``SOURCE_POOL_SHUTDOWN_GRACE_SECONDS``, and no uvicorn flag at all -- which is
the "two independently configured knobs that must agree" defect class this lane
already root-caused once (checkout-grace vs DDL-timeout, R3).

This applies to all FIVE services whose lifespan shutdown calls
``close_all_pools``: model-service, query-router, optimizer, scheduler AND the
gateway. The gateway was omitted from the first version of this model, which is
why the list is now derived from the code by a test rather than hand-written.

Consumers (all derive, none re-invent):

- ``tessallite/services/*/Dockerfile`` (docker-compose builds) AND
  ``tessallite/infra/cloud-run/Dockerfile.*`` (the ones
  ``deploy/gcp/steps/06_build.sh`` actually builds for GCP) --
  ``uvicorn --timeout-graceful-shutdown
  "$(python -m shared.config.shutdown_budget request_drain_seconds)"``.
  Both sets must carry it; patching only one reaches only one platform.
- ``services/gateway/src/main.py`` -- sets ``timeout_graceful_shutdown`` on its
  in-process ``uvicorn.run``, and bounds its DAX session-store flush + JDBC
  listener drain by ``pre_close_drain_seconds``.
- ``services/model-service/src/main.py`` -- bounds its licence-beacon stop by the
  same slice.
- ``shared/source_pool.py::_pool_shutdown_grace_seconds`` -- the pool close
  grace, with ``SOURCE_POOL_SHUTDOWN_GRACE_SECONDS`` retained as an override
  that is CLAMPED DOWN to what the budget allows (raising it past the budget
  used to silently reintroduce the SIGKILL-mid-close race).
- ``tessallite/infra/docker-compose.yml`` and
  ``deploy/community/docker-compose.yml`` -- ``stop_grace_period`` is
  ``${TESSALLITE_SHUTDOWN_BUDGET_SECONDS:-15}s``.
- ``deploy/gcp/steps/07_services.sh`` -- exports the budget as 10 (Cloud Run
  fully-managed has a FIXED ~10s SIGTERM->SIGKILL window and no knob for it)
  and passes it into every Cloud Run manifest.
- ``deploy/gcp/db-vm/db-vm-deploy-gateway.sh`` -- the VM gateway's ``docker run``
  overrides the image CMD, so it passes both ``--stop-timeout`` and uvicorn's
  ``--timeout-graceful-shutdown`` itself.

Stdlib only, no logging side effects at import: this module is executed as a
``python -m`` one-liner during container start-up, before anything is
configured.
"""
from __future__ import annotations

import math
import os
import sys
from typing import NamedTuple

#: Operator-facing knob: total SIGTERM -> SIGKILL window granted by the platform.
BUDGET_ENV_VAR = "TESSALLITE_SHUTDOWN_BUDGET_SECONDS"
#: Compose default. Must equal the ``stop_grace_period`` default in both
#: docker-compose files (a unit test pins this).
DEFAULT_BUDGET_SECONDS = 15
#: Smallest total the phase split can actually satisfy: 1s drain + 1s pool close
#: + 1s terminate + 1s exit margin. Below this the invariant
#: ``worst_case <= total`` is arithmetically impossible, so the value is clamped
#: UP -- and because docker-compose interpolates the operator's RAW value into
#: ``stop_grace_period``, a clamp is a silent divergence between the window the
#: platform grants and the window the process plans for. ``budget_is_below_minimum``
#: exists so every consumer can shout about it instead of drifting quietly.
MIN_BUDGET_SECONDS = 4
#: Symmetric with the other pool-lifecycle ceilings in this codebase.
MAX_BUDGET_SECONDS = 300
#: Seconds reserved for the synchronous ``pool.terminate()`` step that runs
#: after the pool close grace expires.
TERMINATE_MARGIN_SECONDS = 1
#: Upper bound on the slice reserved for interpreter exit / log flush.
MAX_PLATFORM_MARGIN_SECONDS = 5


class ShutdownBudget(NamedTuple):
    """A fully-derived, mutually-consistent shutdown time allocation."""

    total_seconds: int
    request_drain_seconds: int
    pre_close_drain_seconds: int
    pool_grace_seconds: int
    terminate_margin_seconds: int
    platform_margin_seconds: int

    @property
    def worst_case_seconds(self) -> int:
        """Longest wall time the shutdown sequence can take, end to end.

        Every phase a service can spend time in MUST be counted here, or the
        "fits inside the platform window" invariant is checked against an
        incomplete model. ``pre_close_drain_seconds`` exists because the gateway
        has a second, non-HTTP drain phase (its JDBC ``asyncio.Server``) inside
        the lifespan shutdown; funding it from the same ``usable`` pool rather
        than adding it on top is what keeps the invariant true for that service.
        """
        return (
            self.request_drain_seconds
            + self.pre_close_drain_seconds
            + self.pool_grace_seconds
            + self.terminate_margin_seconds
            + self.platform_margin_seconds
        )


def _raw_budget_seconds() -> int:
    """Read + clamp the operator knob. Never raises; never logs."""
    raw = os.getenv(BUDGET_ENV_VAR)
    if raw is None or raw.strip() == "":
        return DEFAULT_BUDGET_SECONDS
    try:
        val = int(raw.strip())
    except (TypeError, ValueError):
        return DEFAULT_BUDGET_SECONDS
    return max(MIN_BUDGET_SECONDS, min(val, MAX_BUDGET_SECONDS))


#: Set by a service that has EXTRA work inside its lifespan shutdown BEFORE
#: ``close_all_pools()`` (gateway: DAX session-store flush + JDBC
#: ``asyncio.Server`` drain; model-service: licence-beacon stop).
#: Bug-8041 R7 round-2 F3 -- this second used to be carved out of every
#: service's budget, so services that never execute such a phase silently lost a
#: second of pool-close grace (on Cloud Run: 4s -> 3s, a 25% cut funding a phase
#: they don't have). Bug-8041 R7 round-4 F2 -- renamed from "listener" because
#: the gateway-specific name led straight to model-service's ``beacon.stop()``
#: being left unbounded AND unfunded.
#: A process-scoped declaration rather than an env var: it is a property of the
#: code that is running, not an operator choice, and keeping it out of the
#: environment means it cannot drift from the deployment config.
#:
#: KNOWN OVER-DECLARATION (Bug-8041 R8 review round 6, accepted): model-service
#: declares unconditionally, but its pre-close phase is the licence-beacon stop
#: and ``build_beacon_emitter()`` returns None unless ``LICENSE_BEACON_URL`` is
#: configured -- which it is not in ``.env.example``, ``infra/docker-compose.yml``
#: or the Cloud Run manifest. So in a default deployment model-service funds one
#: second it never spends, taking its Cloud Run pool grace from 4s to 3s. The
#: budget invariant still holds (nothing overruns), and the alternative --
#: declaring conditionally on an env var -- makes the declaration a runtime
#: choice again, which is exactly what this flag exists to avoid. Tracked
#: separately rather than traded for that.
_has_pre_close_drain = False


def declare_pre_close_drain() -> None:
    """Declare that THIS process does bounded work before ``close_all_pools()``.

    Any service whose lifespan shutdown awaits something before closing source
    pools MUST call this, or the budget funds that phase zero seconds and the
    time it takes is stolen from the pool close.

    Must be called before the first ``resolve_shutdown_budget()`` that feeds a
    real timeout -- i.e. at import/startup, not lazily. Idempotent.
    """
    global _has_pre_close_drain
    _has_pre_close_drain = True


def reset_pre_close_drain_for_tests() -> None:
    """Test-only: clear the process-scoped declaration."""
    global _has_pre_close_drain
    _has_pre_close_drain = False


def budget_is_malformed() -> bool:
    """Whether the budget env var is set but NOT parseable as an integer.

    Bug-8041 R7 round-5 F4. ``_raw_budget_seconds`` swallows the ``ValueError``
    and returns the COMPOSE default, and ``budget_is_below_minimum`` is False for
    a non-numeric value -- so a malformed budget was completely silent. That is
    how an unsubstituted deploy placeholder becomes a live defect: two of the
    three GCP Cloud Run renderers did not substitute
    ``${TESSALLITE_SHUTDOWN_BUDGET_SECONDS}``, and a placeholder that no renderer
    rule matches survives VERBATIM in the rendered manifest (``envsubst`` leaves
    an unlisted ``${VAR}`` alone; a cmd/PowerShell ``-replace`` chain that has no
    rule for it likewise never touches it). The container therefore received the
    literal ``${...}`` string, silently planned for 15s, and was SIGKILLed inside
    Cloud Run's fixed ~10s window with no log line anywhere.

    Bug-8041 R8 correction: an earlier version of this note also claimed that an
    UNDEFINED ``%VAR%`` in cmd expands to the literal ``%VAR%``. That is true at
    an interactive cmd prompt but FALSE inside a batch file, where it expands to
    the empty string -- verified by executing the real renderer. The defect above
    is real; only that one mechanism claim was wrong, and it is corrected here so
    the next round does not "fix" a non-existent bug on the strength of it.

    Below-minimum is loud; malformed must be too, or the next renderer gap is
    just as invisible as this one was.
    """
    raw = os.getenv(BUDGET_ENV_VAR)
    if raw is None or raw.strip() == "":
        return False
    try:
        int(raw.strip())
    except (TypeError, ValueError):
        return True
    return False


def budget_is_below_minimum() -> bool:
    """Whether the operator configured a budget SMALLER than the split can honour.

    This is the one case where the derived numbers and the platform window
    genuinely disagree: ``resolve_shutdown_budget`` clamps up to
    ``MIN_BUDGET_SECONDS`` (below it the invariant is arithmetically
    unsatisfiable), but docker-compose interpolates the operator's RAW value
    into ``stop_grace_period``. So ``TESSALLITE_SHUTDOWN_BUDGET_SECONDS=2``
    yields a process budgeting 4s inside a 2s SIGKILL window -- the exact
    "two knobs that disagree" failure this module exists to remove, just from
    the other direction. Consumers use this to fail LOUDLY instead of drifting
    silently; there is no arithmetic fix, only a visible one.
    """
    raw = os.getenv(BUDGET_ENV_VAR)
    if raw is None or raw.strip() == "":
        return False
    try:
        return int(raw.strip()) < MIN_BUDGET_SECONDS
    except (TypeError, ValueError):
        return False


def budget_diagnostics() -> list[tuple[str, str]]:
    """Every operator-facing problem with the CURRENT budget configuration.

    Returns ``[(key, message), ...]``. The key is a stable, process-lifetime
    identifier so a consumer with warn-once semantics (the pool manager's hot
    path) can dedup on it without restating the message.

    Bug-8041 R8 (6th external gate, MEDIUM). "Malformed must be loud too" was
    added in R7 round 5 and then hand-copied into the Dockerfile ``--check`` mode
    and the pool manager's runtime warn-once path -- but NOT into the gateway's
    ``python -m src.main`` entrypoint, which is a second start-up diagnostic
    written in an earlier round. Three independent copies of one property is the
    same defect class this lane already root-caused twice (two independently
    configured knobs that must agree); the fix is the same one: ONE
    implementation that every start-up path consumes, so a new path cannot ship
    half the checks and a new check cannot miss a path.

    ``test_every_budget_predicate_has_a_diagnostic`` derives the predicate list
    from this module's own namespace, so adding a ``budget_is_*`` check without
    wiring it in here fails rather than shipping silently.

    Bug-8041 R8 review finding 7: ``shared/source_pool.py`` used to carry a
    FOURTH hand-written copy of these messages, so the "one implementation"
    claim was not yet true and a new predicate would still have missed the pool
    manager's runtime path. It now consumes this function too.
    """
    messages: list[tuple[str, str]] = []
    if budget_is_malformed():
        messages.append((
            "malformed",
            f"{BUDGET_ENV_VAR}={os.getenv(BUDGET_ENV_VAR)!r} is not an integer, "
            f"so this process silently fell back to the compose default of "
            f"{DEFAULT_BUDGET_SECONDS}s. On Cloud Run (~10s window) that is a "
            f"SIGKILL mid-pool-close with source connections still open. The "
            f"usual cause is a deploy renderer that did not substitute the "
            f"placeholder -- check the envsubst whitelist / cmd -replace chain "
            f"for {BUDGET_ENV_VAR}."
        ))
    if budget_is_below_minimum():
        messages.append((
            "below_minimum",
            f"{BUDGET_ENV_VAR}={os.getenv(BUDGET_ENV_VAR)!r} is below the minimum "
            f"workable budget ({MIN_BUDGET_SECONDS}s). This process will plan for "
            f"{resolve_shutdown_budget().total_seconds}s of shutdown, but the "
            f"platform (docker-compose stop_grace_period / docker run "
            f"--stop-timeout) uses your RAW value -- so it will SIGKILL "
            f"mid-shutdown and leave source connections open. Raise "
            f"{BUDGET_ENV_VAR} to at least {MIN_BUDGET_SECONDS}."
        ))
    return messages


def log_budget_diagnostics(log) -> list[str]:
    """Emit :func:`budget_diagnostics` through a ``logging.Logger`` at ERROR.

    For in-process entrypoints (``python -m src.main``) that have a logger
    configured but never run the Dockerfile CMD's ``--check`` step. Returns what
    was emitted so a caller/test can assert on it."""
    messages = [message for _key, message in budget_diagnostics()]
    for message in messages:
        log.error("%s", message)
    return messages


def resolve_shutdown_budget(total_seconds: int | None = None) -> ShutdownBudget:
    """Split the total SIGTERM->SIGKILL window into its ordered phases.

    ``total_seconds`` is for tests / tooling; production reads
    ``TESSALLITE_SHUTDOWN_BUDGET_SECONDS``.

    The split guarantees ``worst_case_seconds <= total_seconds`` -- i.e. the
    whole sequence finishes before the platform SIGKILLs the container, which is
    the property that makes "no pool is left in an uncontrolled state" true in
    production rather than only in a unit test.
    """
    if total_seconds is None:
        total = _raw_budget_seconds()
    else:
        total = max(MIN_BUDGET_SECONDS, min(int(total_seconds), MAX_BUDGET_SECONDS))

    platform_margin = min(MAX_PLATFORM_MARGIN_SECONDS, max(1, math.ceil(total * 0.10)))
    usable = total - platform_margin - TERMINATE_MARGIN_SECONDS
    # MIN_BUDGET_SECONDS=4 with platform_margin=1 and terminate_margin=1 leaves
    # usable=2, so both mandatory slices are >= 1 without a special case.
    request_drain = max(1, usable // 2)
    remaining = usable - request_drain
    # The extra pre-close drain is funded from `usable` -- never added on top --
    # so its presence cannot push any service past the platform window. It is
    # only allocated when there is room left for a pool grace too; at the
    # smallest budgets the phase gets no drain and is cancelled outright.
    # Bug-8041 R7 round-2 F3: and ONLY for a process that actually has that
    # phase, so the other four services keep the second for their pool close.
    pre_close_drain = 1 if (_has_pre_close_drain and remaining >= 2) else 0
    pool_grace = max(1, remaining - pre_close_drain)
    return ShutdownBudget(
        total_seconds=total,
        request_drain_seconds=request_drain,
        pre_close_drain_seconds=pre_close_drain,
        pool_grace_seconds=pool_grace,
        terminate_margin_seconds=TERMINATE_MARGIN_SECONDS,
        platform_margin_seconds=platform_margin,
    )


_FIELDS = (
    "total_seconds",
    "request_drain_seconds",
    "pre_close_drain_seconds",
    "pool_grace_seconds",
    "terminate_margin_seconds",
    "platform_margin_seconds",
)


def main(argv: list[str] | None = None) -> int:
    """Print one derived field so a container CMD can interpolate it.

    Used by the service Dockerfiles to derive ``--timeout-graceful-shutdown``
    from this module instead of hardcoding a second, drift-prone constant.
    Fails OPEN to the default budget (with a stderr note) rather than printing
    nothing: an empty ``$(...)`` would make uvicorn reject its own CLI and
    crash-loop the container, and the fallback is still a FINITE bound, which is
    the property that matters.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    field = args[0] if args else "request_drain_seconds"
    if field == "--check":
        # Bug-8041 R7 round-2 F2: DIAGNOSTIC-ONLY mode.
        # The value-printing mode is always invoked as ``$(... 2>/dev/null)``,
        # because a traceback on stderr would be harmless but a traceback on
        # STDOUT would poison the command substitution. That redirect also threw
        # away the below-minimum ERROR, so the "impossible to miss" warning was
        # in practice impossible to SEE on every real container-start path.
        # This mode prints nothing to stdout, so callers can run it WITHOUT any
        # redirect, and it never fails the start-up command.
        # Bug-8041 R8: the message set comes from ``budget_diagnostics()`` -- the
        # single implementation every start-up path shares.
        for _key, message in budget_diagnostics():
            print(f"ERROR: {message}", file=sys.stderr)
        return 0
    if field not in _FIELDS:
        print(f"unknown shutdown-budget field {field!r}; known: {', '.join(_FIELDS)}",
              file=sys.stderr)
        return 2
    try:
        budget = resolve_shutdown_budget()
    except Exception as exc:  # pragma: no cover - stdlib arithmetic cannot fail
        print(f"shutdown-budget resolution failed ({exc}); using defaults",
              file=sys.stderr)
        budget = resolve_shutdown_budget(DEFAULT_BUDGET_SECONDS)
    # Runs once per container start, so this lands at the top of the service log
    # where an operator will actually see it -- when the caller has not redirected
    # stderr. The ``--check`` mode above exists precisely because the value mode is
    # always invoked as ``$(... 2>/dev/null)``.
    for _key, message in budget_diagnostics():
        print(f"ERROR: {message}", file=sys.stderr)
    print(getattr(budget, field))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    raise SystemExit(main())
