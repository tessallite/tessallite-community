"""Behavioral guard: the gateway KEEPS the blanket per-tenant rate limiter.

Rate-limit placement decision (user 2026-08-14,
docs/architecture/architecture_rate-limit-placement.md): the per-tenant
request-rate throttle belongs where BI-client USER QUERIES enter — the gateway —
and is REMOVED from the model-service/agent-service operational API. This test
pins the KEEP side of that decision: if someone removes the limiter from the
gateway (mistaking this lane for "rate limiting is gone"), the gateway would stop
throttling user-query ingress and this guard fails.

The blanket ``TenantRateLimitMiddleware`` is attached via ``attach_limiter``,
which also sets ``app.state.limiter``. Both are asserted on the real, constructed
gateway app.

Pure test: imports the constructed app and inspects its middleware list; no
lifespan, no DB, no network.
"""
from __future__ import annotations

from shared.middleware.rate_limiter import TenantRateLimitMiddleware


def _has_blanket_limiter(fastapi_app) -> bool:
    return any(
        getattr(m, "cls", None) is TenantRateLimitMiddleware
        for m in fastapi_app.user_middleware
    )


def test_gateway_app_keeps_the_blanket_rate_limiter():
    from src.main import app

    assert _has_blanket_limiter(app), (
        "gateway/src/main.py must KEEP the blanket TenantRateLimitMiddleware — "
        "the gateway is the user-query ingress and the sole home of the throttle "
        "(architecture_rate-limit-placement.md)."
    )
    assert getattr(app.state, "limiter", None) is not None, (
        "gateway app.state.limiter must be set by attach_limiter."
    )
