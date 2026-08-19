"""Per-IP access governance for the JDBC TCP listener (F-001-07).

The JDBC listener is a publicly exposed, pre-authentication surface. The HTTP
rate limiter on the FastAPI app does not cover the raw TCP socket, so two
in-process controls bound abuse before a connection reaches model-service:

- **Concurrent-connection cap** — at most ``GATEWAY_JDBC_MAX_CONN_PER_IP``
  open connections per client IP at any moment. A connection-flood from one
  host cannot exhaust the event loop / file descriptors for everyone.
- **Failed-auth throttle** — a sliding window of recent authentication
  failures per IP. Once ``GATEWAY_JDBC_MAX_AUTH_FAILURES`` failures occur
  within ``GATEWAY_JDBC_AUTH_FAILURE_WINDOW_SECONDS``, further connection
  attempts from that IP are refused until the window drains, throttling
  password brute-force.

Both controls are fail-closed in the sense that, when uncertain, they deny:
a tracked-over-limit IP is rejected rather than served. They are state-light
(plain dicts guarded by a lock) and process-local — adequate for the
single-instance gateway deployment; a shared store is flagged as infra-needs
for a multi-replica future (see docs/questions).

A limit of 0 disables the respective control entirely (opt-out for trusted
private networks). Defaults are generous so legitimate BI tools — which open
many short-lived connections — are never blocked.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Dict

from shared.config.settings import get_settings

settings = get_settings()


class JdbcConnectionGovernor:
    """In-process per-IP concurrent-connection cap + failed-auth throttle."""

    def __init__(
        self,
        max_conn_per_ip: int | None = None,
        max_auth_failures: int | None = None,
        auth_failure_window_seconds: int | None = None,
        max_tracked_keys: int | None = None,
        *,
        time_fn=time.monotonic,
    ) -> None:
        self._max_conn = (
            settings.GATEWAY_JDBC_MAX_CONN_PER_IP
            if max_conn_per_ip is None
            else max_conn_per_ip
        )
        self._max_failures = (
            settings.GATEWAY_JDBC_MAX_AUTH_FAILURES
            if max_auth_failures is None
            else max_auth_failures
        )
        self._window = (
            settings.GATEWAY_JDBC_AUTH_FAILURE_WINDOW_SECONDS
            if auth_failure_window_seconds is None
            else auth_failure_window_seconds
        )
        # The canonical default + env override live on Settings
        # (GATEWAY_JDBC_MAX_TRACKED_FAILURE_KEYS). getattr guards a rolling
        # deploy / stale in-image `shared` where the field predates this change,
        # falling back to the same default rather than failing import.
        self._max_tracked_keys = (
            getattr(settings, "GATEWAY_JDBC_MAX_TRACKED_FAILURE_KEYS", 20000)
            if max_tracked_keys is None
            else max_tracked_keys
        )
        self._time = time_fn
        self._lock = threading.Lock()
        self._active: Dict[str, int] = {}
        self._failures: Dict[str, Deque[float]] = {}

    # ------------------------------------------------------------------
    # Connection admission
    # ------------------------------------------------------------------

    def try_acquire(self, ip: str) -> tuple[bool, str | None]:
        """Admit a new connection from *ip*.

        Returns ``(True, None)`` when admitted and the slot is reserved (the
        caller MUST call :meth:`release` exactly once). Returns
        ``(False, reason)`` when refused — either the per-IP concurrency cap is
        reached or the IP is currently throttled for repeated auth failures.
        Fail-closed: an unknown/blank IP is treated as a single shared bucket
        rather than waved through.
        """
        key = ip or "unknown"
        with self._lock:
            if self._is_throttled_locked(key):
                return False, "too many authentication failures; try again later"
            if self._max_conn > 0 and self._active.get(key, 0) >= self._max_conn:
                return False, "too many concurrent connections from this address"
            self._active[key] = self._active.get(key, 0) + 1
            return True, None

    def release(self, ip: str) -> None:
        """Release a connection slot previously reserved by :meth:`try_acquire`."""
        key = ip or "unknown"
        with self._lock:
            current = self._active.get(key, 0)
            if current <= 1:
                self._active.pop(key, None)
            else:
                self._active[key] = current - 1

    # ------------------------------------------------------------------
    # Auth-failure accounting
    # ------------------------------------------------------------------

    def record_auth_failure(self, ip: str) -> None:
        """Record an authentication failure for *ip* in the sliding window."""
        if self._max_failures <= 0:
            return
        key = ip or "unknown"
        now = self._time()
        with self._lock:
            window = self._failures.setdefault(key, deque())
            window.append(now)
            self._evict_locked(window, now)
            if 0 < self._max_tracked_keys < len(self._failures):
                self._enforce_capacity_locked(now)

    def record_auth_success(self, ip: str) -> None:
        """Clear the failure window for *ip* after a successful auth."""
        key = ip or "unknown"
        with self._lock:
            self._failures.pop(key, None)

    def is_throttled(self, ip: str) -> bool:
        key = ip or "unknown"
        with self._lock:
            return self._is_throttled_locked(key)

    # ------------------------------------------------------------------
    # Internals (caller holds the lock)
    # ------------------------------------------------------------------

    def _is_throttled_locked(self, key: str) -> bool:
        if self._max_failures <= 0:
            return False
        window = self._failures.get(key)
        if not window:
            return False
        self._evict_locked(window, self._time())
        if not window:
            self._failures.pop(key, None)
            return False
        return len(window) >= self._max_failures

    def _evict_locked(self, window: Deque[float], now: float) -> None:
        cutoff = now - self._window
        while window and window[0] < cutoff:
            window.popleft()

    def _enforce_capacity_locked(self, now: float) -> None:
        """Bound the failure map's size (caller holds the lock).

        Bug-8143: JDBC keys the governor on the raw peer IP (naturally bounded),
        but the XMLA throttle key embeds a client-supplied identity, so an
        attacker could otherwise grow ``_failures`` without bound (one bucket
        per fabricated username) and exhaust the gateway process, which also
        serves JDBC. This runs only when the map already exceeds the cap.

        Step 1 reclaims fully-expired windows — semantically free, because an
        empty/expired window never throttles. Step 2, reached only under a
        deliberate high-cardinality flood, evicts down to a low-water mark —
        sub-threshold buckets before at-threshold ones (least-recently-active
        within a tier) so an actively-throttling bucket is not flushed — so this
        O(n) sweep amortises (it leaves headroom before it can run again)
        instead of firing on every subsequent call.
        """
        for key in list(self._failures.keys()):
            window = self._failures[key]
            self._evict_locked(window, now)
            if not window:
                del self._failures[key]
        if len(self._failures) <= self._max_tracked_keys:
            return
        low_water = max(1, (self._max_tracked_keys * 9) // 10)
        # Eviction order (every remaining window is non-empty here, so
        # ``window[-1]`` is a safe recency key):
        #   1. sub-threshold buckets before at-threshold ones, so a
        #      high-cardinality flood of single-failure fabricated keys is
        #      reclaimed FIRST and an already-throttling bucket (a real account
        #      under active attack) is not silently flushed and un-blocked by
        #      the flood;
        #   2. within a tier, least-recently-active first.
        # Memory stays bounded regardless: filling the cap with at-threshold
        # buckets would cost the attacker ``cap * max_failures`` real rejected
        # logins, and even then at-threshold buckets are evicted oldest-first.
        def _rank(item):
            window = item[1]
            at_threshold = len(window) >= self._max_failures
            return (1 if at_threshold else 0, window[-1])

        victims = sorted(self._failures.items(), key=_rank)
        for key, _ in victims[: len(self._failures) - low_water]:
            del self._failures[key]


# Process-wide singleton used by the wire server.
_governor = JdbcConnectionGovernor()


def get_governor() -> JdbcConnectionGovernor:
    return _governor


def _reset_for_tests() -> None:
    """Test helper: rebuild the singleton from current settings."""
    global _governor
    _governor = JdbcConnectionGovernor()
