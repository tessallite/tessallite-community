"""Walk every allow-listed model's example_questions and exercise the
agent pipeline against each one. Returns a structured report — counts
of ok/refused/error plus per-row plan + status."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    AgentConversation,
    ProjectAgentConfig,
    ProjectAgentModel,
    ProjectAgentModelContext,
)
from src.pipeline import run_turn

logger = logging.getLogger(__name__)


@dataclass
class EvalRow:
    model_id: str
    question: str
    expected_decomposition: Optional[str]
    status: str
    plan: Optional[dict[str, Any]] = None
    answer_text: Optional[str] = None
    error: Optional[str] = None


@dataclass
class EvalReport:
    project_id: str
    total: int = 0
    ok: int = 0
    refused: int = 0
    clarify: int = 0
    error: int = 0
    rows: list[EvalRow] = field(default_factory=list)


async def run_eval_for_project(
    db: AsyncSession,
    project_id: UUID,
    jwt_token: str = "",
) -> EvalReport:
    cfg_q = await db.execute(
        select(ProjectAgentConfig).where(
            ProjectAgentConfig.project_id == project_id
        )
    )
    cfg = cfg_q.scalar_one_or_none()
    if cfg is None:
        raise ValueError(f"Project {project_id} has no agent config.")

    allow_q = await db.execute(
        select(ProjectAgentModel.model_id).where(
            ProjectAgentModel.project_id == project_id
        )
    )
    allow_ids = [row[0] for row in allow_q.all()]
    if not allow_ids:
        return EvalReport(project_id=str(project_id))

    ctx_q = await db.execute(
        select(ProjectAgentModelContext).where(
            ProjectAgentModelContext.project_id == project_id,
            ProjectAgentModelContext.model_id.in_(allow_ids),
        )
    )
    contexts = list(ctx_q.scalars().all())

    # Build a transient conversation so the prompt assembler has a header.
    conv = AgentConversation(
        id=uuid4(),
        project_id=project_id,
        caller_kind="eval",
        caller_ref="eval-harness",
    )

    report = EvalReport(project_id=str(project_id))
    for ctx in contexts:
        questions = ctx.example_questions or []
        for q in questions:
            if not isinstance(q, dict):
                continue
            text = (q.get("q") or "").strip()
            decomp = q.get("decomposition")
            if not text:
                continue
            row = EvalRow(
                model_id=str(ctx.model_id),
                question=text,
                expected_decomposition=decomp,
                status="error",
            )
            try:
                outcome = await run_turn(
                    db=db,
                    cfg=cfg,
                    conversation=conv,
                    user_message=text,
                    jwt_token=jwt_token,
                )
                row.status = outcome.status
                row.plan = outcome.plan
                row.answer_text = outcome.answer_text
            except Exception as exc:
                logger.exception("Eval row failed")
                row.status = "error"
                row.error = str(exc)

            report.total += 1
            report.rows.append(row)
            if row.status == "ok":
                report.ok += 1
            elif row.status == "refused":
                report.refused += 1
            elif row.status == "clarify":
                report.clarify += 1
            else:
                report.error += 1

    return report
