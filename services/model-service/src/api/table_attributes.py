"""
Unified table attribute listing (physical + user-defined).
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from shared.db.models import (
    Dimension,
    Join,
    Measure,
    ModelColumn,
    ModelTable,
    UserDefinedAttribute,
    UserDefinedAttributeColumnRef,
)
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import ModelColumnUpdate, TableAttributeResponse
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role
from src.api._scope import ensure_model_in_project
from src.api._uda_refs import assert_uda_deletable

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/tables/{table_id}",
    tags=["table-attributes"],
)


class ColumnSyncItem(BaseModel):
    column_name: str
    data_type: str
    is_nullable: bool = True


@router.get("/attributes", response_model=list[TableAttributeResponse])
async def list_table_attributes(
    project_id: UUID,
    model_id: UUID,
    table_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> list[TableAttributeResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        table = await db.get(ModelTable, table_id)
        if table is None or table.model_id != model_id:
            raise HTTPException(status_code=404, detail="Model table not found")

        cols_result = await db.execute(
            select(ModelColumn)
            .where(ModelColumn.model_table_id == table_id)
            .order_by(ModelColumn.column_name)
        )
        uda_result = await db.execute(
            select(UserDefinedAttribute)
            .where(
                UserDefinedAttribute.model_id == model_id,
                UserDefinedAttribute.table_id == table_id,
            )
            .order_by(UserDefinedAttribute.name)
        )

        physical = [
            TableAttributeResponse(
                kind="physical",
                id=c.id,
                table_id=table_id,
                name=c.column_name,
                display_name=c.display_name,
                description=c.description,
                is_hidden=c.is_hidden,
                hidden_reason=c.hidden_reason,
                is_primary_key=c.is_primary_key,
                data_type=c.data_type,
                is_user_defined=False,
                validated=None,
                validation_error=None,
            )
            for c in cols_result.scalars().all()
        ]
        user_defined = [
            TableAttributeResponse(
                kind="user_defined",
                id=a.id,
                table_id=table_id,
                name=a.name,
                display_name=None,
                description=a.description,
                is_hidden=False,
                is_primary_key=False,
                data_type=a.output_data_type,
                is_user_defined=True,
                is_generated=a.is_generated,
                expression=a.expression,
                validated=a.validated,
                validation_error=a.validation_error,
            )
            for a in uda_result.scalars().all()
        ]
        return sorted(physical + user_defined, key=lambda x: x.name.lower())


@router.post("/sync-columns", status_code=status.HTTP_204_NO_CONTENT)
async def sync_columns(
    project_id: UUID,
    model_id: UUID,
    table_id: UUID,
    body: list[ColumnSyncItem],
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("modeler"),
) -> None:
    """Upsert ModelColumn records from a discovered schema.

    Creates new columns and upgrades existing ``data_type="unknown"`` entries
    with the real type.  Called by the frontend after table classification.
    """
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        table = await db.get(ModelTable, table_id)
        if table is None or table.model_id != model_id:
            raise HTTPException(status_code=404, detail="Model table not found")

        # Fetch existing columns for this table in one query
        existing_result = await db.execute(
            select(ModelColumn).where(ModelColumn.model_table_id == table_id)
        )
        existing = {c.column_name: c for c in existing_result.scalars().all()}

        for item in body:
            if item.column_name in existing:
                col = existing[item.column_name]
                if col.data_type == "unknown" and item.data_type != "unknown":
                    col.data_type = item.data_type
                    col.is_nullable = item.is_nullable
            else:
                db.add(ModelColumn(
                    model_table_id=table_id,
                    column_name=item.column_name,
                    data_type=item.data_type,
                    is_nullable=item.is_nullable,
                ))

        await db.commit()


@router.patch(
    "/columns/{column_id}",
    response_model=TableAttributeResponse,
    dependencies=[require_role("modeler")],
)
async def update_physical_column(
    project_id: UUID,
    model_id: UUID,
    table_id: UUID,
    column_id: UUID,
    body: ModelColumnUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> TableAttributeResponse:
    """Update modeller-editable fields on a physical column.

    Phase 0 of the semantic-layer plan: lets the modeller set a friendly
    display name, write a business description, hide the column from the
    catalog, and declare primary keys consumed by generated LookML. The
    hidden flag drives the visibility cascade in Phase 1's gateway work.
    """
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        table = await db.get(ModelTable, table_id)
        if table is None or table.model_id != model_id:
            raise HTTPException(status_code=404, detail="Model table not found")
        col = await db.get(ModelColumn, column_id)
        if col is None or col.model_table_id != table_id:
            raise HTTPException(status_code=404, detail="Physical column not found")

        updates = body.model_dump(exclude_unset=True)

        # Track hidden_reason when user explicitly sets is_hidden
        if "is_hidden" in updates:
            if updates["is_hidden"]:
                updates["hidden_reason"] = "user"
            else:
                updates["hidden_reason"] = None

        for k, v in updates.items():
            setattr(col, k, v)
        await db.commit()
        await db.refresh(col)

        return TableAttributeResponse(
            kind="physical",
            id=col.id,
            table_id=table_id,
            name=col.column_name,
            display_name=col.display_name,
            description=col.description,
            is_hidden=col.is_hidden,
            hidden_reason=col.hidden_reason,
            is_primary_key=col.is_primary_key,
            data_type=col.data_type,
            is_user_defined=False,
            validated=None,
            validation_error=None,
        )


@router.delete(
    "/attributes/{attribute_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("modeler")],
)
async def delete_table_attribute(
    project_id: UUID,
    model_id: UUID,
    table_id: UUID,
    attribute_id: UUID,
    kind: str = Query(default="physical", pattern="^(physical|user_defined)$"),
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        table = await db.get(ModelTable, table_id)
        if table is None or table.model_id != model_id:
            raise HTTPException(status_code=404, detail="Model table not found")

        if kind == "user_defined":
            attr = await db.get(UserDefinedAttribute, attribute_id)
            if attr is None or attr.model_id != model_id or attr.table_id != table_id:
                raise HTTPException(status_code=404, detail="User-defined attribute not found")
            # Bug-1505 / F-016-08: the AttributesTab Delete button reaches this
            # endpoint. It must apply the SAME reference guard as the direct REST
            # delete and the hierarchy delete path — including HierarchyLevel /
            # HierarchyLevelAttribute references — so a generated UDA that keys a
            # hierarchy level cannot be deleted out from under it.
            await assert_uda_deletable(db, model_id=model_id, attribute_id=attribute_id)
            await db.delete(attr)
            await db.commit()
            return

        col = await db.get(ModelColumn, attribute_id)
        if col is None or col.model_table_id != table_id:
            raise HTTPException(status_code=404, detail="Physical attribute not found")

        dim_result = await db.execute(
            select(Dimension.name).where(
                Dimension.model_id == model_id,
                Dimension.source_column_id == attribute_id,
            )
        )
        meas_result = await db.execute(
            select(Measure.name).where(
                Measure.model_id == model_id,
                Measure.source_column_id == attribute_id,
            )
        )
        join_result = await db.execute(
            select(Join.id).where(
                Join.model_id == model_id,
                (Join.left_column_id == attribute_id) | (Join.right_column_id == attribute_id),
            )
        )
        uda_ref_result = await db.execute(
            select(UserDefinedAttributeColumnRef.id).where(
                UserDefinedAttributeColumnRef.column_id == attribute_id
            )
        )

        dim_refs = [r[0] for r in dim_result.fetchall()]
        meas_refs = [r[0] for r in meas_result.fetchall()]
        join_refs = [str(r[0]) for r in join_result.fetchall()]
        uda_refs = [str(r[0]) for r in uda_ref_result.fetchall()]
        if dim_refs or meas_refs or join_refs or uda_refs:
            refs = []
            if dim_refs:
                refs.append(f"dimensions: {', '.join(dim_refs)}")
            if meas_refs:
                refs.append(f"measures: {', '.join(meas_refs)}")
            if join_refs:
                refs.append(f"joins: {', '.join(join_refs)}")
            if uda_refs:
                refs.append("user-defined-attribute dependencies exist")
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot delete physical attribute; it is referenced by {'; '.join(refs)}",
            )

        await db.delete(col)
        await db.commit()
