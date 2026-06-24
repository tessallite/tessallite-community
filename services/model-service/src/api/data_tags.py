"""Data tag CRUD and persona tag restriction endpoints."""
from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from shared.db.models import (
    DataTag,
    Model,
    ModelColumn,
    ModelTable,
    Persona,
    PersonaTagRestriction,
)
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    DataTagColumnInfo,
    DataTagCreate,
    DataTagResponse,
    DataTagUpdate,
    PersonaTagRestrictionRequest,
    PersonaTagRestrictionResponse,
)
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

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

        # Assign the id client-side so the row can be re-selected after
        # commit without touching expired attributes (MissingGreenlet).
        tag = DataTag(
            id=uuid4(),
            model_id=model_id,
            tag_name=body.tag_name,
            description=body.description,
        )
        tag_id = tag.id

        if body.column_ids:
            # F-008-14: only columns belonging to this model may be tagged.
            tag.columns = await _resolve_model_columns(db, model_id, body.column_ids)

        db.add(tag)
        try:
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
        if body.column_ids is not None:
            # F-008-14: only columns belonging to this model may be tagged.
            tag.columns = await _resolve_model_columns(db, model_id, body.column_ids)

        try:
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
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)
        # F-008-01: eager-load columns — ORM delete cascades the secondary
        # association rows, which lazy-loads the collection if not loaded.
        tag = await _load_tag(db, model_id, tag_id)
        if tag is None:
            raise _not_found("Data tag not found")
        await db.delete(tag)
        await db.commit()


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
        # F-008-14: validate ownership before mutating — a nonexistent or
        # foreign persona would otherwise FK-error as an unhandled 500, and a
        # foreign tag id would attach a restriction across model boundaries.
        await _load_persona_in_model(db, model_id, persona_id)
        await _validate_tags_in_model(db, model_id, body.tag_ids)

        await db.execute(
            delete(PersonaTagRestriction)
            .where(PersonaTagRestriction.persona_id == persona_id)
        )

        for tag_id in body.tag_ids:
            db.add(PersonaTagRestriction(persona_id=persona_id, data_tag_id=tag_id))

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
