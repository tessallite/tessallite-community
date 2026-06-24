"""Eval harness HTTP surface — Phase C3.

POST /projects/{project_id}/agent/eval/run

Runs every allow-listed model's example_questions through the live
agent pipeline and returns a structured report. Cheap to invoke (no DB
writes), but expensive in LLM token spend — admin-gated.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from shared.db.models import UserAccessBinding
from shared.db.session import get_tenant_db
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.eval.runner import run_eval_for_project

router = APIRouter(prefix="/projects/{project_id}/agent/eval", tags=["agent-eval"])



class EvalRowOut(BaseModel):
    model_id: str
    question: str
    expected_decomposition: Optional[str]
    status: str
    plan: Optional[dict] = None
    answer_text: Optional[str] = None
    error: Optional[str] = None


class EvalReportOut(BaseModel):
    project_id: str
    total: int
    ok: int
    refused: int
    clarify: int
    error: int
    rows: list[EvalRowOut]


async def _require_modeller(current_user: CurrentUser) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        result = await db.execute(
            select(UserAccessBinding).where(
                UserAccessBinding.user_identity == current_user.user_id,
                # Bug-1082 — canonical role spelling only ("modeler"); the
                # interim accept-both was removed after confirming no tenant
                # schema stores a "modeller" binding.
                UserAccessBinding.role.in_(("admin", "modeler")),
            ).limit(1)
        )
        if result.scalar_one_or_none() is not None:
            return
        any_binding = await db.execute(select(UserAccessBinding).limit(1))
        if any_binding.scalar_one_or_none() is None:
            return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Modeller or Admin access required",
    )


@router.post("/run", response_model=EvalReportOut)
async def run_eval(
    project_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> EvalReportOut:
    await _require_modeller(current_user)
    jwt_token = current_user.raw_token
    async for db in get_tenant_db(current_user.tenant_id):
        try:
            report = await run_eval_for_project(db, project_id, jwt_token=jwt_token)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return EvalReportOut(**asdict(report))
    raise HTTPException(status_code=500, detail="DB session exhausted")
