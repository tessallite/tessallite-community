"""
Access-binding management routes.

Only admins (or bootstrap users) may manage bindings.

Routes:
  POST   /api/v1/projects/{project_id}/access           — grant role to user
  GET    /api/v1/projects/{project_id}/access           — list bindings
  DELETE /api/v1/projects/{project_id}/access/{id}      — revoke binding
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from shared.db.models import UserAccessBinding
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import UserAccessBindingCreate, UserAccessBindingResponse
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

router = APIRouter(
    prefix="/projects/{project_id}/access",
    tags=["access"],
)


@router.post(
    "",
    response_model=UserAccessBindingResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("admin")],
)
async def grant_access(
    project_id: UUID,
    body: UserAccessBindingCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> UserAccessBindingResponse:
    """Grant a role to a user for this project (or a specific model in it).

    F-021-04/F-021-11: the upsert key is the binding's true identity
    ``(user_identity, project_id, model_id)`` — role is a mutable attribute,
    not part of identity. A model-scoped grant therefore no longer overwrites
    a project-level row, and a user holding both a project-level and a
    model-level binding can no longer cause a ``MultipleResultsFound`` 500.
    NULL ``model_id`` (project-level) is matched distinctly from any concrete
    model id.
    """
    async for db in get_tenant_db(current_user.tenant_id):
        model_filter = (
            UserAccessBinding.model_id == body.model_id
            if body.model_id is not None
            else UserAccessBinding.model_id.is_(None)
        )
        existing = await db.execute(
            select(UserAccessBinding).where(
                UserAccessBinding.user_identity == body.user_identity,
                UserAccessBinding.project_id == project_id,
                model_filter,
            )
        )
        binding = existing.scalar_one_or_none()
        if binding:
            binding.role = body.role
        else:
            binding = UserAccessBinding(
                user_identity=body.user_identity,
                role=body.role,
                project_id=project_id,
                model_id=body.model_id,
            )
            db.add(binding)
        await db.commit()
        await db.refresh(binding)
        return UserAccessBindingResponse.model_validate(binding)


@router.get("", response_model=list[UserAccessBindingResponse])
async def list_access(
    project_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> list[UserAccessBindingResponse]:
    """List all bindings for this project. Requires at least viewer access."""
    async for db in get_tenant_db(current_user.tenant_id):
        result = await db.execute(
            select(UserAccessBinding).where(
                UserAccessBinding.project_id == project_id
            ).order_by(UserAccessBinding.created_at)
        )
        return [
            UserAccessBindingResponse.model_validate(b)
            for b in result.scalars().all()
        ]


@router.delete(
    "/{binding_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("admin")],
)
async def revoke_access(
    project_id: UUID,
    binding_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    """Revoke an access binding."""
    async for db in get_tenant_db(current_user.tenant_id):
        binding = await db.get(UserAccessBinding, binding_id)
        if binding is None or binding.project_id != project_id:
            raise HTTPException(status_code=404, detail="Binding not found")
        await db.delete(binding)
        await db.commit()
