"""
Gateway service — FastAPI (XMLA on 8080) + asyncio TCP (JDBC on 5433).

Startup sequence:
  1. FastAPI lifespan starts the asyncio TCP JDBC server in the background.
  2. Uvicorn runs the FastAPI app on port 8080 (XMLA/HTTP).
  3. On shutdown, the TCP server is gracefully closed.

Entry point: python -m src.main
"""
from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

# Ensure our loggers write to stderr even when uvicorn overrides the root config
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from shared.config.bootstrap import refresh_system_snapshot
from shared.config.settings import get_settings
from shared.metrics import PrometheusMiddleware, metrics_response
from shared.middleware.rate_limiter import attach_limiter, build_limiter
from src.dax.auth_basic import BasicAuthMiddleware
from src.dax.xmla_server import router as xmla_router
from src.jdbc.server import start_jdbc_server

logger = logging.getLogger(__name__)
settings = get_settings()


async def _jdbc_serve(server: asyncio.Server) -> None:
    """Run the JDBC server and log any unexpected crash."""
    try:
        await server.serve_forever()
    except Exception as exc:
        logger.error("JDBC server crashed: %s", exc, exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await refresh_system_snapshot()

    # Start JDBC TCP server as a background asyncio task
    try:
        jdbc_server = await start_jdbc_server()
        jdbc_task = asyncio.create_task(_jdbc_serve(jdbc_server))
        logger.info(
            "Gateway started — XMLA on :%d, JDBC on :%d",
            settings.XMLA_PORT,
            settings.JDBC_PORT,
        )
    except Exception as exc:
        logger.error("Failed to start JDBC server: %s", exc, exc_info=True)
        jdbc_server = None
        jdbc_task = None

    yield

    # Shutdown
    from src.dax.session_store import flush_now as flush_session_store
    await flush_session_store()
    if jdbc_server:
        jdbc_server.close()
        await jdbc_server.wait_closed()
    if jdbc_task:
        jdbc_task.cancel()
        try:
            await jdbc_task
        except asyncio.CancelledError:
            pass
    from shared.source_pool import close_all_pools
    await close_all_pools()
    logger.info("Gateway stopped")


app = FastAPI(
    title="Tessallite Gateway",
    description=(
        "JDBC (PostgreSQL wire protocol, port 5433) and "
        "DAX/XMLA-over-HTTP (port 8080) gateway for Tessallite."
    ),
    version="0.5.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Rate limiting (per-tenant; no-op when RATE_LIMIT_ENABLED=False)
# ---------------------------------------------------------------------------
_limiter = build_limiter()
attach_limiter(app, _limiter)

# Expert Directive: Enforce Basic auth for Excel XMLA handshake
app.add_middleware(BasicAuthMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS.split(","),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "Accept-Language", "X-CSRF-Token", "SOAPAction"],
)
from shared.auth.csrf import CSRFMiddleware
app.add_middleware(CSRFMiddleware)
app.add_middleware(PrometheusMiddleware, service_name="gateway")

app.include_router(xmla_router, prefix="/api/v1")


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics(request: Request):
    return metrics_response(request)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "service": "gateway",
        "xmla_port": settings.XMLA_PORT,
        "jdbc_port": settings.JDBC_PORT,
    }


# ---------------------------------------------------------------------------
# Module entry point — launched via: python -m src.main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn_kwargs: dict = {
        "host": "0.0.0.0",
        "port": settings.XMLA_PORT,
        "log_level": "info",
    }

    # Serve XMLA/HTTP over TLS when exposed directly to BI clients (e.g. the
    # GCP VM gateway publishes 8080 to the public internet). Reuses the same
    # cert/key as the JDBC listener.
    if settings.GATEWAY_XMLA_TLS_ENABLED:
        cert = settings.GATEWAY_SSL_CERT_FILE
        key = settings.GATEWAY_SSL_KEY_FILE
        if not cert or not key:
            raise RuntimeError(
                "GATEWAY_XMLA_TLS_ENABLED=True but GATEWAY_SSL_CERT_FILE / "
                "GATEWAY_SSL_KEY_FILE are not set"
            )
        uvicorn_kwargs["ssl_certfile"] = cert
        uvicorn_kwargs["ssl_keyfile"] = key
        logger.info("XMLA SSL/TLS enabled (cert=%s)", cert)

    uvicorn.run("src.main:app", **uvicorn_kwargs)
