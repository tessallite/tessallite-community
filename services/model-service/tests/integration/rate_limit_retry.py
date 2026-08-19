"""
Rate-limit-aware retry for the model-service integration harness.

Why this exists (Bug-8863 / Bug-8865 / Bug-8885)
------------------------------------------------
The integration tests drive ONE live model-service over HTTP. That service's
``TenantRateLimitMiddleware`` caps a tenant at ``rate_limit.per_minute``
(default 60) requests per minute and answers the excess with HTTP 429 plus a
``Retry-After`` header. The KPI modules alone issue several hundred requests
against a single tenant in well under a minute, so the ceiling is reached
routinely.

Nothing in the harness recognised a 429. A throttled request therefore
surfaced as an ordinary assertion failure inside whichever helper happened to
make it -- ``Create failed (429)``, ``Save (create version) failed (429)``,
``Evaluate failed (429)`` -- with a membership that drifted from run to run
because throttling depends on wall-clock request rate, not on the code under
test. That drifting red was repeatedly mis-attributed to whichever lane was
running at the time.

The harness's previous mitigation was to PUT ``rate_limit.enabled=False`` on
the running service for the whole session. That is the defect Bug-8885 records
at HIGH: a test run reconfigures the deployed product for every caller, and it
fails OPEN -- when the disable could not be performed (its own system login is
subject to the stricter ``rate_limit.login_per_minute`` ceiling of 10/minute
per client address, which back-to-back sessions exhaust) it logged a warning
and let the whole session run WITH throttling on.

What this module does
---------------------
Treat 429 as what it is: a documented, retryable protocol response. A client
that honours ``Retry-After`` cannot be made to fail by throttling, so the
harness stops depending on the deployed service's global configuration for its
correctness. Throttling then costs wall-clock time instead of producing a
false, drifting red.

Deliberate boundaries
---------------------
* Retries apply ONLY to requests aimed at the integration target host, so the
  in-process unit tests (which drive the FastAPI app through
  ``httpx.AsyncClient``) are untouched. Only the synchronous ``httpx.Client``
  path used by the integration tests is wrapped.
* Only 429 is retried. Every other status -- including 5xx -- is returned
  unchanged, so a real defect still fails the test that found it.
* Exhausting the retry budget raises :class:`RateLimitExhausted` rather than
  returning the 429, so the failure names the cause instead of appearing as a
  generic "... failed (429)" assertion.
* No test assertion is weakened: assertions still run against the real
  response the service eventually returns.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

# HTTP status the service uses for tenant/login throttling.
TOO_MANY_REQUESTS = 429

# Tunables. Defaults are documented here and overridable from the environment
# so no timing constant is pinned in code.
_ENV_MAX_ATTEMPTS = "INTEGRATION_TEST_RATE_LIMIT_ATTEMPTS"
_ENV_MAX_WAIT = "INTEGRATION_TEST_RATE_LIMIT_MAX_WAIT"
# Raised 4 -> 8 on 2026-08-11. With _disable_live_rate_limiting deleted the
# limiter is genuinely ON for the whole package, and a burst that is throttled
# retries as a herd: every queued request wakes at the same Retry-After and is
# throttled again. Four attempts left the suite on the edge -- one run in two
# lost a single KPI test to an exhausted budget. This is HEADROOM, not a proven
# floor; if it exhausts again the answer is a higher ceiling for the test tenant
# or a dedicated tenant, not another bump.
_DEFAULT_MAX_ATTEMPTS = 8
_DEFAULT_MAX_WAIT_SECONDS = 65.0
# Used when a 429 arrives without a usable Retry-After header. The service's
# window is one minute, so a short probe re-tries a few times inside it rather
# than sleeping a whole window on a header-less response.
_FALLBACK_WAIT_SECONDS = 5.0


class RateLimitExhausted(RuntimeError):
    """Raised when a request stayed throttled for the whole retry budget."""


def max_attempts() -> int:
    """Total attempts (first try + retries) for a throttled request."""
    raw = os.environ.get(_ENV_MAX_ATTEMPTS)
    if raw is None:
        return _DEFAULT_MAX_ATTEMPTS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MAX_ATTEMPTS
    return value if value >= 1 else 1


def max_wait_seconds() -> float:
    """Upper bound on a single sleep between attempts."""
    raw = os.environ.get(_ENV_MAX_WAIT)
    if raw is None:
        return _DEFAULT_MAX_WAIT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_MAX_WAIT_SECONDS
    return value if value >= 0 else 0.0


def parse_retry_after(raw: Any, *, cap: float) -> float:
    """Seconds to wait before the next attempt.

    ``raw`` is the response's ``Retry-After`` header value. Only the
    delay-seconds form is emitted by this service, so an absent, malformed or
    negative value falls back to a short probe rather than failing. The result
    is always clamped to ``cap`` so a hostile or mistaken header cannot stall
    a test run indefinitely.
    """
    if raw is None:
        seconds = _FALLBACK_WAIT_SECONDS
    else:
        try:
            seconds = float(str(raw).strip())
        except (TypeError, ValueError):
            seconds = _FALLBACK_WAIT_SECONDS
        if seconds < 0:
            seconds = _FALLBACK_WAIT_SECONDS
    return min(seconds, cap)


def targets_host(url: Any, api_base: str) -> bool:
    """True when ``url`` addresses the same origin as ``api_base``.

    Scope guard: the retry must never change how the in-process unit tests
    behave, so it engages only for the live integration target's origin
    (scheme, host and port). Comparison is on the origin, not on the path, so
    every endpoint of that service is covered while anything else -- an ASGI
    transport, a mock host, another service -- is passed straight through.
    """
    try:
        from urllib.parse import urlsplit

        target = urlsplit(api_base)
        actual = urlsplit(str(url))
    except Exception:  # noqa: BLE001 - never let the guard break a request
        return False
    if not target.scheme or not target.netloc:
        return False
    return (actual.scheme, actual.netloc) == (target.scheme, target.netloc)


def send_with_retry(
    send: Callable[..., Any],
    request: Any,
    /,
    *args: Any,
    api_base: str,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> Any:
    """Call ``send``, re-issuing the request while the service answers 429.

    ``send`` is the unwrapped ``httpx.Client.send`` bound method (or any
    callable with the same shape). Non-429 responses -- and requests aimed
    anywhere other than ``api_base``'s origin -- are returned on the first
    attempt with no added behaviour.
    """
    attempts = max_attempts()
    cap = max_wait_seconds()
    response = send(request, *args, **kwargs)
    if not targets_host(getattr(request, "url", ""), api_base):
        return response

    attempt = 1
    while (
        getattr(response, "status_code", None) == TOO_MANY_REQUESTS
        and attempt < attempts
    ):
        headers = getattr(response, "headers", {}) or {}
        try:
            retry_after = headers.get("Retry-After")
        except Exception:  # noqa: BLE001 - defensive on exotic header objects
            retry_after = None
        # The body of a 429 is never consumed by the harness, but httpx keeps
        # the connection tied to the response until it is closed.
        close = getattr(response, "close", None)
        if callable(close):
            close()
        sleep(parse_retry_after(retry_after, cap=cap))
        response = send(request, *args, **kwargs)
        attempt += 1

    if getattr(response, "status_code", None) == TOO_MANY_REQUESTS:
        raise RateLimitExhausted(
            f"{getattr(request, 'method', '?')} {getattr(request, 'url', '?')} "
            f"stayed rate limited (HTTP 429) across {attempts} attempts. The "
            f"live service is throttling this tenant "
            f"(rate_limit.per_minute). This is an environment condition, not "
            f"a product assertion failure -- see Bug-8865/Bug-8885."
        )
    return response
