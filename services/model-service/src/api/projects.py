"""
Project CRUD routes.

Role requirements:
  GET (list / get)  → viewer+
  POST              → tenant-level structural op → tenant_admin
  PATCH             → modeler+ for rename/config; admin for is_active (enable/disable)
  DELETE            → admin

See the Explorer RBAC matrix
(docs/architecture/architecture_explorer-rbac-matrix.md) for the full per-role
compliance table. A project modeller may rename a project but must not toggle its
active state (a structural, tenant-level effect) or delete it.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from shared.audit.logger import audit
from shared.db.models import Model, Project
from shared.db.session import get_tenant_db
from src.api._cascade_delete import delete_project_cascade
from shared.schemas.pydantic_models import ProjectCreate, ProjectResponse, ProjectUpdate
from src.auth.middleware import (
    CurrentEmbedUser,
    CurrentUser,
    forbid_embed_user,
    get_current_user,
    require_tenant_admin,
)
from src.auth.rbac import caller_has_role, require_role

router = APIRouter(prefix="/projects", tags=["projects"])


def _enforce_embed_project_scope(current_user: CurrentUser, project_id: UUID) -> None:
    """Block embed users whose project_ids claim excludes this project."""
    if isinstance(current_user, CurrentEmbedUser):
        if current_user.project_ids is not None:
            if str(project_id).lower() not in [p.lower() for p in current_user.project_ids]:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Embed token does not grant access to this project",
                )


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectCreate,
    # Creating a project is a tenant-level structural action. ``require_role``
    # cannot gate it (no project_id exists yet), so enforce tenant_admin
    # explicitly — this is the backend half of the Explorer UI gate and closes
    # the gap where any authenticated member could create a project via the API.
    current_user: CurrentUser = Depends(require_tenant_admin),
    _: None = Depends(forbid_embed_user),
) -> ProjectResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        # Projects are uncapped in Community — the constraint is total models.
        existing = await db.execute(
            select(Project).where(Project.slug == body.slug)
        )
        existing_project = existing.scalar_one_or_none()
        if existing_project:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Project with slug '{body.slug}' already exists",
            )
        project = Project(
            slug=body.slug,
            display_name=(body.display_name or body.slug),
            is_active=True,
            pocket_size_budget_bytes=body.pocket_size_budget_bytes,
        )
        db.add(project)
        await db.commit()
        await db.refresh(project)
        return ProjectResponse.model_validate(project)


@router.get("", response_model=list[ProjectResponse])
async def list_projects(
    current_user: CurrentUser = Depends(get_current_user),
) -> list[ProjectResponse]:
    """List projects the authenticated user can see.

    Tenant discovery is a listing operation, not a per-project
    action, so ``require_role`` can't scope it. Instead we apply
    ``filter_projects_by_user_access`` which pulls the user's
    UserAccessBindings and filters out projects they don't have a
    binding on. Projects with zero bindings remain visible to
    everyone (bootstrap rule — matches ``require_role``). This
    scopes the XMLA catalog list and the Explorer project tree to
    just the tenants / projects the caller can actually use.
    """
    from src.auth.rbac import filter_projects_by_user_access

    async for db in get_tenant_db(current_user.tenant_id):
        result = await db.execute(select(Project).order_by(Project.slug))
        projects = list(result.scalars().all())

        if isinstance(current_user, CurrentEmbedUser):
            if current_user.project_ids is not None:
                pid_set = {p.lower() for p in current_user.project_ids}
                projects = [
                    p for p in projects if str(p.id).lower() in pid_set
                ]
            if current_user.model_ids:
                mid_result = await db.execute(
                    select(Model.project_id).where(
                        Model.id.in_(current_user.model_ids)
                    )
                )
                scoped_pids = {r for r in mid_result.scalars().all()}
                projects = [p for p in projects if p.id in scoped_pids]
            return [ProjectResponse.model_validate(p) for p in projects]

        project_ids = [p.id for p in projects]
        visible_ids = await filter_projects_by_user_access(
            db, project_ids, current_user.user_id, role=current_user.role
        )
        return [
            ProjectResponse.model_validate(p)
            for p in projects
            if p.id in visible_ids
        ]


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> ProjectResponse:
    _enforce_embed_project_scope(current_user, project_id)
    async for db in get_tenant_db(current_user.tenant_id):
        p = await db.get(Project, project_id)
        if p is None:
            raise HTTPException(status_code=404, detail="Project not found")
        return ProjectResponse.model_validate(p)


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: UUID,
    body: ProjectUpdate,
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("modeler"),
) -> ProjectResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        p = await db.get(Project, project_id)
        if p is None:
            raise HTTPException(status_code=404, detail="Project not found")
        updates = body.model_dump(exclude_unset=True)
        # Enabling/disabling a project takes the whole project (and all its
        # models) on/offline — a structural, tenant-level effect. A modeller may
        # rename a project but must not flip its active state; require admin for
        # any is_active change. The route dependency already admits modeler+.
        if "is_active" in updates and not await caller_has_role(
            db, current_user, project_id, "admin"
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: enabling/disabling a project requires 'admin'",
            )
        old_slug = p.slug
        new_slug = updates.get("slug")
        if new_slug and new_slug != old_slug:
            existing = await db.execute(
                select(Project).where(Project.slug == new_slug, Project.id != project_id)
            )
            existing_project = existing.scalar_one_or_none()
            if existing_project:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Project with slug '{new_slug}' already exists",
                )
            if "display_name" not in updates and (not p.display_name or p.display_name == old_slug):
                updates["display_name"] = new_slug
        if "display_name" in updates and (updates["display_name"] is None or not str(updates["display_name"]).strip()):
            updates["display_name"] = new_slug or p.slug
        for key, val in updates.items():
            setattr(p, key, val)
        await db.commit()
        await db.refresh(p)
        return ProjectResponse.model_validate(p)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("admin"),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        p = await db.get(Project, project_id)
        if p is None:
            raise HTTPException(status_code=404, detail="Project not found")
        project_name = p.display_name or p.slug
        errors = await delete_project_cascade(db, project_id)
        if errors:
            await db.rollback()
            raise HTTPException(
                status_code=500,
                detail=f"Project deletion failed at: {'; '.join(errors)}",
            )
        await audit(
            db, action="project.delete", severity="critical",
            actor_email=current_user.email,
            target_type="project", target_id=project_id,
            target_name=project_name,
        )
        await db.commit()
