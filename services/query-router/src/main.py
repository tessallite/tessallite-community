"""
Tessallite Query Router — FastAPI application entry point.
"""
from __future__ import annotations

import logging
import sys

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from shared.config.bootstrap import refresh_system_snapshot
from shared.config.settings import get_settings
from shared.metrics import PrometheusMiddleware, metrics_response
from src.api.connection_introspect import router as conn_introspect_router
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
app.include_router(conn_introspect_router, prefix="/api/v1")
app.include_router(headless_router)
app.include_router(plugin_router)


async def _metadata_db_ready() -> tuple[bool, str]:
    """Cheap serving-readiness ping (F-030-01). Process liveness is ``/liveness``."""
    from sqlalchemy import text

    from shared.db.session import SystemSessionLocal

    try:
        async with SystemSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return True, "ok"
    except Exception as exc:  # noqa: BLE001 — health must never raise
        return False, str(exc)


@app.get("/liveness")
async def liveness() -> dict:
    return {"status": "ok", "service": "query-router"}


@app.get("/health")
async def health(response: Response) -> dict:
    """Serving readiness: 503 when the metadata database cannot be reached.

    F-030-01 / F-105-12: a 200 here used to mean only "uvicorn is running".
    Compose, Helm, GCP smoke, and the SPA ``/health`` front door treat this
    status code as "the query path can serve". Keep ``/liveness`` process-only.
    """
    body: dict = {"status": "ok", "service": "query-router"}
    ready, detail = await _metadata_db_ready()
    if not ready:
        body["status"] = "degraded"
        body["detail"] = "metadata database unreachable"
        response.status_code = 503
        logging.getLogger(__name__).error(
            "query-router /health DEGRADED — metadata DB unreachable: %s", detail
        )
    return body


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics(request: Request):
    return metrics_response(request)
