"""Row-security rule CRUD + simulate-as-user preview (Phase 5.1.D).

Two rule shapes are stored in a single table (see
``shared/db/models.py::RowSecurityRule``); the shape invariant is
enforced by the pydantic validator + a DB CHECK constraint.

The simulate endpoint shares its compilation path with the query router
via :mod:`shared.security` — a single source of truth means the preview
is what the user will actually get at query time.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import re

from shared.audit.logger import audit
from shared.db.models import (
    DataSource,
    Dimension,
    Model,
    ModelColumn,
    ModelTable,
    ProjectConnection,
    RowSecurityRule,
)
from shared.db.session import get_tenant_db
from shared.schemas.connection_type import normalize_connection_type
from shared.schemas.pydantic_models import (
    RowSecurityRuleCreate,
    RowSecurityRuleResponse,
    RowSecurityRuleUpdate,
    RowSecuritySimulateRequest,
    RowSecuritySimulateResponse,
)
from shared.security import (
    Principal,
    RowSecurityCompileError,
    compile_row_security,
)
from shared.security.predicate_compiler import _compile_dsl_expression
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/row-security",
    tags=["row-security"],
)


async def _get_scoped_model(db, project_id: UUID, model_id: UUID) -> Model:
    model = await db.get(Model, model_id)
    if model is None or model.project_id != project_id:
        raise HTTPException(status_code=404, detail="Model not found")
    return model


async def _get_scoped_rule(
    db, project_id: UUID, model_id: UUID, rule_id: UUID
) -> RowSecurityRule:
    await _get_scoped_model(db, project_id, model_id)
    rule = await db.get(RowSecurityRule, rule_id)
    if rule is None or rule.model_id != model_id:
        raise HTTPException(status_code=404, detail="Row-security rule not found")
    return rule


async def _validate_mapping_table_in_model(
    db, model_id: UUID, mapping_table_id: UUID
) -> None:
    """user_mapping rules must point at a table registered on the same model.

    This closes the door on a modeler smuggling in a table from another
    model (or another tenant schema) as a stealth data-exfil surface.
    """
    table = await db.get(ModelTable, mapping_table_id)
    if table is None or table.model_id != model_id:
        raise HTTPException(
            status_code=400,
            detail="mapping_table_id must reference a table on this model",
        )


async def _validate_mapping_columns(
    db, mapping_table_id: UUID | None,
    mapping_user_column: str | None,
    mapping_value_column: str | None,
) -> None:
    """Bug-5207: verify mapping_user_column and mapping_value_column exist
    on the mapping table. Without this, a modeler could reference a
    nonexistent column, producing a runtime SQL error on every matched query.
    """
    if mapping_table_id is None:
        return
    cols_to_check = []
    if mapping_user_column:
        cols_to_check.append(mapping_user_column)
    if mapping_value_column:
        cols_to_check.append(mapping_value_column)
    if not cols_to_check:
        return
    result = await db.execute(
        select(ModelColumn.column_name).where(
            ModelColumn.model_table_id == mapping_table_id,
            ModelColumn.column_name.in_(cols_to_check),
        )
    )
    found = set(result.scalars().all())
    missing = [c for c in cols_to_check if c not in found]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Mapping column(s) {', '.join(repr(c) for c in missing)} "
                f"not found on the mapping table."
            ),
        )


async def _resolve_model_connector(db, model_id: UUID) -> str:
    """Resolve the connector string for a model's source so the simulate
    preview compiles the predicate with the SAME quoting the runtime path
    uses (F-007-07).

    The router resolves the touched-source dialect and passes its connector
    into ``compile_row_security``; the simulate endpoint previously omitted
    the argument and defaulted to ``"postgresql"``, so a BigQuery-backed
    model previewed double-quoted identifiers while runtime used backticks.

    Falls back to ``"postgresql"`` only when the model has no source yet —
    the same default the compiler already uses — so preview still works for
    a freshly-created model.
    """
    source = (
        await db.execute(
            select(DataSource).where(DataSource.model_id == model_id).limit(1)
        )
    ).scalar_one_or_none()
    if source is None:
        return "postgresql"
    conn = await db.get(ProjectConnection, source.project_connection_id)
    if conn is None:
        return "postgresql"
    return normalize_connection_type((conn.connection_type or "").lower()) or "postgresql"


def _extract_predicate_paths(expr: str) -> list[str]:
    """Extract all dimension path strings from a DSL expression.

    The DSL functions ``dimension_equals('path', ...)`` and ``in('path', ...)``
    both take the path as a single-quoted first argument. This extracts all
    such paths so the API can verify they match the declared dimension_path.
    """
    paths: list[str] = []
    # Match dimension_equals('path', ...) and in('path', ...)
    for m in re.finditer(
        r"(?:dimension_equals|in)\s*\(\s*'([^']+)'", expr, re.IGNORECASE
    ):
        paths.append(m.group(1))
    return paths


async def _validate_dimension_path_exists(
    db, model_id: UUID, dimension_path: str
) -> None:
    """Bug-5206: verify the declared protected dimension exists in the model.

    The dimension_path's last segment is the column name used at query time.
    We check that a dimension with a matching name exists in this model.
    """
    dim_name = dimension_path.rsplit(".", 1)[-1]
    result = await db.execute(
        select(Dimension.id).where(
            Dimension.model_id == model_id,
            Dimension.name == dim_name,
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"dimension_path {dimension_path!r} does not match any "
                f"dimension in this model (looked for dimension named "
                f"{dim_name!r})."
            ),
        )


def _validate_predicate_matches_dimension(
    predicate_expression: str | None, dimension_path: str
) -> None:
    """Bug-5206: ensure the predicate's columns match the declared
    dimension_path. A rule that declares one protected dimension but
    filters on a different column would silently mis-filter at query time.
    """
    if not predicate_expression:
        return
    paths = _extract_predicate_paths(predicate_expression)
    if not paths:
        # The expression uses only boolean combinators (and/or/not) around
        # sub-expressions that do reference paths. Those sub-expressions are
        # validated recursively, but at the top level there may not be a
        # direct path. Skip the check in this case; the expression has
        # already been compiled successfully.
        return
    expected_col = dimension_path.rsplit(".", 1)[-1]
    for path in paths:
        actual_col = path.rsplit(".", 1)[-1]
        if actual_col != expected_col:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Predicate references column {actual_col!r} (from path "
                    f"{path!r}) but the rule's dimension_path resolves to "
                    f"{expected_col!r}. The predicate must filter on the "
                    f"declared protected dimension."
                ),
            )


def _validate_predicate_compiles(predicate_expression: str | None) -> None:
    """F-007-04: compile the DSL at save time so a malformed expression is
    rejected with a 422 here, not a generic 500 to every matched caller at
    query time.

    The compiler is the single source of truth for the DSL grammar; reusing
    it guarantees the save-time check and the runtime path agree (no rule
    that saves can fail to compile later). The check is dialect-agnostic for
    grammar purposes — connector quoting cannot change whether the
    expression parses — so the default connector is used.
    """
    if not predicate_expression:
        return
    try:
        _compile_dsl_expression(predicate_expression)
    except RowSecurityCompileError as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                "row-security predicate is not a valid expression: "
                f"{exc}. Use the restricted DSL (dimension_equals, in, "
                "and, or, not) with single-quoted string values."
            ),
        )


# ---------------------------------------------------------------------------
# List / get
# ---------------------------------------------------------------------------


@router.get("", response_model=list[RowSecurityRuleResponse])
async def list_rules(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> list[RowSecurityRuleResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_scoped_model(db, project_id, model_id)
        result = await db.execute(
            select(RowSecurityRule)
            .where(RowSecurityRule.model_id == model_id)
            .order_by(RowSecurityRule.created_at.asc())
        )
        return [
            RowSecurityRuleResponse.model_validate(r) for r in result.scalars().all()
        ]


@router.get("/{rule_id}", response_model=RowSecurityRuleResponse)
async def get_rule(
    project_id: UUID,
    model_id: UUID,
    rule_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> RowSecurityRuleResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        rule = await _get_scoped_rule(db, project_id, model_id, rule_id)
        return RowSecurityRuleResponse.model_validate(rule)


# ---------------------------------------------------------------------------
# Create / update / delete
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=RowSecurityRuleResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_rule(
    project_id: UUID,
    model_id: UUID,
    body: RowSecurityRuleCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> RowSecurityRuleResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_scoped_model(db, project_id, model_id)

        # Bug-5206: validate dimension_path exists in the model.
        await _validate_dimension_path_exists(db, model_id, body.dimension_path)

        if body.rule_type == "user_mapping":
            await _validate_mapping_table_in_model(
                db, model_id, body.mapping_table_id
            )
            # Bug-5207: validate mapping columns exist on the mapping table.
            await _validate_mapping_columns(
                db, body.mapping_table_id,
                body.mapping_user_column, body.mapping_value_column,
            )
        else:  # role_predicate
            _validate_predicate_compiles(body.predicate_expression)
            # Bug-5206: validate predicate columns match dimension_path.
            _validate_predicate_matches_dimension(
                body.predicate_expression, body.dimension_path,
            )

        rule = RowSecurityRule(
            model_id=model_id,
            name=body.name,
            dimension_path=body.dimension_path,
            rule_type=body.rule_type,
            predicate_expression=body.predicate_expression,
            applies_to_roles=body.applies_to_roles,
            mapping_table_id=body.mapping_table_id,
            mapping_user_column=body.mapping_user_column,
            mapping_value_column=body.mapping_value_column,
            is_enabled=body.is_enabled,
            attribute_source=body.attribute_source,
            attribute_claim_name=body.attribute_claim_name,
        )
        db.add(rule)
        try:
            await db.flush()
            await audit(
                db, action="security.rule_create", severity="critical",
                actor_email=current_user.email,
                target_type="row_security_rule", target_id=rule.id,
                target_name=rule.name,
                detail={"rule_type": rule.rule_type, "dimension": rule.dimension_path},
            )
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A row-security rule named {body.name!r} already exists for this model",
            )
        await db.refresh(rule)
        return RowSecurityRuleResponse.model_validate(rule)


@router.patch(
    "/{rule_id}",
    response_model=RowSecurityRuleResponse,
    dependencies=[require_role("modeler")],
)
async def update_rule(
    project_id: UUID,
    model_id: UUID,
    rule_id: UUID,
    body: RowSecurityRuleUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> RowSecurityRuleResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        rule = await _get_scoped_rule(db, project_id, model_id, rule_id)

        updates = body.model_dump(exclude_unset=True)

        # Bug-5206: if dimension_path is being updated, validate it exists.
        if "dimension_path" in updates:
            await _validate_dimension_path_exists(db, model_id, updates["dimension_path"])

        # F-007-04: validate the DSL on update too. The runtime error path
        # is the same (a malformed expression 500s every matched caller),
        # so the same save-time gate must guard the update path. Only run it
        # for role_predicate rules — rule_type is immutable, so the stored
        # type is authoritative.
        if (
            rule.rule_type == "role_predicate"
            and "predicate_expression" in updates
        ):
            _validate_predicate_compiles(updates["predicate_expression"])
            # Bug-5206: validate predicate columns match dimension_path
            # (use the updated dimension_path if supplied, else the stored one).
            eff_dim_path = updates.get("dimension_path", rule.dimension_path)
            _validate_predicate_matches_dimension(
                updates["predicate_expression"], eff_dim_path,
            )
        elif (
            rule.rule_type == "role_predicate"
            and "dimension_path" in updates
            and rule.predicate_expression
        ):
            # dimension_path changed but predicate_expression did not —
            # still need to check the existing predicate matches the new path.
            _validate_predicate_matches_dimension(
                rule.predicate_expression, updates["dimension_path"],
            )

        # Bug-5207: validate mapping columns on update if they are being changed.
        if rule.rule_type == "user_mapping":
            eff_table = updates.get("mapping_table_id", rule.mapping_table_id)
            if "mapping_table_id" in updates:
                await _validate_mapping_table_in_model(db, model_id, eff_table)
            eff_user_col = updates.get("mapping_user_column", rule.mapping_user_column)
            eff_val_col = updates.get("mapping_value_column", rule.mapping_value_column)
            if any(k in updates for k in ("mapping_table_id", "mapping_user_column", "mapping_value_column")):
                await _validate_mapping_columns(db, eff_table, eff_user_col, eff_val_col)

        # rule_type is immutable — switching shapes would leave the row
        # violating the shape invariant until every field was updated in
        # the same request. The update schema omits it for this reason.
        for k, v in updates.items():
            setattr(rule, k, v)

        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Update would violate row-security shape or uniqueness invariants",
            )
        await db.refresh(rule)
        return RowSecurityRuleResponse.model_validate(rule)


@router.delete(
    "/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("modeler")],
)
async def delete_rule(
    project_id: UUID,
    model_id: UUID,
    rule_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        rule = await _get_scoped_rule(db, project_id, model_id, rule_id)
        rule_name = rule.name
        await audit(
            db, action="security.rule_delete", severity="critical",
            actor_email=current_user.email,
            target_type="row_security_rule", target_id=rule_id,
            target_name=rule_name,
        )
        await db.delete(rule)
        await db.commit()
        return None


# ---------------------------------------------------------------------------
# Simulate-as-user
# ---------------------------------------------------------------------------


@router.post(
    "/simulate",
    response_model=RowSecuritySimulateResponse,
    dependencies=[require_role("modeler")],
)
async def simulate_as_user(
    project_id: UUID,
    model_id: UUID,
    body: RowSecuritySimulateRequest,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> RowSecuritySimulateResponse:
    """Preview which rules would fire — and the exact WHERE fragment that
    would wrap the query — for a hypothetical ``(user_identity, roles)``.

    Uses :func:`shared.security.compile_row_security` directly, so the
    preview is identical to the runtime behavior. That shared path is
    the only sanctioned way to reason about row-security output.
    """
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_scoped_model(db, project_id, model_id)
        principal = Principal(
            user_identity=body.user_identity,
            roles=frozenset(body.roles),
            groups=frozenset(body.groups),
            claims=dict(body.claims),
        )
        # F-007-07: compile with the model's real target connector so the
        # previewed predicate is byte-identical to the runtime predicate
        # (BigQuery backticks vs PostgreSQL double-quotes).
        connector = await _resolve_model_connector(db, model_id)
        try:
            compiled = await compile_row_security(
                model_id, principal, db, connector=connector,
            )
        except RowSecurityCompileError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Row-security compilation failed: {exc}",
            )

        if compiled is None:
            return RowSecuritySimulateResponse(
                user_identity=body.user_identity,
                roles=list(body.roles),
                active_rule_ids=[],
                compiled_predicate=None,
            )
        return RowSecuritySimulateResponse(
            user_identity=body.user_identity,
            roles=list(body.roles),
            active_rule_ids=[UUID(r) for r in compiled.active_rule_ids],
            compiled_predicate=compiled.sql_expression,
        )
