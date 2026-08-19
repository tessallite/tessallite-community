"""Behavioral guard: agent-service is NOT throttled by the blanket per-tenant
rate limiter.

Rate-limit placement decision (user 2026-08-14,
docs/architecture/architecture_rate-limit-placement.md): agent-service is
LLM-bound; its real exposure is LLM COST, already guarded by per-project
budget/spend controls (Bug-6334, api/eval.py cost ledger), not request rate. A
request-rate limiter here does not bound that cost, so the blanket limiter is
removed and must not be re-added.

The blanket ``TenantRateLimitMiddleware`` runs BEFORE routing and is the only
source of a per-tenant HTTP-429 on these endpoints. Asserting it is absent from
the real, constructed app's middleware stack is the deterministic runtime proof
that no request can be throttled by it — the runtime equivalent of "no 429 under
a burst", without depending on snapshot state or a live DB.

Fails-before: the pre-decision main.py called ``attach_limiter(app, ...)`` (added
for Bug-5953), so ``TenantRateLimitMiddleware`` was in the stack and
``app.state.limiter`` was set — both assertions below fail against the pre-change
code.

Pure test: imports the constructed app and inspects its middleware list; no
lifespan, no DB, no network.
"""
from __future__ import annotations

from shared.middleware.rate_limiter import TenantRateLimitMiddleware
from src.main import app
import src.main as agent_service_main


def _has_blanket_limiter(fastapi_app) -> bool:
    return any(
        getattr(m, "cls", None) is TenantRateLimitMiddleware
        for m in fastapi_app.user_middleware
    )


def test_agent_service_app_does_not_attach_the_blanket_rate_limiter():
    assert not _has_blanket_limiter(app), (
        "agent-service must NOT attach the blanket TenantRateLimitMiddleware. "
        "Its exposure is LLM cost, guarded by per-project budget controls "
        "(Bug-6334), not request rate (architecture_rate-limit-placement.md)."
    )
    assert getattr(app.state, "limiter", None) is None, (
        "agent-service app.state.limiter must be unset once the blanket limiter "
        "is removed (attach_limiter sets it)."
    )


def test_agent_service_dropped_the_now_dead_limiter_import():
    """With the limiter unattached, build_limiter/attach_limiter must not be
    imported into main (no unused imports)."""
    assert not hasattr(agent_service_main, "attach_limiter"), (
        "agent-service/src/main.py still binds attach_limiter — remove the dead "
        "import from shared.middleware.rate_limiter."
    )
    assert not hasattr(agent_service_main, "build_limiter"), (
        "agent-service/src/main.py still binds build_limiter — remove the dead "
        "import from shared.middleware.rate_limiter."
    )
