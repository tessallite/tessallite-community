"""
Tessallite Model Service — FastAPI application entry point.

Registers all API routers.
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

from shared.auth.csrf import CSRFMiddleware
from shared.config.bootstrap import refresh_system_snapshot
from shared.config.settings import get_settings
from shared.config.shutdown_budget import (
    declare_pre_close_drain as _declare_pre_close_drain,
)
from shared.metrics import PrometheusMiddleware, metrics_response
from shared.middleware.rate_limiter import attach_limiter, build_limiter
from src.api import (
    access,
    admin,
    aggregates,
    alerts,
    alias_map,
    analytics,
    audit,
    auth,
    calendar,
    connections,
    data_quality,
    data_tags,
    atscale_import,
    catalog_import,
    collibra,
    solidatus,
    cube_import,
    dbt_import,
    dimensions,
    edition,
    embed,
    downstream_assets,
    export,
    field_compatibility,
    git_settings,
    glossary,
    group_mappings,
    hierarchies,
    impact_analysis,
    impact_scan,
    import_export,
    pat_tokens,
    joins,
    kpis,
    project_import_export,
    yaml_export,
    lineage,
    llm_config,
    logs,
    lookml_export,
    measures,
    metrics,
    model_docs,
    model_settings,
    models,
    named_sets,
    named_queries,
    notifications,
    odc,
    parameters,
    personas,
    pivot_views,
    pockets,
    preferences,
    project_settings,
    projects,
    refresh,
    join_population_health,
    relationship_health,
    row_security,
    saved_queries,
    scheduler_config,
    scratchpad_measures,
    hierarchy_health,
    schema_changes,
    schema_drift,
    security_audit,
    sources,
    sso,
    system_settings,
    webhooks,
    table_attributes,
    table_preview,
    tables,
    targets,
    tenant_branding,
    tenants,
    refresh_stream,
    user_defined_attributes,
    translations,
    validation,
    versions,
)

settings = get_settings()


# Bug-8041 R7 round-4 F2: this service does bounded work (stopping the licence
# beacon) in its lifespan shutdown BEFORE close_all_pools(), so it must declare
# that phase or the shutdown budget funds it zero seconds and the time it takes
# is stolen from the pool close. Declared at import, before any budget is
# resolved for a real timeout.
_declare_pre_close_drain()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # startup
    await refresh_system_snapshot()
    # Build the license manager from the persisted (UI-fed) license in the system
    # DB so the edition/limits + any enforcement reflect it without a restart.
    try:
        from src.licensing_guard import reload_license_manager
        await reload_license_manager()
    except Exception:  # noqa: BLE001 — never block startup on license load
        import logging
        logging.getLogger(__name__).warning("license manager load failed", exc_info=True)
    # Product-side licence beacon emitter (Bug-5459). Default-OFF: no-op unless
    # LICENSE_BEACON_URL is configured. license_id-only, offline-tolerant.
    beacon = None
    # Bug-8374: fail-FAST beacon-encryption config check at startup. A set
    # BEACON_ENC_KEY that is not a valid Fernet key RAISES BeaconConfigError, which
    # is deliberately NOT caught here — an explicitly misconfigured key must abort
    # startup rather than silently lose every beacon. The valid half-config case
    # (sink key missing) only logs a WARNING inside the validator and returns.
    from shared.licensing.beacon import beacon_startup_validate
    beacon_startup_validate()
    try:
        from src.beacon_runtime import build_beacon_emitter
        beacon = build_beacon_emitter()
        if beacon is not None:
            beacon.start()
    except Exception:  # noqa: BLE001 — beacon must never block startup
        import logging
        logging.getLogger(__name__).debug("beacon emitter start skipped", exc_info=True)
    yield
    # shutdown
    if beacon is not None:
        # Bug-8041 R7 round-4 F2: BOUND this. ``BeaconEmitter.stop()`` cancels
        # its task and awaits it with no timeout; that task can be inside an
        # httpx POST to an operator-configured EXTERNAL url, whose ``__aexit__``
        # runs ``client.aclose()`` during the unwind. Unbounded, it eats the
        # pool-close grace and ``close_all_pools()`` may never finish inside the
        # platform's SIGTERM window — the same defect class as the gateway's
        # previously-unbounded JDBC drain, in the service nobody re-scanned.
        # ``declare_pre_close_drain()`` (above the app) is what FUNDS this slice;
        # without it the budget formally allots this service zero pre-close time.
        import asyncio as _asyncio

        from shared.config.shutdown_budget import resolve_shutdown_budget
        _pre_close = resolve_shutdown_budget().pre_close_drain_seconds
        try:
            await _asyncio.wait_for(beacon.stop(), timeout=_pre_close)
        except _asyncio.TimeoutError:
            import logging
            logging.getLogger(__name__).warning(
                "licence beacon did not stop within the %ss pre-close budget; "
                "continuing so source pools are closed inside the shutdown "
                "budget", _pre_close,
            )
        except Exception:  # noqa: BLE001
            pass
    # Bug-8041 R3: retire all source connection pools with a bounded grace so
    # a long DDL (e.g. calendar auto-create) does not block SIGTERM shutdown.
    from shared.source_pool import close_all_pools
    await close_all_pools()


def create_app() -> FastAPI:
    """Build and return a fully-wired model-service FastAPI application.

    The module-level ``app`` singleton (``app = create_app()`` below) is the
    process application; ``uvicorn src.main:app`` and every
    ``from src.main import app`` import keep the identical, fully-wired app —
    this factory only makes that same wiring constructible on demand.

    Tests that must assert route/middleware wiring against a clean, unpolluted
    instance build their own app via this factory instead of inspecting the
    process-global ``app``. The global is shared by every test in the process
    and can, in principle, be mutated by a sibling (route table, dependency
    overrides, openapi cache), which makes any assertion read off it fragile to
    collection order. Building a fresh app removes that whole class — see
    ``tests/test_body_fk_scope_p3c.py::test_every_guarded_route_takes_its_scope_ids_from_the_path``.

    Bug-9002 correction (2026-08-13): this factory is real, independent
    hardening, but it was NOT what fixed that test's standing CI-red failure.
    The actual cause was FastAPI version drift — CI's editable install
    (``uv pip install -e ".[dev]"``) does not read ``uv.lock`` and floated past
    the pinned 0.136.1, and a newer FastAPI's ``include_router()`` stopped
    flattening an included router's routes into ``APIRoute`` instances on
    ``.routes`` (wrapping them in ``_IncludedRouter`` instead), which broke
    ``isinstance(route, APIRoute)`` route-inspection on a FRESH ``create_app()``
    instance identically to the global — no sibling pollution required,
    reproduced in single-test isolation. Fixed by pinning ``fastapi==0.136.1``
    in CI's install step (``.github/workflows/ci.yml``, every
    ``python-tests-*`` job) and in ``scripts/run-like-ci.sh``, not in this
    module. See ``docs/execution/issue-intake/2026-08-12-model-service-ci-route-matrix-test-isolation-failure.md``
    for the full reproduction.
    """
    app = FastAPI(
        title="Tessallite Model Service",
        version="0.1.0",
        description="Semantic model management, metadata, and lineage for Tessallite.",
        lifespan=lifespan,
    )

    # -----------------------------------------------------------------------
    # Rate limiting — LOGIN-ONLY on this service.
    # Per docs/architecture/architecture_rate-limit-placement.md (user decision
    # 2026-08-14), the per-tenant request-rate throttle for USER QUERIES lives on
    # the gateway, NOT on the operational/metadata API: attaching the blanket
    # limiter here throttled the model builder's own operational-DB metadata reads
    # (one /tables/{id}/attributes call per table) and 429'd on model-open. But
    # this service OWNS the SPA/direct auth endpoints (/auth/login[/discover],
    # /auth/system/login), whose brute-force protection (rate_limit.login_per_minute)
    # must NOT be dropped with it. login_only=True keeps that login throttle and
    # passes the operational API through un-throttled. Other user-query surfaces
    # here keep their own scoped limits (ad-hoc KPI: rate_limit.adhoc_kpi_per_minute
    # in api/kpis.py).
    # -----------------------------------------------------------------------
    attach_limiter(app, build_limiter(), login_only=True)

    # -----------------------------------------------------------------------
    # CORS
    # -----------------------------------------------------------------------
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
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(PrometheusMiddleware, service_name="model-service")

    # -----------------------------------------------------------------------
    # Routers
    # -----------------------------------------------------------------------
    PREFIX = "/api/v1"

    app.include_router(admin.router, prefix=PREFIX)
    app.include_router(analytics.router, prefix=PREFIX)
    app.include_router(audit.router, prefix=PREFIX)
    app.include_router(auth.router, prefix=PREFIX)
    app.include_router(pat_tokens.router, prefix=PREFIX)
    app.include_router(tenants.router, prefix=PREFIX)
    app.include_router(projects.router, prefix=PREFIX)
    app.include_router(edition.router, prefix=PREFIX)
    app.include_router(access.router, prefix=PREFIX)
    app.include_router(connections.router, prefix=PREFIX)
    app.include_router(models.router, prefix=PREFIX)
    app.include_router(pockets.router, prefix=PREFIX)
    app.include_router(sources.router, prefix=PREFIX)
    app.include_router(calendar.router, prefix=PREFIX)
    app.include_router(tables.router, prefix=PREFIX)
    app.include_router(tables.batch_router, prefix=PREFIX)
    app.include_router(table_preview.router, prefix=PREFIX)
    app.include_router(table_attributes.router, prefix=PREFIX)
    app.include_router(user_defined_attributes.router, prefix=PREFIX)
    app.include_router(targets.router, prefix=PREFIX)
    app.include_router(hierarchies.router, prefix=PREFIX)
    app.include_router(dimensions.router, prefix=PREFIX)
    app.include_router(dimensions.bulk_router, prefix=PREFIX)
    app.include_router(measures.router, prefix=PREFIX)
    app.include_router(field_compatibility.router, prefix=PREFIX)
    app.include_router(named_sets.router, prefix=PREFIX)
    app.include_router(named_queries.router, prefix=PREFIX)
    app.include_router(kpis.router, prefix=PREFIX)
    app.include_router(joins.router, prefix=PREFIX)
    app.include_router(parameters.router, prefix=PREFIX)
    app.include_router(personas.router, prefix=PREFIX)
    app.include_router(aggregates.router, prefix=PREFIX)
    app.include_router(row_security.router, prefix=PREFIX)
    app.include_router(refresh.router, prefix=PREFIX)
    app.include_router(refresh.model_refresh_router, prefix=PREFIX)
    app.include_router(saved_queries.router, prefix=PREFIX)
    app.include_router(pivot_views.router, prefix=PREFIX)
    app.include_router(alerts.router, prefix=PREFIX)
    app.include_router(alerts.revalidate_router, prefix=PREFIX)
    app.include_router(lineage.router, prefix=PREFIX)
    app.include_router(logs.router, prefix=PREFIX)
    app.include_router(notifications.router, prefix=PREFIX)
    app.include_router(preferences.router, prefix=PREFIX)
    app.include_router(preferences.project_router, prefix=PREFIX)
    app.include_router(metrics.router, prefix=PREFIX)
    app.include_router(export.router, prefix=PREFIX)
    app.include_router(scheduler_config.router, prefix=PREFIX)
    app.include_router(llm_config.router, prefix=PREFIX)
    app.include_router(system_settings.router, prefix=PREFIX)
    app.include_router(project_settings.router, prefix=PREFIX)
    app.include_router(model_settings.router, prefix=PREFIX)
    app.include_router(relationship_health.router, prefix=PREFIX)
    app.include_router(join_population_health.router, prefix=PREFIX)
    app.include_router(git_settings.router, prefix=PREFIX)
    app.include_router(glossary.router, prefix=PREFIX)
    app.include_router(glossary.public_router, prefix=PREFIX)
    app.include_router(translations.router, prefix=PREFIX)
    app.include_router(alias_map.router, prefix=PREFIX)
    app.include_router(versions.router, prefix=PREFIX)
    app.include_router(import_export.router, prefix=PREFIX)
    app.include_router(lookml_export.router, prefix=PREFIX)
    app.include_router(project_import_export.router, prefix=PREFIX)
    app.include_router(yaml_export.router, prefix=PREFIX)
    app.include_router(dbt_import.router, prefix=PREFIX)
    app.include_router(cube_import.router, prefix=PREFIX)
    app.include_router(atscale_import.router, prefix=PREFIX)
    app.include_router(catalog_import.router, prefix=PREFIX)
    app.include_router(collibra.router, prefix=PREFIX)
    app.include_router(solidatus.router, prefix=PREFIX)
    app.include_router(embed.router, prefix=PREFIX)
    app.include_router(sso.router, prefix=PREFIX)
    app.include_router(group_mappings.router, prefix=PREFIX)
    app.include_router(webhooks.router, prefix=PREFIX)
    app.include_router(hierarchy_health.router, prefix=PREFIX)
    app.include_router(schema_changes.router, prefix=PREFIX)
    app.include_router(schema_drift.router, prefix=PREFIX)
    app.include_router(security_audit.router, prefix=PREFIX)
    app.include_router(data_quality.router, prefix=PREFIX)
    app.include_router(data_tags.router, prefix=PREFIX)
    app.include_router(data_tags.restriction_router, prefix=PREFIX)
    app.include_router(downstream_assets.router, prefix=PREFIX)
    app.include_router(downstream_assets.query_ref_router, prefix=PREFIX)
    app.include_router(impact_scan.router, prefix=PREFIX)
    app.include_router(impact_analysis.router, prefix=PREFIX)
    app.include_router(validation.router, prefix=PREFIX)
    app.include_router(refresh_stream.router, prefix=PREFIX)
    app.include_router(tenant_branding.router, prefix=PREFIX)
    app.include_router(model_docs.router, prefix=PREFIX)
    app.include_router(scratchpad_measures.router, prefix=PREFIX)
    app.include_router(odc.router, prefix=PREFIX)

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
        return {"status": "ok", "service": "model-service"}

    @app.get("/health")
    async def health(response: Response) -> dict:
        """Serving readiness: 503 when the metadata database cannot be reached.

        F-030-01 / F-105-12: a 200 here used to mean only "uvicorn is running".
        Compose, Helm, GCP smoke, and the SPA ``/health`` front door treat this
        status code as "the control plane can serve". Keep ``/liveness`` process-only.
        """
        body: dict = {"status": "ok", "service": "model-service"}
        ready, detail = await _metadata_db_ready()
        # G-028-01: surface the Alembic stamp when readable. Do NOT 503 on a
        # missing stamp — local deploy starts this process before migrations.
        try:
            from sqlalchemy import text as _text

            from shared.db.session import SystemSessionLocal as _Sys

            async with _Sys() as session:
                row = await session.execute(_text("SELECT version_num FROM alembic_version"))
                rev = row.scalar()
            if rev:
                body["schema_revision"] = str(rev)
        except Exception:  # noqa: BLE001 — health must never raise
            body["schema_revision"] = None
        if not ready:
            body["status"] = "degraded"
            body["detail"] = "metadata database unreachable"
            response.status_code = 503
            logging.getLogger(__name__).error(
                "model-service /health DEGRADED — metadata DB unreachable: %s", detail
            )
        return body

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics(request: Request):
        return metrics_response(request)

    return app


app = create_app()
