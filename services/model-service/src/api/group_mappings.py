"""CRUD endpoints for IdP group-to-role mappings.

Tenant admins configure which IdP group names map to which project-scoped
roles.  When a user SSO-logs in, the JIT logic looks up these mappings to
assign the correct role instead of the default JIT role.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from shared.audit.logger import audit
from shared.auth.roles import ALLOWED_GROUP_MAPPING_ROLES
from shared.db.models import IdpGroupRoleMapping, Project
from shared.db.session import get_tenant_db
from src.auth.middleware import CurrentUser, require_tenant_admin

router = APIRouter(prefix="/admin/group-mappings", tags=["group-mappings"])


class GroupMappingCreate(BaseModel):
    idp_group_name: str
    project_id: uuid.UUID | None = None
    role: str


class GroupMappingUpdate(BaseModel):
    role: str


class GroupMappingResponse(BaseModel):
    id: uuid.UUID
    idp_group_name: str
    project_id: uuid.UUID | None
    role: str

    model_config = {"from_attributes": True}


async def _get_db(tenant_id: str):
    async for db in get_tenant_db(tenant_id):
        yield db


@router.get("", response_model=list[GroupMappingResponse])
async def list_group_mappings(
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> list[GroupMappingResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        result = await db.execute(
            select(IdpGroupRoleMapping).order_by(IdpGroupRoleMapping.idp_group_name)
        )
        return [GroupMappingResponse.model_validate(m) for m in result.scalars().all()]


@router.post("", response_model=GroupMappingResponse, status_code=status.HTTP_201_CREATED)
async def create_group_mapping(
    body: GroupMappingCreate,
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> GroupMappingResponse:
    if body.role not in ALLOWED_GROUP_MAPPING_ROLES:
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid role. Must be one of: "
                f"{', '.join(sorted(ALLOWED_GROUP_MAPPING_ROLES))}"
            ),
        )

    async for db in get_tenant_db(current_user.tenant_id):
        # Bug-5272: validate that project_id references an existing project
        # in the tenant before persisting the mapping.
        if body.project_id is not None:
            project = await db.get(Project, body.project_id)
            if project is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Project {body.project_id} does not exist in this tenant",
                )

        existing = await db.execute(
            select(IdpGroupRoleMapping).where(
                IdpGroupRoleMapping.idp_group_name == body.idp_group_name,
                IdpGroupRoleMapping.project_id == body.project_id,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Mapping already exists for this group and project")

        mapping = IdpGroupRoleMapping(
            idp_group_name=body.idp_group_name,
            project_id=body.project_id,
            role=body.role,
        )
        db.add(mapping)
        await audit(
            db, action="group_mapping.create", severity="warn",
            actor_email=current_user.email,
            target_type="group_mapping", target_name=body.idp_group_name,
            detail={"role": body.role, "project_id": str(body.project_id) if body.project_id else None},
        )
        await db.commit()
        await db.refresh(mapping)
        return GroupMappingResponse.model_validate(mapping)


@router.put("/{mapping_id}", response_model=GroupMappingResponse)
async def update_group_mapping(
    mapping_id: uuid.UUID,
    body: GroupMappingUpdate,
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> GroupMappingResponse:
    if body.role not in ALLOWED_GROUP_MAPPING_ROLES:
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid role. Must be one of: "
                f"{', '.join(sorted(ALLOWED_GROUP_MAPPING_ROLES))}"
            ),
        )

    async for db in get_tenant_db(current_user.tenant_id):
        mapping = await db.get(IdpGroupRoleMapping, mapping_id)
        if mapping is None:
            raise HTTPException(status_code=404, detail="Mapping not found")
        old_role = mapping.role
        mapping.role = body.role
        await audit(
            db, action="group_mapping.update", severity="warn",
            actor_email=current_user.email,
            target_type="group_mapping", target_id=mapping.id,
            target_name=mapping.idp_group_name,
            detail={"old_role": old_role, "new_role": body.role},
        )
        await db.commit()
        await db.refresh(mapping)
        return GroupMappingResponse.model_validate(mapping)


@router.delete("/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group_mapping(
    mapping_id: uuid.UUID,
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        mapping = await db.get(IdpGroupRoleMapping, mapping_id)
        if mapping is None:
            raise HTTPException(status_code=404, detail="Mapping not found")
        group_name = mapping.idp_group_name
        await db.delete(mapping)
        await audit(
            db, action="group_mapping.delete", severity="critical",
            actor_email=current_user.email,
            target_type="group_mapping", target_id=mapping_id,
            target_name=group_name,
        )
        await db.commit()
