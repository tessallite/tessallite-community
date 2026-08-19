"""Behavioral guard: model-service runs the rate limiter in LOGIN-ONLY mode —
the operational/metadata API is NOT per-tenant throttled, but the /auth/login
brute-force throttle is preserved.

Rate-limit placement decision (user 2026-08-14,
docs/architecture/architecture_rate-limit-placement.md): the per-tenant
request-rate throttle for USER QUERIES belongs on the gateway, NOT on the
model-service operational/metadata API — attaching the full blanket limiter here
threw one per-tenant bucket over the model builder's own operational-DB metadata
reads (one ``/tables/{id}/attributes`` call per table), 429-ing on model-open.

But this service owns the SPA/direct auth endpoints, and the login brute-force
throttle (``rate_limit.login_per_minute``) lived in that same middleware.
Removing the middleware outright dropped it — a HIGH security regression. The
fix attaches the limiter in ``login_only`` mode: login paths stay throttled, the
operational API passes through un-throttled.

This module proves the WIRING structurally (login_only=True on the real
constructed app, no DB/lifespan needed). The behavioral proof of what
login_only mode DOES (login throttled, operational not) lives with the limiter's
own suite in ``tessallite/tests/unit/test_rate_limiter.py``.

Fails-before: the pre-fix main.py attached the FULL limiter (429 storm) or, in
the intermediate lane commit, attached nothing at all (login throttle dropped).
Both fail the login_only assertion below.
"""
from __future__ import annotations

from shared.middleware.rate_limiter import TenantRateLimitMiddleware
from src.main import app


def _limiter_middleware(fastapi_app):
    for m in fastapi_app.user_middleware:
        if getattr(m, "cls", None) is TenantRateLimitMiddleware:
            return m
    return None


def _is_login_only(middleware) -> bool:
    # Starlette stores add_middleware kwargs on ``.kwargs`` (newer) or
    # ``.options`` (older); read defensively so the guard is version-robust.
    opts = getattr(middleware, "kwargs", None)
    if opts is None:
        opts = getattr(middleware, "options", {}) or {}
    return bool(opts.get("login_only", False))


def test_model_service_attaches_the_limiter_in_login_only_mode():
    mw = _limiter_middleware(app)
    assert mw is not None, (
        "model-service must attach TenantRateLimitMiddleware in login-only mode. "
        "Removing it outright drops the /auth/login brute-force throttle "
        "(rate_limit.login_per_minute) — a HIGH security regression."
    )
    assert _is_login_only(mw) is True, (
        "the model-service limiter must be login_only=True: the operational/"
        "metadata API must NOT be per-tenant request-rate throttled (that is what "
        "429'd model-open), while the login brute-force throttle is preserved "
        "(architecture_rate-limit-placement.md)."
    )
    assert getattr(app.state, "limiter", None) is not None, (
        "attach_limiter must set app.state.limiter for the middleware to enforce."
    )
