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

Fail-closed host resolution (Bug-7172)
---------------------------------------
``source_db.fallback_host`` carries a compiled-in registry default
("localhost") purely so the admin settings UI has something to display and
edit. Before this fix, a connection whose credentials and config both omitted
a host silently resolved to that compiled-in default with no admin override
ever having been proven to exist — producing a connection string that looked
valid and then failed opaquely deep inside execution (or, worse, quietly
addressed an unintended local database). :func:`resolve_source_db_endpoint`
now RAISES :class:`MissingConnectionHostError` at config-resolution time
instead: fail clearly and early, at the point the misconfiguration actually
exists. Port and database keep the original silent-default behaviour — a
default port/database name is comparatively benign; an unproven default HOST
is not.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from shared.config.resolver import _read_system, get_setting

logger = logging.getLogger(__name__)


class MissingConnectionHostError(RuntimeError):
    """No source-DB host is resolvable from any proven source (Bug-7172).

    Raised when a connection's credentials and config both omit ``host`` and
    no admin-configured ``source_db.fallback_host`` system override could be
    proven to exist (no session was available to check it, or the setting is
    unset). The registry's compiled-in default host must never be used as a
    live connection endpoint by simply doing nothing — that is exactly the
    silent misconfiguration this error replaces.
    """


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

    Port and database fall through to the registry default when nothing else
    resolves them — missing sessions are acceptable there, and callers
    without DB access still get a deterministic answer.

    Host does not: it must come from ``creds``, ``config``, or a PROVEN
    ``source_db.fallback_host`` system override — never the registry's
    compiled-in default with nothing behind it (Bug-7172).

    Raises:
        MissingConnectionHostError: neither ``creds`` nor ``config`` supplies
            a host, and no ``system_session`` was available (or the setting
            is unset) to prove an admin-configured override exists.
    """
    explicit_host = creds.get("host") or config.get("host")
    host: Any
    if explicit_host:
        host = explicit_host
    else:
        host = await get_setting(
            "source_db.fallback_host",
            system_session=system_session,
            tenant_session=tenant_session,
            project_id=project_id,
        )
        # Bug-7172: ``get_setting`` cannot distinguish "an admin proved this
        # override exists" from "nothing was ever configured, so the
        # compiled-in registry default was returned" — both paths return the
        # same string. Re-check presence directly: this hits the resolver's
        # own 30s cache (populated by the ``get_setting`` call above) rather
        # than issuing a second query, so it costs no extra round trip.
        host_is_proven = False
        if system_session is not None:
            host_is_proven, _ = await _read_system(
                system_session, "source_db.fallback_host"
            )
        if not host_is_proven:
            raise MissingConnectionHostError(
                "No source DB host is resolvable: the connection's "
                "credentials and config both omit 'host', and no "
                "admin-configured source_db.fallback_host system override "
                "could be proven to exist. Refusing to silently fall back "
                "to the registry's compiled-in default host."
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
        # Bug-8796: route through qualify_table_name so the SAME guard against
        # a dotted dataset (project.dataset) can never disagree between the
        # two producers of a qualified BigQuery table reference.
        from shared.connector_qualify import qualify_table_name

        if self.bq_project:
            return qualify_table_name(
                "bigquery",
                physical_name,
                schema=self.schema,
                project_id=self.bq_project,
            )
        return qualify_table_name(
            "bigquery", physical_name, schema=self.schema,
        )


def resolve_target_schema(
    connector: str,
    target_config: dict[str, Any],
    target_defaults: dict[str, Any],
    *,
    schema_override: str | None = None,
    connection_bq_project: str | None = None,
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
    connection_bq_project:
        BigQuery project ID from the ProjectConnection (config/creds/service-account).
        The authoritative project for all BigQuery artifact paths (Bug-8790).
        Replaces the removed ``target_config.project_id`` override.
    """
    if connector == "bigquery":
        # BigQuery's "schema" concept is a dataset. The configured target
        # ``dataset`` wins; if the target config carries neither a dataset nor a
        # schema, fall back to the system default *dataset* first
        # (``agg_target.default_dataset``) and only then to the default schema —
        # otherwise a BQ target with no config dataset would silently resolve to
        # an empty/PG-shaped value and create the table in an unqualified ref.
        #
        # NOTE (behaviour change): the last-resort fallback for BigQuery is
        # ``target_defaults.get("dataset")`` (the BQ-aware key) BEFORE
        # ``target_defaults.get("schema")``. This intentionally changed the
        # resolved dataset for a BQ target that has NO config.dataset, NO
        # schema_override, and NO config.schema: it previously resolved to the
        # PG-shaped default_schema (``agg_target.default_schema`` -> "aggregates")
        # and now resolves to the BQ default_dataset
        # (``agg_target.default_dataset`` -> "default"). Deployments that relied
        # on the old PG-named fallback for a BigQuery target should set
        # ``config.dataset`` explicitly on the target connection.
        schema = (
            target_config.get("dataset")
            or schema_override
            or target_config.get("schema")
            or target_defaults.get("dataset")
            or target_defaults.get("schema", "")
        )
        # Bug-8790: the one-project rule — the connection's own project is
        # authoritative. Reject a dotted dataset name, which would let a
        # DataTarget specify a foreign project through its dataset field.
        if "." in schema:
            raise ValueError(
                f"BigQuery dataset must not contain a dot: {schema!r}. "
                "Use a view in the connection's project to reference "
                "external tables."
            )
        # bq_project is the connection's project; ``connection_bq_project`` may
        # be None when the connection resolves its project at runtime (ADC-only).
        # In that case the build emits an unqualified dataset.table reference
        # and BigQuery resolves against the client's project — safe because
        # there is no other project to disagree with.
        bq_project = connection_bq_project or ""
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


class BigQueryProjectResolutionError(ValueError):
    """A stored BigQuery project identity exists but cannot be read safely."""


def resolve_connection_bq_project(conn: Any) -> str | None:
    """Return the canonical BigQuery project for a connection.

    Resolution order: config.project_id -> creds.project_id ->
    service-account project_id. Returns ``None`` only for a non-BigQuery
    connection or a genuine ADC/no-credentials BigQuery connection.

    Malformed config or unreadable stored credentials raise a safe, generic
    error. Treating those states as ADC would silently erase a persisted
    project identity and could make a build use an unintended client default.
    """
    from shared.schemas.connection_type import normalize_connection_type

    ctype = normalize_connection_type(
        str(getattr(conn, "connection_type", "") or "").lower()
    )
    if ctype != "bigquery":
        return None

    connection_id = str(getattr(conn, "id", "") or "unknown")
    raw_config = getattr(conn, "config", None)
    if raw_config is None:
        config: dict[str, Any] = {}
    elif isinstance(raw_config, dict):
        config = raw_config
    else:
        logger.warning(
            "BigQuery project resolution refused malformed config "
            "(connection_id=%s)",
            connection_id,
        )
        raise BigQueryProjectResolutionError(
            "BigQuery connection project configuration is invalid"
        )

    config_project = config.get("project_id")
    if config_project is not None:
        if not isinstance(config_project, str) or not config_project.strip():
            logger.warning(
                "BigQuery project resolution refused invalid config project_id "
                "(connection_id=%s)",
                connection_id,
            )
            raise BigQueryProjectResolutionError(
                "BigQuery connection project configuration is invalid"
            )
        return config_project.strip()

    encrypted = getattr(conn, "encrypted_credentials", None)
    if not encrypted:
        return None

    try:
        from shared.security.credential_crypto import decrypt_json
        from shared.source_introspection import _bq_project, _bq_sa_info

        creds = decrypt_json(encrypted)
        if not isinstance(creds, dict):
            raise TypeError("decrypted credentials are not an object")
        project = _bq_project(creds, {}, _bq_sa_info(creds))
    except Exception as exc:
        logger.warning(
            "BigQuery project resolution refused unreadable credentials "
            "(connection_id=%s, error_type=%s)",
            connection_id,
            type(exc).__name__,
        )
        raise BigQueryProjectResolutionError(
            "BigQuery connection credentials could not be read"
        ) from None

    if project is None:
        return None
    if not isinstance(project, str) or not project.strip():
        logger.warning(
            "BigQuery project resolution refused invalid credential project_id "
            "(connection_id=%s)",
            connection_id,
        )
        raise BigQueryProjectResolutionError(
            "BigQuery connection project configuration is invalid"
        )
    return project.strip()


def target_connection_authority_is_provable(target: Any, conn: Any) -> bool:
    """Return whether a target can safely be served through ``conn``.

    The connection is the route/executor authority. ``DataTarget.target_type``
    is retained for API compatibility, but a legacy mismatch must never decide
    that a BigQuery target is safe. This read-side proof covers old rows created
    before the write validator, imported snapshots, and a partially upgraded
    tenant until the transition migration has forced a rebuild.

    BigQuery needs one additional proof: its dataset is a bare dataset name,
    and any target project override agrees with the explicit project resolved
    from the connection. ADC-only and unreadable connection state are not
    provable for artifact serving and therefore fail closed.
    """
    from shared.schemas.connection_type import normalize_connection_type

    connection_type = normalize_connection_type(
        str(getattr(conn, "connection_type", "") or "").lower()
    )
    target_type = normalize_connection_type(
        str(getattr(target, "target_type", "") or "").lower()
    )
    if not connection_type or target_type != connection_type:
        return False
    if connection_type != "bigquery":
        return True

    config = getattr(target, "config", None)
    if not isinstance(config, dict):
        return False
    dataset = config.get("dataset") or config.get("schema") or ""
    if not isinstance(dataset, str) or "." in dataset:
        return False
    try:
        connection_project = resolve_connection_bq_project(conn)
    except BigQueryProjectResolutionError:
        return False
    if not connection_project:
        return False
    target_project = config.get("project_id")
    return target_project in (None, "", connection_project)


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
