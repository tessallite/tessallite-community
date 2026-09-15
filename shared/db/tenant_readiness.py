"""Fail-closed readiness checks for tenant metadata schemas.

The system tenant row is the authority for a tenant's connection details.  The
schema revision is the authority for whether this build may use that connection
for normal tenant work.  This module deliberately keeps the check at the
shared database boundary so login, API, worker, pooled, and snapshot callers
cannot choose a separate availability policy.
"""
from __future__ import annotations

import asyncio
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import NoReturn

from alembic.config import Config
from alembic.script import Script, ScriptDirectory
from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

_TENANT_READINESS_ERROR_CODE = "TENANT_DATABASE_UNAVAILABLE"
_TENANT_READINESS_CONDITION = "tenant_database_unavailable"
_RETRY_AFTER_SECONDS = 5
_MIGRATION_INI = Path(__file__).resolve().parent / "migrations" / "alembic.ini"
_DSN_PATTERN = re.compile(
    r"\b(?:postgres(?:ql)?|mysql|mariadb|mssql|oracle|redshift|snowflake|sqlite)"
    r"(?:\+[a-z0-9_.-]+)?://[^\s\"'<>]+",
    re.IGNORECASE,
)


def _safe_token(value: object | None, *, limit: int = 128) -> str | None:
    """Keep database-owned values safe for JSON and log messages."""
    if value is None:
        return None
    token = str(value).replace("\r", " ").replace("\n", " ").strip()
    return token[:limit] or None


def _safe_exception(exc: BaseException) -> str:
    """Retain useful driver context without retaining a DSN or credentials."""
    detail = _DSN_PATTERN.sub("<redacted-dsn>", str(exc).strip())
    detail = detail.replace("\r", " ").replace("\n", " ")
    return detail[:512] or type(exc).__name__


class TenantReadinessError(HTTPException):
    """Typed 503 raised before a tenant schema can be used.

    The detail is intentionally safe for an API response.  The operator log
    carries the exception class and redacted driver text separately, while the
    response names the tenant, operation, cause, and repair direction.
    """

    error_code = _TENANT_READINESS_ERROR_CODE
    condition = _TENANT_READINESS_CONDITION

    def __init__(
        self,
        *,
        tenant_slug: str,
        operation: str,
        cause: str,
        current_revision: str | None = None,
        required_revision: str | None = None,
    ) -> None:
        self.tenant_slug = _safe_token(tenant_slug) or "<unknown>"
        self.operation = _safe_token(operation) or "tenant database access"
        self.cause = _safe_token(cause, limit=256) or "tenant readiness could not be verified"
        self.current_revision = _safe_token(current_revision, limit=64)
        self.required_revision = _safe_token(required_revision, limit=64)

        revision_text = ""
        if self.current_revision or self.required_revision:
            revision_text = (
                f" Current revision: {self.current_revision or '<missing>'};"
                f" required revision: {self.required_revision or '<unknown>'}."
            )
        message = (
            f"Tenant '{self.tenant_slug}' access refused for {self.operation}: "
            f"{self.cause}.{revision_text} Repair with the system-admin tenant "
            "migration API, then retry."
        )
        detail = {
            "error_code": self.error_code,
            "condition": self.condition,
            "tenant_slug": self.tenant_slug,
            "operation": self.operation,
            "cause": self.cause,
            "current_revision": self.current_revision,
            "required_revision": self.required_revision,
            "message": message,
        }
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=detail,
            headers={"Retry-After": str(_RETRY_AFTER_SECONDS)},
        )


def _log_refusal(error: TenantReadinessError, original: BaseException | None) -> None:
    detail = _safe_exception(original) if original is not None else "none"
    logger.error(
        "tenant database access refused tenant=%s operation=%s cause=%s "
        "current_revision=%s required_revision=%s exception=%s",
        error.tenant_slug,
        error.operation,
        error.cause,
        error.current_revision or "<missing>",
        error.required_revision or "<unknown>",
        detail,
    )


def make_tenant_readiness_error(
    *,
    tenant_slug: str,
    operation: str,
    cause: str,
    current_revision: str | None = None,
    required_revision: str | None = None,
    original: BaseException | None = None,
) -> TenantReadinessError:
    """Build and log a safe tenant readiness refusal."""
    error = TenantReadinessError(
        tenant_slug=tenant_slug,
        operation=operation,
        cause=cause,
        current_revision=current_revision,
        required_revision=required_revision,
    )
    _log_refusal(error, original)
    return error


def raise_tenant_readiness_error(
    *,
    tenant_slug: str,
    operation: str,
    cause: str,
    current_revision: str | None = None,
    required_revision: str | None = None,
    original: BaseException | None = None,
) -> NoReturn:
    """Raise a logged, typed tenant readiness refusal."""
    error = make_tenant_readiness_error(
        tenant_slug=tenant_slug,
        operation=operation,
        cause=cause,
        current_revision=current_revision,
        required_revision=required_revision,
        original=original,
    )
    if original is None:
        raise error
    raise error from original


@lru_cache(maxsize=1)
def migration_scripts() -> ScriptDirectory:
    """Load the shipped Alembic graph used by all readiness checks."""
    return ScriptDirectory.from_config(Config(str(_MIGRATION_INI)))


def required_tenant_revision() -> str:
    """Resolve the tenant branch head from Alembic's graph, never a constant."""
    revision: Script | None = migration_scripts().get_revision("tenant@head")
    if revision is None or not revision.revision:
        raise RuntimeError("Alembic tenant@head did not resolve to a revision")
    return str(revision.revision)


def _schema_name(quoted_schema: str) -> str:
    """Recover the schema name used by env.py's advisory-lock key."""
    if len(quoted_schema) >= 2 and quoted_schema.startswith('"') and quoted_schema.endswith('"'):
        return quoted_schema[1:-1].replace('""', '"')
    return quoted_schema


def _is_connection_failure(exc: BaseException) -> bool:
    if isinstance(exc, (sa_exc.InterfaceError, TimeoutError, asyncio.TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, sa_exc.OperationalError):
        original = getattr(exc, "orig", None)
        sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
        return isinstance(sqlstate, str) and sqlstate[:2] in {"08", "53", "57", "58"}
    return False


def _is_missing_revision_state(exc: BaseException) -> bool:
    """Identify an absent schema/version table without exposing its SQL text."""
    original = getattr(exc, "orig", None)
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    if sqlstate == "42P01":
        return True
    detail = str(exc).lower()
    return "alembic_version" in detail and "does not exist" in detail


async def ensure_tenant_schema_ready(
    engine: AsyncEngine,
    *,
    tenant_slug: str,
    quoted_schema: str,
    operation: str,
) -> None:
    """Check the tenant stamp under the same advisory lock as Alembic.

    A migration either commits its final stamp before this transaction reads it
    or remains invisible while this check waits on the per-schema lock.  The
    check runs for both fresh and cached engines, so a cached factory cannot
    bypass a repaired, stale, missing, or unknown schema state.
    """
    try:
        required = required_tenant_revision()
    except Exception as exc:  # noqa: BLE001 - an unreadable graph must fail closed
        raise_tenant_readiness_error(
            tenant_slug=tenant_slug,
            operation=operation,
            cause="tenant migration graph could not be resolved",
            original=exc,
        )

    schema = _schema_name(quoted_schema)
    try:
        async with engine.connect() as connection:
            async with connection.begin():
                await connection.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
                    {"lock_name": f"tessallite:alembic:{schema}"},
                )
                result = await connection.execute(
                    text(f"SELECT version_num FROM {quoted_schema}.alembic_version")
                )
                rows = list(result.scalars().all())
    except TenantReadinessError:
        raise
    except Exception as exc:  # noqa: BLE001 - convert every boundary failure to 503
        if _is_missing_revision_state(exc):
            cause = "tenant schema revision is missing"
        elif _is_connection_failure(exc):
            cause = "tenant database connection could not be opened"
        else:
            cause = "tenant schema state could not be read"
        raise_tenant_readiness_error(
            tenant_slug=tenant_slug,
            operation=operation,
            cause=cause,
            required_revision=required,
            original=exc,
        )

    if not rows:
        raise_tenant_readiness_error(
            tenant_slug=tenant_slug,
            operation=operation,
            cause="tenant schema revision is missing",
            required_revision=required,
        )
    if len(rows) != 1 or rows[0] is None:
        current = ",".join(str(row) for row in rows)[:64] or None
        raise_tenant_readiness_error(
            tenant_slug=tenant_slug,
            operation=operation,
            cause="tenant schema revision is unreadable",
            current_revision=current,
            required_revision=required,
        )

    current = str(rows[0])
    if current == required:
        return

    try:
        known = migration_scripts().get_revision(current)
    except Exception:
        known = None
    cause = (
        "tenant schema revision is older than the service"
        if known is not None
        else "tenant schema revision is unknown to the service"
    )
    raise_tenant_readiness_error(
        tenant_slug=tenant_slug,
        operation=operation,
        cause=cause,
        current_revision=current,
        required_revision=required,
    )
