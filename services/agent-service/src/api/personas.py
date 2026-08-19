"""Project persona CRUD endpoints.

Personas scope the conversational agent's view of models and attributes.
A persona is defined at the project level and can have per-model attribute
restrictions via model scopes.

Bug-5608 — all CRUD endpoints enforce project-level RBAC via the same
helpers used by agent_config.py:
  - list / get: viewer binding or higher (``_require_project_viewer``).
  - create / update / delete: modeller or admin binding
    (``_require_project_modeller``).
"""
from __future__ import annotations

import logging
import re
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from shared.db.models import (
    Dimension,
    Measure,
    Model,
    ProjectPersona,
    ProjectPersonaModelScope,
)
from shared.db.session import get_tenant_db
from src.api._body_scope import ensure_ref_in_project
from src.auth.middleware import (
    CurrentUser,
    forbid_embed_user,
)
# Bug-8445 / Bug-8446 — the service's ONE project authorization primitive.
from src.auth.project_access import (
    ANY_ROLE,
    MODELER_BINDING_ROLES,
    require_project_role,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{project_id}/agent/personas",
    tags=["agent-personas"],
)

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

# Bug-1082 — canonical role spelling: "modeler" (not "modeller"). One home
# (src/auth/project_access.py); re-exported so this module's local name stays.
_MODELER_BINDING_ROLES = MODELER_BINDING_ROLES


async def _require_project_modeller(
    project_id: UUID, current_user: CurrentUser,
) -> None:
    """Modeller or admin role on the project (or a human tenant/system admin).

    Mirrors ``agent_config._require_project_modeller`` — persona mutation
    requires the same privilege tier as agent configuration mutation. Bug-8445:
    "mirrors" is now literal rather than aspirational; both are thin wrappers
    over the same primitive, so the two can no longer drift apart (they
    already had, on the admin-bypass axis: this copy admitted a canonical
    system admin and ``agent_config``'s did not).

    F-021-04 HARD CUTOVER (decision #9, Bug-9442): a binding-less project no
    longer bootstrap-opens — an ordinary caller with no binding is denied.
    """
    await require_project_role(
        project_id,
        current_user,
        db_factory=get_tenant_db,
        roles=_MODELER_BINDING_ROLES,
        detail="Modeller or Admin access required",
    )


async def _require_project_viewer(
    project_id: UUID, current_user: CurrentUser,
) -> None:
    """Any authenticated user with a binding on THIS project, or a human
    tenant/system admin. Read-only endpoints use this.

    Bug-8444 (second occurrence) — this gate used to accept ANY
    ``UserAccessBinding`` row for the caller anywhere in the tenant, with no
    ``project_id`` predicate, so a user bound only to project B could list
    and read project A's personas: their names, slugs, descriptions and the
    per-model measure/dimension scope restrictions that ARE the project's
    analytics governance model. Identical defect and identical fix to
    ``agent_config._require_project_viewer``; found by a fresh reviewer when
    the first enumeration pass covered ``agent_config.py``'s three gates but
    stopped short of this file's second gate.

    Bug-8445 (the structural fix that stops a sixth copy appearing) has now
    landed: there is exactly ONE binding lookup in this service, in
    ``src/auth/project_access.py``, and this is a named wrapper over it.

    F-021-04 HARD CUTOVER (decision #9, Bug-9442): a binding-less project no
    longer bootstrap-opens — an ordinary caller with no binding is denied.
    """
    await require_project_role(
        project_id,
        current_user,
        db_factory=get_tenant_db,
        roles=ANY_ROLE,
        detail="No project binding found",
    )


def _make_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:63]
    return slug or "persona"


class ModelScopeBody(BaseModel):
    model_id: UUID
    included_measure_ids: list[UUID] = Field(default_factory=list)
    included_dimension_ids: list[UUID] = Field(default_factory=list)


class PersonaCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: Optional[str] = Field(default=None, max_length=64)
    description: Optional[str] = None
    model_scopes: list[ModelScopeBody] = Field(default_factory=list)


class PersonaUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=255)
    description: Optional[str] = None
    model_scopes: Optional[list[ModelScopeBody]] = None


class ModelScopeResponse(BaseModel):
    model_config = {"from_attributes": True}

    model_id: UUID
    included_measure_ids: list
    included_dimension_ids: list


class PersonaResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    project_id: UUID
    name: str
    slug: str
    description: Optional[str] = None
    model_scopes: list[ModelScopeResponse] = Field(default_factory=list)


async def _ensure_refs_in_model(
    db,
    entity: type,
    *,
    ref_ids: list[UUID],
    model_id: UUID,
    field_name: str,
) -> None:
    """Prove every include-list id belongs to the scope's validated model.

    Measure and dimension ids are tenant-schema-wide. A database FK proves
    that they exist, not that they belong to the model named beside them in
    the request body. Unknown and foreign ids deliberately share one response
    so this guard is not a cross-project existence oracle.
    """
    requested = set(ref_ids)
    if not requested:
        return
    rows = (
        await db.execute(
            select(entity).where(
                entity.model_id == model_id,
                entity.id.in_(requested),
            )
        )
    ).scalars().all()
    missing = requested - {row.id for row in rows}
    if missing:
        shown = ", ".join(sorted(str(ref_id) for ref_id in missing))
        raise HTTPException(
            status_code=422,
            detail=(
                f"{field_name} contains references that are not in this "
                f"model: {shown}."
            ),
        )


async def _validate_model_scopes(
    db,
    scopes: list[ModelScopeBody],
    *,
    project_id: UUID,
) -> None:
    """Validate all persona body FKs before the first ORM mutation."""
    for index, scope in enumerate(scopes):
        prefix = f"model_scopes[{index}]"
        await ensure_ref_in_project(
            db,
            Model,
            ref_id=scope.model_id,
            project_id=project_id,
            field_name=f"{prefix}.model_id",
            required=True,
        )
        await _ensure_refs_in_model(
            db,
            Measure,
            ref_ids=scope.included_measure_ids,
            model_id=scope.model_id,
            field_name=f"{prefix}.included_measure_ids",
        )
        await _ensure_refs_in_model(
            db,
            Dimension,
            ref_ids=scope.included_dimension_ids,
            model_id=scope.model_id,
            field_name=f"{prefix}.included_dimension_ids",
        )


@router.get("", response_model=list[PersonaResponse])
async def list_personas(
    project_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[PersonaResponse]:
    await _require_project_viewer(project_id, current_user)
    async for db in get_tenant_db(current_user.tenant_id):
        q = await db.execute(
            select(ProjectPersona)
            .options(selectinload(ProjectPersona.model_scopes))
            .where(ProjectPersona.project_id == project_id)
            .order_by(ProjectPersona.name)
        )
        personas = q.scalars().all()
        return [_to_response(p) for p in personas]
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.post("", response_model=PersonaResponse, status_code=201)
async def create_persona(
    project_id: UUID,
    body: PersonaCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> PersonaResponse:
    await _require_project_modeller(project_id, current_user)
    async for db in get_tenant_db(current_user.tenant_id):
        slug = body.slug or _make_slug(body.name)
        if not _SLUG_RE.match(slug):
            raise HTTPException(
                status_code=422,
                detail=f"Invalid slug: '{slug}'. Must be lowercase alphanumeric with hyphens/underscores.",
            )

        # Bug-8949: prove every model/measure/dimension body reference before
        # adding or flushing the new persona.
        await _validate_model_scopes(
            db, body.model_scopes, project_id=project_id,
        )

        persona = ProjectPersona(
            project_id=project_id,
            name=body.name,
            slug=slug,
            description=body.description,
        )
        db.add(persona)
        await db.flush()

        for scope in body.model_scopes:
            db.add(ProjectPersonaModelScope(
                project_persona_id=persona.id,
                model_id=scope.model_id,
                included_measure_ids=[str(mid) for mid in scope.included_measure_ids],
                included_dimension_ids=[str(did) for did in scope.included_dimension_ids],
            ))

        await db.commit()
        await db.refresh(persona)

        q = await db.execute(
            select(ProjectPersona)
            .options(selectinload(ProjectPersona.model_scopes))
            .where(ProjectPersona.id == persona.id)
        )
        persona = q.scalar_one()
        return _to_response(persona)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.get("/{persona_id}", response_model=PersonaResponse)
async def get_persona(
    project_id: UUID,
    persona_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> PersonaResponse:
    await _require_project_viewer(project_id, current_user)
    async for db in get_tenant_db(current_user.tenant_id):
        q = await db.execute(
            select(ProjectPersona)
            .options(selectinload(ProjectPersona.model_scopes))
            .where(
                ProjectPersona.id == persona_id,
                ProjectPersona.project_id == project_id,
            )
        )
        persona = q.scalar_one_or_none()
        if persona is None:
            raise HTTPException(status_code=404, detail="Persona not found")
        return _to_response(persona)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.patch("/{persona_id}", response_model=PersonaResponse)
async def update_persona(
    project_id: UUID,
    persona_id: UUID,
    body: PersonaUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> PersonaResponse:
    await _require_project_modeller(project_id, current_user)
    async for db in get_tenant_db(current_user.tenant_id):
        q = await db.execute(
            select(ProjectPersona)
            .options(selectinload(ProjectPersona.model_scopes))
            .where(
                ProjectPersona.id == persona_id,
                ProjectPersona.project_id == project_id,
            )
        )
        persona = q.scalar_one_or_none()
        if persona is None:
            raise HTTPException(status_code=404, detail="Persona not found")

        # Bug-8949: validate the complete replacement before changing scalar
        # fields or deleting any existing scope row. The guard's SELECTs may
        # autoflush, so validation after mutation would still permit a refused
        # request to write pending state.
        if body.model_scopes is not None:
            await _validate_model_scopes(
                db, body.model_scopes, project_id=project_id,
            )

        if body.name is not None:
            persona.name = body.name
        if body.description is not None:
            persona.description = body.description

        if body.model_scopes is not None:
            for scope in list(persona.model_scopes):
                await db.delete(scope)
            await db.flush()

            for scope in body.model_scopes:
                db.add(ProjectPersonaModelScope(
                    project_persona_id=persona.id,
                    model_id=scope.model_id,
                    included_measure_ids=[str(mid) for mid in scope.included_measure_ids],
                    included_dimension_ids=[str(did) for did in scope.included_dimension_ids],
                ))

        await db.commit()

        q2 = await db.execute(
            select(ProjectPersona)
            .options(selectinload(ProjectPersona.model_scopes))
            .where(ProjectPersona.id == persona.id)
        )
        persona = q2.scalar_one()
        return _to_response(persona)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.delete("/{persona_id}", status_code=204)
async def delete_persona(
    project_id: UUID,
    persona_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    await _require_project_modeller(project_id, current_user)
    async for db in get_tenant_db(current_user.tenant_id):
        persona = await db.get(ProjectPersona, persona_id)
        if persona is None or persona.project_id != project_id:
            raise HTTPException(status_code=404, detail="Persona not found")
        await db.delete(persona)
        await db.commit()
        return
    raise HTTPException(status_code=500, detail="DB session exhausted")


def _to_response(persona: ProjectPersona) -> PersonaResponse:
    return PersonaResponse(
        id=persona.id,
        project_id=persona.project_id,
        name=persona.name,
        slug=persona.slug,
        description=persona.description,
        model_scopes=[
            ModelScopeResponse(
                model_id=s.model_id,
                included_measure_ids=s.included_measure_ids,
                included_dimension_ids=s.included_dimension_ids,
            )
            for s in persona.model_scopes
        ],
    )
