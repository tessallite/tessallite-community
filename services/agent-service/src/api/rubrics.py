"""Judge rubric CRUD (Phase Agent-B4.1).

A rubric is a project-scoped, versioned list of `sections` — each
section is a free-form heading + bullet list the judge LLM is told to
score against. The schema is intentionally loose JSON so authors can
iterate without a migration; the judge module renders it into prompt
text at evaluation time.

Endpoints:
  GET    /projects/{project_id}/agent/rubrics
  POST   /projects/{project_id}/agent/rubrics
  GET    /projects/{project_id}/agent/rubrics/{rubric_id}
  PUT    /projects/{project_id}/agent/rubrics/{rubric_id}
  DELETE /projects/{project_id}/agent/rubrics/{rubric_id}
  POST   /projects/{project_id}/agent/rubrics/bulk-import
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from shared.db.models import AgentJudgeRubric
from shared.db.session import get_tenant_db
from src.auth.middleware import CurrentUser, forbid_embed_user

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/projects/{project_id}/agent/rubrics",
    tags=["agent-rubrics"],
)


class RubricBody(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    sections: list[dict[str, Any]] = Field(default_factory=list)


class RubricResponse(RubricBody):
    model_config = ConfigDict(from_attributes=True)

    id: UUID


def _serialise(record: AgentJudgeRubric) -> RubricResponse:
    return RubricResponse(
        id=record.id,
        name=record.name,
        sections=list(record.sections or []),
    )


@router.get("", response_model=list[RubricResponse])
async def list_rubrics(
    project_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[RubricResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        rows = await db.execute(
            select(AgentJudgeRubric)
            .where(AgentJudgeRubric.project_id == project_id)
            .order_by(AgentJudgeRubric.name)
        )
        return [_serialise(r) for r in rows.scalars().all()]
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.post("", response_model=RubricResponse, status_code=201)
async def create_rubric(
    project_id: UUID,
    body: RubricBody,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> RubricResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        record = AgentJudgeRubric(
            project_id=project_id,
            name=body.name,
            sections=list(body.sections),
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)
        return _serialise(record)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.get("/{rubric_id}", response_model=RubricResponse)
async def get_rubric(
    project_id: UUID,
    rubric_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> RubricResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        record = await db.get(AgentJudgeRubric, rubric_id)
        if record is None or record.project_id != project_id:
            raise HTTPException(status_code=404, detail="Rubric not found")
        return _serialise(record)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.put("/{rubric_id}", response_model=RubricResponse)
async def update_rubric(
    project_id: UUID,
    rubric_id: UUID,
    body: RubricBody,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> RubricResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        record = await db.get(AgentJudgeRubric, rubric_id)
        if record is None or record.project_id != project_id:
            raise HTTPException(status_code=404, detail="Rubric not found")
        record.name = body.name
        record.sections = list(body.sections)
        await db.commit()
        await db.refresh(record)
        return _serialise(record)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.delete("/{rubric_id}", status_code=204)
async def delete_rubric(
    project_id: UUID,
    rubric_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        record = await db.get(AgentJudgeRubric, rubric_id)
        if record is None or record.project_id != project_id:
            raise HTTPException(status_code=404, detail="Rubric not found")
        await db.delete(record)
        await db.commit()


class BulkImportBody(BaseModel):
    rubrics: list[RubricBody]


@router.post("/bulk-import", response_model=list[RubricResponse])
async def bulk_import_rubrics(
    project_id: UUID,
    body: BulkImportBody,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[RubricResponse]:
    """Replace-by-name import. A row whose name matches an existing rubric
    in this project gets updated; new names get inserted. Other rubrics
    are left untouched."""
    async for db in get_tenant_db(current_user.tenant_id):
        rows = await db.execute(
            select(AgentJudgeRubric).where(
                AgentJudgeRubric.project_id == project_id
            )
        )
        by_name = {r.name: r for r in rows.scalars().all()}
        out: list[AgentJudgeRubric] = []
        for incoming in body.rubrics:
            existing = by_name.get(incoming.name)
            if existing is None:
                created = AgentJudgeRubric(
                    project_id=project_id,
                    name=incoming.name,
                    sections=list(incoming.sections),
                )
                db.add(created)
                out.append(created)
            else:
                existing.sections = list(incoming.sections)
                out.append(existing)
        await db.commit()
        for r in out:
            await db.refresh(r)
        return [_serialise(r) for r in out]
    raise HTTPException(status_code=500, detail="DB session exhausted")
