"""
Tessallite Query Router — FastAPI application entry point.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from shared.config.bootstrap import refresh_system_snapshot
from shared.config.settings import get_settings
from shared.metrics import PrometheusMiddleware, metrics_response
from src.api.drill_routes import router as drill_router
from src.api.headless import router as headless_router
from src.api.introspect import router as introspect_router
from src.api.plugin import router as plugin_router
from src.api.routes import router as query_router

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    await refresh_system_snapshot()
    yield
    from shared.source_pool import close_all_pools
    await close_all_pools()


app = FastAPI(
    title="Tessallite Query Router",
    version="0.1.0",
    description="Intercepts queries, routes to aggregates or source, executes and returns results.",
    lifespan=lifespan,
)

origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "Accept-Language", "X-CSRF-Token"],
)
from shared.auth.csrf import CSRFMiddleware
app.add_middleware(CSRFMiddleware)
app.add_middleware(PrometheusMiddleware, service_name="query-router")

app.include_router(query_router, prefix="/api/v1")
app.include_router(drill_router, prefix="/api/v1")
app.include_router(introspect_router, prefix="/api/v1")
app.include_router(headless_router)
app.include_router(plugin_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "query-router"}


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics(request: Request):
    return metrics_response(request)
