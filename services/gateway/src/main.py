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
import os
import sys
from contextlib import asynccontextmanager

# Ensure our loggers write to stderr even when uvicorn overrides the root config.
# GATEWAY_LOG_LEVEL=DEBUG surfaces the XMLA request-type / auth-challenge traces
# needed to diagnose BI-client (MSOLAP/ADOMD) connection failures.
_LOG_LEVEL_NAME = os.getenv("GATEWAY_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    stream=sys.stderr,
    level=getattr(logging, _LOG_LEVEL_NAME, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
if _LOG_LEVEL_NAME == "DEBUG":
    # Keep third-party wire chatter out of a DEBUG trace; only gateway loggers matter.
    for _noisy in ("httpx", "httpcore", "asyncio", "watchfiles", "urllib3"):
        logging.getLogger(_noisy).setLevel(logging.INFO)

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from shared.config.bootstrap import refresh_system_snapshot
from shared.config.settings import get_settings
from shared.gateway_liveness import probe_jdbc_accept_loop_async
from shared.config.shutdown_budget import (
    declare_pre_close_drain as _declare_pre_close_drain,
)
from shared.metrics import PrometheusMiddleware, metrics_response
from shared.middleware.rate_limiter import attach_limiter, build_limiter
from src.dax.auth_basic import BasicAuthMiddleware
from src.dax.xmla_server import router as xmla_router
from src.jdbc.liveness_watchdog import run_jdbc_liveness_watchdog
from src.jdbc.server import start_jdbc_server

logger = logging.getLogger(__name__)
settings = get_settings()


async def _jdbc_serve(server: asyncio.Server) -> None:
    """Run the JDBC server and log any unexpected crash."""
    try:
        await server.serve_forever()
    except Exception as exc:
        logger.error("JDBC server crashed: %s", exc, exc_info=True)


# Bug-8041 R7 round-2 F3: the gateway is the ONLY service with a second drain
# phase inside its lifespan shutdown (the JDBC asyncio.Server below). Declaring
# it here -- at import, before any budget is resolved for a real timeout -- funds
# that phase out of THIS process's budget only, so model-service, query-router,
# optimizer and scheduler keep the full pool-close grace.
_declare_pre_close_drain()


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

    # Bug-8533 auto-recovery half. /health telling the truth (Bug-8974) does not
    # restart anything: Compose's `restart: unless-stopped` acts on process EXIT,
    # not on health, and the GCP db-vm gateway has no healthcheck at all. The
    # watchdog re-probes the accept loop on an interval and exits non-zero after
    # three CONSECUTIVE failures so the restart policy recycles the container.
    # Started only when the listener actually started -- there is nothing to
    # watch otherwise, and a listener that never bound is not fixed by a restart.
    # Gated on the /health probe switch too, so turning the probe off on a
    # deployment that does not serve JDBC turns this off with it rather than
    # leaving half the mechanism running.
    watchdog_task = None
    if (
        jdbc_server is not None
        and settings.GATEWAY_JDBC_WATCHDOG_ENABLED
        and settings.GATEWAY_HEALTH_JDBC_PROBE_ENABLED
    ):
        watchdog_task = asyncio.create_task(
            run_jdbc_liveness_watchdog(
                "127.0.0.1",
                settings.JDBC_PORT,
                interval_seconds=settings.GATEWAY_JDBC_WATCHDOG_INTERVAL_SECONDS,
                failure_limit=settings.GATEWAY_JDBC_WATCHDOG_FAILURE_LIMIT,
                probe_timeout_seconds=(
                    settings.GATEWAY_HEALTH_JDBC_PROBE_TIMEOUT_SECONDS
                ),
            )
        )
        logger.info(
            "JDBC liveness watchdog started — probing :%d every %ss, exiting "
            "after %d consecutive failures",
            settings.JDBC_PORT,
            settings.GATEWAY_JDBC_WATCHDOG_INTERVAL_SECONDS,
            settings.GATEWAY_JDBC_WATCHDOG_FAILURE_LIMIT,
        )

    yield

    # The watchdog is cancelled FIRST, before any listener teardown below. A
    # deliberate shutdown closes the accept loop, which is indistinguishable to
    # the probe from the wedge this watchdog exists to catch -- leaving it
    # running would let an orderly stop trigger a non-zero exit and a restart.
    if watchdog_task:
        watchdog_task.cancel()
        try:
            await watchdog_task
        except asyncio.CancelledError:
            pass
        except Exception:
            # The watchdog died on its own before shutdown, so this gateway had
            # no auto-recovery for the rest of its life. Say so -- a silently
            # dead watchdog is the failure mode that makes a watchdog worse than
            # none, because it is believed.
            logger.warning(
                "JDBC liveness watchdog ended with an error before shutdown; "
                "this gateway had no JDBC auto-recovery from that point on",
                exc_info=True,
            )

    # Shutdown
    #
    # Bug-8041 R7: EVERY phase before close_all_pools() must be bounded, and all
    # of them together must fit the ONE slice the budget funds for this service
    # (``pre_close_drain_seconds``). An unbounded phase here is the same defect as
    # uvicorn's missing graceful-shutdown timeout, one layer further in:
    # close_all_pools() never starts and the platform SIGKILLs with source
    # connections still open. Both pre-close phases therefore share a SINGLE
    # deadline rather than each getting their own timeout, so adding a phase can
    # never silently extend the total.
    from shared.config.shutdown_budget import resolve_shutdown_budget
    _pre_close_budget = resolve_shutdown_budget().pre_close_drain_seconds
    _pre_close_deadline = asyncio.get_running_loop().time() + _pre_close_budget

    def _remaining() -> float:
        return max(0.0, _pre_close_deadline - asyncio.get_running_loop().time())

    from src.dax.session_store import flush_now as flush_session_store
    try:
        # Bug-8041 R7 round-2 F8: previously unbounded. It is a local disk write
        # under a lock (``asyncio.to_thread``), so it is short in practice -- but
        # "short in practice" is exactly the reasoning that left wait_closed()
        # unbounded, and a stuck disk would eat the pool-close grace.
        await asyncio.wait_for(flush_session_store(), timeout=_remaining())
    except asyncio.TimeoutError:
        logger.warning(
            "DAX session-store flush did not finish within the %ss pre-close "
            "budget; continuing so source pools are closed inside the shutdown "
            "budget (some session state may not be persisted).", _pre_close_budget,
        )
    except Exception:
        logger.warning("DAX session-store flush failed during shutdown",
                       exc_info=True)
    if jdbc_server:
        jdbc_server.close()
        # ``asyncio.Server.wait_closed()`` blocks until every active connection
        # handler finishes, and a JDBC client mid-query is precisely the
        # long-running case this gateway exists for. The courtesy drain is
        # deliberately SHORT because the per-connection work is already bounded
        # by ``close_all_pools`` (grace + force-terminate).
        _jdbc_drain = _remaining()
        try:
            await asyncio.wait_for(jdbc_server.wait_closed(), timeout=_jdbc_drain)
        except asyncio.TimeoutError:
            logger.warning(
                "JDBC connections still active after the %ss pre-close drain; "
                "proceeding to close source pools so shutdown stays inside the "
                "budget. The listener stops accepting and its serve loop is "
                "cancelled; connection handlers already running are NOT "
                "cancelled here -- their source connections are force-terminated "
                "by close_all_pools within the pool grace.", _pre_close_budget,
            )
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
app.include_router(xmla_router)


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics(request: Request):
    return metrics_response(request)


@app.get("/health")
async def health(response: Response) -> dict:
    """Liveness for BOTH surfaces this service serves, not just the HTTP one.

    Bug-8974: this endpoint answering 200 used to mean only "uvicorn is
    running". The gateway also serves the JDBC wire protocol on a separate TCP
    port, and that listener can be bound-but-not-accepting — a TCP connect
    completes in the kernel listen backlog with or without a live accept loop,
    so every probe in the estate reported healthy while BI clients hung. Nine
    probe owners each rolled their own TCP-connect check and all nine shared
    the blind spot; fixing them one at a time is the "fixed caller A only"
    defect. So the PRODUCER answers the question once, here, and every consumer
    of /health inherits the truth — including the Helm chart, whose gateway
    probes are httpGet on this path and therefore could not have been fixed any
    other way.

    The proof is a credential-free loopback PostgreSQL SSLRequest exchange. It
    deliberately does NOT run a query: a query needs auth via model-service, so
    a model-service blip would mark the GATEWAY unhealthy and (under Kubernetes)
    restart it — turning someone else's outage into a gateway restart loop.
    Readiness, which SHOULD depend on model-service, is a separate question
    answered by callers that hold credentials running
    ``shared.gateway_liveness.READINESS_PROBE_SQL`` over an ordinary connection.
    """
    body: dict = {
        "status": "ok",
        "service": "gateway",
        "xmla_port": settings.XMLA_PORT,
        "jdbc_port": settings.JDBC_PORT,
    }
    if not settings.GATEWAY_HEALTH_JDBC_PROBE_ENABLED:
        body["jdbc_listening"] = None
        body["jdbc_detail"] = "JDBC liveness probe disabled by configuration"
        return body

    alive, detail = await probe_jdbc_accept_loop_async(
        "127.0.0.1",
        settings.JDBC_PORT,
        timeout=settings.GATEWAY_HEALTH_JDBC_PROBE_TIMEOUT_SECONDS,
    )
    body["jdbc_listening"] = alive
    body["jdbc_detail"] = detail
    if not alive:
        # 503, not 200-with-a-flag: a healthcheck, a Kubernetes livenessProbe
        # and a deploy gate all act on the STATUS CODE. Reporting the failure
        # only in the body would leave the estate exactly as blind as before.
        body["status"] = "degraded"
        response.status_code = 503
        logger.error(
            "Gateway /health reporting DEGRADED — the JDBC accept loop is not "
            "answering on port %d: %s", settings.JDBC_PORT, detail,
        )
    return body


# ---------------------------------------------------------------------------
# Module entry point — launched via: python -m src.main
# ---------------------------------------------------------------------------

def build_uvicorn_kwargs() -> dict:
    """The arguments this service starts uvicorn with.

    Bug-8041 R8 review round 4: this used to be built INSIDE
    ``if __name__ == "__main__":``, where no test can evaluate it -- so the
    guard for it was a substring check, and a hardcoded
    ``"timeout_graceful_shutdown": 3600`` passed the whole suite while restoring
    R7's HIGH-2 defect verbatim (one in-flight XMLA/DAX request postpones
    ``close_all_pools()`` for an hour and the container is SIGKILLed with
    authenticated source connections open). A module-level function can be
    imported and its VALUE asserted at several budgets, which is what the other
    four services get from their Dockerfile CMD guard.
    """
    from shared.config.shutdown_budget import resolve_shutdown_budget

    kwargs: dict = {
        "host": "0.0.0.0",
        "port": settings.XMLA_PORT,
        "log_level": "info",
        # Bug-8041 R7 HIGH-2: the gateway is a source-pool-using service -- its
        # lifespan shutdown calls close_all_pools() above. Without an explicit
        # graceful-shutdown timeout, uvicorn's shutdown does
        # `asyncio.wait_for(self._wait_tasks_to_complete(), timeout=None)` and only
        # calls `self.lifespan.shutdown()` afterwards, so ONE in-flight XMLA/DAX
        # request (an Excel pivot refresh is routinely long) postpones
        # close_all_pools() indefinitely and the container is SIGKILLed with
        # authenticated source connections still open. Derived from the single
        # shutdown-budget source of truth so this, the compose stop_grace_period
        # and the pool close grace cannot drift apart.
        "timeout_graceful_shutdown": resolve_shutdown_budget().request_drain_seconds,
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
        kwargs["ssl_certfile"] = cert
        kwargs["ssl_keyfile"] = key
        logger.info("XMLA SSL/TLS enabled (cert=%s)", cert)
    return kwargs


if __name__ == "__main__":
    import uvicorn

    from shared.config.shutdown_budget import log_budget_diagnostics

    # Bug-8041 R7 round-2 F2: the gateway's compose image starts via
    # `python -m src.main`, so it never runs the Dockerfile CMD's
    # `shutdown_budget --check`. This entrypoint must emit the same start-up
    # diagnostics or a misconfigured budget is completely silent on this path.
    #
    # Bug-8041 R8 (6th external gate, MEDIUM): this used to RESTATE one of the
    # two checks, and when "malformed must be loud too" was added in R7 round 5
    # it reached the Dockerfile `--check` and the pool-manager runtime path but
    # not this copy -- so an unsubstituted deploy placeholder was silent here
    # (a non-numeric value makes `budget_is_below_minimum()` return False).
    # It now consumes the SINGLE shared implementation, so the divergence is not
    # possible again rather than merely fixed once.
    log_budget_diagnostics(logger)

    uvicorn_kwargs = build_uvicorn_kwargs()

    uvicorn.run("src.main:app", **uvicorn_kwargs)
