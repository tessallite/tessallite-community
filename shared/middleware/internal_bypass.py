"""Internal service-to-service request marker (rate-limit bypass).

Tessallite's per-tenant rate limiter (``shared.middleware.rate_limiter``)
scopes enforcement to user-facing ingress. Internal pipeline traffic —
gateway -> model-service metadata/login relays, scheduler sweeps, agent
pipeline reads — attaches the header from :func:`internal_request_headers`
so it is never throttled into breaking query routing.

The header value is an HMAC of a fixed label plus a coarse time window under
the shared ``JWT_SECRET_KEY``: only processes holding the platform secret can
mint it, and verification is a constant-time compare.

Time-window binding (F-H27R1-04): the HMAC message includes
``floor(now / WINDOW_SECONDS)``, so the value rotates every window. A
verifier accepts the current and the immediately previous window, which both
tolerates clock skew up to one window and lets a request minted near a
boundary still verify. A leaked header value therefore stops working after at
most two windows instead of being a permanent, replayable bypass. The binding
is dependency-free (stdlib ``hmac``/``hashlib``/``time`` only) so services
that never enforce limits — scheduler, agent-service — can attach the header
without shipping limiter packages.
"""
from __future__ import annotations

import hashlib
import hmac
import time

INTERNAL_BYPASS_HEADER = "X-Tessallite-Internal"
_INTERNAL_BYPASS_LABEL = b"tessallite-internal-rate-limit-bypass"

# Rotation window for the bypass HMAC, in seconds. The value changes every
# window; the verifier accepts the current and previous window, so the
# effective skew/replay tolerance is one window. 300s keeps internal HTTP
# calls (sub-second) comfortably inside a single window while bounding the
# lifetime of any leaked value to at most 600s.
WINDOW_SECONDS = 300


def _current_window() -> int:
    return int(time.time()) // WINDOW_SECONDS


def _compute_internal_bypass_value(window: int) -> str:
    from shared.config.settings import get_settings

    secret = get_settings().JWT_SECRET_KEY.encode("utf-8")
    message = _INTERNAL_BYPASS_LABEL + b":" + str(window).encode("ascii")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def internal_request_headers() -> dict[str, str]:
    """Headers internal service-to-service HTTP calls attach so the
    receiving service's rate limiter does not throttle the pipeline.

    The value is bound to the current time window and rotates automatically;
    the receiver accepts it for the current and the next window.
    """
    return {INTERNAL_BYPASS_HEADER: _compute_internal_bypass_value(_current_window())}


def is_internal_request_header(presented: str | None) -> bool:
    """Constant-time check of a presented bypass header value.

    Accepts the current and the immediately previous time window so a value
    minted just before a window rollover still verifies, and to tolerate up
    to one window of clock skew between the minting and verifying processes.
    Both candidates are always compared (no short-circuit) to keep the check
    constant-time regardless of which window matches.
    """
    if not presented:
        return False
    window = _current_window()
    valid_current = hmac.compare_digest(
        presented, _compute_internal_bypass_value(window)
    )
    valid_previous = hmac.compare_digest(
        presented, _compute_internal_bypass_value(window - 1)
    )
    return valid_current or valid_previous
