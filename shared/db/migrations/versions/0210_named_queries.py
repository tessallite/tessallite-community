"""Named Queries — definition + artifact + refresh policy/runs tables.

Named Queries (governed, modeler-authored semantic queries served
materialised-first / source-fallback, referenced as ``SELECT * FROM @Name``):

* ``named_queries`` — the DEFINITION (model-bound logical SQL, output-column
  schema, shape, caps, certification). Uniqueness on
  ``(model_id, lower(name))`` — the ``@`` namespace is case-insensitive per
  model (Bug-7663 precedent); the cross-table uniqueness against
  ``model_parameters`` and ``named_sets`` is enforced at create time in the
  model-service.
* ``named_query_artifacts`` — the MATERIALISATION on the shared artifact
  substrate (physical table, row manifest, lifecycle status with the same
  CHECK as pockets, refresh liveness pointer, version binding).
* ``named_query_refresh_policies`` — 1:1 schedule policy (mirror
  ``pocket_refresh_policies``).
* ``named_query_refresh_runs`` — run history (mirror ``pocket_refresh_runs``).

This is a TENANT-chain migration: it revises 0209 (the physical-cleanup
tenant migration, which is the tenant head), NOT the system head (0207).
``alembic heads`` stays at exactly two heads (system 0207 + tenant 0210).

Revision ID: 0210
Revises: 0209
Create Date: 2026-08-14
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0210"
down_revision = "0209"
branch_labels = None
depends_on = None

_NAMED_QUERIES_STATUS_CHECK = "ck_named_query_artifacts_status"
_NAMED_QUERIES_STATUSES = ("fresh", "stale", "invalidating", "failed")
_TABLES = frozenset(
    {
        "named_queries",
        "named_query_artifacts",
        "named_query_refresh_policies",
        "named_query_refresh_runs",
    }
)

# The legacy repair is deliberately strict. These signatures describe the
# observed pre-0210 ``TenantBase.metadata.create_all`` shape. Its client-side
# UUID/status defaults are absent from PostgreSQL, unlike 0210's server-side
# defaults; accepting both shapes would turn this into a broad object-exists
# bypass. Any different required object needs operator inspection.
_COLUMNS = {
    "named_queries": {
        "id": ("uuid", False, None),
        "model_id": ("uuid", False, None),
        "name": ("varchar(255)", False, None),
        "display_name": ("varchar(255)", True, None),
        "description": ("text", True, None),
        "display_folder": ("varchar(255)", True, None),
        "definition_sql": ("text", False, None),
        "output_columns": ("jsonb", True, None),
        "shape": ("varchar(16)", False, None),
        "row_cap": ("integer", True, None),
        "column_cap": ("integer", True, None),
        "certification_status": ("varchar(32)", False, None),
        "created_by": ("varchar(255)", True, None),
        "created_at": ("timestamptz", False, "now()"),
        "updated_at": ("timestamptz", False, "now()"),
    },
    "named_query_refresh_runs": {
        "id": ("uuid", False, None),
        "named_query_id": ("uuid", False, None),
        "refresh_mode": ("varchar(32)", False, None),
        "status": ("varchar(32)", False, None),
        "started_at": ("timestamptz", False, "now()"),
        "completed_at": ("timestamptz", True, None),
        "rows_written": ("bigint", True, None),
        "bytes_processed": ("bigint", True, None),
        "error_message": ("text", True, None),
        "triggered_by": ("varchar(32)", False, None),
    },
    "named_query_artifacts": {
        "id": ("uuid", False, None),
        "named_query_id": ("uuid", False, None),
        "target_id": ("uuid", False, None),
        "physical_table_name": ("varchar(512)", False, None),
        "target_schema": ("varchar(255)", True, None),
        "row_manifest": ("jsonb", True, None),
        "row_count": ("bigint", True, None),
        "status": ("varchar(32)", False, None),
        "failure_reason": ("text", True, None),
        "active_refresh_run_id": ("uuid", True, None),
        "last_refresh_at": ("timestamptz", True, None),
        "retired_at": ("timestamptz", True, None),
        "built_for_version_id": ("uuid", True, None),
        "built_for_epoch": ("integer", True, None),
    },
    "named_query_refresh_policies": {
        "id": ("uuid", False, None),
        "named_query_id": ("uuid", False, None),
        "cron_expression": ("varchar(128)", True, None),
        "is_enabled": ("boolean", False, None),
        "created_at": ("timestamptz", False, "now()"),
        "updated_at": ("timestamptz", False, "now()"),
    },
}
_PRIMARY_KEYS = {table: ("id",) for table in _TABLES}
_UNIQUE_CONSTRAINTS = {
    "named_queries": frozenset(),
    "named_query_refresh_runs": frozenset(),
    "named_query_artifacts": frozenset(),
    "named_query_refresh_policies": frozenset({("named_query_id",)}),
}
_FOREIGN_KEYS = {
    "named_queries": frozenset({(("model_id",), "models", ("id",), "CASCADE")}),
    "named_query_refresh_runs": frozenset(
        {(("named_query_id",), "named_queries", ("id",), "CASCADE")}
    ),
    "named_query_artifacts": frozenset(
        {
            (("named_query_id",), "named_queries", ("id",), "CASCADE"),
            (("target_id",), "data_targets", ("id",), None),
            (
                ("active_refresh_run_id",),
                "named_query_refresh_runs",
                ("id",),
                "SET NULL",
            ),
        }
    ),
    "named_query_refresh_policies": frozenset(
        {(("named_query_id",), "named_queries", ("id",), "CASCADE")}
    ),
}
_INDEXES = {
    "named_queries": frozenset(
        {
            ("ix_named_queries_model_id", False, ("model_id",)),
            (
                "uq_named_queries_model_lower_name",
                True,
                ("model_id", "lower(name)"),
            ),
        }
    ),
    "named_query_refresh_runs": frozenset(
        {
            (
                "ix_named_query_refresh_runs_named_query_id",
                False,
                ("named_query_id",),
            )
        }
    ),
    "named_query_artifacts": frozenset(
        {
            (
                "ix_named_query_artifacts_named_query_id",
                False,
                ("named_query_id",),
            ),
            ("ix_named_query_artifacts_target_id", False, ("target_id",)),
        }
    ),
    "named_query_refresh_policies": frozenset(),
}


class LegacyNamedQuerySchemaError(RuntimeError):
    """The pre-existing 0210 objects are not safe to accept as this revision."""


def _current_schema(bind: object) -> str:
    result = bind.execute(sa.text("SELECT current_schema()"))
    schema = result.scalar_one()
    if not isinstance(schema, str) or not schema:
        raise LegacyNamedQuerySchemaError(
            "Bug-9187: could not resolve the current tenant schema; "
            "refusing to create or accept the 0210 Named Query tables"
        )
    return schema


def _type_signature(column_type: sa.types.TypeEngine) -> str:
    if isinstance(column_type, UUID):
        return "uuid"
    if isinstance(column_type, JSONB):
        return "jsonb"
    if isinstance(column_type, sa.BigInteger):
        return "bigint"
    if isinstance(column_type, sa.Integer):
        return "integer"
    if isinstance(column_type, sa.Boolean):
        return "boolean"
    if isinstance(column_type, sa.DateTime):
        return "timestamptz" if column_type.timezone else "timestamp"
    if isinstance(column_type, sa.Text):
        collation = getattr(column_type, "collation", None)
        return "text" if collation is None else f"text collate {collation}"
    if isinstance(column_type, sa.String):
        signature = f"varchar({column_type.length})"
        collation = getattr(column_type, "collation", None)
        return signature if collation is None else f"{signature} collate {collation}"
    return str(column_type).lower()


def _strip_outer_parentheses(value: str) -> str:
    while value.startswith("(") and value.endswith(")"):
        depth = 0
        encloses_all = True
        for index, char in enumerate(value):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(value) - 1:
                    encloses_all = False
                    break
        if not encloses_all or depth != 0:
            break
        value = value[1:-1].strip()
    return value


def _default_signature(value: object) -> str | None:
    if value is None:
        return None
    normal = str(value).strip().lower()
    normal = re.sub(
        r"::(?:character varying|varchar|text|boolean|uuid)", "", normal
    )
    normal = _strip_outer_parentheses(normal)
    if len(normal) >= 2 and normal[0] == normal[-1] == "'":
        normal = normal[1:-1].replace("''", "'")
    return normal


def _index_term(value: object) -> str:
    normal = str(value).strip().lower().replace('"', "")
    normal = re.sub(r"::(?:character varying|varchar|text)", "", normal)
    normal = re.sub(r"\s+", "", normal)
    normal = _strip_outer_parentheses(normal)
    lower_match = re.fullmatch(r"lower\(\(*([a-z0-9_.]+)\)*\)", normal)
    if lower_match:
        return f"lower({lower_match.group(1).rsplit('.', 1)[-1]})"
    return normal.rsplit(".", 1)[-1]


def _option_is_set(value: object) -> bool:
    return value not in (None, False, "", (), [], {})


def _index_signature(index: dict[str, object]) -> tuple[object, ...]:
    columns = list(index.get("column_names") or ())
    expressions = list(index.get("expressions") or columns)
    terms = []
    for position in range(max(len(columns), len(expressions))):
        column = columns[position] if position < len(columns) else None
        expression = expressions[position] if position < len(expressions) else column
        terms.append(_index_term(column if column is not None else expression))

    dialect_options = index.get("dialect_options") or {}
    known_options = {
        "postgresql_include",
        "postgresql_nulls_not_distinct",
        "postgresql_where",
    }
    extras = (
        tuple(index.get("include_columns") or ()),
        tuple(
            sorted(
                (str(term), tuple(ordering))
                for term, ordering in (index.get("column_sorting") or {}).items()
            )
        ),
        str(dialect_options.get("postgresql_where") or ""),
        tuple(dialect_options.get("postgresql_include") or ()),
        bool(dialect_options.get("postgresql_nulls_not_distinct", False)),
        tuple(
            sorted(
                (str(key), repr(value))
                for key, value in dialect_options.items()
                if key not in known_options and _option_is_set(value)
            )
        ),
    )
    return (index.get("name"), bool(index.get("unique")), tuple(terms), extras)


def _check_signature(check: dict[str, object]) -> tuple[object, ...]:
    sql = str(check.get("sqltext") or "").strip().lower().replace('"', "")
    sql = re.sub(
        r"::(?:character varying\[\]|text\[\]|character varying|varchar|text)",
        "",
        sql,
    )
    sql = re.sub(r"\s+", " ", _strip_outer_parentheses(sql)).strip()
    sql = re.sub(r"\(\s*status\s*\)", "status", sql)
    in_match = re.fullmatch(r"status\s+in\s*\((.*)\)", sql)
    any_match = re.fullmatch(
        r"status\s*=\s*any\s*\(\s*\(*\s*array\[(.*)\]\s*\)*\s*\)",
        sql,
    )
    match = in_match or any_match
    values = None
    if match:
        body = match.group(1)
        remainder = re.sub(r"'(?:''|[^'])*'", "", body)
        if re.fullmatch(r"\s*(?:,\s*)*", remainder):
            literals = re.findall(r"'((?:''|[^'])*)'", body)
            if len(literals) == len(set(literals)):
                values = frozenset(literals)
    return (check.get("name"), values, sql if values is None else None)


def _schema_mismatches(inspector: object, schema: str) -> list[str]:
    mismatches: list[str] = []
    for table in sorted(_TABLES):
        reflected_columns = inspector.get_columns(table, schema=schema)
        columns = {
            column["name"]: (
                _type_signature(column["type"]),
                bool(column["nullable"]),
                _default_signature(column.get("default")),
            )
            for column in reflected_columns
        }
        generated_columns = any(
            column.get("computed") or column.get("identity")
            for column in reflected_columns
        )
        column_order = tuple(column["name"] for column in reflected_columns)
        if (
            generated_columns
            or column_order != tuple(_COLUMNS[table])
            or columns != _COLUMNS[table]
        ):
            mismatches.append(f"{table}.columns")

        primary_key = tuple(
            inspector.get_pk_constraint(table, schema=schema).get(
                "constrained_columns"
            )
            or ()
        )
        if primary_key != _PRIMARY_KEYS[table]:
            mismatches.append(f"{table}.primary_key")

        unique_constraints = []
        invalid_unique_options = False
        for constraint in inspector.get_unique_constraints(table, schema=schema):
            unique_constraints.append(tuple(constraint.get("column_names") or ()))
            dialect_options = constraint.get("dialect_options") or {}
            if any(_option_is_set(value) for value in dialect_options.values()):
                invalid_unique_options = True
        if (
            invalid_unique_options
            or len(unique_constraints) != len(_UNIQUE_CONSTRAINTS[table])
            or frozenset(unique_constraints) != _UNIQUE_CONSTRAINTS[table]
        ):
            mismatches.append(f"{table}.unique_constraints")

        foreign_keys = []
        invalid_fk_schema = False
        for foreign_key in inspector.get_foreign_keys(table, schema=schema):
            referred_schema = foreign_key.get("referred_schema")
            if referred_schema not in (None, schema):
                invalid_fk_schema = True
            options = foreign_key.get("options") or {}
            ondelete = options.get("ondelete")
            if any(
                key != "ondelete" and _option_is_set(value)
                for key, value in options.items()
            ):
                invalid_fk_schema = True
            foreign_keys.append(
                (
                    tuple(foreign_key.get("constrained_columns") or ()),
                    foreign_key.get("referred_table"),
                    tuple(foreign_key.get("referred_columns") or ()),
                    str(ondelete).upper() if ondelete else None,
                )
            )
        if (
            invalid_fk_schema
            or len(foreign_keys) != len(_FOREIGN_KEYS[table])
            or frozenset(foreign_keys) != _FOREIGN_KEYS[table]
        ):
            mismatches.append(f"{table}.foreign_keys")

        indexes = frozenset(
            _index_signature(index)
            for index in inspector.get_indexes(table, schema=schema)
            if not index.get("duplicates_constraint")
        )
        expected_indexes = frozenset(
            (name, unique, terms, ((), (), "", (), False, ()))
            for name, unique, terms in _INDEXES[table]
        )
        if len(indexes) != len(expected_indexes) or indexes != expected_indexes:
            mismatches.append(f"{table}.indexes")

        checks = []
        invalid_check_options = False
        for check in inspector.get_check_constraints(table, schema=schema):
            checks.append(_check_signature(check))
            dialect_options = check.get("dialect_options") or {}
            if any(_option_is_set(value) for value in dialect_options.values()):
                invalid_check_options = True
        expected_checks = (
            frozenset(
                {
                    (
                        _NAMED_QUERIES_STATUS_CHECK,
                        frozenset(_NAMED_QUERIES_STATUSES),
                        None,
                    )
                }
            )
            if table == "named_query_artifacts"
            else frozenset()
        )
        if (
            invalid_check_options
            or len(checks) != len(expected_checks)
            or frozenset(checks) != expected_checks
        ):
            mismatches.append(f"{table}.check_constraints")
    return mismatches


def _accept_complete_legacy_schema() -> bool:
    get_context = getattr(op, "get_context", None)
    if callable(get_context) and getattr(get_context(), "as_sql", False):
        # Offline SQL generation cannot reflect a tenant. Emit the normal 0210
        # CREATE statements; applying them to a legacy schema still fails
        # closed rather than manufacturing an unverified stamp.
        return False
    bind = op.get_bind()
    schema = _current_schema(bind)
    inspector = sa.inspect(bind)
    present = _TABLES.intersection(inspector.get_table_names(schema=schema))
    if not present:
        return False
    if present != _TABLES:
        missing = ", ".join(sorted(_TABLES - present))
        found = ", ".join(sorted(present))
        raise LegacyNamedQuerySchemaError(
            f"Bug-9187: tenant schema {schema!r} contains a partial 0210 Named "
            f"Query schema (present: {found}; missing: {missing}); refusing "
            "to create tables or stamp revision 0210"
        )

    mismatches = _schema_mismatches(inspector, schema)
    if mismatches:
        raise LegacyNamedQuerySchemaError(
            f"Bug-9187: tenant schema {schema!r} contains all four 0210 Named "
            "Query tables but does not match the approved exact legacy schema "
            f"({', '.join(mismatches)}); refusing to stamp revision 0210"
        )
    return True


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    schema = _current_schema(bind)
    inspector = sa.inspect(bind)
    return table in set(inspector.get_table_names(schema=schema))


def upgrade() -> None:
    if _accept_complete_legacy_schema():
        return

    op.create_table(
        "named_queries",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("model_id", UUID(as_uuid=True), sa.ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255)),
        sa.Column("description", sa.Text()),
        sa.Column("display_folder", sa.String(255)),
        sa.Column("definition_sql", sa.Text(), nullable=False),
        sa.Column("output_columns", JSONB()),
        sa.Column("shape", sa.String(16), nullable=False, server_default="projection"),
        sa.Column("row_cap", sa.Integer()),
        sa.Column("column_cap", sa.Integer()),
        sa.Column("certification_status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("created_by", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index(
        "uq_named_queries_model_lower_name",
        "named_queries",
        ["model_id", sa.text("lower(name)")],
        unique=True,
    )

    op.create_table(
        "named_query_refresh_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("named_query_id", UUID(as_uuid=True), sa.ForeignKey("named_queries.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("refresh_mode", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("rows_written", sa.BigInteger()),
        sa.Column("bytes_processed", sa.BigInteger()),
        sa.Column("error_message", sa.Text()),
        sa.Column("triggered_by", sa.String(32), nullable=False, server_default="scheduler"),
    )

    # named_query_artifacts references named_query_refresh_runs
    # (active_refresh_run_id, SET NULL), so the runs table must exist first.
    op.create_table(
        "named_query_artifacts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("named_query_id", UUID(as_uuid=True), sa.ForeignKey("named_queries.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("target_id", UUID(as_uuid=True), sa.ForeignKey("data_targets.id"), nullable=False, index=True),
        sa.Column("physical_table_name", sa.String(512), nullable=False),
        sa.Column("target_schema", sa.String(255)),
        sa.Column("row_manifest", JSONB()),
        sa.Column("row_count", sa.BigInteger()),
        sa.Column("status", sa.String(32), nullable=False, server_default="stale"),
        sa.Column("failure_reason", sa.Text()),
        sa.Column("active_refresh_run_id", UUID(as_uuid=True), sa.ForeignKey("named_query_refresh_runs.id", ondelete="SET NULL")),
        sa.Column("last_refresh_at", sa.DateTime(timezone=True)),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.Column("built_for_version_id", UUID(as_uuid=True)),
        sa.Column("built_for_epoch", sa.Integer()),
    )
    op.create_check_constraint(
        _NAMED_QUERIES_STATUS_CHECK,
        "named_query_artifacts",
        "status IN ('fresh', 'stale', 'invalidating', 'failed')",
    )

    op.create_table(
        "named_query_refresh_policies",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("named_query_id", UUID(as_uuid=True), sa.ForeignKey("named_queries.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("cron_expression", sa.String(128)),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def downgrade() -> None:
    # named_query_artifacts.active_refresh_run_id references
    # named_query_refresh_runs, so drop the artifact table first.
    if _table_exists("named_query_artifacts"):
        op.drop_table("named_query_artifacts")
    if _table_exists("named_query_refresh_policies"):
        op.drop_table("named_query_refresh_policies")
    if _table_exists("named_query_refresh_runs"):
        op.drop_table("named_query_refresh_runs")
    if _table_exists("named_queries"):
        op.drop_table("named_queries")
