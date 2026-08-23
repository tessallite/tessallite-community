"""Data tag CRUD and persona tag restriction endpoints."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from shared.db.models import (
    AuditEvent,
    DataTag,
    Model,
    ModelColumn,
    ModelTable,
    Persona,
    PersonaTagRestriction,
)
from shared.audit.logger import AuditWriteError, audit_required
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    DataTagColumnInfo,
    DataTagCreate,
    DataTagResponse,
    DataTagUpdate,
    PersonaTagRestrictionRequest,
    PersonaTagRestrictionResponse,
)
from src.api._model_lock import acquire_model_definition_lock
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/data-tags",
    tags=["data-tags"],
)

restriction_router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/personas/{persona_id}/tag-restrictions",
    tags=["data-tags"],
)


def _not_found(msg: str = "Not found") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)


async def _get_model(db, project_id: UUID, model_id: UUID) -> Model:
    model = await db.get(Model, model_id)
    if model is None or model.project_id != project_id:
        raise _not_found("Model not found")
    return model


# F-008-01: DataTag.columns is a lazy relationship; touching it inside an
# AsyncSession raises MissingGreenlet at serialisation time. Every read of a
# tag must eager-load columns (and each column's table, for table_name).
_TAG_LOAD_OPTIONS = (
    selectinload(DataTag.columns).selectinload(ModelColumn.table),
)


async def _load_tag(db, model_id: UUID, tag_id: UUID) -> DataTag | None:
    """Load one tag with its columns (and their tables) eagerly loaded."""
    result = await db.execute(
        select(DataTag)
        .options(*_TAG_LOAD_OPTIONS)
        .where(DataTag.id == tag_id, DataTag.model_id == model_id)
    )
    return result.scalar_one_or_none()


async def _resolve_model_columns(db, model_id: UUID, column_ids) -> list[ModelColumn]:
    """Load the requested columns, rejecting any that do not belong to the
    model (F-008-14). A column from another model would otherwise be silently
    attached to the tag and could carry a restriction the modeler never
    intended — fail closed with 422 instead.
    """
    ids = list(column_ids or [])
    if not ids:
        return []
    cols = (
        await db.execute(
            select(ModelColumn)
            .join(ModelTable, ModelColumn.model_table_id == ModelTable.id)
            .where(ModelColumn.id.in_(ids), ModelTable.model_id == model_id)
        )
    ).scalars().all()
    found = {str(c.id) for c in cols}
    missing = [str(i) for i in ids if str(i) not in found]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "DATA_TAG_COLUMN_NOT_IN_MODEL",
                "message": (
                    "These columns do not belong to this model and cannot be "
                    f"tagged: {', '.join(missing)}."
                ),
            },
        )
    return list(cols)


async def _load_persona_in_model(db, model_id: UUID, persona_id: UUID) -> Persona:
    """Load a persona scoped to the model, 404 otherwise (F-008-14)."""
    p = await db.get(Persona, persona_id)
    if p is None or p.model_id != model_id:
        raise _not_found("Persona not found")
    return p


async def _validate_tags_in_model(db, model_id: UUID, tag_ids) -> None:
    """Reject restriction requests that reference tags from another model
    (F-008-14). A bad tag id would otherwise FK-error as an unhandled 500."""
    ids = list(tag_ids or [])
    if not ids:
        return
    found = set(
        (
            await db.execute(
                select(DataTag.id).where(
                    DataTag.id.in_(ids), DataTag.model_id == model_id
                )
            )
        ).scalars().all()
    )
    missing = [str(i) for i in ids if i not in found]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "DATA_TAG_NOT_IN_MODEL",
                "message": (
                    "These data tags do not belong to this model: "
                    f"{', '.join(missing)}."
                ),
            },
        )


async def _audit_tag_columns_set(
    db,
    *,
    current_user: CurrentUser,
    model_id: UUID,
    project_id: UUID,
    tag_id: UUID,
    tag_name: str,
    before_col_ids: list[str],
    after_col_ids: list[str],
    operation: str,
) -> None:
    """F-008-06 (Bug-8019): durable, fail-closed audit of a data-tag ->
    column-membership mutation.

    The tag -> column set is a direct input to the CLS restricted-column
    closure: adding or removing a column from a tag silently widens or narrows
    the effective CLS surface for every persona that restricts this tag. This
    records who changed the membership and the exact before/after column sets so
    the change is never invisible.

    Emitted on the SAME session as the mutation and BEFORE ``db.commit()`` via
    :func:`audit_required` (mirrors ``security.persona_tag_restrictions_set``): a
    write failure raises :class:`AuditWriteError`, which rolls the whole
    transaction back — the membership change and its evidence commit atomically
    or neither does. ``widens_surface`` is True when the new set is not a subset
    of the old one (any column added broadens what a restriction can cover).
    """
    widens_surface = not set(after_col_ids).issubset(set(before_col_ids))
    await audit_required(
        db,
        action="security.data_tag_columns_set",
        severity="critical",
        actor_email=current_user.email,
        target_type="data_tag",
        target_id=tag_id,
        target_name=tag_name,
        detail={
            "model_id": str(model_id),
            "project_id": str(project_id),
            "actor_user_id": str(current_user.user_id),
            "operation": operation,
            "before_column_ids": before_col_ids,
            "after_column_ids": after_col_ids,
            "widens_surface": widens_surface,
        },
    )


def _column_table_name(c: ModelColumn) -> str:
    # F-008-10: populate table_name from the eagerly-loaded ModelTable
    # (semantic alias) instead of the never-set `_table_name` attribute.
    table = getattr(c, "table", None)
    if table is None:
        return ""
    return getattr(table, "alias", None) or getattr(table, "physical_name", "") or ""


def _tag_to_response(tag: DataTag) -> DataTagResponse:
    cols = [
        DataTagColumnInfo(
            column_id=c.id,
            table_name=_column_table_name(c),
            column_name=c.column_name,
        )
        for c in tag.columns
    ]
    return DataTagResponse(
        id=tag.id,
        model_id=tag.model_id,
        tag_name=tag.tag_name,
        description=tag.description,
        created_at=tag.created_at,
        columns=cols,
    )


@router.get(
    "",
    response_model=list[DataTagResponse],
    dependencies=[require_role("viewer")],
)
async def list_tags(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[DataTagResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)
        result = await db.execute(
            select(DataTag)
            .options(*_TAG_LOAD_OPTIONS)
            .where(DataTag.model_id == model_id)
            .order_by(DataTag.tag_name)
        )
        tags = result.scalars().all()
        return [_tag_to_response(t) for t in tags]


@router.post(
    "",
    response_model=DataTagResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_tag(
    project_id: UUID,
    model_id: UUID,
    body: DataTagCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> DataTagResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock

        # Assign the id client-side so the row can be re-selected after
        # commit without touching expired attributes (MissingGreenlet).
        tag = DataTag(
            id=uuid4(),
            model_id=model_id,
            tag_name=body.tag_name,
            description=body.description,
        )
        tag_id = tag.id

        _after_col_ids: list[str] = []
        if body.column_ids:
            # F-008-14: only columns belonging to this model may be tagged.
            resolved = await _resolve_model_columns(db, model_id, body.column_ids)
            tag.columns = resolved
            _after_col_ids = sorted(str(c.id) for c in resolved)

        db.add(tag)

        try:
            # F-008-17: flush the new row FIRST so a duplicate (model_id,
            # tag_name) surfaces here as an IntegrityError we can map to a clean
            # 409 — BEFORE the audit write below. If we let the tag insert flush
            # for the first time inside audit_required, its IntegrityError would
            # be re-raised as an AuditWriteError (500) and the caller would lose
            # the 409 name-conflict contract.
            await db.flush()

            # F-008-06 (Bug-8019): a tag->column binding is a direct input to the
            # CLS restricted-column closure — adding columns to a tag widens the
            # surface a persona restriction can later cover. Record the initial
            # membership as a DURABLE, fail-closed audit event on the SAME
            # session BEFORE commit (mirrors
            # security.persona_tag_restrictions_set): a write failure raises and
            # rolls the tag creation back, so a restricted-column membership
            # never lands without a surviving actor/before/after record.
            # New tag => before is empty.
            await _audit_tag_columns_set(
                db,
                current_user=current_user,
                model_id=model_id,
                project_id=project_id,
                tag_id=tag_id,
                tag_name=body.tag_name,
                before_col_ids=[],
                after_col_ids=_after_col_ids,
                operation="create",
            )

            await db.commit()
        except IntegrityError:
            # F-008-17: a duplicate (model_id, tag_name) is a client error,
            # not a 500 — mirror the personas-CRUD 409 contract.
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "DATA_TAG_NAME_CONFLICT",
                    "message": (
                        f"A data tag named '{body.tag_name}' already exists "
                        "in this model."
                    ),
                },
            )
        except AuditWriteError as exc:
            # F-008-06 (Bug-8019): the required columns_set audit could not be
            # persisted — fail closed. Roll back so no restricted-column
            # membership lands without a surviving audit trail.
            await db.rollback()
            logger.exception(
                "[CLS_AUDIT] Failed to record data-tag columns_set audit on "
                "create for model %s — aborting to avoid an un-audited CLS "
                "surface change.",
                model_id,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    "Could not record the required security audit for this data "
                    "tag; the tag was not created. Retry."
                ),
            ) from exc

        # F-008-01: re-select with eager-loaded columns instead of relying on
        # lazy relationship access on the (expired) committed instance.
        created = await _load_tag(db, model_id, tag_id)
        if created is None:
            raise _not_found("Data tag not found after create")
        return _tag_to_response(created)


@router.put(
    "/{tag_id}",
    response_model=DataTagResponse,
    dependencies=[require_role("modeler")],
)
async def update_tag(
    project_id: UUID,
    model_id: UUID,
    tag_id: UUID,
    body: DataTagUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> DataTagResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        # F-008-01: eager-load columns — replacing the collection requires the
        # current contents, which would otherwise lazy-load (MissingGreenlet).
        tag = await _load_tag(db, model_id, tag_id)
        if tag is None:
            raise _not_found("Data tag not found")

        # F-008-20: use model_fields_set so an explicit ``null`` description
        # clears the field (distinguishable from omission); ``is not None``
        # alone silently dropped a clear-to-empty.
        fields_set = body.model_fields_set
        if body.tag_name is not None:
            tag.tag_name = body.tag_name
        if "description" in fields_set:
            tag.description = body.description

        _membership_changed = body.column_ids is not None
        _before_col_ids: list[str] = []
        _after_col_ids: list[str] = []
        if _membership_changed:
            # F-008-06 (Bug-8019): capture the current membership BEFORE
            # replacing it — the tag was eager-loaded with its columns, so this
            # reads the pre-mutation set without a lazy load. Replacing this set
            # widens or narrows the CLS restricted-column closure for every
            # persona that restricts this tag.
            _before_col_ids = sorted(str(c.id) for c in tag.columns)
            # F-008-14: only columns belonging to this model may be tagged.
            resolved = await _resolve_model_columns(db, model_id, body.column_ids)
            tag.columns = resolved
            _after_col_ids = sorted(str(c.id) for c in resolved)

        try:
            if _membership_changed:
                # F-008-17: flush the rename/membership change FIRST so a
                # name collision surfaces as an IntegrityError we can map to a
                # clean 409 — BEFORE the fail-closed audit write. If the insert
                # first flushed inside audit_required, its IntegrityError would
                # be re-raised as an AuditWriteError (500) and the caller would
                # lose the 409 name-conflict contract.
                await db.flush()

                # DURABLE, fail-closed audit of the membership replacement on
                # the SAME session BEFORE commit — a tag's column set never
                # changes without a surviving actor/before/after record (mirrors
                # security.persona_tag_restrictions_set). A write failure raises
                # and rolls the mutation back.
                await _audit_tag_columns_set(
                    db,
                    current_user=current_user,
                    model_id=model_id,
                    project_id=project_id,
                    tag_id=tag_id,
                    tag_name=tag.tag_name,
                    before_col_ids=_before_col_ids,
                    after_col_ids=_after_col_ids,
                    operation="update",
                )

            await db.commit()
        except IntegrityError:
            # F-008-17: a rename that collides with another tag is a 409.
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "DATA_TAG_NAME_CONFLICT",
                    "message": (
                        f"A data tag named '{body.tag_name}' already exists "
                        "in this model."
                    ),
                },
            )
        except AuditWriteError as exc:
            # F-008-06 (Bug-8019): the required columns_set audit could not be
            # persisted — fail closed. Roll back so the membership change (a CLS
            # surface widen/narrow) never lands without a surviving audit trail.
            await db.rollback()
            logger.exception(
                "[CLS_AUDIT] Failed to record data-tag columns_set audit on "
                "update for tag %s on model %s — aborting to avoid an "
                "un-audited CLS surface change.",
                tag_id,
                model_id,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    "Could not record the required security audit for this data "
                    "tag; the column membership was not changed. Retry."
                ),
            ) from exc

        updated = await _load_tag(db, model_id, tag_id)
        if updated is None:
            raise _not_found("Data tag not found")
        return _tag_to_response(updated)


@router.delete(
    "/{tag_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("modeler")],
)
async def delete_tag(
    project_id: UUID,
    model_id: UUID,
    tag_id: UUID,
    force: bool = Query(
        default=False,
        description=(
            "Delete even when personas restrict columns via this tag. The "
            "dependent CLS restrictions are then purged explicitly and the "
            "action is audit-logged. Restricted columns become visible to "
            "those personas."
        ),
    ),
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        # F-008-01: eager-load columns — ORM delete cascades the secondary
        # association rows, which lazy-loads the collection if not loaded.
        tag = await _load_tag(db, model_id, tag_id)
        if tag is None:
            raise _not_found("Data tag not found")

        # F-008-06 (Bug-8299/Bug-8308): capture the tag's column membership
        # BEFORE deletion. Deleting the tag removes every tag->column binding,
        # so the CLS restricted-column closure for this tag collapses from
        # {these columns} -> {}. That is a CLS-surface change on the same
        # footing as a create/update membership replacement and must be audited
        # durably, not left to the silent ORM delete. The tag was eager-loaded
        # with its columns, so this reads the pre-mutation set without a lazy
        # load.
        _deleted_col_ids = sorted(str(c.id) for c in tag.columns)

        # Bug-7790 [SECURITY / CLS DATA LEAK]: PersonaTagRestriction.data_tag_id
        # is ondelete=CASCADE, so a bare ``db.delete(tag)`` silently drops every
        # persona column-level-security restriction bound to this tag — columns
        # those personas could never see suddenly become visible, with no guard
        # or warning. A restricted column must NEVER become visible as a delete
        # side effect. Block the delete when dependent restrictions exist and
        # name the personas that would lose protection, unless the caller
        # explicitly forces it (then purge the restrictions deliberately and
        # audit-log the exposure).
        #
        # Lock the tag row FOR UPDATE first so a concurrent
        # set_persona_tag_restrictions cannot insert a new PersonaTagRestriction
        # (whose FK references this tag) between the dependency check below and
        # the delete — such a row would otherwise be silently cascade-dropped,
        # re-opening the leak under concurrency. The insert blocks on this lock
        # and then fails its own FK once the tag is gone.
        await db.execute(
            select(DataTag.id)
            .where(DataTag.id == tag_id, DataTag.model_id == model_id)
            .with_for_update()
        )

        dependent_personas = (
            await db.execute(
                select(Persona.name)
                .join(
                    PersonaTagRestriction,
                    PersonaTagRestriction.persona_id == Persona.id,
                )
                .where(
                    PersonaTagRestriction.data_tag_id == tag_id,
                    Persona.model_id == model_id,
                )
                .order_by(Persona.name)
            )
        ).scalars().all()

        if dependent_personas and not force:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "DATA_TAG_CLS_RESTRICTION_DEPENDENTS",
                    "message": (
                        f"Data tag '{tag.tag_name}' still enforces column-level "
                        "security for these personas: "
                        f"{', '.join(dependent_personas)}. Deleting it would "
                        "expose the restricted columns to them. Remove the tag "
                        "restrictions from those personas first, or retry with "
                        "force=true to delete and drop the restrictions "
                        "explicitly."
                    ),
                    "dependent_personas": list(dependent_personas),
                },
            )

        if dependent_personas and force:
            # Deliberate exposure: purge the restrictions explicitly (rather
            # than relying on the silent FK cascade) and record a DURABLE
            # critical audit event in the same transaction — a security-config
            # weakening must survive as an AuditEvent row, not only a process
            # log line that vanishes on rollback.
            #
            # This audit is UNGATED and FAIL-CLOSED: unlike shared.audit.logger
            # .audit (which returns None and writes NOTHING when the tenant's
            # audit.log_level="off"), a CLS deny-list weakening must ALWAYS be
            # recorded regardless of the log-level setting, and if it cannot be
            # written the delete must NOT proceed. We therefore build the
            # AuditEvent directly and flush it inside this transaction; any
            # failure aborts the request so no restricted column is ever exposed
            # without a surviving audit trail.
            await db.execute(
                delete(PersonaTagRestriction).where(
                    PersonaTagRestriction.data_tag_id == tag_id
                )
            )
            try:
                db.add(
                    AuditEvent(
                        id=uuid4(),
                        timestamp=datetime.now(timezone.utc),
                        actor_email=current_user.email,
                        action="data_tag.force_delete_cls_widening",
                        target_type="data_tag",
                        target_id=tag_id,
                        target_name=tag.tag_name,
                        severity="critical",
                        detail={
                            "model_id": str(model_id),
                            "project_id": str(project_id),
                            "actor_user_id": current_user.user_id,
                            "dropped_cls_restriction_personas": list(
                                dependent_personas
                            ),
                        },
                    )
                )
                await db.flush()
            except Exception as exc:  # fail closed — never expose un-audited
                await db.rollback()
                logger.exception(
                    "[CLS_AUDIT] Failed to record forced data-tag delete audit "
                    "for tag %s on model %s — aborting delete to avoid "
                    "un-audited CLS exposure.",
                    tag_id,
                    model_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=(
                        "Could not record the required security audit for this "
                        "forced deletion; the tag was not deleted. Retry."
                    ),
                ) from exc
            logger.warning(
                "[CLS_AUDIT] Forced delete of data tag %s ('%s') on model %s "
                "dropped column-level-security restrictions for personas: %s",
                tag_id,
                tag.tag_name,
                model_id,
                ", ".join(dependent_personas),
            )

        try:
            # F-008-06 (Bug-8299/Bug-8308): a tag delete empties the tag's
            # column membership (before -> {}), which narrows the CLS
            # restricted-column closure for this tag. Record that membership
            # change as a DURABLE, fail-closed audit event on the SAME session
            # BEFORE commit (mirrors security.persona_tag_restrictions_set): a
            # write failure raises AuditWriteError and rolls the delete back, so
            # a tag's column set never collapses to empty without a surviving
            # actor/before/after record. This is distinct from and additional to
            # the force_delete_cls_widening event above (which records the
            # dropped persona restrictions): both fire on the forced path, and
            # only this one fires on the plain no-dependents delete.
            await _audit_tag_columns_set(
                db,
                current_user=current_user,
                model_id=model_id,
                project_id=project_id,
                tag_id=tag_id,
                tag_name=tag.tag_name,
                before_col_ids=_deleted_col_ids,
                after_col_ids=[],
                operation="delete",
            )

            await db.delete(tag)
            await db.commit()
        except AuditWriteError as exc:
            # F-008-06 (Bug-8299/Bug-8308): the required columns_set audit could
            # not be persisted — fail closed. Roll back so the tag (and its
            # column bindings / any CLS surface it defines) is not deleted
            # without a surviving audit trail.
            await db.rollback()
            logger.exception(
                "[CLS_AUDIT] Failed to record data-tag columns_set audit on "
                "delete for tag %s on model %s — aborting to avoid an "
                "un-audited CLS surface change.",
                tag_id,
                model_id,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    "Could not record the required security audit for this data "
                    "tag; the tag was not deleted. Retry."
                ),
            ) from exc


# ---------------------------------------------------------------------------
# Persona tag restrictions
# ---------------------------------------------------------------------------


@restriction_router.get(
    "",
    response_model=list[PersonaTagRestrictionResponse],
    dependencies=[require_role("viewer")],
)
async def list_persona_tag_restrictions(
    project_id: UUID,
    model_id: UUID,
    persona_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[PersonaTagRestrictionResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)
        # F-008-14: the persona must belong to this model — accepting any
        # tenant persona id leaks restriction membership across models.
        await _load_persona_in_model(db, model_id, persona_id)
        result = await db.execute(
            select(PersonaTagRestriction, DataTag)
            .join(DataTag, PersonaTagRestriction.data_tag_id == DataTag.id)
            .options(selectinload(DataTag.columns))
            .where(PersonaTagRestriction.persona_id == persona_id)
        )
        rows = result.all()
        return [
            PersonaTagRestrictionResponse(
                tag_id=r.DataTag.id,
                tag_name=r.DataTag.tag_name,
                description=r.DataTag.description,
                column_count=len(r.DataTag.columns),
            )
            for r in rows
        ]


@restriction_router.put(
    "",
    response_model=list[PersonaTagRestrictionResponse],
    dependencies=[require_role("modeler")],
)
async def set_persona_tag_restrictions(
    project_id: UUID,
    model_id: UUID,
    persona_id: UUID,
    body: PersonaTagRestrictionRequest,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[PersonaTagRestrictionResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        # F-008-14: validate ownership before mutating — a nonexistent or
        # foreign persona would otherwise FK-error as an unhandled 500, and a
        # foreign tag id would attach a restriction across model boundaries.
        await _load_persona_in_model(db, model_id, persona_id)
        await _validate_tags_in_model(db, model_id, body.tag_ids)

        # F-008-06: capture the before-state so the audit reconstructs what
        # changed (which tags a persona is CLS-restricted from). Replacing this
        # set widens or narrows column-level access.
        _before_ids = sorted(
            str(t) for t in (
                await db.execute(
                    select(PersonaTagRestriction.data_tag_id)
                    .where(PersonaTagRestriction.persona_id == persona_id)
                )
            ).scalars().all()
        )

        await db.execute(
            delete(PersonaTagRestriction)
            .where(PersonaTagRestriction.persona_id == persona_id)
        )

        for tag_id in body.tag_ids:
            db.add(PersonaTagRestriction(persona_id=persona_id, data_tag_id=tag_id))

        _after_ids = sorted(str(t) for t in body.tag_ids)
        # F-008-06: durable, fail-closed audit BEFORE commit — a persona's CLS
        # restriction set never changes without a record of who/what.
        await audit_required(
            db, action="security.persona_tag_restrictions_set", severity="critical",
            actor_email=current_user.email,
            target_type="persona", target_id=persona_id,
            target_name=str(persona_id),
            detail={
                "model_id": str(model_id),
                "before_tag_ids": _before_ids,
                "after_tag_ids": _after_ids,
                "widens_access": len(_after_ids) < len(_before_ids)
                or not set(_before_ids).issubset(set(_after_ids)),
            },
        )

        await db.commit()

        result = await db.execute(
            select(PersonaTagRestriction, DataTag)
            .join(DataTag, PersonaTagRestriction.data_tag_id == DataTag.id)
            .options(selectinload(DataTag.columns))
            .where(PersonaTagRestriction.persona_id == persona_id)
        )
        rows = result.all()
        return [
            PersonaTagRestrictionResponse(
                tag_id=r.DataTag.id,
                tag_name=r.DataTag.tag_name,
                description=r.DataTag.description,
                column_count=len(r.DataTag.columns),
            )
            for r in rows
        ]
