"""
User-defined attribute CRUD and validation routes.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import sqlglot
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlglot import exp

from shared.db.models import (
    ModelColumn,
    ModelTable,
    UserDefinedAttribute,
    UserDefinedAttributeColumnRef,
)
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    UserDefinedAttributeCreate,
    UserDefinedAttributeFunctionOption,
    UserDefinedAttributeLiveValidationResult,
    UserDefinedAttributeResponse,
    UserDefinedAttributeUpdate,
    UserDefinedAttributeValidateRequest,
    UserDefinedAttributeValidateResponse,
)
from src.api._model_lock import acquire_model_definition_lock
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role
from src.api._scope import ensure_model_in_project
from src.api._uda_refs import assert_uda_deletable

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/tables/{table_id}/user-defined-attributes",
    tags=["user-defined-attributes"],
)

ALLOWED_OUTPUT_TYPES = {"varchar", "integer", "numeric", "date"}
FUNCTION_CATALOG: list[UserDefinedAttributeFunctionOption] = [
    UserDefinedAttributeFunctionOption(
        name="CONCAT",
        signature="CONCAT(value1, value2, ...)",
        template="CONCAT(col_a, col_b)",
        description="Concatenate values into a single string.",
    ),
    UserDefinedAttributeFunctionOption(
        name="UPPER",
        signature="UPPER(value)",
        template="UPPER(col_a)",
        description="Convert text to uppercase.",
    ),
    UserDefinedAttributeFunctionOption(
        name="LOWER",
        signature="LOWER(value)",
        template="LOWER(col_a)",
        description="Convert text to lowercase.",
    ),
    UserDefinedAttributeFunctionOption(
        name="TRIM",
        signature="TRIM(value)",
        template="TRIM(col_a)",
        description="Trim spaces from both sides of text.",
    ),
    UserDefinedAttributeFunctionOption(
        name="LTRIM",
        signature="LTRIM(value)",
        template="LTRIM(col_a)",
        description="Trim spaces from the left side of text.",
    ),
    UserDefinedAttributeFunctionOption(
        name="RTRIM",
        signature="RTRIM(value)",
        template="RTRIM(col_a)",
        description="Trim spaces from the right side of text.",
    ),
    UserDefinedAttributeFunctionOption(
        name="LEFT",
        signature="LEFT(value, count)",
        template="LEFT(col_a, 3)",
        description="Take leftmost characters from text.",
    ),
    UserDefinedAttributeFunctionOption(
        name="RIGHT",
        signature="RIGHT(value, count)",
        template="RIGHT(col_a, 3)",
        description="Take rightmost characters from text.",
    ),
    UserDefinedAttributeFunctionOption(
        name="SUBSTRING",
        signature="SUBSTRING(value, start, length)",
        template="SUBSTRING(col_a, 1, 3)",
        description="Extract substring from text.",
    ),
    UserDefinedAttributeFunctionOption(
        name="LENGTH",
        signature="LENGTH(value)",
        template="LENGTH(col_a)",
        description="Return text length.",
    ),
    UserDefinedAttributeFunctionOption(
        name="REPLACE",
        signature="REPLACE(value, from, to)",
        template="REPLACE(col_a, 'old', 'new')",
        description="Replace substring occurrences.",
    ),
    UserDefinedAttributeFunctionOption(
        name="SPLIT_PART",
        signature="SPLIT_PART(value, delimiter, index)",
        template="SPLIT_PART(col_a, '-', 1)",
        description="Return one token from a split string.",
    ),
    UserDefinedAttributeFunctionOption(
        name="LPAD",
        signature="LPAD(value, length, pad)",
        template="LPAD(col_a, 8, '0')",
        description="Left-pad text to target length.",
    ),
    UserDefinedAttributeFunctionOption(
        name="RPAD",
        signature="RPAD(value, length, pad)",
        template="RPAD(col_a, 8, '0')",
        description="Right-pad text to target length.",
    ),
    UserDefinedAttributeFunctionOption(
        name="CAST",
        signature="CAST(value AS type)",
        template="CAST(col_a AS INTEGER)",
        description="Cast value to a target type.",
    ),
    UserDefinedAttributeFunctionOption(
        name="COALESCE",
        signature="COALESCE(value1, value2, ...)",
        template="COALESCE(col_a, 'unknown')",
        description="Return first non-null value.",
    ),
    UserDefinedAttributeFunctionOption(
        name="NULLIF",
        signature="NULLIF(value1, value2)",
        template="NULLIF(col_a, '')",
        description="Return null when values are equal.",
    ),
]
ALLOWED_FUNCTIONS = {fn.name for fn in FUNCTION_CATALOG}
FUNCTION_ALIASES = {
    "SUBSTR": "SUBSTRING",
}
AGGREGATE_FUNCTIONS = {"SUM", "COUNT", "AVG", "MIN", "MAX", "COUNT_DISTINCT", "COUNTDISTINCT"}


@dataclass
class _ValidationResult:
    parse_valid: bool
    columns_resolved: bool
    referenced_columns: list[str]
    referenced_column_ids: list[UUID]
    unsupported_functions: list[str]
    validation_error: str | None = None


def _normalize_output_type(raw_type: str) -> str:
    normalized = (raw_type or "").strip().lower()
    if normalized not in ALLOWED_OUTPUT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="output_data_type must be one of: varchar, integer, numeric, date",
        )
    return normalized


def _func_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return (node.name or "").upper()
    if isinstance(node, exp.SplitPart):
        return "SPLIT_PART"
    try:
        return (node.sql_name() or "").upper()
    except Exception:
        return (getattr(node, "key", "") or "").upper()


def _parse_expression(expression: str) -> exp.Expression:
    try:
        wrapper = sqlglot.parse_one(
            f"SELECT {expression} AS __expr",
            read="postgres",
            error_level=sqlglot.ErrorLevel.RAISE,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Expression parse error: {exc}",
        )

    if not isinstance(wrapper, exp.Select) or not wrapper.expressions:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Expression parse error: empty expression",
        )
    first = wrapper.expressions[0]
    return first.this if isinstance(first, exp.Alias) else first


def _allowed_qualifiers(table: ModelTable) -> set[str]:
    out: set[str] = set()
    if table.alias:
        out.add(table.alias.strip('"').lower())
    if table.display_name:
        out.add(table.display_name.strip('"').lower())
    parts = [p.strip('"') for p in table.physical_name.split(".") if p.strip('"')]
    out.update(p.lower() for p in parts)
    if parts:
        out.add(parts[-1].lower())
    return out


def _validate_ast(
    expr_ast: exp.Expression,
    *,
    table: ModelTable,
    columns_by_name: dict[str, ModelColumn],
    skip_function_allowlist: bool = False,
) -> _ValidationResult:
    # F-016-06: generated UDAs (date-hierarchy templates) emit EXTRACT/CASE —
    # functions deliberately outside the user editor's catalogue. When a modeller
    # renames or re-describes a generated UDA without touching its system-authored
    # expression, the function allowlist is skipped so the generator's own output
    # is not rejected as "Unsupported function(s)". The structural guards below
    # (no subquery/SELECT/LIKE/window/aggregate, same-table columns only) still run.
    # Disallow subqueries/SELECT-in-expression
    if expr_ast.find(exp.Subquery) is not None or expr_ast.find(exp.Select) is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Subqueries are not allowed in user-defined attributes",
        )

    # Disallow LIKE/ILIKE
    if expr_ast.find(exp.Like) is not None or expr_ast.find(exp.ILike) is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="LIKE / ILIKE are not supported in user-defined attributes",
        )

    # Disallow window functions
    if expr_ast.find(exp.Window) is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Window functions are not allowed in user-defined attributes",
        )

    # Disallow aggregate functions
    for node in expr_ast.find_all(exp.AggFunc):
        name = _func_name(node) if isinstance(node, exp.Func) else getattr(node, "key", "AGG")
        if str(name).upper() in AGGREGATE_FUNCTIONS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Aggregate functions (SUM, COUNT, AVG, MIN, MAX, ...) are not allowed in user-defined attributes",
            )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Aggregate functions are not allowed in user-defined attributes",
        )

    # Function allowlist enforcement
    if not skip_function_allowlist:
        unsupported_functions: list[str] = []
        for node in expr_ast.find_all(exp.Func):
            fname = _func_name(node)
            normalized = FUNCTION_ALIASES.get(fname, fname)
            if normalized not in ALLOWED_FUNCTIONS:
                unsupported_functions.append(fname)
        if unsupported_functions:
            unsupported = sorted(set(unsupported_functions))
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unsupported function(s): {', '.join(unsupported)}",
            )

    # Resolve column refs against same table
    allowed_quals = _allowed_qualifiers(table)
    referenced_columns: list[str] = []
    referenced_column_ids: list[UUID] = []

    for col in expr_ast.find_all(exp.Column):
        col_name = (col.name or "").strip('"')
        if not col_name:
            continue

        qualifier = (col.table or "").strip('"').lower() if col.table else ""
        if qualifier and qualifier not in allowed_quals:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Cross-table reference is not allowed: {qualifier}.{col_name}",
            )

        model_col = columns_by_name.get(col_name.lower())
        if model_col is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Column '{col_name}' not found in table '{table.alias or table.physical_name}'",
            )

        if col_name not in referenced_columns:
            referenced_columns.append(col_name)
            referenced_column_ids.append(model_col.id)

    return _ValidationResult(
        parse_valid=True,
        columns_resolved=True,
        referenced_columns=referenced_columns,
        referenced_column_ids=referenced_column_ids,
        unsupported_functions=[],
    )


async def _get_table_or_404(
    db,
    *,
    project_id: UUID,
    model_id: UUID,
    table_id: UUID,
) -> ModelTable:
    await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
    table = await db.get(ModelTable, table_id)
    if table is None or table.model_id != model_id:
        raise HTTPException(status_code=404, detail="Model table not found")
    return table


async def _table_columns_by_name(db, table_id: UUID) -> dict[str, ModelColumn]:
    result = await db.execute(
        select(ModelColumn).where(ModelColumn.model_table_id == table_id)
    )
    return {c.column_name.lower(): c for c in result.scalars().all()}


async def _build_response(db, attr: UserDefinedAttribute) -> UserDefinedAttributeResponse:
    result = await db.execute(
        select(ModelColumn.column_name)
        .join(
            UserDefinedAttributeColumnRef,
            UserDefinedAttributeColumnRef.column_id == ModelColumn.id,
        )
        .where(UserDefinedAttributeColumnRef.attribute_id == attr.id)
        .order_by(ModelColumn.column_name)
    )
    referenced = [row[0] for row in result.fetchall()]
    return UserDefinedAttributeResponse(
        id=attr.id,
        table_id=attr.table_id,
        model_id=attr.model_id,
        name=attr.name,
        expression=attr.expression,
        output_data_type=attr.output_data_type,
        description=attr.description,
        validated=attr.validated,
        validation_error=attr.validation_error,
        is_generated=attr.is_generated,
        referenced_columns=referenced,
        created_at=attr.created_at,
        updated_at=attr.updated_at,
    )


async def _replace_refs(db, attribute_id: UUID, column_ids: list[UUID]) -> None:
    await db.execute(
        delete(UserDefinedAttributeColumnRef).where(
            UserDefinedAttributeColumnRef.attribute_id == attribute_id
        )
    )
    for col_id in column_ids:
        db.add(
            UserDefinedAttributeColumnRef(
                attribute_id=attribute_id,
                column_id=col_id,
            )
        )


async def _validate_expression_for_table(
    *,
    db,
    table: ModelTable,
    expression: str,
    output_data_type: str,
    skip_function_allowlist: bool = False,
) -> _ValidationResult:
    _normalize_output_type(output_data_type)
    expr_ast = _parse_expression(expression)
    columns_by_name = await _table_columns_by_name(db, table.id)
    return _validate_ast(
        expr_ast,
        table=table,
        columns_by_name=columns_by_name,
        skip_function_allowlist=skip_function_allowlist,
    )


@router.get(
    "/function-catalog",
    response_model=list[UserDefinedAttributeFunctionOption],
)
async def list_user_defined_attribute_function_catalog(
    project_id: UUID,
    model_id: UUID,
    table_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> list[UserDefinedAttributeFunctionOption]:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_table_or_404(
            db,
            project_id=project_id,
            model_id=model_id,
            table_id=table_id,
        )
        return FUNCTION_CATALOG


@router.post(
    "",
    response_model=UserDefinedAttributeResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_user_defined_attribute(
    project_id: UUID,
    model_id: UUID,
    table_id: UUID,
    body: UserDefinedAttributeCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> UserDefinedAttributeResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        # Read-modify-write: ModelTable is snapshot-owned and feeds the expression
        # validation, so it must be read UNDER the lock (consistent with
        # table_attributes.py). _get_table_or_404 also enforces ownership.
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        table = await _get_table_or_404(
            db,
            project_id=project_id,
            model_id=model_id,
            table_id=table_id,
        )
        validation = await _validate_expression_for_table(
            db=db,
            table=table,
            expression=body.expression,
            output_data_type=body.output_data_type,
        )

        attr = UserDefinedAttribute(
            model_id=model_id,
            table_id=table_id,
            name=body.name,
            expression=body.expression,
            output_data_type=_normalize_output_type(body.output_data_type),
            description=body.description,
            validated=True,
            validation_error=None,
            # User-editor UDAs are never generator output.
            is_generated=False,
        )
        db.add(attr)
        try:
            await db.flush()
            await _replace_refs(db, attr.id, validation.referenced_column_ids)
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"User-defined attribute '{body.name}' already exists for this table",
            )
        await db.refresh(attr)
        return await _build_response(db, attr)


@router.get("", response_model=list[UserDefinedAttributeResponse])
async def list_user_defined_attributes(
    project_id: UUID,
    model_id: UUID,
    table_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> list[UserDefinedAttributeResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_table_or_404(
            db,
            project_id=project_id,
            model_id=model_id,
            table_id=table_id,
        )
        result = await db.execute(
            select(UserDefinedAttribute)
            .where(
                UserDefinedAttribute.model_id == model_id,
                UserDefinedAttribute.table_id == table_id,
            )
            .order_by(UserDefinedAttribute.name)
        )
        attrs = result.scalars().all()
        return [await _build_response(db, a) for a in attrs]


@router.get("/{attr_id}", response_model=UserDefinedAttributeResponse)
async def get_user_defined_attribute(
    project_id: UUID,
    model_id: UUID,
    table_id: UUID,
    attr_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> UserDefinedAttributeResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_table_or_404(
            db,
            project_id=project_id,
            model_id=model_id,
            table_id=table_id,
        )
        attr = await db.get(UserDefinedAttribute, attr_id)
        if attr is None or attr.model_id != model_id or attr.table_id != table_id:
            raise HTTPException(status_code=404, detail="User-defined attribute not found")
        return await _build_response(db, attr)


@router.put(
    "/{attr_id}",
    response_model=UserDefinedAttributeResponse,
    dependencies=[require_role("modeler")],
)
async def update_user_defined_attribute(
    project_id: UUID,
    model_id: UUID,
    table_id: UUID,
    attr_id: UUID,
    body: UserDefinedAttributeUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> UserDefinedAttributeResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        # Read-modify-write: read ModelTable + the UDA under the lock.
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        table = await _get_table_or_404(
            db,
            project_id=project_id,
            model_id=model_id,
            table_id=table_id,
        )
        attr = await db.get(UserDefinedAttribute, attr_id)
        if attr is None or attr.model_id != model_id or attr.table_id != table_id:
            raise HTTPException(status_code=404, detail="User-defined attribute not found")

        updates = body.model_dump(exclude_unset=True)
        expression = updates.get("expression", attr.expression)
        output_data_type = updates.get("output_data_type", attr.output_data_type)
        # F-016-06: a generated UDA whose system-authored expression is unchanged
        # (rename / description / output-type edit) must not be rejected by the
        # user-editor function allowlist. The moment a modeller replaces the
        # expression itself, full validation applies again.
        skip_allowlist = bool(attr.is_generated) and expression == attr.expression
        validation = await _validate_expression_for_table(
            db=db,
            table=table,
            expression=expression,
            output_data_type=output_data_type,
            skip_function_allowlist=skip_allowlist,
        )

        if "name" in updates:
            attr.name = updates["name"]
        if "expression" in updates:
            attr.expression = updates["expression"]
        if "output_data_type" in updates:
            attr.output_data_type = _normalize_output_type(updates["output_data_type"])
        if "description" in updates:
            attr.description = updates["description"]
        attr.validated = True
        attr.validation_error = None

        try:
            await _replace_refs(db, attr.id, validation.referenced_column_ids)
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"User-defined attribute '{attr.name}' already exists for this table",
            )
        await db.refresh(attr)
        return await _build_response(db, attr)


@router.delete(
    "/{attr_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("modeler")],
)
async def delete_user_defined_attribute(
    project_id: UUID,
    model_id: UUID,
    table_id: UUID,
    attr_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        # Read-modify-write: read ModelTable + the UDA under the lock.
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        await _get_table_or_404(
            db,
            project_id=project_id,
            model_id=model_id,
            table_id=table_id,
        )
        attr = await db.get(UserDefinedAttribute, attr_id)
        if attr is None or attr.model_id != model_id or attr.table_id != table_id:
            raise HTTPException(status_code=404, detail="User-defined attribute not found")

        # Bug-1505 / F-016-08: shared guard rejects deletion when the UDA is
        # referenced by a dimension, measure, hierarchy-level key, or
        # hierarchy-level attribute. Same check as the table-attributes delete
        # path so neither endpoint can orphan a hierarchy level.
        await assert_uda_deletable(db, model_id=model_id, attribute_id=attr_id)

        await db.delete(attr)
        await db.commit()


async def _probe_uda_expression_live(
    db,
    *,
    table: ModelTable,
    expression: str,
    project_id: UUID,
    tenant_slug: str | None,
) -> UserDefinedAttributeLiveValidationResult:
    """F-016-03: actually execute the UDA expression against the source.

    Static AST validation cannot catch type errors (e.g. arithmetic on a text
    column) or function-argument mismatches — those surface only when the
    source database evaluates the expression. This runs a bounded
    ``SELECT (<expr>) FROM <table> LIMIT 1`` through the audited source
    boundary and reports the real outcome. ``executed`` is True only when the
    probe ran; ``success`` is NEVER reported True without an execution.
    """
    from shared.db.models import DataSource
    from shared.schemas.connection_type import normalize_connection_type
    from shared.source_executor import execute_source_sql
    from src.api._scope import resolve_source_connection
    from src.api._table_qualify import qualify_physical_name

    source_id = getattr(table, "source_id", None)
    source = await db.get(DataSource, source_id) if source_id is not None else None
    if source is None:
        return UserDefinedAttributeLiveValidationResult(
            executed=False, success=False,
            error="Source not found for this table.", sample_value=None,
        )
    try:
        connection = await resolve_source_connection(
            db, source, expected_project_id=project_id
        )
    except HTTPException as exc:
        return UserDefinedAttributeLiveValidationResult(
            executed=False, success=False,
            error=str(exc.detail), sample_value=None,
        )

    connector = normalize_connection_type((connection.connection_type or "").lower())
    # Bug-8294: include sqlserver (was missing → fell back to postgres and
    # emitted invalid T-SQL). sqlglot's SQL Server dialect token is "tsql".
    dialect_map = {
        "postgresql": "postgres",
        "redshift": "redshift",
        "bigquery": "bigquery",
        "hadoop_spark": "spark",
        "snowflake": "snowflake",
        "sqlserver": "tsql",
    }
    dialect = dialect_map.get(connector, "postgres")

    qualified = qualify_physical_name(table.physical_name, connection, source)

    # Bug-8294 [SQL rule 1]: author the WHOLE probe as canonical PostgreSQL and
    # transpile it via sqlglot — never hand-write dialect-specific clauses. This
    # converts the row-limit correctly per dialect (LIMIT 1 → TOP 1 on SQL
    # Server) and quotes identifiers per dialect, so the probe is valid on every
    # connector instead of breaking on SQL Server.
    try:
        # Bug-8294 follow-up (Fable re-gate): quote each dotted part of the
        # qualified table so a hyphenated identifier (e.g. a BigQuery GCP
        # project id like ``tessallite-io``) parses as a quoted multi-part
        # identifier instead of raising a sqlglot ParseError on the hyphen.
        qualified_quoted = ".".join(
            '"' + p.replace('"', '""') + '"' for p in qualified.split(".")
        )
        canonical_probe = (
            f'SELECT ({expression}) AS __uda_probe '
            f'FROM {qualified_quoted} LIMIT 1'
        )
        probe_sql = sqlglot.parse_one(canonical_probe, read="postgres").sql(
            dialect=dialect
        )
    except Exception as exc:
        return UserDefinedAttributeLiveValidationResult(
            executed=False, success=False,
            error=f"Could not translate probe to source dialect: {exc}",
            sample_value=None,
        )

    try:
        rows, _cols = await execute_source_sql(
            connection, probe_sql,
            tenant_session=db,
            purpose="uda_validation_probe",
            tenant_slug=tenant_slug,
        )
    except Exception as exc:
        return UserDefinedAttributeLiveValidationResult(
            executed=True, success=False,
            error=f"{type(exc).__name__}: {exc}", sample_value=None,
        )
    sample = None
    if rows:
        val = rows[0].get("__uda_probe")
        sample = None if val is None else str(val)
    return UserDefinedAttributeLiveValidationResult(
        executed=True, success=True, error=None, sample_value=sample,
    )


@router.post(
    "/validate",
    response_model=UserDefinedAttributeValidateResponse,
    dependencies=[require_role("modeler")],
)
async def validate_user_defined_attribute_expression(
    project_id: UUID,
    model_id: UUID,
    table_id: UUID,
    body: UserDefinedAttributeValidateRequest,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> UserDefinedAttributeValidateResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        table = await _get_table_or_404(
            db,
            project_id=project_id,
            model_id=model_id,
            table_id=table_id,
        )
        try:
            validation = await _validate_expression_for_table(
                db=db,
                table=table,
                expression=body.expression,
                output_data_type=body.output_data_type,
            )
        except HTTPException as exc:
            # Static validation failed — do NOT claim live success.
            return UserDefinedAttributeValidateResponse(
                parse_valid=False,
                columns_resolved=False,
                referenced_columns=[],
                unsupported_functions=[],
                live_validation=UserDefinedAttributeLiveValidationResult(
                    executed=False,
                    success=False,
                    error=str(exc.detail),
                    sample_value=None,
                ),
            )
        # Static validation passed — now actually execute against the source.
        # F-016-03: never report live success without an execution.
        live = await _probe_uda_expression_live(
            db,
            table=table,
            expression=body.expression,
            project_id=project_id,
            tenant_slug=current_user.tenant_id,
        )
        return UserDefinedAttributeValidateResponse(
            parse_valid=validation.parse_valid,
            columns_resolved=validation.columns_resolved,
            referenced_columns=validation.referenced_columns,
            unsupported_functions=validation.unsupported_functions,
            live_validation=live,
        )
