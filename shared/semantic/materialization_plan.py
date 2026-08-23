"""Connector-neutral aggregate materialisation plan and SQLGlot renderer.

Bug-7076: optimizer creation and scheduler refresh used parallel caller-level
BigQuery/PostgreSQL/Spark SQL programs. This module is the shared lifecycle
boundary: producers supply one PostgreSQL-canonical plan; connector capability
data selects replacement semantics; SQLGlot renders every executable statement.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

import sqlglot
from sqlglot import exp

from shared.aggregate_type_mapping import map_column_type
from shared.schemas.connection_type import normalize_connection_type


@dataclass(frozen=True, slots=True)
class ConnectorMaterializationCapabilities:
    sqlglot_dialect: str
    supports_create_or_replace: bool = False


_CAPABILITIES: dict[str, ConnectorMaterializationCapabilities] = {
    "postgresql": ConnectorMaterializationCapabilities("postgres"),
    "redshift": ConnectorMaterializationCapabilities("redshift"),
    "snowflake": ConnectorMaterializationCapabilities("snowflake"),
    "sqlserver": ConnectorMaterializationCapabilities("tsql"),
    "bigquery": ConnectorMaterializationCapabilities(
        "bigquery", supports_create_or_replace=True,
    ),
    "hadoop_spark": ConnectorMaterializationCapabilities("spark"),
}


@dataclass(frozen=True, slots=True)
class MaterializationPlan:
    """One canonical lifecycle program, independent of connector syntax."""

    drop_sql: str | None
    create_sql: str
    select_sql: str | None = None
    column_defs: tuple[tuple[str, str], ...] = ()
    allow_create_or_replace: bool = True


@dataclass(frozen=True, slots=True)
class RenderedMaterializationPlan:
    statements: tuple[str, ...]
    select_sql: str | None
    column_defs: tuple[tuple[str, str], ...]
    destructive: bool


async def execute_materialization_statements(
    statements: Iterable[str],
    *,
    connection: Any,
    tenant_session: Any,
    execute_ddl: Callable[..., Any],
    as_batch: bool = False,
    on_statement_complete: Callable[[int, str], None] | None = None,
) -> None:
    """Execute only SQL returned by the shared materialisation renderer.

    Lifecycle owners pass their sanctioned ``execute_source_ddl`` boundary as
    a callback, keeping this module independent of connection plumbing while
    making the executable data flow explicit and statically checkable. Batch
    mode preserves operations that deliberately hand a discrete statement
    sequence to the executor; individual mode allows refresh to checkpoint a
    successful destructive DROP before attempting its CTAS (Bug-7076).
    """
    rendered = tuple(statement.strip() for statement in statements if statement.strip())
    if not rendered:
        raise ValueError("A rendered materialisation program cannot be empty")
    if as_batch:
        await execute_ddl(
            connection, list(rendered), tenant_session=tenant_session,
        )
        if on_statement_complete is not None:
            for index, statement in enumerate(rendered):
                on_statement_complete(index, statement)
        return
    for index, statement in enumerate(rendered):
        await execute_ddl(connection, statement, tenant_session=tenant_session)
        if on_statement_complete is not None:
            on_statement_complete(index, statement)


def _capabilities(connector: str) -> ConnectorMaterializationCapabilities:
    normalized = normalize_connection_type(connector)
    capabilities = _CAPABILITIES.get(normalized)
    if capabilities is None:
        raise ValueError(
            f"Unsupported materialisation connector: {normalized!r}"
        )
    return capabilities


def _render_canonical(sql: str, dialect: str) -> str:
    rendered = sqlglot.transpile(sql, read="postgres", write=dialect)
    if len(rendered) != 1:
        raise ValueError("A materialisation-plan statement must render once")
    return rendered[0]


def render_materialization_statement(
    canonical_sql: str,
    *,
    target_connector: str,
) -> str:
    """Render one PostgreSQL-canonical lifecycle statement via SQLGlot."""
    target = _capabilities(target_connector)
    return _render_canonical(canonical_sql, target.sqlglot_dialect)


def render_materialization_plan(
    plan: MaterializationPlan,
    *,
    target_connector: str,
    source_connector: str | None = None,
) -> RenderedMaterializationPlan:
    """Render one canonical plan for its target and optional source.

    BigQuery's atomic ``CREATE OR REPLACE`` is selected from capability data,
    not a caller SQL branch. Every other target receives discrete DROP/CREATE
    statements, allowing the executor to release between independent units.
    """
    target = _capabilities(target_connector)
    use_replace = (
        plan.allow_create_or_replace and target.supports_create_or_replace
    )

    create_tree = sqlglot.parse_one(plan.create_sql, read="postgres")
    if not isinstance(create_tree, exp.Create):
        raise ValueError("MaterializationPlan.create_sql must be CREATE TABLE")
    statements: list[str] = []
    if use_replace:
        create_tree.set("replace", True)
        statements.append(
            create_tree.sql(dialect=target.sqlglot_dialect, pretty=True)
        )
    else:
        if plan.drop_sql:
            statements.append(
                _render_canonical(plan.drop_sql, target.sqlglot_dialect)
            )
        statements.append(
            create_tree.sql(dialect=target.sqlglot_dialect, pretty=True)
        )

    rendered_select = None
    if plan.select_sql is not None:
        if source_connector is None:
            raise ValueError(
                "source_connector is required when a plan carries select_sql"
            )
        source = _capabilities(source_connector)
        rendered_select = _render_canonical(
            plan.select_sql, source.sqlglot_dialect,
        )

    normalized_target = normalize_connection_type(target_connector)
    rendered_defs = tuple(
        (
            name,
            map_column_type(
                data_type, "postgresql", normalized_target,
            ),
        )
        for name, data_type in plan.column_defs
    )
    return RenderedMaterializationPlan(
        statements=tuple(statements),
        select_sql=rendered_select,
        column_defs=rendered_defs,
        destructive=bool(plan.drop_sql) and not use_replace,
    )


def supports_pg_numeric_scale(connector: str) -> bool:
    """Whether the live numeric(p,s) probe is valid for this connector."""
    return normalize_connection_type(connector) in {"postgresql", "redshift"}
