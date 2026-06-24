"""Source DB endpoint resolution and aggregate target schema helpers.

A handful of code paths need a (host, port, database) triple for the source
PostgreSQL/Spark instance when the connection's stored credentials and the
non-secret config dict both come up empty. They fall back to the system-level
settings ``source_db.fallback_host`` / ``source_db.fallback_port`` /
``source_db.fallback_database``.

After the 2026-04 admin-config restructure, these fallback keys live at
the system level (surfaced=False — operational, not user-edited). The
``tenant_session`` argument is retained for backwards compatibility with
existing callers; new call sites should also pass ``system_session`` so
admin-set system overrides are honoured.

This module owns the precedence chain so the same logic doesn't drift
across query-router, model-service, scheduler, and optimizer call sites.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from shared.config.resolver import get_setting

logger = logging.getLogger(__name__)


async def resolve_source_db_endpoint(
    creds: dict,
    config: dict,
    *,
    tenant_session: Optional[AsyncSession] = None,
    system_session: Optional[AsyncSession] = None,
    project_id: Optional[UUID] = None,
) -> tuple[str, int, str]:
    """Return (host, port, database) using the documented precedence.

    Precedence:
      1. ``creds`` dict (decrypted from project_connections.encrypted_credentials)
      2. ``config`` dict (project_connections.config — non-secret)
      3. ``source_db.fallback_*`` setting (system level, surfaced=False)

    Missing sessions are acceptable; the resolver falls through to the
    registry default so callers without DB access still get a deterministic
    answer.
    """
    host = (
        creds.get("host")
        or config.get("host")
        or await get_setting(
            "source_db.fallback_host",
            system_session=system_session,
            tenant_session=tenant_session,
            project_id=project_id,
        )
    )
    port_raw = (
        creds.get("port")
        or config.get("port")
        or await get_setting(
            "source_db.fallback_port",
            system_session=system_session,
            tenant_session=tenant_session,
            project_id=project_id,
        )
    )
    database = (
        creds.get("database")
        or config.get("database")
        or await get_setting(
            "source_db.fallback_database",
            system_session=system_session,
            tenant_session=tenant_session,
            project_id=project_id,
        )
    )
    return str(host), int(port_raw), str(database)


async def resolve_aggregate_target_defaults(
    *,
    tenant_session: Optional[AsyncSession] = None,
    system_session: Optional[AsyncSession] = None,
    project_id: Optional[UUID] = None,
) -> dict[str, Any]:
    """Return the (schema, dataset, database) triple for aggregate targets."""
    schema = await get_setting(
        "agg_target.default_schema",
        system_session=system_session,
        tenant_session=tenant_session,
        project_id=project_id,
    )
    dataset = await get_setting(
        "agg_target.default_dataset",
        system_session=system_session,
        tenant_session=tenant_session,
        project_id=project_id,
    )
    database = await get_setting(
        "agg_target.default_database",
        system_session=system_session,
        tenant_session=tenant_session,
        project_id=project_id,
    )
    return {"schema": schema, "dataset": dataset, "database": database}


@dataclasses.dataclass(frozen=True, slots=True)
class AggregateTargetRef:
    """Resolved target schema identifiers for aggregate table creation."""
    schema: str
    bq_project: str = ""

    def qualified_table(self, physical_name: str) -> str:
        if self.bq_project:
            return f"{self.bq_project}.{self.schema}.{physical_name}"
        return f"{self.schema}.{physical_name}"


def resolve_target_schema(
    connector: str,
    target_config: dict[str, Any],
    target_defaults: dict[str, Any],
    *,
    schema_override: str | None = None,
) -> AggregateTargetRef:
    """Resolve the target schema/dataset for aggregate DDL.

    Centralises the per-connector fallback chain that was previously
    duplicated across full_refresh.py, creator.py, and incremental_refresh.py.

    Parameters
    ----------
    connector:
        Normalised connector type.
    target_config:
        ``project_connections.config`` dict for the aggregate target.
    target_defaults:
        Result of ``resolve_aggregate_target_defaults()``.
    schema_override:
        Explicit schema from the aggregate definition (``agg_def.target_schema``).
        Takes priority over config/defaults for non-BQ connectors, and over
        defaults (but not config ``dataset``) for BigQuery.
    """
    if connector == "bigquery":
        schema = (
            target_config.get("dataset")
            or schema_override
            or target_config.get("schema")
            or target_defaults.get("schema", "")
        )
        bq_project = target_config.get("project_id", "")
        return AggregateTargetRef(schema=schema, bq_project=bq_project)

    if connector in ("postgresql", "redshift"):
        fallback = target_defaults.get("database", "")
    elif connector == "snowflake":
        fallback = target_defaults.get("schema", "PUBLIC")
    elif connector == "sqlserver":
        fallback = "dbo"
    elif connector == "hadoop_spark":
        fallback = target_defaults.get("dataset", "")
    else:
        raise ValueError(f"resolve_target_schema: unsupported connector {connector!r}")

    schema = schema_override or target_config.get("schema", fallback)
    return AggregateTargetRef(schema=schema)


async def resolve_spark_thrift_defaults(
    *,
    tenant_session: Optional[AsyncSession] = None,
    system_session: Optional[AsyncSession] = None,
) -> dict[str, Any]:
    """Return Spark Thrift port/database/auth_mode defaults."""
    return {
        "port": int(
            await get_setting(
                "spark.thrift_port",
                system_session=system_session,
                tenant_session=tenant_session,
            )
        ),
        "database": str(
            await get_setting(
                "spark.thrift_database",
                system_session=system_session,
                tenant_session=tenant_session,
            )
        ),
        "auth_mode": str(
            await get_setting(
                "spark.thrift_auth_mode",
                system_session=system_session,
                tenant_session=tenant_session,
            )
        ),
    }
