"""Eval harness HTTP surface — Phase C3.

POST /projects/{project_id}/agent/eval/run

Runs every allow-listed model's example_questions through the live
agent pipeline and returns a structured report with per-question
decomposition comparison. Expensive in LLM token spend and writes
cost-ledger rows per question (Bug-6334) — admin-gated.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from shared.db.session import get_tenant_db
from src.api.agent_config import _require_project_modeller
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.eval.runner import EvalReport, run_eval_for_project

router = APIRouter(prefix="/projects/{project_id}/agent/eval", tags=["agent-eval"])


class FieldComparisonOut(BaseModel):
    field_name: str
    matched: bool
    expected: Any = None
    actual: Any = None
    detail: Optional[str] = None
    # True when the field could not be compared (e.g. compound/expression
    # predicates in the plan) — visible in the diff, not a mismatch.
    skipped: bool = False


class DecompositionComparisonOut(BaseModel):
    matched: bool
    skipped: bool = False
    skip_reason: Optional[str] = None
    fields: Optional[list[FieldComparisonOut]] = None


class EvalRowOut(BaseModel):
    model_id: str
    question: str
    expected_decomposition: Optional[Any] = None
    status: str
    plan: Optional[dict] = None
    answer_text: Optional[str] = None
    error: Optional[str] = None
    decomposition_match: Optional[bool] = None
    decomposition_comparison: Optional[DecompositionComparisonOut] = None


class EvalReportOut(BaseModel):
    project_id: str
    total: int
    ok: int
    refused: int
    clarify: int
    error: int
    # Bug-6334 — populated when the run stopped early on an exhausted budget.
    budget_stopped: Optional[str] = None
    rows: list[EvalRowOut]
    accuracy_score: Optional[float] = None
    decomposition_regressions: int = 0
    decomposition_compared: int = 0
    decomposition_unparseable: int = 0
    # R0(c) — True when any question regressed from its expected
    # decomposition baseline. A client gating on eval accuracy must treat
    # this as a FAILED run regardless of the ok/refused/error counts.
    regressed: bool = False


def report_to_out(report: EvalReport) -> EvalReportOut:
    """Single serialisation home for EvalReport -> EvalReportOut.

    ``regressed`` is a computed property on the dataclass, so ``asdict``
    (fields only) omits it — it must be set explicitly here or the API
    would always report False. Tests exercise THIS helper so the endpoint
    and the pinned behaviour cannot drift apart.
    """
    return EvalReportOut(**asdict(report), regressed=report.regressed)


@router.post("/run", response_model=EvalReportOut)
async def run_eval(
    project_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> EvalReportOut:
    # Bug-6334 — the eval run spends LLM tokens against THIS project, so the
    # gate must be project-scoped, not tenant-wide. The previous local gate
    # accepted any ``admin``/``modeler`` binding anywhere in the tenant, letting
    # a modeller of project A spend project B's budget. Reuse the canonical
    # project-scoped modeller gate (accepts tenant_admin, or an admin/modeler
    # binding on this project or a tenant-wide null-project binding).
    await _require_project_modeller(project_id, current_user)
    jwt_token = current_user.raw_token
    async for db in get_tenant_db(current_user.tenant_id):
        try:
            report = await run_eval_for_project(db, project_id, jwt_token=jwt_token)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return report_to_out(report)
    raise HTTPException(status_code=500, detail="DB session exhausted")
