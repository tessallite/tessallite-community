"""Connection pool manager for source PostgreSQL databases.

Pools are keyed by ``(scope, host, port, database, user, password-hash)`` and
created lazily on first use.  BigQuery and Spark connections are not pooled —
they use HTTP/Thrift transports with their own session management.

Tenant isolation (F-014-05 / Bug-8039): the ``scope`` component is a stable
tenant/connection identity supplied by the caller and is the FIRST part of the
key. Without it, two tenants that configured the SAME managed-database login
(identical host/port/db/user/password) collapsed onto one shared pool, so a
pooled, already-authenticated connection could be handed to a different tenant
(session-state reuse) and one tenant's long-running workload could starve
another. Scoping the pool by tenant makes cross-tenant hand-out structurally
impossible: each tenant gets its own pool even against an identical login.
Every pooled connection is additionally reset to a clean session on hand-out
(``_reset_session`` below) so no ``search_path``/GUC/role/temp state survives
across acquisitions.

Connector pooling status (Bug-7154 / Bug-7155):

- **PostgreSQL / Redshift**: pooled via asyncpg below. Credential rotation is
  handled by including a password fingerprint in the pool key (F-014-06) so a
  rotated credential automatically routes to a new pool. A *changed* credential
  therefore ages the stale pool out via the idle reaper (no more callers reach
  it). A *remotely revoked* credential on a pool that stays in active use is a
  different case: the idle reaper never fires (``last_used`` keeps refreshing),
  so the enforced revocation bound is instead the security-age max-lifetime cap
  (``_pool_max_lifetime_seconds``, Bug-8040/Bug-8041) — a finite, positive
  wall-clock age (enabled by default and clamped to a 24h ceiling; it can be
  fully disabled ONLY behind the explicit, non-default, non-production opt-in
  ``SOURCE_POOL_MAX_LIFETIME_ALLOW_UNBOUNDED_UNSAFE`` — without that flag a
  ``<=0`` override is rejected and the finite default is used) after which the
  pool is retired and its in-flight connections force-terminated (via
  ``_pool_security_max_checkout_seconds``) regardless of use. A periodic
  background sweep task (Bug-8041 R3 Finding 1,
  ``_lifecycle_sweep_loop``, interval ``SOURCE_POOL_SWEEP_INTERVAL_SECONDS``,
  default 60s) enforces both security-age and idle deadlines proactively,
  independent of new acquisitions — so a pool whose credential is revoked and
  receives no new traffic is still retired within ``max_lifetime +
  sweep_interval + checkout_grace`` (worst case ~76 minutes at defaults).
  Security-age retirement takes precedence over idle reaping whenever both
  conditions could apply (see ``_reap_idle_pools_locked``).
- **BigQuery**: not pooled here. The Google BigQuery Python SDK manages its own
  HTTP/2 connection multiplexing internally; per-query ``Client()`` construction
  carries less overhead than it appears. Credentials are decrypted per call, so
  credential rotation takes effect on the next query.
- **Spark / Hive**: not pooled. PyHive Thrift connections are inherently
  lightweight and stateless per cursor. Credentials are decrypted per call.
- **Snowflake**: not pooled. Each ``snowflake.connector.connect()`` performs a
  full OAuth/key-pair handshake, adding 200-500ms per query. Credentials are
  decrypted per call, so rotation takes effect on the next query. Pooling would
  reduce latency for busy tenants but requires an architectural decision about
  the pooling library and session keep-alive strategy.
- **SQL Server**: not pooled. Each ``aioodbc.connect()`` opens a TCP + TLS
  handshake. Credentials are decrypted per call, so rotation takes effect on
  the next query. ODBC-level pooling (driver-managed) is available but
  requires platform-specific configuration.

For all non-PG connectors, credential rotation is safe for the query path
because credentials are decrypted from the encrypted blob on every call.
The ``open_source_connection`` persistent wrapper (``SourceConnection``) holds
the decrypted client for the lifetime of the context manager; if credentials
are rotated mid-batch, the in-memory client retains the old credential until
the context exits. Long-running batch operations should handle auth errors
with a retry that re-decrypts credentials.

Credential lifecycle (F-014-06): asyncpg authenticates at connect time, so a
pool created with an old password keeps working until the source revokes that
password.  Because pools live in per-replica process memory, an API-triggered
purge cannot reach every replica.  Including a short hash of the password in
the key makes a credential change route to a *new* pool automatically; the
stale pool then simply ages out via the idle reaper below.  This is the only
replica-safe invalidation strategy (no cross-process purge needed) for a
CHANGED credential. For a REVOKED credential still receiving continuous
traffic (the case Bug-8040/Bug-8041 close), see the security-age max-lifetime
cap described above — it is the hard bound, not the idle reaper.

Shutdown (Bug-8041 R7): "shutdown completes before the platform SIGKILLs us,
with no pool left in an uncontrolled state" is NOT a property this module can
hold on its own. Three things outside it decide whether ``close_all_pools`` even
gets to run: uvicorn's ``--timeout-graceful-shutdown`` (with no flag it waits
INDEFINITELY for in-flight requests before invoking the lifespan shutdown hook
that calls ``close_all_pools``), the platform's SIGTERM-to-SIGKILL window
(``stop_grace_period`` in compose, a fixed ~10s on Cloud Run), and this module's
own close grace. All three are now DERIVED from one knob,
``TESSALLITE_SHUTDOWN_BUDGET_SECONDS`` — see ``shared/config/shutdown_budget.py``
for the phase split and ``_pool_shutdown_grace_seconds`` below for this module's
share. ``close_all_pools`` additionally waits for in-flight pool CREATIONS, not
just already-scheduled closes, because a creator still inside
``asyncpg.create_pool`` at SIGTERM would otherwise take ownership of a live
authenticated pool after shutdown had already returned.

WHAT ``close_all_pools()`` ACTUALLY GUARANTEES (Bug-8041 R8 — stated precisely
because the previous wording implied more than any bounded function can deliver,
and the 6th external gate correctly called that out). The absolute form ("no
source connection is alive once this returns") is NOT achievable: it contradicts
the hard requirement that the call RETURN inside a fixed platform SIGTERM window.
A creator suspended inside a third-party coroutine has no cancellation latency we
control, so a bounded waiter can never assert an unbounded-latency dependency has
finished. The guarantee is therefore two-part, and both parts are enforced:

1. ``close_all_pools()`` ALWAYS returns inside its slice of the shutdown budget.
   It never blocks on a borrower, a close, or a creator beyond that deadline.
2. Every source connection this process still OWNS — i.e. reachable through a
   pool's connection holders — ends in exactly one of THREE states, and the
   third is part of the claim, not a caveat added later:
     (a) CLIENT-SIDE terminated by this call;
     (b) inside a running bounded close that terminates it on its own grace;
     (c) genuinely undisposable — the pool-level terminate was refused AND the
         holder-level kill failed, or a "clean" close left holders open. That is
         recorded in ``_undisposed_pools`` and reported by COUNT in a WARNING
         this call emits BEFORE it returns, with a per-pool
         ``Force-terminating source pool ... FAILED`` line naming each one.
         The one thing it must never be is silent.
   The split below says which is which: the pools this function owns
   DIRECTLY -- both cached pools and in-flight creations -- are force-terminated
   at the deadline before it returns; the ones already running as
   ``_pending_closes`` tasks are terminated by their own bounded grace, which at
   the derived split lands about a second INSIDE this function's deadline but is
   not guaranteed to. There are THREE populations, not two:
     - pools cached in ``_pools``, closed via ``_close_pool`` and, if that gather
       has not finished when the deadline expires, force-terminated in the
       deadline branch (Bug-8041 R8 review round 6: leaving them to their own
       close task's grace was a timing coincidence, not a guarantee -- a cached
       pool whose ``close()`` ignores cancellation kept 2 authenticated holders
       open past the return, and the WARNING reported 0 undisposed);
     - in-flight CREATIONS, disposed of at the deadline by
       ``_abandon_unsettled_creations``;
     - pools already popped out of ``_pools`` and running inside
       ``_retire_pool_secure``/``_close_pool`` as ``_pending_closes`` tasks.
       ``close_all_pools`` WAITS on these but does not dispose of them itself;
       each one is bounded by its own grace and force-terminates itself, which
       can complete just after this function returns (see the third sub-bullet
       below). They observe the shutdown flag, so that grace is the SHORT
       shutdown grace, not the hours-long security-age one.
   The first two go through ``_force_terminate_pool``. New creation is refused
   from the moment the flag is set, so nothing can be installed behind
   shutdown's back.

   Two disposal paths are outside all three TRACKED populations, both bounded by
   the same shutdown grace and neither reachable on a normal shutdown
   (Bug-8041 R8 review round 4, named here so no scope note contradicts the
   headline):
     - ``_terminate_when_settled``'s DEFERRED branch. Both call sites
       ``await asyncio.wait({init_task})`` first, so the immediate branch is
       taken; the deferred one needs that await itself to be cancelled (the
       re-delivered-cancellation case). If it is, the creator's ``finally`` can
       release ``close_all_pools`` before the callback fires.
     - ``remove_pool``, which awaits ``_close_pool`` inline rather than through
       ``_schedule_pool_task``, so its victim is in no tracked set. It has no
       production caller today.

   Part 2 says "client-side" and "owns" deliberately; both words were measured,
   not assumed (Bug-8041 R8 review round 2):

   - A source BACKEND that is mid-statement does not die when we abort the
     socket. PostgreSQL reaps it when that statement finishes and the write
     fails. Measured: a pool with one checked-out connection running
     ``pg_sleep(30)`` goes from 2 backends to 1 across a successful
     force-terminate. That remaining backend is bounded — by the per-checkout
     server-side ``statement_timeout`` that ``source_executor`` sets, NOT by this
     call. See ``_retire_pool_secure`` for the same statement in its own scope.
   - A connection an initializer has opened but not yet handed to a holder is
     invisible to us, and therefore counted as disposed. Measured reachable only
     by patching asyncpg (this codebase passes neither ``init=`` nor ``connect=``,
     and stock ``_get_new_connection`` has no await between connecting and
     attaching: 0 of 7 samples of a real initialisation showed a gap). So the
     WARNING's "0 still open" means "nothing we could see", not "nothing alive"
     — it is only wrong jointly with the non-cooperating-initializer residual
     below, which is the same scenario the deadline branch exists for.
   - A ``_close_pool`` scheduled very late into the drain starts its own grace
     clock then, so it can outlive the shared deadline; it force-terminates
     itself, but after this function has returned. Creators resume within
     milliseconds of the first await, so this is not reachable in practice.

Part 2 is only true because of ``_force_terminate_pool``. The obvious
implementation — ``pool.terminate()`` — does NOT deliver it: asyncpg routes
``Pool.terminate()`` through ``_check_init()`` and REFUSES it while the pool is
``_initializing``, which is the only state a creator suspended inside
``create_pool`` can be in. Every ``pool.terminate()`` fallback in this module was
therefore a silent no-op in exactly the window it was written for, and a live
probe against real asyncpg + real PostgreSQL left 2 authenticated backends open
after ``close_all_pools()`` returned. ``PoolConnectionHolder.terminate()`` has no
such guard, so the helper drops to the holder level when the pool-level call is
refused; the same probe then ends with 0 open backends, still inside the budget.

The remaining residual is narrow and outside this module: we kill the connections
that EXIST when we run. A pool initializer that ignored cancellation could open
MORE afterwards. Real asyncpg's ``_initialize`` propagates cancellation, so it
does not — and no bounded function could constrain a third-party coroutine that
refuses to stop. The function logs a WARNING whenever the deadline is hit OR any
pool ends undisposable — the two are INDEPENDENT, and gating the second on the
first is how a reproduced two-connection leak once returned in silence.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

import asyncpg

from shared.config.bootstrap import system_snapshot_get
from shared.config.ddl_timeout import get_effective_ddl_timeout_seconds
from shared.config.shutdown_budget import (
    budget_diagnostics,
    resolve_shutdown_budget,
)

logger = logging.getLogger(__name__)

# value: (pool, last_used_monotonic, created_at_monotonic)
_pools: dict[str, tuple[asyncpg.Pool, float, float]] = {}
# Bug-8039 hardening: a LOG-SAFE descriptor per pool (tenant/project/conn scope +
# a random instance id). The pool key itself carries the password fingerprint and
# MUST NEVER be logged; logs use this descriptor via ``_pool_log_label`` instead.
_pool_desc: dict[str, str] = {}
# Bug-7177: per-key creation futures so different keys can initialize
# concurrently while same-key callers share one in-flight creation.
_pending_creates: dict[str, asyncio.Future] = {}
_lock = asyncio.Lock()


class _PoolCreationHandle:
    """Handle on an IN-FLIGHT pool creation (Bug-8041 R7 HIGH-1).

    ``_pending_creates`` tracks the shared FUTURE for a key, which
    ``close_all_pools`` resolves and discards. That is not the same thing as the
    creator TASK, which keeps running inside ``asyncpg.create_pool``: before this
    handle existed, shutdown resolved/cleared the futures and returned while a
    creator was still connecting, and that creator then installed (or, once the
    install guard was added, opened and separately closed) a live pool AFTER the
    process believed it had shut down.

    The creator registers a handle under ``_lock`` in the SAME critical section
    that reserves the key, and sets ``finished`` in a ``finally`` that covers its
    entire body -- including the install-guard close scheduling -- so a handle
    that is ``finished`` guarantees the creator has fully relinquished ownership
    of any pool it opened.
    """

    # Bug-8041 R7 round-2 F7: no ``key`` attribute -- it was set and never read.
    __slots__ = ("finished", "pool")

    def __init__(self) -> None:
        self.finished = asyncio.Event()
        # The Pool object, recorded as soon as it exists so a shutdown abort can
        # force-terminate connections a partially-initialised pool already opened.
        # Bug-8041 R8: also what ``close_all_pools`` disposes of when its deadline
        # expires with this creator still unsettled -- ``pool is None`` there means
        # no connection can exist yet, so there is nothing to terminate.
        self.pool: object | None = None


# Bug-8041 R7 HIGH-1: every in-flight pool creation, so ``close_all_pools`` can
# WAIT for creators to finish instead of only draining already-scheduled closes.
# Registration happens under ``_lock`` and is refused once ``_shutting_down`` is
# set, so a snapshot taken inside the shutdown lock section is complete: no new
# creation can appear afterwards.
_inflight_creates: set[_PoolCreationHandle] = set()
# Fable FINDING-5: strong references to in-flight close tasks so they survive
# GC. Without this, `asyncio.create_task` returns the only reference and a GC
# pass between creation and completion can drop it, leaking unclosed pools.
_pending_closes: set[asyncio.Task] = set()
# Bug-8041 R8 review round 8: pools ``_force_terminate_pool`` could NOT dispose
# of, keyed by object identity so a repeat attempt on the same pool collapses.
# ``close_all_pools`` reports its overrun count from here rather than from its
# own call sites: FOUR of the seven callers discard the boolean (``_close_pool``
# x2, ``_retire_pool_secure``, ``_close_pool_within_grace``), so a pool
# ``_close_pool`` already failed to dispose of was reported as 0 undisposed
# while ``_force_terminate_pool``'s own log said 2 connections were still open.
_undisposed_pools: dict[int, str] = {}
# Bug-8041 R3 Finding 1: periodic lifecycle sweep task. Without a clock-driven
# sweeper, the reaper in ``_reap_idle_pools_locked`` only runs opportunistically
# when a new ``_get_or_create_pool`` call happens — if no new acquire reaches a
# pool whose credential was revoked, that pool sits indefinitely with the revoked
# credential still usable by whoever is already holding a checked-out connection.
# The sweep task runs at ``_POOL_SWEEP_INTERVAL_SECONDS`` (configurable) and
# enforces both security-age and idle deadlines proactively.
_lifecycle_sweep_task: asyncio.Task | None = None
# Bug-8041 R4: flag set by the sweep task's done-callback to signal that the
# task exited (cancelled, crashed, etc.) and the next
# ``_ensure_lifecycle_sweep_running()`` call must unconditionally restart it --
# this closes the gap where ``task.done() == True`` but the stale reference
# kept ``_ensure_lifecycle_sweep_running`` from detecting the death during
# steady-state reuse traffic (the reuse path now calls the function, but the
# done() check alone is sufficient only when the global reference is still the
# same task object).
_lifecycle_sweep_needs_restart: bool = False
# Bug-8041 R4 MEDIUM-1: shutdown flag, checked under ``_lock``.  Once set:
# (a) ``_retire_pool_secure`` uses the short shutdown grace, not the DDL-derived
#     security-age grace;
# (b) ``_get_or_create_pool`` rejects new pool creation attempts;
# (c) ``_ensure_lifecycle_sweep_running`` is a no-op.
_shutting_down: bool = False
# Bug-8041 R6 HIGH-1: an event that an in-flight retirement's grace-wait can be
# interrupted by the moment shutdown begins, so a retirement already running when
# SIGTERM arrives switches from the long DDL-derived grace to the short shutdown
# grace instead of keeping the (possibly hours-long) grace it sampled at entry.
# Created lazily and rebound per running loop so a module-global Event cannot
# bleed across pytest-asyncio's per-test loops.
_shutdown_event: asyncio.Event | None = None
_shutdown_event_loop: object | None = None
_POOL_SWEEP_INTERVAL_DEFAULT_SECONDS = 60
# Bug-8041 R6 MEDIUM-3: bound the sweep self-heal so a sweep coroutine that exits
# before its first await cannot spin the restart loop at tens of thousands of
# restarts per second.  A non-zero backoff plus a rolling-window restart budget
# turns an otherwise-invisible tight loop into a bounded, logged failure.
_SWEEP_RESTART_BACKOFF_SECONDS: float = 1.0
_SWEEP_RESTART_MAX: int = 5
_SWEEP_RESTART_WINDOW_SECONDS: float = 60.0
_sweep_restart_times: list[float] = []
# Bug-8041 R7 MEDIUM-1: the give-up message is emitted once per exhaustion, not
# once per blocked restart attempt (the acquire path can call the ensure
# function on every single pooled acquisition).
_sweep_restart_budget_exhausted: bool = False
# Bug-8041 R6 MEDIUM-5: the creator-cancellation waiter retry is bounded so
# repeated creator cancellations cannot recurse without limit.
_POOL_CREATE_MAX_ATTEMPTS = 5
# Bug-8041 R6 HIGH-2 (defense-in-depth): a waiter joined on the shared creation
# future must never be ABLE to hang forever.  The wait is bounded (see
# ``_pool_create_wait_timeout_seconds``) so even a future regression that fails
# to resolve the future surfaces a clear timeout error rather than a permanent
# hang.  Absolute floor for that derived bound, in seconds.
_POOL_CREATE_WAIT_FLOOR_SECONDS = 120.0
# Upper bound so the anti-hang guarantee holds even under an absurd connect
# timeout (24h, symmetric with the other pool-lifecycle ceilings).
_POOL_CREATE_WAIT_CEILING_SECONDS = 86400.0


def _get_shutdown_event() -> asyncio.Event:
    """Return the shutdown-signal event bound to the CURRENT running loop.

    Rebinds if the running loop changed (pytest-asyncio uses a fresh loop per
    test), so ``.set()``/``.wait()`` never raise "bound to a different event
    loop".  The authoritative shutdown state is the ``_shutting_down`` flag; this
    event is only a wake-up so an in-flight retirement can observe the flag flip
    without polling."""
    global _shutdown_event, _shutdown_event_loop
    loop = asyncio.get_running_loop()
    if _shutdown_event is None or _shutdown_event_loop is not loop:
        _shutdown_event = asyncio.Event()
        _shutdown_event_loop = loop
    return _shutdown_event

class _CreatorCancelledRetry(Exception):
    """Internal signal: the pool creator task was cancelled mid-creation.

    Bug-8041 R4 MEDIUM-2: published onto the shared creation future INSTEAD of
    the creator's own ``CancelledError`` so uninvolved waiters get a retryable
    signal rather than an unrecoverable cancellation.  The waiter path catches
    this and starts a fresh ``_get_or_create_pool`` attempt -- either becoming
    the new creator or joining another in-flight creation."""


class PoolScopeError(ValueError):
    """Fail-closed: a pooled source acquisition lacked a canonical tenant identity.

    Bug-8039: the pool must never hand a physical connection across tenants. The
    only globally-unique tenant boundary is the canonical tenant id (the tenant
    slug / ``tess_system`` identity); ``project_id``/``connection_id`` are UUIDs
    in a per-tenant ``{slug}_meta`` schema and can legitimately COLLIDE across
    tenants (an imported/seeded project bundle preserves those IDs). When no
    canonical tenant is available we refuse to acquire rather than fall back to a
    shared key that two tenants could collide on.
    """


def session_tenant_id(tenant_session: object | None) -> str | None:
    """Best-effort canonical tenant id from a tenant-bound session.

    ``get_tenant_db`` stores the tenant slug in ``session.info['tenant_id']``.
    Returns ``None`` when the session is absent or carries no tenant id."""
    if tenant_session is None:
        return None
    try:
        tid = tenant_session.info.get("tenant_id")  # type: ignore[attr-defined]
    except Exception:
        return None
    return str(tid) if tid else None


def build_pool_scope(
    tenant: str | None, *, project_id: object = None, conn_id: object = None,
) -> str:
    """Build a fail-closed, tenant-FIRST pool scope (Bug-8039).

    ``tenant`` is the canonical (globally-unique) tenant identity and is the
    primary isolation component. ``project_id``/``conn_id`` add within-tenant
    separation but are NOT trusted as a tenant boundary (they can collide across
    tenants). A missing/blank tenant raises :class:`PoolScopeError` — we never
    collapse an unknown tenant onto a shared key."""
    t = str(tenant).strip() if tenant is not None else ""
    if not t:
        raise PoolScopeError(
            "Refusing to acquire a pooled source connection without a canonical "
            "tenant identity: pass an explicit tenant_slug or a tenant-bound "
            "session (session.info['tenant_id']) so a pooled connection is never "
            "shared across tenants."
        )
    return f"tenant={t}|project={project_id}|conn={conn_id}"


MIN_POOL_SIZE = 2
MAX_POOL_SIZE = 10
# F-014-06: a pool untouched for this long is closed by the reaper so a stale
# (rotated-credential or changed-host) pool does not hold idle connections
# forever. 30 minutes balances connection reuse against credential freshness.
POOL_IDLE_TTL_SECONDS = 1800
# Documented 24h ceiling for the security-age max-checkout grace, symmetric with
# the DDL timeout ceiling so a typo cannot restore an unbounded graceful wait.
_POOL_MAX_CHECKOUT_CEILING_SECONDS = 86400
# Bug-8041 R7: there is no independent shutdown-grace default any more -- the
# grace is DERIVED from the single shutdown budget (see
# ``_pool_shutdown_grace_seconds`` and ``shared/config/shutdown_budget.py``).
_POOL_MAX_LIFETIME_DEFAULT_SECONDS = 900
# Documented ceiling for the max-lifetime security-age bound, symmetric with the
# checkout-grace/DDL ceilings so a typo cannot restore an effectively-unbounded
# pool lifetime.
_POOL_MAX_LIFETIME_CEILING_SECONDS = 86400
# Bug-8041 residual review round 2 (NEW-2): keys already warned about, so a
# persistent misconfiguration logs ONCE instead of flooding — several of these
# resolvers run on a per-acquisition (``_pool_max_lifetime_seconds``, via the
# reaper on every pooled acquire) or per-retirement hot path, and an unbounded
# repeat would drown the very signal this hardening depends on being noticed.
_warned_config_issues: set[str] = set()


def _warn_once(key: str, msg: str, *args: object) -> None:
    """Emit ``msg`` at WARNING only the first time ``key`` is seen this process."""
    if key in _warned_config_issues:
        return
    _warned_config_issues.add(key)
    logger.warning(msg, *args)


def _reset_config_warning_state() -> None:
    """Test-only: clear the warn-once dedup state so a test asserting on one of
    these warnings is not silently short-circuited by an earlier test in the
    same process that already tripped the same key (Bug-8041 residual review
    round 3, finding 7)."""
    _warned_config_issues.clear()


def _reset_shutdown_state() -> None:
    """Test-only: clear the shutdown flag and restart-needed flag so tests that
    call ``close_all_pools`` (which sets ``_shutting_down``) do not leak the
    shutdown state into subsequent tests (Bug-8041 R4 MEDIUM-1).

    Bug-8041 R6: also drop the per-loop shutdown event and the sweep restart
    budget so neither bleeds across pytest-asyncio's per-test event loops."""
    global _shutting_down, _lifecycle_sweep_needs_restart
    global _shutdown_event, _shutdown_event_loop, _sweep_restart_budget_exhausted
    _shutting_down = False
    _lifecycle_sweep_needs_restart = False
    _shutdown_event = None
    _shutdown_event_loop = None
    _sweep_restart_times.clear()
    _sweep_restart_budget_exhausted = False
    # Bug-8041 R7 HIGH-1: drop any creation handles left over from a previous
    # test's aborted creators so they cannot make the next shutdown wait on an
    # event bound to a dead event loop.
    _inflight_creates.clear()
    _undisposed_pools.clear()


def reset_pool_manager_state() -> None:
    """Re-enable pool creation after ``close_all_pools`` has been called.

    Bug-8041 R6 MEDIUM-4: ``close_all_pools`` sets a process-lifetime shutdown
    flag that PERMANENTLY refuses new pool creation. That is correct for a real
    lifespan shutdown (the process is terminating), but a test harness that
    closes pools between tests needs to create pools again in the next test —
    e.g. the connector-acceptance suite's autouse per-test fixture calls
    ``close_all_pools`` after every test. This function clears the shutdown flag
    (and the sweep restart-needed flag / per-loop event) so creation is possible
    again.

    Intended for test fixtures ONLY. It must NOT be called from any production
    request or shutdown path: a production shutdown is one-way by design, and
    re-enabling creation after lifespan shutdown would reopen the exact
    post-shutdown pool-install / sweep-restart window Bug-8041 R4 closed."""
    _reset_shutdown_state()


def _pool_create_wait_timeout_seconds() -> float:
    """Upper bound (seconds) a waiter blocks on the shared pool-creation future.

    Bug-8041 R6 HIGH-2: a waiter joined on another caller's in-flight creation
    must never be able to hang indefinitely, even under a future regression that
    leaves the future unresolved.

    DERIVED from existing configuration rather than a new env knob (a new
    unwired ``SOURCE_POOL_*`` knob is exactly the class prior gates flagged): a
    waiter is only waiting for the creator's ``asyncpg.create_pool``, which opens
    ``MIN_POOL_SIZE`` connections (each up to the ``query.connect_timeout_seconds``
    bound) plus per-connection ``_reset_session`` setup. The bound is therefore
    ``connect_timeout * (MIN_POOL_SIZE + 1)`` plus a fixed slack, floored at
    ``_POOL_CREATE_WAIT_FLOOR_SECONDS`` so a small connect timeout can never make
    the wait spuriously trip a legitimately-slow creation. Fail-open to the floor
    if the snapshot lookup itself raises."""
    try:
        connect_timeout = float(system_snapshot_get("query.connect_timeout_seconds"))
    except Exception:
        connect_timeout = 10.0
    derived = connect_timeout * (MIN_POOL_SIZE + 1) + 30.0
    # Clamp to [floor, ceiling]: the floor stops a tiny connect timeout from
    # tripping a legitimately-slow creation; the ceiling preserves the whole point
    # of this bound (a waiter can NEVER hang forever) even if an operator sets an
    # absurd connect timeout.
    return max(_POOL_CREATE_WAIT_FLOOR_SECONDS, min(derived, _POOL_CREATE_WAIT_CEILING_SECONDS))


def _pool_max_lifetime_unbounded_unsafe_enabled() -> bool:
    """Whether security-age pool retirement may be fully DISABLED (Bug-8041
    residual 1 hardening).

    Gated behind an explicit non-production opt-in so a plain
    ``SOURCE_POOL_MAX_LIFETIME_SECONDS<=0`` sentinel can never silently leave a
    continuously-used pool (whose ``last_used`` keeps refreshing on every
    acquire, so the idle reaper never touches it) authenticated forever even
    after the source revokes its password remotely — the exact gap this
    hardening closes. Intentionally NOT wired into ``infra/docker-compose.yml``;
    it applies to host/dev processes only, never the container stack."""
    return os.getenv(
        "SOURCE_POOL_MAX_LIFETIME_ALLOW_UNBOUNDED_UNSAFE", "",
    ).strip().lower() in ("1", "true", "yes", "on")


def _pool_max_lifetime_seconds() -> int:
    """Maximum wall-clock age of a pool before it is retired (F-014-06 / Bug-8040).

    A continuously *used* pool refreshes ``last_used`` on every acquire, so the
    idle reaper never touches it. That leaves a pool authenticated with an
    old-but-continuously-used password reusable forever even after the source
    revokes that password remotely. Capping the pool's total lifetime forces a
    periodic teardown + reconnect, at which point asyncpg re-authenticates and a
    revoked credential is finally rejected. 15 minutes bounds the revocation
    window while keeping reconnect churn low.

    Hardening (Bug-8041 residual 1): the production bound is ALWAYS finite and
    positive. Override via ``SOURCE_POOL_MAX_LIFETIME_SECONDS`` (default 900s),
    clamped to ``_POOL_MAX_LIFETIME_CEILING_SECONDS`` (24h). Disabling the cap
    (``<=0`` — a continuously-used, revoked-credential pool would then be
    reusable forever) is honoured ONLY behind the explicit non-production
    ``SOURCE_POOL_MAX_LIFETIME_ALLOW_UNBOUNDED_UNSAFE`` opt-in; otherwise a
    ``<=0`` value is rejected and the finite default is used.
    """
    raw = os.getenv("SOURCE_POOL_MAX_LIFETIME_SECONDS")
    if raw is None or raw.strip() == "":
        return _POOL_MAX_LIFETIME_DEFAULT_SECONDS
    try:
        val = int(raw)
    except (TypeError, ValueError):
        _warn_once(
            "max_lifetime_invalid",
            "Invalid SOURCE_POOL_MAX_LIFETIME_SECONDS=%r; using default %ds",
            raw, _POOL_MAX_LIFETIME_DEFAULT_SECONDS,
        )
        return _POOL_MAX_LIFETIME_DEFAULT_SECONDS
    if val <= 0:
        if _pool_max_lifetime_unbounded_unsafe_enabled():
            return 0  # unbounded — DEV ONLY, explicit unsafe opt-in
        _warn_once(
            "max_lifetime_nonpositive_refused",
            "SOURCE_POOL_MAX_LIFETIME_SECONDS<=0 (disables security-age pool "
            "retirement, letting a revoked-credential pool with continuous "
            "traffic be reused indefinitely) refused without "
            "SOURCE_POOL_MAX_LIFETIME_ALLOW_UNBOUNDED_UNSAFE; using default %ds",
            _POOL_MAX_LIFETIME_DEFAULT_SECONDS,
        )
        return _POOL_MAX_LIFETIME_DEFAULT_SECONDS
    return min(val, _POOL_MAX_LIFETIME_CEILING_SECONDS)


_POOL_MAX_CHECKOUT_DEFAULT_SECONDS = 3600


def _pool_strict_checkout_override_enabled() -> bool:
    """Whether an operator may configure a checkout grace SHORTER than the
    effective DDL timeout, or a strict immediate-terminate (Bug-8041 residual 2
    hardening).

    Checkout grace and DDL timeout are independently configurable knobs.
    Without this guard, an operator could set ``SOURCE_POOL_MAX_CHECKOUT_SECONDS``
    below ``SOURCE_DDL_TIMEOUT_SECONDS`` (e.g. DDL timeout=7200s, checkout
    grace=3600s) and security-age retirement would force-terminate a healthy,
    still-within-budget, long-running DDL/materialisation an hour early. A
    deliberately stricter revocation policy (accepting that trade-off) must be
    explicit rather than an accidental interaction of two independent knobs."""
    return os.getenv(
        "SOURCE_POOL_STRICT_CHECKOUT_UNSAFE", "",
    ).strip().lower() in ("1", "true", "yes", "on")


# Bug-8041 R3 Finding 5: the DDL-timeout resolver now lives in a neutral shared
# config module (``shared.config.ddl_timeout``) imported normally at the top of
# this file. The old dynamic try/except import of
# ``shared.source_executor.get_effective_ddl_timeout_seconds`` and its
# conservative-default fallback, cache, and latched-failure flag are REMOVED.
# There is now exactly one resolver with no divergent-fallback failure mode.


def _pool_security_max_checkout_seconds() -> int:
    """Maximum time (seconds) a security-age pool's IN-FLIGHT connection may keep
    running before it is force-terminated (Bug-8040 hardening).

    When a pool crosses its max lifetime it is removed from ``_pools`` (so no NEW
    checkout can reuse a possibly-revoked credential — new work re-authenticates
    on a fresh pool). Its already-checked-out connections are legitimate in-flight
    materialisation and are allowed to finish, but only up to THIS bound: after it
    the pool is force-terminated so a connection authenticated with a since-revoked
    source password cannot keep executing indefinitely.

    Hardening (Bug-8041 residual 2): this grace is derived FROM the effective DDL
    timeout (``shared.config.ddl_timeout.get_effective_ddl_timeout_seconds``), not
    an independently-typed default, so a single healthy DDL/materialisation
    *statement* running within its own DDL-timeout budget is never killed early
    by a shorter, independently-configured checkout grace. When
    ``SOURCE_POOL_MAX_CHECKOUT_SECONDS`` is unset, the grace is
    ``max(3600, effective DDL timeout)``. When it IS set below the effective DDL
    timeout, it is raised to the DDL timeout (with a warning) unless the operator
    explicitly opts into a stricter revocation window via
    ``SOURCE_POOL_STRICT_CHECKOUT_UNSAFE``. Either way the result is clamped to a
    documented 24h ceiling so a typo cannot restore an effectively unbounded
    graceful wait (the behaviour Bug-8040 removed).

    NOTE — this bound is per-STATEMENT, not per-checkout: a caller that runs
    several DDL statements or bulk-insert batches on ONE checked-out connection
    (each individually within its own DDL-timeout budget) can still have the
    pool force-terminated mid-sequence once the checkout as a whole exceeds this
    grace, because the grace is a wall-clock cap measured from pool retirement,
    not reset per statement. This is a pre-existing limitation of the checkout
    model (see the docs/execution/issue-intake entry filed alongside the
    Bug-8041 residual fix) and out of scope for this fix, which only closes the
    two independently-configured-knobs gap.
    """
    # Bug-8041 R3 Finding 5: direct import, no dynamic try/except fallback.
    ddl_timeout = get_effective_ddl_timeout_seconds()

    raw = os.getenv("SOURCE_POOL_MAX_CHECKOUT_SECONDS")
    configured: int | None
    if raw is None or raw.strip() == "":
        configured = None
    else:
        try:
            configured = int(raw)
        except (TypeError, ValueError):
            _warn_once(
                "checkout_grace_invalid",
                "Invalid SOURCE_POOL_MAX_CHECKOUT_SECONDS=%r; using %ds",
                raw, _POOL_MAX_CHECKOUT_DEFAULT_SECONDS,
            )
            configured = None

    if configured is not None and configured <= 0:
        if _pool_strict_checkout_override_enabled():
            return 0  # strict: force-terminate in-flight connections immediately
        _warn_once(
            "checkout_grace_nonpositive_refused",
            "SOURCE_POOL_MAX_CHECKOUT_SECONDS<=0 refused without "
            "SOURCE_POOL_STRICT_CHECKOUT_UNSAFE (would force-terminate in-flight "
            "connections immediately); using %ds",
            _POOL_MAX_CHECKOUT_DEFAULT_SECONDS,
        )
        configured = None

    # Bug-8041 R3 Finding 6: when the DDL timeout is explicitly unbounded (0)
    # via the non-production opt-in, a finite checkout grace silently defeats the
    # deliberately-unbounded DDL. Handle this interaction explicitly.
    if ddl_timeout == 0:
        if configured is None:
            # No explicit checkout grace + unbounded DDL: use the ceiling so the
            # deliberately-unbounded DDL is not killed after the default 3600s.
            # The ceiling (24h) maintains a finite security bound while being
            # large enough for any practical DDL.  Require explicit configuration
            # for a tighter or looser window.
            _warn_once(
                "checkout_grace_ddl_unbounded_unset",
                "SOURCE_DDL_TIMEOUT_SECONDS is unbounded (0, explicit unsafe "
                "opt-in) but SOURCE_POOL_MAX_CHECKOUT_SECONDS is unset; using "
                "the ceiling %ds so a deliberately-unbounded DDL is not killed "
                "after the default %ds. Set SOURCE_POOL_MAX_CHECKOUT_SECONDS "
                "explicitly to control the credential-revocation window when DDL "
                "is unbounded.",
                _POOL_MAX_CHECKOUT_CEILING_SECONDS,
                _POOL_MAX_CHECKOUT_DEFAULT_SECONDS,
            )
            return _POOL_MAX_CHECKOUT_CEILING_SECONDS
        # Explicit checkout grace + unbounded DDL: the operator set both; honor
        # the configured value but warn that it will terminate unbounded DDLs
        # unless the strict flag acknowledges the trade-off.
        if not _pool_strict_checkout_override_enabled():
            _warn_once(
                "checkout_grace_finite_ddl_unbounded",
                "SOURCE_POOL_MAX_CHECKOUT_SECONDS=%ds is finite but the DDL "
                "timeout is unbounded (0); the checkout grace will terminate "
                "deliberately-unbounded DDLs after %ds. Set "
                "SOURCE_POOL_STRICT_CHECKOUT_UNSAFE=1 to acknowledge this "
                "trade-off, or remove the explicit checkout grace to use the "
                "auto-derived ceiling (%ds).",
                configured, configured, _POOL_MAX_CHECKOUT_CEILING_SECONDS,
            )
        return min(configured, _POOL_MAX_CHECKOUT_CEILING_SECONDS)

    if configured is None:
        # Auto-derive so the grace can never be shorter than the effective DDL
        # bound: a healthy long-running DDL must survive its own timeout before
        # security-age retirement force-terminates the pool underneath it.
        derived = max(_POOL_MAX_CHECKOUT_DEFAULT_SECONDS, ddl_timeout)
        clamped = min(derived, _POOL_MAX_CHECKOUT_CEILING_SECONDS)
        if clamped > _POOL_MAX_CHECKOUT_DEFAULT_SECONDS:
            # Bug-8041 residual review finding: raising SOURCE_DDL_TIMEOUT_SECONDS
            # silently widens the credential-revocation window too (the checkout
            # grace tracks it), since SOURCE_POOL_MAX_CHECKOUT_SECONDS is unset.
            # Surface that security-relevant side effect instead of a silent
            # derivation.
            _warn_once(
                "checkout_grace_derived_widened",
                "SOURCE_POOL_MAX_CHECKOUT_SECONDS is unset; deriving %ds from the "
                "effective DDL timeout (%ds). This widens the credential-"
                "revocation window to match — set SOURCE_POOL_MAX_CHECKOUT_SECONDS "
                "explicitly if a shorter revocation window is required "
                "independent of the DDL timeout.",
                clamped, ddl_timeout,
            )
        return clamped

    if (
        ddl_timeout > 0
        and configured < ddl_timeout
        and not _pool_strict_checkout_override_enabled()
    ):
        _warn_once(
            "checkout_grace_raised_to_ddl",
            "SOURCE_POOL_MAX_CHECKOUT_SECONDS=%ds is shorter than the effective "
            "DDL timeout %ds; raising to %ds so a healthy DDL statement is not "
            "killed early. Set SOURCE_POOL_STRICT_CHECKOUT_UNSAFE=1 to "
            "intentionally allow a stricter revocation window.",
            configured, ddl_timeout, ddl_timeout,
        )
        configured = ddl_timeout

    return min(configured, _POOL_MAX_CHECKOUT_CEILING_SECONDS)


def _password_fingerprint(password: str) -> str:
    """Short, non-reversible fingerprint of the password for the pool key.

    Bug-8039 hardening: this value keys the pool internally but is NEVER logged —
    a 12-hex SHA-256 prefix in logs enables offline password guessing. It never
    appears in a log line; ``_pool_log_label`` strips it."""
    return hashlib.sha256((password or "").encode()).hexdigest()[:12]


def _pool_log_label(key: str) -> str:
    """Log-safe label for a pool: the tenant/project/conn scope + a random pool
    instance id, NEVER the password fingerprint (Bug-8039 hardening).

    Falls back to stripping the ``#<fingerprint>`` suffix for test-seeded pools
    that have no descriptor, so the fingerprint still never reaches a log line."""
    desc = _pool_desc.get(key)
    if desc:
        return desc
    return key.rsplit("#", 1)[0]


def _pool_key(
    scope: str, host: str, port: int, database: str, user: str, password: str,
) -> str:
    # F-014-05 (Bug-8039): ``scope`` (tenant/project + connection identity) is the
    # FIRST key component so a pooled connection is NEVER shared across tenants,
    # even when two tenants configure the same managed-DB login. F-014-06: the
    # password fingerprint keeps a rotated credential routing to a fresh pool
    # instead of reusing one authenticated with the old (now-revoked) password.
    return (
        f"{scope}|{user}@{host}:{port}/{database}"
        f"#{_password_fingerprint(password)}"
    )


async def _reset_session(conn: asyncpg.Connection) -> None:
    """Reset session state at hand-out so nothing bleeds between acquisitions.

    F-014-05 (Bug-8039): pools are tenant-keyed, so this only ever runs within a
    single tenant's own pool — but resetting on acquire makes the isolation
    explicit and independent of asyncpg's release-time reset. ``RESET ALL``
    restores every settable GUC (``search_path``, ``statement_timeout``,
    ``role``/``session authorization``, etc.) to its connection default;
    ``DISCARD TEMP`` drops any leftover temp tables. Prepared statements are
    left intact (asyncpg caches them), and no data-visibility state can carry
    across a hand-out.
    """
    await conn.execute("RESET ALL")
    await conn.execute("DISCARD TEMP")


async def _get_or_create_pool(
    scope: str, host: str, port: int, database: str, user: str, password: str,
    *, _create_attempt: int = 0,
) -> asyncpg.Pool:
    """Get an existing pool or create a new one.

    Bug-7177: the lock is held only for the short dictionary lookup/mutation,
    NOT during the slow ``asyncpg.create_pool`` network I/O. Per-key creation
    futures ensure different keys initialise concurrently and same-key callers
    share one in-flight creation.

    Bug-8041 R6 MEDIUM-5: ``_create_attempt`` bounds the waiter's
    creator-cancellation retry so repeated creator cancellations cannot recurse
    without limit (the previous "bounded retry" comment was false — it was a
    plain unbounded recursive call).
    """
    key = _pool_key(scope, host, port, database, user, password)

    is_creator = False
    handle: _PoolCreationHandle | None = None
    async with _lock:
        await _reap_idle_pools_locked()
        if key in _pools:
            pool, _, created_at = _pools[key]
            if not pool._closed:
                _pools[key] = (pool, time.monotonic(), created_at)
                # Bug-8041 R4: ensure the sweep driver is (re)started on the
                # REUSE path too, not only on pool creation.  Without this, a
                # sweep task that dies (CancelledError, event-loop restart) is
                # never restarted as long as all traffic reuses existing pools
                # -- the exact regression the R4 gate proved.
                _ensure_lifecycle_sweep_running()
                return pool
            del _pools[key]
            _pool_desc.pop(key, None)  # keep descriptor lifecycle symmetric

        # Bug-8041 R4 MEDIUM-1: reject new pool creation after shutdown has
        # begun.  An in-flight create completing after close_all_pools returns
        # would install a pool and restart the sweep task post-shutdown.
        if _shutting_down:
            raise asyncpg.InterfaceError(
                "Source pool manager is shutting down; new pool creation refused"
            )
        if key in _pending_creates:
            future = _pending_creates[key]
        else:
            future = asyncio.get_running_loop().create_future()
            _pending_creates[key] = future
            # Bug-8041 R7 HIGH-1: register the in-flight CREATION (not just the
            # shared future) in the same critical section that reserves the key,
            # and while ``_shutting_down`` is known False -- so the snapshot
            # ``close_all_pools`` takes under this same lock is complete.
            handle = _PoolCreationHandle()
            _inflight_creates.add(handle)
            is_creator = True

    if not is_creator:
        # Bug-8041 R3 Finding 4: shield the waiter's await so one waiter's
        # cancellation (e.g. its own request times out) does not propagate into
        # the shared future and poison the creator's ``set_result`` call with an
        # ``InvalidStateError``. Each waiter's cancellation is isolated.
        #
        # Bug-8041 R4 MEDIUM-2: if the creator itself was cancelled mid-creation,
        # it publishes ``_CreatorCancelledRetry`` (not CancelledError) onto the
        # shared future.  Catch that and retry -- the waiter becomes the new
        # creator (or joins a fresh in-flight creation).  The retry re-enters
        # the full function to pick up a new future/creator role.
        #
        # Bug-8041 R6 MEDIUM-5: the retry is now genuinely bounded (see
        # _create_attempt), matching what the old (false) "bounded retry"
        # comment claimed.
        #
        # Bug-8041 R6 HIGH-2 (defense-in-depth): the waiter's await is bounded by
        # a timeout so it can NEVER hang forever, even under a future regression
        # that leaves the shared future unresolved. ``asyncio.shield`` still
        # isolates this waiter's own timeout/cancel from the shared future (a
        # timeout here cancels the shield, not the creator's ``set_result``).
        try:
            return await asyncio.wait_for(
                asyncio.shield(future),
                timeout=_pool_create_wait_timeout_seconds(),
            )
        except _CreatorCancelledRetry:
            if _create_attempt + 1 >= _POOL_CREATE_MAX_ATTEMPTS:
                raise asyncpg.InterfaceError(
                    "Source pool creator was cancelled "
                    f"{_POOL_CREATE_MAX_ATTEMPTS} times in a row; giving up"
                )
            logger.debug(
                "Pool creator cancelled mid-creation; waiter retrying "
                "(attempt %d/%d)",
                _create_attempt + 2, _POOL_CREATE_MAX_ATTEMPTS,
            )
            return await _get_or_create_pool(
                scope, host, port, database, user, password,
                _create_attempt=_create_attempt + 1,
            )
        except asyncio.TimeoutError:
            raise asyncpg.InterfaceError(
                "Timed out waiting for source pool creation "
                f"({_pool_create_wait_timeout_seconds():.0f}s); the creating "
                "task did not resolve the shared creation future"
            )

    # Bug-8041 R4 review FINDING-1: the ENTIRE creator body (create_pool +
    # install) must be wrapped so a cancellation landing between create_pool
    # returning and the install completing does not orphan the pool or leave
    # the key permanently stuck in _pending_creates.
    pool: asyncpg.Pool | None = None
    installed = False  # Bug-8041 R6 review NB-5: once True, ``pool`` is live in
    # ``_pools`` and must NOT be closed by the except handler (an exception after
    # a successful install — e.g. from _ensure_lifecycle_sweep_running on a
    # closing loop — would otherwise close a pool that is installed and in use).
    try:
        command_timeout = float(system_snapshot_get("query.statement_timeout_seconds"))
        connect_timeout = float(system_snapshot_get("query.connect_timeout_seconds"))
        # Bug-8041 R7 HIGH-1: this await used to be an unobservable black box --
        # a creator inside ``asyncpg.create_pool`` when SIGTERM arrived kept
        # connecting for its full connect timeout, long after ``close_all_pools``
        # had returned. It now races the shutdown signal and aborts.
        pool = await _create_pool_observing_shutdown(
            handle,
            host=host, port=port, database=database,
            user=user, password=password,
            command_timeout=command_timeout,
            connect_timeout=connect_timeout,
        )
        now = time.monotonic()
        log_label = (
            f"{scope}|{user}@{host}:{port}/{database} pool={secrets.token_hex(4)}"
        )
        async with _lock:
            _pending_creates.pop(key, None)
            # Bug-8041 R4 review FINDING-3: re-check _shutting_down inside the
            # install critical section.  An in-flight creator that passed the
            # pre-create check can reach this point AFTER close_all_pools has
            # returned.  Without this guard the pool is installed post-shutdown,
            # invisible to the shutdown close and with an unbounded credential
            # lifetime.
            if _shutting_down:
                # Bug-8041 R7 MEDIUM-2: infallible hand-off (schedule, else
                # terminate synchronously) so a closing event loop cannot leave
                # this pool live AND leave the future below unresolved.
                _schedule_close_or_terminate(pool, log_label)
                if not future.done():
                    future.set_exception(asyncpg.InterfaceError(
                        "Source pool manager is shutting down; pool creation "
                        "completed but not installed"
                    ))
                    try:
                        future.exception()
                    except Exception:
                        pass
                # Bug-8041 R6 LOW-6: this branch already scheduled a close for
                # ``pool``; clear the local reference so the outer ``except``
                # handler does not schedule a SECOND concurrent ``_close_pool``
                # on the same object (ownership hand-off is complete here).
                # Bug-8041 R7: the creation handle holds the same reference and
                # is the except handler's fallback owner, so it must be disowned
                # here too or the double close comes back through that path.
                pool = None
                if handle is not None:
                    handle.pool = None
                raise asyncpg.InterfaceError(
                    "Source pool manager shut down during pool creation"
                )
            _pools[key] = (pool, now, now)
            # Bug-8039 hardening: record a log-safe descriptor.
            _pool_desc[key] = log_label
            installed = True  # NB-5: pool is now owned by _pools
        # Bug-8041 R3 Finding 4: guard set_result against an already-done future.
        if not future.done():
            future.set_result(pool)
        # Bug-8041 R3 Finding 1: lazily start the periodic lifecycle sweep on
        # pool creation so revoked-credential pools are reaped proactively.
        _ensure_lifecycle_sweep_running()
        logger.debug(
            "Created source pool %s (min=%d, max=%d)",
            _pool_log_label(key), MIN_POOL_SIZE, MAX_POOL_SIZE,
        )
        return pool
    except BaseException as exc:
        # Bug-8041 R6 HIGH-2: perform the failure cleanup SYNCHRONOUSLY, with NO
        # await between entering this handler and popping the key + resolving the
        # future. The previous code did this cleanup inside ``async with _lock``,
        # whose lock-acquire is an await point: if a SECOND cancellation was
        # re-delivered while that lock was contended (this repo's anyio 4.14.1
        # CancelScope re-issues ``task.cancel()`` every event-loop tick while a
        # task is suspended), the handler could abort BEFORE popping the key or
        # resolving the future — permanently poisoning the key and hanging every
        # waiter on the shared future. A cancellation can only take effect at an
        # await point, so a cleanup with no await cannot be interrupted partway.
        #
        # The lock is unnecessary here: a dict pop and a Future resolution are
        # atomic between await points, and we only ever remove OUR OWN future for
        # this key (a concurrent same-key caller becomes a waiter on this future;
        # it never replaces it). Pop-if-matches guards the case where the success
        # path already popped the key and installed the pool.
        #
        # Bug-8041 R7 MEDIUM-2 — ORDERING. "No await" was necessary but not
        # sufficient: the previous order was pop-key, SCHEDULE-CLOSE, then
        # resolve-future, and ``asyncio.create_task`` genuinely raises
        # (``RuntimeError: Event loop is closed`` / no running loop) when the
        # creator unwinds on a loop that is already tearing down. That raise
        # escaped this handler with the key popped and the future NEVER
        # resolved, so every waiter hung on the bounded wait and the created
        # pool was never closed -- the exact permanent-hang class the R6 fix was
        # supposed to eliminate, merely relocated. The two steps that MUST
        # happen (release the key, resolve the future) are now both done before
        # anything fallible is attempted, and the fallible step itself is
        # infallible-by-construction via ``_schedule_close_or_terminate``.
        if _pending_creates.get(key) is future:
            del _pending_creates[key]
            # Only drop the descriptor when WE still owned the in-flight create
            # (failure before install). If the success path already installed the
            # pool + descriptor, leave the installed descriptor intact.
            _pool_desc.pop(key, None)
        # Bug-8041 R4 MEDIUM-2: when the CREATOR task is cancelled (e.g. its
        # HTTP request times out or is disconnected), do NOT propagate the
        # CancelledError onto the shared future -- that would cancel every
        # uninvolved waiter whose own request is still healthy.  Instead,
        # signal waiters to retry by publishing a recoverable exception that
        # the waiter path translates into a fresh creation attempt.
        if not future.done():
            if isinstance(exc, asyncio.CancelledError):
                future.set_exception(
                    _CreatorCancelledRetry(
                        "Pool creator cancelled; waiters should retry"
                    )
                )
            else:
                future.set_exception(exc)
            # R6 OPEN-3: mark the traceback as retrieved so asyncio does not
            # emit a spurious ERROR log ("Future exception was never retrieved")
            # when no waiters are present.
            try:
                future.exception()
            except Exception:
                pass
        # Bug-8041 R4 review FINDING-1: if the pool was already created but
        # installation failed (cancellation between create_pool and install),
        # close it so it is not leaked as an untracked, unbounded-lifetime
        # authenticated pool.
        # Bug-8041 R6 review NB-5: never close a pool that WAS installed (an
        # exception after install would otherwise close a live, in-use pool).
        #
        # Bug-8041 R7: ``pool`` is only assigned once the creation helper has
        # RETURNED. A cancellation landing inside the hand-over itself (the
        # initialisation finished, but the helper had not yet returned) left this
        # local None while a fully-connected pool existed — orphaned, live and
        # authenticated, with nothing tracking it. The creation handle records
        # the pool as soon as it exists, so use it as the fallback owner.
        #
        # HONEST COVERAGE NOTE (Bug-8041 R7 round 2): this fallback is now a
        # SECOND line of defence, not the primary one. The ``except BaseException``
        # inside ``_create_pool_observing_shutdown`` cancels the init task and
        # terminates the partial pool before this handler is ever reached, so a
        # mutation that deletes these three lines alone SURVIVES the suite. It is
        # kept deliberately (this lane has reopened the same window four times)
        # but it is NOT counted as mutation-proven coverage — the guarantee is
        # carried by the F1 handler, which IS mutation-proven.
        orphan = pool
        if orphan is None and handle is not None and not installed:
            orphan = handle.pool
        if orphan is not None and not installed and not getattr(orphan, "_closed", False):
            _schedule_close_or_terminate(
                orphan, f"{scope}|{user}@{host}:{port}/{database}",
            )
        raise
    finally:
        # Bug-8041 R7 HIGH-1: signal that this creator has fully relinquished
        # ownership -- AFTER any install / close scheduling above, so a
        # ``close_all_pools`` woken by this event sees the resulting close task
        # in ``_pending_closes`` on its next drain iteration.
        if handle is not None:
            _inflight_creates.discard(handle)
            handle.finished.set()


async def _create_pool_observing_shutdown(
    handle: _PoolCreationHandle | None, *,
    host: str, port: int, database: str, user: str, password: str,
    command_timeout: float, connect_timeout: float,
) -> asyncpg.Pool:
    """Create an asyncpg pool, ABORTING promptly if process shutdown begins.

    Bug-8041 R7 HIGH-1. ``asyncpg.create_pool`` opens ``MIN_POOL_SIZE``
    connections, each bounded only by the connect timeout. A creator that
    entered it just before SIGTERM used to keep going regardless: shutdown
    resolved its shared future, cleared ``_pending_creates``, found nothing left
    to drain and returned in milliseconds -- and the creator then finished and
    took ownership of a live, authenticated pool AFTER the process considered
    itself shut down. Reproduced live against real asyncpg + PostgreSQL:
    ``close_all_pools`` returned in 0.000s with a creator 1.25s from completing.

    ``asyncpg.create_pool`` is a plain synchronous factory that returns an
    awaitable ``Pool``; only awaiting it performs I/O. Splitting construction
    from initialisation lets us (a) race the initialisation against the shutdown
    signal, and (b) still hold the ``Pool`` object if we abort mid-connect, so
    the connections it already opened can be force-terminated rather than
    orphaned. Tests that monkeypatch the factory with an ``async def`` (returning
    a coroutine) are handled by the same code path.
    """
    factory_result = asyncpg.create_pool(
        host=host, port=port, database=database,
        user=user, password=password,
        min_size=MIN_POOL_SIZE,
        max_size=MAX_POOL_SIZE,
        command_timeout=command_timeout,
        timeout=connect_timeout,
        # Bug-8039: clean the session on every hand-out so no GUC/temp state
        # bleeds across acquisitions within the (tenant-scoped) pool.
        setup=_reset_session,
    )

    async def _init():
        obj = factory_result
        if inspect.isawaitable(obj):
            if handle is not None and not inspect.iscoroutine(obj):
                # A real asyncpg Pool exists before initialisation completes;
                # record it now so an abort can terminate half-open connections.
                handle.pool = obj
            awaited = await obj
            if awaited is not None:
                obj = awaited
        if handle is not None:
            handle.pool = obj
        return obj

    init_task = asyncio.ensure_future(_init())
    shutdown_wait = asyncio.ensure_future(_get_shutdown_event().wait())
    try:
        done, _pending = await asyncio.wait(
            {init_task, shutdown_wait}, return_when=asyncio.FIRST_COMPLETED,
        )
    except BaseException:
        # Bug-8041 R7 round-2 F1 — CANCELLATION MUST PROPAGATE BOTH WAYS.
        # Detaching the initialisation into its own task (above) made shutdown
        # able to abort it, but it also DECOUPLED it from our own cancellation:
        # ``asyncio.wait`` never cancels the things it waits on, so a
        # CancelledError delivered to THIS task used to leave ``init_task``
        # running. It then completed, opened every ``MIN_POOL_SIZE`` connection,
        # and owned a live authenticated pool that nothing referenced --
        # invisible to ``_pools``, so invisible to the idle reaper AND to
        # security-age retirement. Reproduced against real asyncpg + real
        # PostgreSQL: 2 backends leaked for the life of the process, surviving
        # ``close_all_pools()``. Before R7 this could not happen, because the
        # cancellation propagated straight into ``_async__init__`` and aborted
        # the connect -- so this was a regression R7 introduced, and the R7
        # uvicorn flag makes it MORE reachable (uvicorn only started cancelling
        # request tasks once ``--timeout-graceful-shutdown`` was set).
        init_task.cancel()
        try:
            await asyncio.wait({init_task})
        finally:
            _terminate_when_settled(init_task, handle, factory_result)
        raise
    finally:
        if not shutdown_wait.done():
            shutdown_wait.cancel()
    if init_task in done:
        return await init_task
    # Shutdown began while we were still connecting. Cancel the initialisation
    # and CONFIRM termination before failing, so no half-open connection escapes.
    init_task.cancel()
    try:
        # asyncio.wait (not ``await init_task``) so a cancellation delivered to
        # THIS task is not confused with the one we just issued: this raises only
        # if we ourselves are cancelled, which the outer handler must still see.
        await asyncio.wait({init_task})
    finally:
        _terminate_when_settled(init_task, handle, factory_result)
    raise asyncpg.InterfaceError(
        "Source pool manager shut down while a pool was being created; "
        "creation aborted and any partially-created connections terminated"
    )


def _live_connection_holders(pool: object) -> list:
    """The pool's connection holders that currently own an OPEN connection.

    Bug-8041 R8 review. This is the only way to answer "does this pool still hold
    an authenticated source connection?" for a pool asyncpg will not let us
    inspect through its public API (one that is still ``_initializing``). It is
    also strictly better than the ``_initialized``/``_holders`` heuristic it
    replaces, which guessed from flags instead of looking at the connections.
    """
    live = []
    for holder in (getattr(pool, "_holders", None) or ()):
        con = getattr(holder, "_con", None)
        if con is None:
            continue
        try:
            if con.is_closed():
                continue
        except Exception:
            pass  # unknown state -- assume live and try to kill it
        live.append(holder)
    return live


def _force_terminate_pool(pool: object, label: str) -> bool:
    """Kill every connection ``pool`` owns, INCLUDING mid-initialisation.

    Returns True iff no live connection remains afterwards.

    Bug-8041 R8 review, BLOCKING-1/2 -- and the reason the R8 first attempt did
    not actually close the 6th gate's HIGH. ``asyncpg.Pool.terminate()`` goes
    through ``_check_init()`` (asyncpg 0.31.0 ``pool.py:990``) and is REFUSED
    while the pool is ``_initializing``. That is not a corner case: it is the
    ONLY state a creator suspended inside ``create_pool`` can be in, because
    ``_async__init__`` flips ``_initializing = False`` in a ``finally`` that
    cannot run until ``_initialize()`` returns. Every ``pool.terminate()``
    fallback in this module was therefore a guaranteed no-op in exactly the
    window it was added for, and a live probe against real asyncpg + real
    PostgreSQL showed 2 authenticated backends still open after
    ``close_all_pools()`` returned.

    ``PoolConnectionHolder.terminate()`` (``pool.py:270``) has NO ``_check_init``
    guard -- it just calls ``Connection.terminate()``, whose ``_release_on_close``
    cleanup is safe on a half-built pool. Dropping to the holder level when the
    pool-level call is refused takes the same probe to 0 open backends, with
    ``close_all_pools()`` still returning inside its budget.

    Residual, stated precisely (see the module docstring): we kill the
    connections that EXIST when we run. An initializer that ignores cancellation
    could open more afterwards -- real asyncpg's ``_initialize`` propagates
    cancellation, so it does not, and nothing this module can do would bound a
    third-party coroutine that refuses to stop.
    """
    pool_terminate_refused = False
    terminate = getattr(pool, "terminate", None)
    if callable(terminate):
        try:
            terminate()
        except Exception:
            pool_terminate_refused = True  # fall through to the holder level
        else:
            # Bug-8041 R8 review round 7: VERIFY, do not assume. This function's
            # contract is "True iff no live connection remains", and returning
            # True the moment ``terminate()`` did not raise broke it on the path
            # EVERY CACHED POOL TAKES -- an installed asyncpg pool never raises
            # there, and ``Pool.terminate()`` early-returns on ``self._closed``,
            # which ``Pool.close()`` sets in a ``finally`` that also runs when
            # its own terminate raised on the way out. So a cached pool could
            # keep live holders while ``close_all_pools``'s overrun WARNING
            # reported "0 pool(s) STILL HAD AN OPEN CONNECTION": disposal
            # silently ineffective and reported clean.
            if not _live_connection_holders(pool):
                # R12: pop here TOO. This is the "terminate() was accepted and
                # the pool is now clean" branch, and it was the only one of three
                # success exits that did not clear the ledger -- so a pool whose
                # FIRST disposal failed and whose retry succeeded stayed counted,
                # and ``close_all_pools`` told the operator connections "may
                # outlive this process" when none did. Over-reporting a leak
                # trains operators to ignore the one signal that means a real
                # one, which is the reasoning this function already states below.
                _undisposed_pools.pop(id(pool), None)
                return True
    live = _live_connection_holders(pool)
    if not live:
        # Nothing was ever opened (or it is already closed): the refusal is
        # benign. Warning here would fire on every SIGTERM that lands during a
        # pool creation and train operators to ignore the one signal that means
        # a REAL leak (Bug-8041 R7 round-4 F4).
        logger.debug(
            "Pool.terminate() was refused for source pool %s and it owns no open "
            "connection; nothing to force-close", label, exc_info=True,
        )
        _undisposed_pools.pop(id(pool), None)
        return True
    killed = 0
    for holder in live:
        try:
            holder.terminate()
            killed += 1
        except Exception:
            logger.warning(
                "Could not force-close a connection holder of source pool %s",
                label, exc_info=True,
            )
    remaining = _live_connection_holders(pool)
    if remaining:
        logger.warning(
            "Force-terminating source pool %s FAILED: %d authenticated source "
            "connection(s) are still open and may outlive this process",
            label, len(remaining),
        )
        _undisposed_pools[id(pool)] = label
        return False
    # Bug-8041 R8 review round 8: name the REASON. Round 7 opened a second route
    # into this message -- a pool whose ``terminate()`` was ACCEPTED but left
    # holders open, because asyncpg early-returns on ``_closed`` (which
    # ``Pool.close()`` sets in a ``finally`` that also runs when its own
    # terminate raised). Telling an operator "still initialising" in that case
    # points the diagnosis at the wrong place entirely.
    logger.warning(
        "Pool.terminate() for source pool %s %s; force-closed its %d open "
        "connection(s) at the holder level instead", label,
        "was refused (still initialising)" if pool_terminate_refused
        else "was accepted but left connections open (already-_closed pool)",
        killed,
    )
    _undisposed_pools.pop(id(pool), None)
    return True


def _terminate_partial_pool(
    handle: _PoolCreationHandle | None, factory_result: object,
    label: str = "(partially-created)",
) -> bool:
    """Force-close whatever a cancelled pool initialisation left behind.

    Returns True iff no live connection remains -- see ``_force_terminate_pool``,
    which is where the actual termination (including the mid-initialisation
    holder-level fallback) happens. ``close_all_pools`` needs that distinction so
    it can report how many outstanding pools it genuinely could not dispose of
    rather than assuming they are all dealt with.
    """
    terminated = False
    candidate = handle.pool if handle is not None and handle.pool is not None else None
    if candidate is None:
        candidate = factory_result
    if candidate is not None and not inspect.iscoroutine(candidate):
        terminated = _force_terminate_pool(candidate, label)
    # A monkeypatched ``async def`` factory leaves an un-awaited coroutine when
    # the abort lands before ``_init`` runs; close it so it does not warn.
    if inspect.iscoroutine(factory_result):
        try:
            factory_result.close()
        except Exception:
            pass
    return terminated


def _terminate_when_settled(
    init_task: asyncio.Future, handle: _PoolCreationHandle | None,
    factory_result: object,
) -> None:
    """Terminate the partial pool ONCE its initialisation task has settled.

    Bug-8041 R7 round-3. ``asyncpg.Pool.terminate()`` goes through
    ``_check_init()`` EXACTLY like ``close()`` does (asyncpg 0.31.0
    ``pool.py``), so it RAISES ``InterfaceError: pool is being initialized, but
    not yet ready`` while ``_initializing`` is True -- and ``pool is not
    initialized`` before that. Terminating straight from our own ``finally``,
    which a re-delivered cancellation can reach BEFORE the init task has run its
    own ``finally``, was therefore a silent no-op that leaked every connection
    the pool had already opened. Verified empirically against asyncpg 0.31.0.

    asyncpg's ``_async__init__`` flips ``_initializing = False; _initialized =
    True`` in a ``finally``, so once the init task is DONE -- cancelled or not --
    ``terminate()`` is accepted. Binding termination to that task's completion
    makes it independent of how our own task unwinds.

    SCOPE (round-4 F5, stated so the guarantee is not overclaimed): on the
    DEFERRED branch the creator's ``finally`` may set ``handle.finished`` -- and
    so release ``close_all_pools()`` -- before the callback has fired, and if the
    loop is already closing the callback never fires at all.

    Bug-8041 R8 review: that scope note is no longer the load-bearing part of the
    guarantee, and ``close_all_pools()`` no longer depends on this deferral to
    dispose of an unsettled creator's connections. It force-closes them itself at
    its deadline via ``_force_terminate_pool``, which does NOT need the init task
    to have settled because it drops to ``PoolConnectionHolder.terminate()`` when
    asyncpg refuses the pool-level call. This function stays as the ordinary,
    non-shutdown disposal path (and as defence in depth on the deferred branch).
    """
    if init_task.done():
        _terminate_partial_pool(handle, factory_result)
        return
    init_task.add_done_callback(
        lambda _t: _terminate_partial_pool(handle, factory_result)
    )


def _abandon_unsettled_creations(
    creations: "list[_PoolCreationHandle]",
) -> int:
    """Force-terminate the pools of creators that missed the shutdown deadline.

    Bug-8041 R8 (6th external gate, HIGH). ``close_all_pools`` bounds how long it
    waits for in-flight creations -- it must, or it overruns the platform's
    SIGTERM window. Before this function existed, hitting that bound only LOGGED:
    the function returned while a creator that had already opened its connections
    still owned a live, authenticated pool, and termination happened whenever
    that creator eventually unwound. The external gate reproduced exactly that
    (3 unsettled creators, 6 live holders at return) by driving a deliberately
    cancellation-resistant initializer through the real path.

    Waiting longer is not an option, so the fix is to DISPOSE rather than wait:
    every pool object that exists at the deadline has its connections
    force-closed here, synchronously, before ``close_all_pools`` returns. A
    creation that has not produced a pool object yet holds no connection, so
    there is nothing to terminate for it.

    Bug-8041 R8 review: the disposal goes through ``_force_terminate_pool``, NOT
    a bare ``pool.terminate()``. The first version of this function used the
    latter, which asyncpg refuses for a pool that is still ``_initializing`` --
    the only state an unsettled creator's pool can actually be in -- so it was a
    no-op in every production-reachable case and the leak the gate reported
    survived. Live-verified: 2 authenticated PostgreSQL backends before the fix,
    0 after, with the same 2.00s return time.

    Returns how many creators were still in flight. Anything it could NOT
    dispose of is recorded in ``_undisposed_pools`` by ``_force_terminate_pool``
    itself (Bug-8041 R8 review round 8), so the count reported to the operator is
    the same one every other disposal path feeds -- a pool that never opened a
    connection is not in it, so the WARNING cannot cry wolf on every SIGTERM that
    lands during a pool creation.
    """
    unsettled = 0
    for handle in creations:
        if handle.finished.is_set():
            continue
        unsettled += 1
        if handle.pool is None:
            # No Pool object exists yet -- no connection can be open for it.
            continue
        # Bug-8041 R8 review round 3: this runs on the SIGTERM path, so it must
        # be incapable of aborting the rest of shutdown. ``_force_terminate_pool``
        # is defensive throughout and no real pool state makes it raise (proven
        # by execution across nine states against real asyncpg), but an
        # exception escaping here would skip every remaining handle AND the
        # overrun warning.
        try:
            _terminate_partial_pool(handle, None, "(in-flight creation)")
        except Exception:
            logger.warning(
                "Force-terminating an in-flight source pool creation raised; "
                "its connections may outlive shutdown", exc_info=True,
            )
            _undisposed_pools[id(handle.pool)] = "(in-flight creation)"
    return unsettled


def _schedule_pool_task(coro) -> None:
    """Schedule a close/retire coroutine off the lock, keeping a strong reference
    (Fable FINDING-5) so GC cannot drop the task before it completes."""
    task = asyncio.create_task(coro)
    _pending_closes.add(task)
    task.add_done_callback(_pending_closes.discard)


def _schedule_close_or_terminate(pool, label: str) -> None:
    """Hand a pool over for closing in a way that CANNOT raise (Bug-8041 R7).

    ``asyncio.create_task`` raises on a loop that is already closed or on no
    running loop -- both reachable while a creator unwinds during interpreter
    shutdown. Callers use this in cleanup paths where a raise would abandon the
    remaining cleanup steps, so a scheduling failure degrades to the synchronous
    ``pool.terminate()`` instead of leaving the pool live.
    """
    try:
        _schedule_pool_task(_close_pool(pool, label))
        return
    except Exception:
        logger.warning(
            "Could not schedule a bounded close for source pool %s "
            "(event loop unavailable); terminating it synchronously",
            label, exc_info=True,
        )
    # Bug-8041 R8 review BLOCKING-2: via the shared helper, so a pool asyncpg
    # refuses to terminate mid-initialisation is still killed at the holder level.
    _force_terminate_pool(pool, label)


async def _reap_idle_pools_locked() -> None:
    """Close and drop pools that are idle *or* past their maximum lifetime.

    Caller must already hold ``_lock``. Pools are closed *outside* the lock to
    avoid holding it during I/O, so this collects victims first.

    Bug-8041 R3 Finding 2: BOTH retirement conditions now route through
    ``_retire_pool_secure`` (the bounded-grace + force-terminate path). The
    previous design used a separate ``_close_pool`` (unbounded graceful close,
    no terminate) for idle-eligible pools. A checked-out borrower does not
    refresh ``last_used`` (only handing the pool out at acquisition time does),
    so with ``max_lifetime > POOL_IDLE_TTL_SECONDS`` a long-held borrower's
    pool went idle-eligible first, got removed from ``_pools`` and put into
    unbounded graceful close, and then at its security-age deadline the
    security branch could never find it in ``_pools`` to force-terminate it --
    so a still-checked-out, revoked-credential-vulnerable connection was never
    force-terminated. Using ``_retire_pool_secure`` for both conditions
    eliminates this gap: a truly idle pool (no borrowers) completes its
    graceful close immediately within the grace period and never reaches
    terminate; a pool with a stuck/long-held borrower is force-terminated
    after the bounded grace, regardless of which retirement condition fired.
    """
    now = time.monotonic()
    max_lifetime = _pool_max_lifetime_seconds()
    idle_keys: list[str] = []
    security_keys: list[str] = []
    for key, (_, last_used, created_at) in _pools.items():
        if max_lifetime > 0 and now - created_at > max_lifetime:
            security_keys.append(key)  # security age takes precedence
        elif now - last_used > POOL_IDLE_TTL_SECONDS:
            idle_keys.append(key)
    # Capture the log label AND drop the descriptor at the SAME time we pop the
    # pool (still under _lock), so a pool re-created with the same key gets a fresh
    # descriptor and the retire/close task cannot later corrupt or drop it
    # (opus5 F5/F6). The captured label travels with the task.
    #
    # Bug-8041 R3 Finding 2: idle pools now use _retire_pool_secure (bounded
    # grace + force-terminate) instead of the unbounded _close_pool, so a
    # checked-out pool can never escape security tracking by hitting the idle
    # deadline before the security-age deadline.
    retire_keys = [(key, "idle") for key in idle_keys] + [
        (key, "security-age") for key in security_keys
    ]
    for key, reason in retire_keys:
        label = _pool_log_label(key)
        pool, _, _ = _pools.pop(key)
        _pool_desc.pop(key, None)
        _schedule_pool_task(_retire_pool_secure(pool, label, reason=reason))


async def _close_pool_within_grace(
    pool: asyncpg.Pool, grace: float, *, observe_shutdown: bool, label: str,
) -> None:
    """Await ``pool.close()`` for up to ``grace`` seconds; the caller
    force-terminates on :class:`asyncio.TimeoutError`.

    Bug-8041 R6 HIGH-1: when ``observe_shutdown`` is True and process shutdown
    begins WHILE we are waiting, the remaining budget is shortened to the
    shutdown grace measured from the moment shutdown began — so a retirement that
    was already running (on the long security-age grace) when SIGTERM arrives
    does not keep waiting the full grace. The shutdown wake-up is delivered via
    ``_get_shutdown_event`` (set by ``close_all_pools``); the flag itself is
    re-read to compute the shortened deadline. When ``grace <= 0`` the pool is
    force-terminated immediately (strict mode). Raises :class:`asyncio.TimeoutError`
    if ``close()`` does not complete before the (possibly shortened) deadline."""
    if grace <= 0:
        # Strict mode: no grace -- terminate in-flight connections immediately.
        # Bug-8041 R8 review BLOCKING-2: via the shared helper (a bare
        # ``pool.terminate()`` is REFUSED, and silently, mid-initialisation).
        _force_terminate_pool(pool, label)
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + grace
    close_task = asyncio.ensure_future(pool.close())
    # If shutdown is already ignored (we entered during shutdown), never shorten.
    shortened = not observe_shutdown
    try:
        while True:
            now = loop.time()
            if not shortened and _shutting_down:
                shortened = True
                # R8 review round 11: the FOURTH grace-lookup call site. Round 10
                # claimed to have wrapped the last unguarded one and this was
                # missed; the property held only because the caller's generic
                # handler force-terminates. Guard it here so the invariant does
                # not depend on which handler happens to catch.
                try:
                    shortened_grace = _pool_shutdown_grace_seconds()
                except Exception:
                    logger.warning(
                        "Failed to compute the shortened shutdown grace for %s; "
                        "force-terminating immediately (fail-closed)", label,
                        exc_info=True,
                    )
                    raise asyncio.TimeoutError from None
                deadline = min(deadline, now + shortened_grace)
            remaining = deadline - now
            if remaining <= 0:
                raise asyncio.TimeoutError
            waiters: set[asyncio.Future] = {close_task}
            evt_task: asyncio.Task | None = None
            if not shortened:
                evt_task = asyncio.ensure_future(_get_shutdown_event().wait())
                waiters.add(evt_task)
            try:
                done, _pending = await asyncio.wait(
                    waiters, timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                if evt_task is not None and not evt_task.done():
                    evt_task.cancel()
            if close_task in done:
                # Propagate any exception raised by close() to the caller.
                await close_task
                return
            # Bug-8041 R6 review NB-6: if the shutdown event fired but the flag
            # is NOT set (event set without _shutting_down — a state the reset
            # helper never produces, but guard defensively), stop re-waiting on
            # the event so we cannot busy-spin: fall through to a plain timed
            # wait on close_task for the remaining budget.
            if evt_task is not None and evt_task in done and not _shutting_down:
                shortened = True
            # Otherwise: either the shutdown event fired with the flag set
            # (recompute a shorter deadline on the next iteration) or the slice
            # timed out (remaining will be <= 0 and we raise TimeoutError).
    except BaseException:
        if not close_task.done():
            close_task.cancel()
        raise


async def _retire_pool_secure(
    pool: asyncpg.Pool, label: str, *, reason: str = "security-age",
) -> None:
    """Retire a pool: let in-flight borrowers finish for a BOUNDED grace,
    then FORCE-terminate (Bug-8040 hardening).

    Used for both security-age and idle retirement (Bug-8041 R3 Finding 2).
    The ``reason`` parameter distinguishes the two in log messages so ops can
    tell whether a pool was retired for credential-age or inactivity.

    The pool is already removed from ``_pools`` (no new checkout can reuse it), so
    only legitimate in-flight materialisation remains. A graceful ``pool.close()``
    would wait for those borrowers *indefinitely*, so a connection authenticated
    with a since-revoked source password could keep executing for the entire
    (possibly hours-long) operation. Bounding the wait at
    ``_pool_security_max_checkout_seconds`` and then calling the synchronous
    ``pool.terminate()`` force-closes any still-checked-out connection, capping the
    CLIENT-side hold. Note the server-side statement is bounded separately by the
    session ``statement_timeout`` (set to the DDL bound at checkout), so the
    effective revocation window is ``max(grace, server statement_timeout)``.

    Fail-CLOSED on the grace lookup itself (Bug-8041 residual review finding):
    the pool is already popped from ``_pools`` by the time this runs, so if
    computing the grace raised instead of force-terminating immediately, the
    pool's live (possibly revoked-credential) connections would be leaked with
    no owner and no bound -- strictly worse than the bug this hardening fixes."""
    # Bug-8041 R6 HIGH-1: capture whether shutdown is ALREADY in progress. If not,
    # the grace-wait below dynamically OBSERVES the shutdown flag flipping so a
    # retirement already running when SIGTERM arrives switches to the short
    # shutdown grace instead of keeping the (possibly hours-long) security-age
    # grace it sampled here.
    observe_shutdown = not _shutting_down
    try:
        # Bug-8041 R4 MEDIUM-1: during shutdown, use the short shutdown grace
        # (not the DDL-derived security-age grace) so the service stops promptly.
        if _shutting_down:
            grace = _pool_shutdown_grace_seconds()
        else:
            grace = _pool_security_max_checkout_seconds()
    except Exception:
        logger.warning(
            "Failed to compute checkout grace for %s (%s); "
            "force-terminating immediately (fail-closed)", label, reason,
            exc_info=True,
        )
        grace = 0
    # Bug-8041 R6 review NB-4: measure the ACTUAL wait so the force-terminate log
    # reports the real elapsed time, not the sampled grace (which is misleading
    # after HIGH-1 shortens the wait mid-flight, and renders a sub-second float as
    # "0s" under %ds).
    start = time.monotonic()
    try:
        await _close_pool_within_grace(
            pool, grace, observe_shutdown=observe_shutdown, label=label,
        )
        # R11: same "close() returned is not evidence" check as ``_close_pool``.
        # This is the SECURITY-AGE path, so a pool reported as retired while its
        # possibly-revoked credential is still connected is the exact failure
        # Bug-8040/8041 exist to prevent.
        if _live_connection_holders(pool):
            logger.warning(
                "%s retirement of source pool %s reported success but it still "
                "owns open connection(s); force-terminating", reason, label,
            )
            _force_terminate_pool(pool, label)
        else:
            logger.debug("Retired %s source pool %s", reason, label)
    except asyncio.TimeoutError:
        logger.warning(
            "Force-terminating %s source pool %s after %.1fs (checkout grace %ss)",
            reason, label, time.monotonic() - start, grace,
        )
        _force_terminate_pool(pool, label)  # R8 review BLOCKING-2: shared helper
    except asyncio.CancelledError:
        raise
    except Exception:
        # Bug-8041 R8 review round 10: force-terminate, do not merely log. This
        # is the SECURITY-AGE / revoked-credential retirement path -- the reason
        # Bug-8040/8041 exist -- and the pool is already popped from ``_pools``,
        # so nothing revisits it: a ``close()`` that raises left two
        # authenticated connections live forever AND invisible to the disposal
        # ledger, because ``_force_terminate_pool`` was never called. Its sibling
        # ``_close_pool`` has carried this exact guard, with this exact
        # reasoning, since R7 round-2 F1; the invariant belongs on both.
        logger.warning("Error retiring %s source pool %s; force-terminating "
                       "instead", reason, label, exc_info=True)
        _force_terminate_pool(pool, label)


async def _acquire_with_retry(
    scope: str, host: str, port: int, database: str, user: str, password: str,
) -> asyncpg.Pool:
    """Get a pool, retrying once if it was closed by a concurrent reaper.

    Fable R2 FINDING-4: the closed-pool check runs before the pool is returned
    (and before the caller's ``yield``), so an InterfaceError raised during the
    caller's use of the connection propagates normally instead of triggering a
    double-yield in the async context manager.
    """
    pool = await _get_or_create_pool(scope, host, port, database, user, password)
    if pool._closed:
        logger.debug("Pool was closed between get and use; retrying with a fresh pool")
        pool = await _get_or_create_pool(scope, host, port, database, user, password)
    return pool


# Bug-8041 R4 MEDIUM-3: maximum number of acquire retries on InterfaceError
# (pool closing/closed between lookup and checkout). Two near-simultaneous
# retirements can cause a second InterfaceError on the fresh pool, so a single
# retry is insufficient.  3 attempts covers two back-to-back retirements with
# margin.
_ACQUIRE_MAX_ATTEMPTS = 3


@asynccontextmanager
async def acquire_source_connection(
    scope: str, host: str, port: int, database: str, user: str, password: str,
) -> AsyncIterator[asyncpg.Connection]:
    """Acquire a pooled source connection scoped to a tenant/connection identity.

    ``scope`` (F-014-05 / Bug-8039) is REQUIRED and must uniquely identify the
    owning tenant (and, where available, the connection). It is the first pool-key
    component, so a connection is never shared across tenants -- callers must not
    pass a constant/empty scope for multi-tenant traffic.

    Bug-8041 R3 Finding 3: the ``pool.acquire()`` call itself is retried on
    ``InterfaceError`` (pool is closing/closed). A concurrent reaper can retire
    the pool between ``_acquire_with_retry`` returning the pool reference and
    ``pool.acquire()`` being called here, setting ``_closing=True`` on the
    asyncpg pool. Without the retry, the caller gets a spurious
    ``InterfaceError: pool is closing`` for no user-visible reason.

    Bug-8041 R4 MEDIUM-3: the retry is now a bounded loop (up to
    ``_ACQUIRE_MAX_ATTEMPTS``) so a second near-simultaneous retirement does
    not leak a spurious InterfaceError to the caller either.
    """
    pool = await _acquire_with_retry(scope, host, port, database, user, password)
    for attempt in range(_ACQUIRE_MAX_ATTEMPTS):
        try:
            ctx = pool.acquire()
            conn = await ctx.__aenter__()
            break
        except Exception as exc:
            # Only retry on pool-closing/closed InterfaceErrors, not on genuine
            # DB errors. The isinstance check prevents an application error whose
            # text happens to contain "pool is closed" from triggering a silent
            # pool re-creation.
            if (
                isinstance(exc, asyncpg.InterfaceError)
                and ("pool is closing" in str(exc) or "pool is closed" in str(exc))
                and attempt < _ACQUIRE_MAX_ATTEMPTS - 1
            ):
                logger.debug(
                    "Pool.acquire() hit closing/closed pool (attempt %d/%d); "
                    "retrying with a fresh pool",
                    attempt + 1, _ACQUIRE_MAX_ATTEMPTS,
                )
                pool = await _get_or_create_pool(
                    scope, host, port, database, user, password,
                )
                continue
            raise
    try:
        yield conn
    finally:
        await ctx.__aexit__(None, None, None)


async def remove_pool(
    scope: str, host: str, port: int, database: str, user: str,
    password: str | None = None,
) -> None:
    """Remove and close pool(s) for a scoped endpoint. Must be called from an async context.

    F-014-06: with ``password`` omitted, every pool for the
    ``scope|user@host:port/database`` endpoint (regardless of credential
    fingerprint) is removed — useful for an explicit invalidation after a
    credential change within a single process."""
    # C-01 fix: acquire lock before modifying _pools dict
    if password is not None:
        keys = [_pool_key(scope, host, port, database, user, password)]
    else:
        prefix = f"{scope}|{user}@{host}:{port}/{database}#"
        keys = [k for k in _pools if k.startswith(prefix)]
    victims = []
    async with _lock:
        for key in keys:
            entry = _pools.pop(key, None)
            if entry is not None:
                # Capture label + drop descriptor together under the lock (F6: the
                # descriptor must not leak even when the pool is already closed).
                label = _pool_log_label(key)
                _pool_desc.pop(key, None)
                victims.append((label, entry[0]))
    # Close the pools outside the lock to avoid holding it during I/O
    for label, pool in victims:
        if pool and not pool._closed:
            await _close_pool(pool, label)


def _pool_shutdown_grace_seconds() -> int:
    """Grace period for explicit pool removal / shutdown close (Bug-8041 R3/R7).

    A short bounded wait before force-terminating, so shutdown and explicit
    removal never hang indefinitely on a checked-out borrower. Distinct from
    the DDL-derived security-age checkout grace (which is much longer).

    Bug-8041 R7 LOW-1 -- DERIVED, not independently defaulted. This grace only
    means anything if the process actually survives long enough to use it, and
    that depends on TWO things outside this module: how long uvicorn spends
    draining requests before it even invokes the lifespan shutdown hook, and how
    long the platform waits before SIGKILL (``stop_grace_period`` in compose,
    a fixed ~10s on Cloud Run). Previously this knob defaulted to 10s completely
    independently of a hardcoded ``stop_grace_period: 15s``, so an operator
    raising ``SOURCE_POOL_SHUTDOWN_GRACE_SECONDS`` past ~14s silently
    reintroduced the SIGKILL-mid-close race this whole effort exists to prevent
    -- the same "two independently configured knobs that must agree" defect
    class this lane already root-caused for checkout-grace vs DDL-timeout.

    Now every phase is derived from ONE knob,
    ``TESSALLITE_SHUTDOWN_BUDGET_SECONDS`` (see
    ``shared/config/shutdown_budget.py``). ``SOURCE_POOL_SHUTDOWN_GRACE_SECONDS``
    survives as an override that can only ever make the grace SHORTER: a value
    above what the budget allows is refused (with a warning naming the knob to
    raise instead) rather than silently overrunning the SIGKILL window.
    """
    budget = resolve_shutdown_budget()
    derived = budget.pool_grace_seconds
    # Bug-8041 R7 round-5 F4 / R8 review finding 7: an unsubstituted deploy
    # placeholder or a below-minimum budget reaches the container silently on
    # every platform with no config-parse step of its own (i.e. Cloud Run), so
    # this hot path shouts about it too -- ONCE per key, because it runs on every
    # pool close. The messages come from ``budget_diagnostics()``, the single
    # implementation every start-up and runtime path shares; this module used to
    # hand-write its own fourth copy, which is how the previous round's new
    # ``malformed`` check reached three of the four paths and missed one.
    for _key, _message in budget_diagnostics():
        _warn_once(f"shutdown_budget_{_key}", "%s", _message)
    raw = os.getenv("SOURCE_POOL_SHUTDOWN_GRACE_SECONDS")
    if raw is None or raw.strip() == "":
        return derived
    try:
        val = int(raw)
    except (TypeError, ValueError):
        _warn_once(
            "shutdown_grace_invalid",
            "Invalid SOURCE_POOL_SHUTDOWN_GRACE_SECONDS=%r; using the "
            "budget-derived %ds",
            raw, derived,
        )
        return derived
    if val > derived:
        _warn_once(
            "shutdown_grace_exceeds_budget",
            "SOURCE_POOL_SHUTDOWN_GRACE_SECONDS=%ds exceeds the %ds the total "
            "shutdown budget allows for pool closing; using %ds. Raise "
            "TESSALLITE_SHUTDOWN_BUDGET_SECONDS (and the platform's SIGTERM "
            "grace with it) instead -- a longer pool grace alone would be "
            "SIGKILLed mid-close, leaving source connections open.",
            val, derived, derived,
        )
        return derived
    return max(1, val)


async def _close_pool(pool: asyncpg.Pool, label: str) -> None:
    """Close a pool with a bounded grace, force-terminating on timeout.

    Bug-8041 R3 OPEN-3: the previous unbounded ``await pool.close()`` would
    hang indefinitely if a borrower was still checked out. ``remove_pool``
    and ``close_all_pools`` (the service shutdown hook) both call this, so a
    long CTAS during SIGTERM would block shutdown until Docker/Cloud Run
    SIGKILLs the container. The bounded grace + terminate mirrors the
    structure of ``_retire_pool_secure`` for consistency.
    """
    # Bug-8041 R8 review round 6: fail CLOSED on the grace lookup, exactly as
    # ``_retire_pool_secure`` already does. Every caller has already popped or
    # disowned the pool by the time it gets here, so a raise from the resolver
    # would leak it with no owner and no bound -- the sibling primitive carries
    # this guard and its reasoning; the invariant belongs on both.
    try:
        grace = _pool_shutdown_grace_seconds()
    except Exception:
        logger.warning(
            "Failed to compute the shutdown grace for %s; force-terminating "
            "immediately (fail-closed)", label, exc_info=True,
        )
        # A zero grace makes the ``wait_for`` below time out immediately, which
        # routes straight into the force-terminate branch -- no separate strict
        # path is needed, and adding one would be code no behaviour reaches.
        grace = 0
    try:
        await asyncio.wait_for(pool.close(), timeout=grace)
        # Bug-8041 R8 review round 11: "close() returned" is NOT evidence. Round
        # 7 established that inside ``_force_terminate_pool`` ("VERIFY, do not
        # assume") because asyncpg early-returns from BOTH ``close()`` and
        # ``terminate()`` once ``_closed`` is set -- which ``Pool.close()`` sets
        # in a ``finally`` that also runs when its own terminate raised on the
        # way out. The invariant was never propagated one layer up: measured,
        # ``close_all_pools()`` returned in 0.00s, logged a clean close, left 2
        # authenticated holders open and reported 0 undisposed with NO warning.
        if _live_connection_holders(pool):
            logger.warning(
                "source pool %s reported a clean close but still owns open "
                "connection(s); force-terminating", label,
            )
            _force_terminate_pool(pool, label)
        else:
            logger.debug("Closed source pool %s", label)
    except asyncio.TimeoutError:
        logger.warning(
            "Force-terminating source pool %s after %ds shutdown grace",
            label, grace,
        )
        _force_terminate_pool(pool, label)  # R8 review BLOCKING-2: shared helper
    except Exception:
        # Bug-8041 R7 round-2 F1: a close that RAISES must still leave the pool
        # dead. asyncpg's ``Pool.close()`` calls ``_check_init()``, which raises
        # ``InterfaceError: pool is being initialized, but not yet ready`` for a
        # pool still in ``_initializing`` -- exactly the state an orphaned
        # creator leaves behind. Logging alone made the orphan-close fallback a
        # no-op in the one window it was added for.
        logger.warning(
            "Error closing source pool %s; force-terminating instead",
            label, exc_info=True,
        )
        # Bug-8041 R8 review BLOCKING-2: the comment above correctly names the
        # ``_initializing`` state as the reason ``close()`` raised -- and the
        # ``pool.terminate()`` that used to be here is refused by ``_check_init``
        # in that SAME state, so this whole fallback was a no-op in the one
        # window it exists for. The shared helper drops to the holder level.
        _force_terminate_pool(pool, label)


async def close_all_pools() -> None:
    """Close all pools. Acquires lock to safely iterate and clear the dict.

    Bug-8041 R3 OPEN-3: uses the bounded ``_close_pool`` (with grace +
    terminate) so a checked-out borrower during service shutdown never blocks
    the lifespan shutdown indefinitely.

    Bug-8041 R4 MEDIUM-1 hardening:
    - Sets ``_shutting_down`` so new pool creations are rejected and in-progress
      retirements use the short shutdown grace, not the DDL-derived grace.
    - Cancels any in-flight ``_pending_creates`` futures so callers blocked on
      pool creation are unblocked immediately.
    - Waits on ``_pending_closes`` (with the shutdown grace as a bound) so the
      function does not return while live, possibly-revoked-credential
      connections are still open.

    Bug-8041 R6 HIGH-1 hardening:
    - Signals ``_get_shutdown_event`` so a retirement ALREADY in flight on the
      long security-age grace observes the flag flip and shortens to the short
      shutdown grace immediately, instead of keeping its sampled grace.
    - Runs the ``_close_pool`` gather AND the ``_pending_closes`` wait
      CONCURRENTLY against a SINGLE shutdown deadline, so the total wall time is
      bounded by ~one shutdown grace (not two serial grace periods that add up
      and overrun the platform SIGTERM-to-SIGKILL window).

    Bug-8041 R8 hardening — DISPOSE AT THE DEADLINE, DO NOT JUST LOG:
    - When the shared deadline expires with in-flight creations still unsettled,
      every pool object those creators already own is force-terminated
      SYNCHRONOUSLY before this function returns (``_abandon_unsettled_creations``).
      Waiting longer is not available -- the platform SIGKILLs us -- so the only
      way to stop a live authenticated connection outliving this call is to kill
      it here. The 6th external gate reproduced the old behaviour: shutdown
      returned with 3 unsettled creators and 6 live connection holders.
    - See the MODULE docstring for the exact two-part guarantee this delivers and
      the one asyncpg-imposed residual (a pool still ``_initializing`` cannot be
      terminated by anyone until its init settles). The WARNING below reports both
      counts so that residual is visible in the log rather than assumed away.
    """
    global _shutting_down
    # Bug-8041 R4 review FINDING-4: set the shutdown flag FIRST under the lock
    # so a concurrent reuse-path acquire that holds the lock cannot restart the
    # sweep between _stop_lifecycle_sweep and flag-set.  Then stop the sweep
    # AFTER releasing the lock (keeping the try/except so a stale task on a
    # closed loop cannot prevent pool cleanup).
    # C-01 fix: acquire lock before accessing _pools dict
    async with _lock:
        _shutting_down = True
        # Bug-8041 R8 review round 8: start the disposal ledger clean, so the
        # overrun count below reflects THIS shutdown.
        _undisposed_pools.clear()
        # Bug-8041 R6 HIGH-1: wake any in-flight retirement so it re-reads the
        # shutdown flag and shortens its grace NOW, under the same lock section
        # that sets the flag (no window where the flag is set but no wake-up).
        _get_shutdown_event().set()
        pools_to_close = [(_pool_log_label(key), entry[0]) for key, entry in _pools.items()]
        _pools.clear()
        _pool_desc.clear()
        # Bug-8041 R4 MEDIUM-1: unblock any caller WAITING on an in-flight pool
        # creation so it fails fast instead of hanging out the shutdown.
        for pending_key, fut in list(_pending_creates.items()):
            if not fut.done():
                fut.set_exception(asyncpg.InterfaceError(
                    "Source pool manager is shutting down; pending creation cancelled"
                ))
                # Suppress "exception was never retrieved" if no waiters.
                try:
                    fut.exception()
                except Exception:
                    pass
        _pending_creates.clear()
        # Bug-8041 R7 HIGH-1: resolving those futures unblocks WAITERS -- it does
        # NOT stop the CREATOR, which is still inside asyncpg.create_pool. Snapshot
        # the in-flight creations so the drain below waits for each creator to
        # relinquish ownership. The snapshot is complete: registration happens
        # under this same lock and is refused once ``_shutting_down`` is set, so no
        # creation can appear after this point.
        inflight_creations = list(_inflight_creates)
    # Bug-8041 R4 review FINDING-4: stop the sweep AFTER the lock block (where
    # _shutting_down is already True), so any concurrent reuse-path acquire that
    # completes between now and cancel sees the flag and does not restart it.
    try:
        _stop_lifecycle_sweep()
    except Exception:
        logger.debug("Lifecycle sweep stop failed (stale task?)", exc_info=True)
    # Bug-8041 R6 HIGH-1: run BOTH close phases CONCURRENTLY against ONE shared
    # deadline, not as two serial phases that add up.
    #
    # The previous design ran the ``_close_pool`` gather (bounded ~grace) and
    # then, separately, the ``_pending_closes`` wait (bounded grace+1) — so the
    # total wall time could reach ~2*grace+1 (measured 21s at the 10s default),
    # overrunning Cloud Run's 10s SIGTERM-to-SIGKILL window. Each underlying task
    # already self-bounds to ~grace and force-terminates (``_close_pool`` and,
    # via HIGH-1, in-flight retirements now observe the short shutdown grace).
    #
    # Bug-8041 R6 review BLOCKING-1: DRAIN ``_pending_closes`` (re-read it after
    # each await) rather than snapshotting it once. A close scheduled DURING
    # shutdown — the install-guard close for a creator that was already inside
    # ``asyncpg.create_pool`` when shutdown began, added to ``_pending_closes``
    # only when that creator resumes on our first await — would be MISSED by a
    # one-shot snapshot and left running with a live connection open, defeating
    # the "does not return while live connections are open" guarantee. Draining
    # under one shared wall-clock deadline keeps the total bounded to ~grace + a
    # small margin while still awaiting every late-scheduled close.
    # Bug-8041 R8 review round 10: fail CLOSED here too. It runs AFTER
    # ``_pools.clear()`` and BEFORE any close is scheduled, so
    # a raise would escape the lifespan hook with every cached pool already
    # disowned and nothing closing them. Strictly worse than the case the sibling
    # guards were added for.
    try:
        grace = _pool_shutdown_grace_seconds()
    except Exception:
        logger.warning(
            "Failed to compute the shutdown grace; force-terminating every "
            "cached pool immediately (fail-closed)", exc_info=True,
        )
        grace = 0
    loop = asyncio.get_running_loop()
    deadline = loop.time() + grace + 1  # single budget: one grace + terminate margin
    close_coros = [_close_pool(pool, label) for label, pool in pools_to_close]
    close_gather = (
        asyncio.ensure_future(asyncio.gather(*close_coros, return_exceptions=True))
        if close_coros else None
    )
    #
    # Bug-8041 R7 HIGH-1: the drain also waits on every IN-FLIGHT CREATION. The
    # R6 drain only ever looked at close TASKS, so with no pool cached and no
    # close scheduled yet the very first ``waitset`` was empty and the loop broke
    # immediately -- returning "shutdown complete" while a creator was still
    # connecting. Waiting on the creation handles closes that hole, and because
    # each creator now aborts as soon as it observes the shutdown event, the wait
    # is short rather than a full connect timeout.
    overrun_outstanding = 0
    overrun_creations = 0
    while True:
        waitset: list = []
        ev_waiters: list[asyncio.Task] = []
        if close_gather is not None and not close_gather.done():
            waitset.append(close_gather)
        # Re-read _pending_closes each iteration so a close scheduled during
        # shutdown (e.g. by a creator's install-guard) is still drained.
        waitset.extend(t for t in _pending_closes if not t.done())
        for creation in inflight_creations:
            if not creation.finished.is_set():
                waiter = asyncio.ensure_future(creation.finished.wait())
                ev_waiters.append(waiter)
                waitset.append(waiter)
        if not waitset:
            break
        remaining = deadline - loop.time()
        if remaining <= 0:
            overrun_outstanding = len(waitset)
            for waiter in ev_waiters:
                waiter.cancel()
            # Bug-8041 R8 (6th external gate, HIGH): waiting is over, so DISPOSE
            # instead. Every in-flight creation that already owns a pool object is
            # force-terminated here, synchronously, BEFORE this function returns --
            # previously the deadline branch only logged and the live connections
            # those creators held stayed open until each creator happened to unwind.
            overrun_creations = _abandon_unsettled_creations(inflight_creations)
            # Bug-8041 R8 review round 6, BLOCKING: do the SAME for the CACHED
            # pools. R8 disposed of in-flight creations here and left population 1
            # to each ``_close_pool`` task reaching its own force-terminate about a
            # second before this deadline -- a timing coincidence, not a guarantee,
            # and the docstring claimed otherwise. Reproduced with a cached pool
            # whose ``close()`` ignores the cancellation ``wait_for`` delivers (the
            # cached mirror of the gate's cancellation-resistant initializer): 2
            # authenticated holders were still open at return while the WARNING
            # below reported 0 undisposed. ``_force_terminate_pool`` is idempotent,
            # so an already-closed pool costs a DEBUG line.
            if close_gather is not None and not close_gather.done():
                for cached_label, cached_pool in pools_to_close:
                    try:
                        _force_terminate_pool(cached_pool, cached_label)
                    except Exception:
                        # HONEST COVERAGE NOTE (R8 review round 7): defence in
                        # depth only. ``_force_terminate_pool`` is defensive
                        # throughout and no real pool state makes it raise, so
                        # deleting this handler does NOT go red -- it is kept
                        # because an exception escaping here would abort the
                        # lifespan shutdown, skipping every remaining cached pool
                        # AND the overrun warning below.
                        logger.warning(
                            "Force-terminating source pool %s at the shutdown "
                            "deadline raised", cached_label, exc_info=True,
                        )
                        _undisposed_pools[id(cached_pool)] = cached_label
            break
        try:
            # asyncio.wait (not wait_for/gather) so hitting the deadline does NOT
            # cancel the in-flight close tasks out from under themselves.
            await asyncio.wait(waitset, timeout=remaining)
        finally:
            for waiter in ev_waiters:
                if not waiter.done():
                    waiter.cancel()
    # Bug-8041 R8 review round 8: count from the LEDGER, not from this
    # function's own call sites. ``_close_pool`` and ``_retire_pool_secure``
    # discard the disposal boolean, so a pool one of them already failed to
    # dispose of used to be reported as 0 undisposed while its own log line said
    # 2 connections were still open.
    overrun_undisposed = len(_undisposed_pools)
    if overrun_outstanding:
        logger.warning(
            "Shutdown: pool close/creation did not complete within the %ds "
            "shutdown grace (%d operation(s) still outstanding, %d of them "
            "in-flight pool creation(s); %d pool(s) STILL HAD AN OPEN CONNECTION "
            "after being force-terminated). Every other source connection has "
            "been force-closed; this process is now past its share of the "
            "shutdown budget (TESSALLITE_SHUTDOWN_BUDGET_SECONDS).",
            grace, overrun_outstanding, overrun_creations, overrun_undisposed,
        )
    elif overrun_undisposed:
        # Bug-8041 R8 review round 9: a disposal can FAIL without anything
        # overrunning -- ``_close_pool`` finishes inside the grace, its own
        # force-terminate is refused, and it discards the boolean. Gating this
        # message on the overrun count alone meant shutdown returned with live
        # authenticated connections and told the operator NOTHING (reproduced:
        # 2 open holders, no warning). It is also not an overrun, so it does not
        # get the overrun wording.
        logger.warning(
            "Shutdown completed inside its %ds grace, but %d pool(s) could NOT "
            "be disposed of: authenticated source connection(s) may outlive this "
            "process. See the per-pool 'Force-terminating source pool ... FAILED' "
            "line(s) above.", grace, overrun_undisposed,
        )


# ---------------------------------------------------------------------------
# Bug-8041 R3 Finding 1: periodic lifecycle sweep
# ---------------------------------------------------------------------------

def _pool_sweep_interval_seconds() -> int:
    """Configurable sweep interval for the periodic lifecycle driver.

    Default 60s. Must be shorter than ``_POOL_MAX_LIFETIME_DEFAULT_SECONDS``
    (900s) so security-age retirement is enforced within a bounded window
    after the deadline, independent of new acquisitions.
    """
    raw = os.getenv("SOURCE_POOL_SWEEP_INTERVAL_SECONDS")
    if raw is None or raw.strip() == "":
        return _POOL_SWEEP_INTERVAL_DEFAULT_SECONDS
    try:
        val = int(raw)
    except (TypeError, ValueError):
        _warn_once(
            "sweep_interval_invalid",
            "Invalid SOURCE_POOL_SWEEP_INTERVAL_SECONDS=%r; using default %ds",
            raw, _POOL_SWEEP_INTERVAL_DEFAULT_SECONDS,
        )
        return _POOL_SWEEP_INTERVAL_DEFAULT_SECONDS
    return max(10, min(val, 600))  # clamp to [10s, 600s]


async def _lifecycle_sweep_loop() -> None:
    """Background loop that sweeps all pools on a fixed interval, enforcing
    both security-age and idle deadlines proactively -- not just
    opportunistically on the next acquire (Bug-8041 R3 Finding 1).

    This task runs for the lifetime of the process (started lazily on the
    first pool creation) and is stopped by ``close_all_pools`` at shutdown.
    """
    interval = _pool_sweep_interval_seconds()
    logger.debug(
        "Source pool lifecycle sweep started (interval=%ds)", interval,
    )
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.debug("Source pool lifecycle sweep cancelled")
            return
        try:
            async with _lock:
                await _reap_idle_pools_locked()
        except Exception:
            logger.warning(
                "Error during periodic pool lifecycle sweep", exc_info=True,
            )


def _on_lifecycle_sweep_done(task: asyncio.Task) -> None:
    """Done-callback for the sweep task (Bug-8041 R4).

    Bug-8041 R4 review FINDING-5: ignore callbacks from a task that is no
    longer the current ``_lifecycle_sweep_task`` -- a late callback from a
    replaced task must not flag a restart for the healthy replacement.

    Bug-8041 R4 review FINDING-6: also schedule a delayed restart so the sweep
    self-heals even when no new acquisitions arrive.  The zero-traffic case (a
    pool whose credential is revoked and receives NO new traffic) is the exact
    scenario the sweep exists for -- relying solely on the next acquire to
    restart it leaves that case unbounded.

    Bug-8041 R6 MEDIUM-3: the self-heal is now BOUNDED. The previous
    ``call_soon`` restart with no delay and no budget meant a sweep coroutine
    that exited before its first ``await`` would respawn tens of thousands of
    times per second (measured), and the callback never retrieved
    ``task.exception()`` so a crashing sweep was invisible in logs. This now:
    (a) retrieves and logs the exception at ERROR when the sweep ended
        abnormally, so a crash is visible;
    (b) schedules the restart via ``call_later`` with a NON-ZERO backoff; and
    (c) gives up automatic restarting (logging an ERROR) after
        ``_SWEEP_RESTART_MAX`` restarts within ``_SWEEP_RESTART_WINDOW_SECONDS``,
        so a persistently-crashing sweep can never consume the event loop.
    """
    global _lifecycle_sweep_needs_restart
    # Bug-8041 R7 MEDIUM-1(b): retrieve and log the exception BEFORE the identity
    # guard. The guard previously returned first, so a SUPERSEDED task's crash was
    # never logged AND its traceback was never retrieved -- a sweep that crashes
    # and is then replaced by an acquire-path restart was completely invisible.
    # A cancelled task has no retrievable exception (retrieving it would raise),
    # so guard on cancellation first.
    if not task.cancelled():
        exc = task.exception()
        if exc is not None:
            logger.error(
                "Source pool lifecycle sweep task crashed%s",
                "" if task is _lifecycle_sweep_task else " (superseded task)",
                exc_info=exc,
            )
    # Ignore RESTART decisions from superseded tasks (FINDING-5): a late callback
    # from a replaced task must not flag a restart for its healthy replacement.
    if task is not _lifecycle_sweep_task:
        return
    if _shutting_down:
        return
    _lifecycle_sweep_needs_restart = True
    logger.debug("Lifecycle sweep task exited; restart flagged")
    # MEDIUM-3(b): back off before the self-heal so a sweep that exits before its
    # first await cannot spin the restart loop. The restart BUDGET itself is
    # enforced inside ``_ensure_lifecycle_sweep_running`` (Bug-8041 R7 MEDIUM-1):
    # enforcing it here only covered this callback path, and every
    # acquisition-driven call bypassed it entirely.
    try:
        loop = task.get_loop()
    except Exception:
        return  # loop closing -- the next acquire will pick up the restart flag
    try:
        loop.call_later(_SWEEP_RESTART_BACKOFF_SECONDS, _ensure_lifecycle_sweep_running)
    except Exception:
        pass  # loop closing -- next acquire will pick up the flag


def _ensure_lifecycle_sweep_running() -> None:
    """Lazily start the periodic lifecycle sweep task on the first pool creation.

    Idempotent -- subsequent calls are no-ops if the task is already running
    on the current event loop. If the task belongs to a different (possibly
    closed) loop, it is treated as absent and a fresh one is started on the
    current loop (Bug-8041 R3 R5-1).

    Bug-8041 R4: also checks ``_lifecycle_sweep_needs_restart`` (set by the
    task's done-callback) so a sweep that died for ANY reason (CancelledError,
    exception, event-loop restart) is restarted on the next pool hand-out --
    from BOTH the creation and reuse paths.

    Uses ``task.get_loop() is loop`` instead of ``id(loop)`` for loop identity
    -- CPython reuses freed event-loop addresses, so ``id()`` can collide and
    make this function a permanent no-op on a new loop. ``Task.get_loop()``
    holds a strong reference to its owning loop, so reuse is impossible.
    """
    global _lifecycle_sweep_task, _lifecycle_sweep_needs_restart
    if _shutting_down:
        return  # Bug-8041 R4 MEDIUM-1: no new sweep during shutdown
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no event loop -- cannot start the task (test/import-time)
    # Check loop identity via the task's own strong loop reference (R5-1).
    if (
        _lifecycle_sweep_task is not None
        and not _lifecycle_sweep_task.done()
        and _lifecycle_sweep_task.get_loop() is loop
        and not _lifecycle_sweep_needs_restart
    ):
        return
    # Bug-8041 R7 MEDIUM-1(a): enforce the restart budget HERE -- this is the
    # single place a sweep task is ever started, so every restart trigger (the
    # done-callback's backed-off self-heal AND every acquisition on the create or
    # reuse path) is bounded by it. Enforcing it only in the done-callback left
    # the acquire path unbounded: with a sweep coroutine that crashes
    # immediately, sustained traffic restarted it once per acquisition forever
    # (measured: a budget of 2 still produced 23 starts across 20 acquires).
    old_task = _lifecycle_sweep_task
    is_restart = old_task is not None
    if is_restart and not _sweep_restart_budget_available(loop):
        return
    # Bug-8041 R4 review FINDING-5: cancel any existing task before creating a
    # new one so a replaced task does not continue running as an orphan that
    # _stop_lifecycle_sweep can no longer reach.
    if old_task is not None and not old_task.done():
        try:
            old_task.cancel()
        except Exception:
            pass  # stale task on a closed loop
    _lifecycle_sweep_needs_restart = False
    if is_restart:
        _sweep_restart_times.append(loop.time())
    _lifecycle_sweep_task = loop.create_task(_lifecycle_sweep_loop())
    _lifecycle_sweep_task.add_done_callback(_on_lifecycle_sweep_done)


def _sweep_restart_budget_available(loop: asyncio.AbstractEventLoop) -> bool:
    """Whether another sweep RESTART is allowed in the current rolling window.

    Bug-8041 R6 MEDIUM-3 / R7 MEDIUM-1: a persistently-crashing sweep must never
    be able to consume the event loop, no matter which path notices it died. The
    give-up message is emitted once per exhaustion (not once per blocked
    attempt) because the acquire path can reach here on every pooled acquire.
    """
    global _sweep_restart_budget_exhausted
    now = loop.time()
    _sweep_restart_times[:] = [
        t for t in _sweep_restart_times if now - t < _SWEEP_RESTART_WINDOW_SECONDS
    ]
    if len(_sweep_restart_times) >= _SWEEP_RESTART_MAX:
        if not _sweep_restart_budget_exhausted:
            _sweep_restart_budget_exhausted = True
            logger.error(
                "Source pool lifecycle sweep restarted %d times within %ss; "
                "giving up automatic restart. Security-age retirement now depends "
                "on acquisition-driven reaping until the process/loop is restarted.",
                len(_sweep_restart_times), _SWEEP_RESTART_WINDOW_SECONDS,
            )
        return False
    _sweep_restart_budget_exhausted = False
    return True


def _stop_lifecycle_sweep() -> None:
    """Cancel the periodic lifecycle sweep task (called at shutdown).

    Bug-8041 R3 OPEN-4: safe against a stale task on a closed loop --
    ``task.cancel()`` on a closed loop raises ``RuntimeError`` which must
    not prevent pool cleanup in ``close_all_pools``.
    """
    global _lifecycle_sweep_task, _lifecycle_sweep_needs_restart
    global _sweep_restart_budget_exhausted
    if _lifecycle_sweep_task is not None and not _lifecycle_sweep_task.done():
        try:
            _lifecycle_sweep_task.cancel()
        except Exception:
            pass  # stale task on a closed loop -- nothing to cancel
    _lifecycle_sweep_task = None
    _lifecycle_sweep_needs_restart = False  # Bug-8041 R4: intentional stop
    _sweep_restart_times.clear()  # Bug-8041 R6 MEDIUM-3: reset the restart budget
    _sweep_restart_budget_exhausted = False
