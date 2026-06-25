"""
Tessallite Model Service — FastAPI application entry point.

Registers all API routers.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from shared.auth.csrf import CSRFMiddleware
from shared.config.bootstrap import refresh_system_snapshot
from shared.config.settings import get_settings
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
    glossary,
    group_mappings,
    hierarchies,
    impact_scan,
    import_export,
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
    notifications,
    parameters,
    personas,
    pivot_views,
    pockets,
    preferences,
    project_settings,
    projects,
    refresh,
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
        try:
            await beacon.stop()
        except Exception:  # noqa: BLE001
            pass
    # asyncpg connection pools close on GC


app = FastAPI(
    title="Tessallite Model Service",
    version="0.1.0",
    description="Semantic model management, metadata, and lineage for Tessallite.",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Rate limiting (per-tenant; no-op when RATE_LIMIT_ENABLED=False)
# ---------------------------------------------------------------------------
_limiter = build_limiter()
attach_limiter(app, _limiter)

# ---------------------------------------------------------------------------
# CORS
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
app.add_middleware(CSRFMiddleware)
app.add_middleware(PrometheusMiddleware, service_name="model-service")

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
PREFIX = "/api/v1"

app.include_router(admin.router, prefix=PREFIX)
app.include_router(analytics.router, prefix=PREFIX)
app.include_router(audit.router, prefix=PREFIX)
app.include_router(auth.router, prefix=PREFIX)
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
app.include_router(metrics.router, prefix=PREFIX)
app.include_router(export.router, prefix=PREFIX)
app.include_router(scheduler_config.router, prefix=PREFIX)
app.include_router(llm_config.router, prefix=PREFIX)
app.include_router(system_settings.router, prefix=PREFIX)
app.include_router(project_settings.router, prefix=PREFIX)
app.include_router(model_settings.router, prefix=PREFIX)
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
app.include_router(validation.router, prefix=PREFIX)
app.include_router(refresh_stream.router, prefix=PREFIX)
app.include_router(tenant_branding.router, prefix=PREFIX)
app.include_router(model_docs.router, prefix=PREFIX)
app.include_router(scratchpad_measures.router, prefix=PREFIX)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "model-service"}


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics(request: Request):
    return metrics_response(request)
