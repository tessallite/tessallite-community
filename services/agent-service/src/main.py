"""
Tessallite Agent Service — FastAPI entry point.

Project-scoped conversational agent backend. Phase Agent-A1 ships the
service skeleton, schema, and conversation/turn store. Real LLM calls
arrive in Phase B; SSE + outbound webhooks in Phase C.

Surfaces:
  - Internal (chat page)   — tenant session cookie auth
  - External (downstream)  — project-scoped API key auth (Phase C)

This file mirrors the layout of model-service / query-router / scheduler.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from shared.config.bootstrap import refresh_system_snapshot
from shared.config.settings import get_settings
from shared.metrics import PrometheusMiddleware, metrics_response
from src.webhooks.dispatcher import close_client as _close_webhook_client
from src.webhooks.dispatcher import init_client as _init_webhook_client
from src.api import (
    agent_config,
    agent_log,
    conversations,
    eval as eval_api,
    kpis,
    maintenance,
    personas,
    recipes,
    rubrics,
    webhooks,
)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    await refresh_system_snapshot()
    # Bug-5756 — initialise the shared httpx client for webhook dispatch.
    await _init_webhook_client()
    yield
    await _close_webhook_client()


app = FastAPI(
    title="Tessallite Agent Service",
    version="0.1.0",
    description=(
        "Project-scoped conversational agent backend. Phase A: scaffolding "
        "+ canned responses. Phase B: real LLM. Phase C: public surface."
    ),
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Rate limiting — deliberately NOT attached here.
# Per docs/architecture/architecture_rate-limit-placement.md (user decision
# 2026-08-14), the per-tenant request-rate throttle lives where USER QUERIES
# enter (the gateway), not on this service. Agent-service is LLM-bound; its
# real exposure is LLM COST, already guarded by per-project budget/spend
# controls (Bug-6334, api/eval.py cost ledger), not by request rate. A
# request-rate limiter here would not bound that cost and is not re-added.
# ---------------------------------------------------------------------------

origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
embed_origins = [o.strip() for o in settings.ALLOWED_EMBED_ORIGINS.split(",") if o.strip()]
origins = list(dict.fromkeys(origins + embed_origins))
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "Accept-Language", "X-CSRF-Token"],
)
from shared.auth.csrf import CSRFMiddleware
app.add_middleware(CSRFMiddleware)
app.add_middleware(PrometheusMiddleware, service_name="agent-service")

PREFIX = "/api/v1"

app.include_router(agent_config.router, prefix=PREFIX)
app.include_router(conversations.router, prefix=PREFIX)
app.include_router(personas.router, prefix=PREFIX)
app.include_router(recipes.router, prefix=PREFIX)
app.include_router(rubrics.router, prefix=PREFIX)
app.include_router(webhooks.router, prefix=PREFIX)
app.include_router(kpis.router, prefix=PREFIX)
app.include_router(eval_api.router, prefix=PREFIX)
app.include_router(maintenance.router, prefix=PREFIX)
app.include_router(agent_log.router, prefix=PREFIX)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "agent-service"}


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics(request: Request):
    return metrics_response(request)
