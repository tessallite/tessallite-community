"""Data Quality rule CRUD, manual validation trigger, and violation history."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select

from shared.data_quality.validator import validate_rules
from shared.db.models import (
    DataQualityRule,
    DataQualityViolation,
    Dimension,
    Measure,
    Model,
    ModelColumn,
)
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    DataQualityRuleCreate,
    DataQualityRuleResponse,
    DataQualityRuleUpdate,
    DataQualityValidateResponse,
    DataQualityViolationResponse,
)
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role
from src.api._model_lock import acquire_model_definition_lock
from src.api._scope import ensure_target_in_model

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/data-quality-rules",
    tags=["data-quality"],
)

# The ORM class that OWNS each ``target_type`` a data-quality rule may name.
#
# Keys must stay identical to ``_DQ_TARGET_TYPES`` in
# ``shared/schemas/domains/governance_advanced.py`` — the request schema's own
# vocabulary. ``tests/test_data_quality_target_scope.py`` pins the two sets
# equal, so adding a target type to the schema without teaching this map fails
# the suite rather than reaching ``ensure_target_in_model`` as an unrecognised
# type (which fails closed, but as a 422 the modeller cannot act on).
#
# Every value is a real entity: unlike glossary attachments there is no id-less
# "concept" target here, so ``None`` never appears and a rule always names a row.
_DQ_RULE_TARGETS: dict[str, type] = {
    "dimension": Dimension,
    "measure": Measure,
    "column": ModelColumn,
}


def _not_found(msg: str = "Not found") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)


async def _get_model(db, project_id: UUID, model_id: UUID) -> Model:
    model = await db.get(Model, model_id)
    if model is None or model.project_id != project_id:
        raise _not_found("Model not found")
    return model


@router.get("", response_model=list[DataQualityRuleResponse], dependencies=[require_role("viewer")])
async def list_rules(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[DataQualityRuleResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)
        result = await db.execute(
            select(DataQualityRule)
            .where(DataQualityRule.model_id == model_id)
            .order_by(DataQualityRule.created_at)
        )
        return [DataQualityRuleResponse.model_validate(r) for r in result.scalars().all()]


@router.post(
    "",
    response_model=DataQualityRuleResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_rule(
    project_id: UUID,
    model_id: UUID,
    body: DataQualityRuleCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> DataQualityRuleResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)
        # Bug-7982 finding 7 then 3: auth before lock; DataQualityRule is
        # snapshot-owned (truncate-reinserted on revert).
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock

        # ``(target_type, target_id)`` is a POLYMORPHIC body foreign key that was
        # written straight into the rule. RBAC proves only that the caller may
        # act in the PATH project; nothing proved the submitted target belonged
        # to it, and ``model_columns.id`` / ``dimensions.id`` / ``measures.id``
        # are all tenant-schema-wide, so a project-B id satisfies the column type
        # and persists.
        #
        # The consequence is a source-data read, not merely a mis-associated row.
        # ``shared/data_quality/validator.py::_resolve_column_ref`` dereferences
        # ``rule.target_id`` with a bare ``db.get(ModelColumn, ...)`` and NO
        # ownership re-check, walks it to its ModelTable's ``physical_name``, and
        # issues a COUNT/GROUP BY against that table through /introspect using a
        # ``system_admin`` service token (``_mint_service_token``). A foreign
        # target therefore turns "add a not-null rule" into a query over another
        # project's physical table, with the row count and up to ten sample
        # VALUES persisted into this model's DataQualityViolation rows and
        # rendered in this model's UI.
        #
        # Validated under the definition lock and BEFORE ``db.add``: the helper's
        # SELECT autoflushes, so a guard placed after the row is added would
        # already have sent the unvalidated foreign key to the database.
        await ensure_target_in_model(
            db,
            target_type=body.target_type,
            target_id=body.target_id,
            model_id=model_id,
            project_id=project_id,
            allowed_targets=_DQ_RULE_TARGETS,
        )

        rule = DataQualityRule(
            model_id=model_id,
            name=body.name,
            target_type=body.target_type,
            target_id=body.target_id,
            rule_type=body.rule_type,
            rule_config=body.rule_config,
            severity=body.severity,
            is_enabled=body.is_enabled,
            block_on_failure=body.block_on_failure,
        )
        db.add(rule)
        await db.commit()
        await db.refresh(rule)
        return DataQualityRuleResponse.model_validate(rule)


@router.put(
    "/{rule_id}",
    response_model=DataQualityRuleResponse,
    dependencies=[require_role("modeler")],
)
async def update_rule(
    project_id: UUID,
    model_id: UUID,
    rule_id: UUID,
    body: DataQualityRuleUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> DataQualityRuleResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        rule = await db.get(DataQualityRule, rule_id)
        if rule is None or rule.model_id != model_id:
            raise _not_found("Rule not found")
        if body.name is not None:
            rule.name = body.name
        if body.rule_config is not None:
            rule.rule_config = body.rule_config
        if body.severity is not None:
            rule.severity = body.severity
        if body.is_enabled is not None:
            rule.is_enabled = body.is_enabled
        if body.block_on_failure is not None:
            rule.block_on_failure = body.block_on_failure
        await db.commit()
        await db.refresh(rule)
        return DataQualityRuleResponse.model_validate(rule)


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
        await _get_model(db, project_id, model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        rule = await db.get(DataQualityRule, rule_id)
        if rule is None or rule.model_id != model_id:
            raise _not_found("Rule not found")
        await db.delete(rule)
        await db.commit()


@router.post(
    "/validate",
    response_model=DataQualityValidateResponse,
    dependencies=[require_role("modeler")],
)
async def run_validation(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> DataQualityValidateResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)
        rules_count_result = await db.execute(
            select(DataQualityRule.id).where(
                DataQualityRule.model_id == model_id,
                DataQualityRule.is_enabled.is_(True),
            )
        )
        rules_checked = len(rules_count_result.scalars().all())
        violations = await validate_rules(model_id, db, tenant_id=current_user.tenant_id)
        await db.commit()
        return DataQualityValidateResponse(
            rules_checked=rules_checked,
            violations_found=len(violations),
            rule_results=[
                {
                    "rule_id": str(v.rule.id),
                    "rule_name": v.rule.name,
                    "violation_count": v.count,
                }
                for v in violations
            ],
        )


@router.delete(
    "/{rule_id}/violations",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("modeler")],
)
async def clear_violations(
    project_id: UUID,
    model_id: UUID,
    rule_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)
        # Bug-8740: this endpoint is named for the VIOLATIONS it clears, and was
        # allow-listed on that basis — but it also resets
        # ``DataQualityRule.last_violation_count``, and ``data_quality_rules`` is
        # a snapshot-owned table the revert delete-and-reinserts. That write went
        # out unlocked, so the runtime guard reported a Bug-7982 violation on an
        # ordinary "clear violations" click in the production ``warn`` default,
        # and under a concurrent revert the reset was either lost or raised
        # ``StaleDataError`` (0 rows matched) as an HTTP 500 to the modeller.
        # READ-UNDER-LOCK: the rule fetch below is the read-modify-write's own
        # ownership check, so the lock is taken first.
        await acquire_model_definition_lock(db, model_id)
        rule = await db.get(DataQualityRule, rule_id)
        if rule is None or rule.model_id != model_id:
            raise _not_found("Rule not found")
        result = await db.execute(
            select(DataQualityViolation).where(DataQualityViolation.rule_id == rule_id)
        )
        for v in result.scalars().all():
            await db.delete(v)
        rule.last_violation_count = None
        await db.commit()


@router.get(
    "/{rule_id}/violations",
    response_model=list[DataQualityViolationResponse],
    dependencies=[require_role("viewer")],
)
async def list_violations(
    project_id: UUID,
    model_id: UUID,
    rule_id: UUID,
    limit: int = 50,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[DataQualityViolationResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)
        rule = await db.get(DataQualityRule, rule_id)
        if rule is None or rule.model_id != model_id:
            raise _not_found("Rule not found")
        result = await db.execute(
            select(DataQualityViolation)
            .where(DataQualityViolation.rule_id == rule_id)
            .order_by(DataQualityViolation.detected_at.desc())
            .limit(limit)
        )
        return [DataQualityViolationResponse.model_validate(v) for v in result.scalars().all()]


@router.get(
    "/aggregate-violations",
    response_model=dict[str, int],
    dependencies=[require_role("viewer")],
)
async def aggregate_violation_summary(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> dict[str, int]:
    """Return {aggregate_id: total_violation_count} for all aggregates with violations in this model."""
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)
        rule_ids_result = await db.execute(
            select(DataQualityRule.id).where(DataQualityRule.model_id == model_id)
        )
        rule_ids = [r for r in rule_ids_result.scalars().all()]
        if not rule_ids:
            return {}
        rows = await db.execute(
            select(
                DataQualityViolation.aggregate_id,
                func.sum(DataQualityViolation.violation_count).label("total"),
            )
            .where(
                DataQualityViolation.rule_id.in_(rule_ids),
                DataQualityViolation.aggregate_id.is_not(None),
            )
            .group_by(DataQualityViolation.aggregate_id)
        )
        return {str(row.aggregate_id): int(row.total) for row in rows.all()}


@router.get(
    "/pocket-violations",
    response_model=dict[str, int],
    dependencies=[require_role("viewer")],
)
async def pocket_violation_summary(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> dict[str, int]:
    """Return {pocket_id: total_violation_count} for all pockets with violations in this model."""
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)
        rule_ids_result = await db.execute(
            select(DataQualityRule.id).where(DataQualityRule.model_id == model_id)
        )
        rule_ids = [r for r in rule_ids_result.scalars().all()]
        if not rule_ids:
            return {}
        rows = await db.execute(
            select(
                DataQualityViolation.pocket_id,
                func.sum(DataQualityViolation.violation_count).label("total"),
            )
            .where(
                DataQualityViolation.rule_id.in_(rule_ids),
                DataQualityViolation.pocket_id.is_not(None),
            )
            .group_by(DataQualityViolation.pocket_id)
        )
        return {str(row.pocket_id): int(row.total) for row in rows.all()}
