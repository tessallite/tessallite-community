"""Model-service startup migration gate with per-tenant isolation.

The deployment wrappers retain their explicit migration checks, but a
container can also be refreshed directly. Keeping the gate in the image makes
that path safe: the process only reaches Uvicorn after the system branch has
reached its mode-scoped Alembic head and each tenant either reaches its head or
is logged as unavailable and refused at the shared request boundary. Readiness
is derived again from the authoritative tenant row and schema stamp; this gate
does not write a second availability state.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from shared.config.bootstrap import refresh_system_snapshot

from shared.db.models import SystemTenant
from shared.db.session import SystemSessionLocal, normalize_tenant_db_url
from shared.security.credential_crypto import decrypt_str
from src.api.admin import AlembicMigrationError, _run_alembic, _redact_dsn

logger = logging.getLogger(__name__)


def _safe_failure_reason(exc: BaseException) -> str:
    """Keep startup diagnostics useful without retaining connection URLs."""
    if isinstance(exc, AlembicMigrationError):
        detail = exc.redacted_stderr
    else:
        detail = f"{type(exc).__name__}: {exc}"
    return _redact_dsn(detail).strip() or type(exc).__name__


def _record_tenant_failure(
    slug: str,
    exc: BaseException,
    *,
    cause: str | None = None,
) -> None:
    reason = cause or _safe_failure_reason(exc)
    logger.error(
        "model-service tenant unavailable after startup migration failure "
        "tenant=%s cause=%s access_refused=true repair=system-admin-tenant-migration",
        slug,
        reason,
    )


def _run_startup_alembic(
    phase: str,
    *,
    tenant_slug: str = "",
    database_url: str = "",
) -> None:
    """Run one gate phase and log safe Alembic diagnostics on failure."""
    try:
        _run_alembic(
            phase,
            tenant_slug=tenant_slug,
            database_url=database_url,
        )
    except AlembicMigrationError as exc:
        logger.error(
            "model-service startup Alembic migration failed phase=%s "
            "tenant=%s target=%s stderr=%s",
            exc.phase,
            exc.tenant_slug,
            exc.target_revision,
            _redact_dsn(exc.redacted_stderr),
        )
        raise


async def _tenant_migration_targets() -> list[tuple[str, str]]:
    """Read every tenant and resolve its migration database URL.

    Inactive tenants are included deliberately. They remain persisted schema
    owners and must not be left behind when an image is refreshed and later
    reactivated. URLs are returned only to the migration subprocess and are
    never written to logs.
    """
    async with SystemSessionLocal() as session:
        result = await session.execute(
            select(SystemTenant).order_by(SystemTenant.slug)
        )
        tenants = result.scalars().all()
        targets: list[tuple[str, str]] = []
        for tenant in tenants:
            try:
                stored_url = decrypt_str(tenant.encrypted_db_url)
            except Exception as exc:  # noqa: BLE001 — isolate this tenant only
                _record_tenant_failure(
                    tenant.slug,
                    exc,
                    cause=(
                        "stored DB credentials cannot be decrypted with configured keys"
                    ),
                )
                continue
            try:
                database_url = normalize_tenant_db_url(stored_url, tenant.slug)
            except Exception as exc:  # noqa: BLE001 — isolate this tenant only
                _record_tenant_failure(tenant.slug, exc)
                continue
            targets.append((tenant.slug, database_url))
        return targets


async def migrate_before_serve() -> None:
    """Migrate system first and isolate tenant migration failures."""
    logger.info("running model-service system migration before serving")
    _run_startup_alembic("system")
    # Once the system schema exists, honour its configured admin timeout for
    # tenant migrations just as the authenticated migration endpoint does.
    await refresh_system_snapshot()

    targets = await _tenant_migration_targets()
    logger.info("running tenant migrations before serving (tenants=%d)", len(targets))
    failed_tenants: list[str] = []
    for slug, database_url in targets:
        logger.info("running tenant migration before serving (tenant=%s)", slug)
        try:
            _run_startup_alembic(
                "tenant",
                tenant_slug=slug,
                database_url=database_url,
            )
        except Exception as exc:  # noqa: BLE001 — isolate this tenant only
            failed_tenants.append(slug)
            _record_tenant_failure(slug, exc)
            continue

    logger.info(
        "model-service startup migration gate passed; healthy tenants are "
        "available and failed tenants remain refused until repaired "
        "(unavailable=%d)",
        len(failed_tenants),
    )


def main() -> None:
    """Run the gate as the container entrypoint's pre-serve process."""
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(migrate_before_serve())
    except Exception as exc:
        # DB/driver exceptions can contain connection URLs. The phase and tenant
        # are logged above; never print exception details containing credentials.
        logger.error(
            "model-service startup migration gate failed (%s); refusing to serve",
            type(exc).__name__,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
