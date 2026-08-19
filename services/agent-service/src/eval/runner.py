"""Walk every allow-listed model's example_questions and exercise the
agent pipeline against each one. Returns a structured report — counts
of ok/refused/error plus per-row plan + status, AND a per-question
decomposition comparison against the expected_decomposition baseline."""
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
from src.eval.comparison import (
    DecompositionComparison,
    compare_decomposition,
)
from src.guardrails.budget import check_budget, record_turn_cost
from src.pipeline import run_turn

logger = logging.getLogger(__name__)


@dataclass
class EvalRow:
    model_id: str
    question: str
    expected_decomposition: Optional[Any]
    status: str
    plan: Optional[dict[str, Any]] = None
    answer_text: Optional[str] = None
    error: Optional[str] = None
    decomposition_match: Optional[bool] = None
    decomposition_comparison: Optional[dict[str, Any]] = None


@dataclass
class EvalReport:
    project_id: str
    total: int = 0
    ok: int = 0
    refused: int = 0
    clarify: int = 0
    error: int = 0
    # Bug-6334 — set when the run stopped early because the project's daily
    # token/cost budget was exhausted. Eval spend is real LLM spend and is now
    # both metered against and recorded in the same ledger as chat turns.
    budget_stopped: Optional[str] = None
    rows: list[EvalRow] = field(default_factory=list)
    # Decomposition accuracy: fraction of questions with expected
    # decompositions that matched the actual plan (0.0-1.0).  None when
    # no questions carried an expected decomposition.
    accuracy_score: Optional[float] = None
    decomposition_regressions: int = 0
    # Number of questions whose expected decomposition was actually
    # compared (parseable baseline present).
    decomposition_compared: int = 0
    # Questions that carried an expected decomposition which could not be
    # parsed into comparable fields. Surfaced so a gate can distinguish
    # "clean run" from "gate silently off because baselines are prose".
    decomposition_unparseable: int = 0

    @property
    def regressed(self) -> bool:
        """R0(c) acceptance signal: True when any question with an
        expected decomposition failed to match the actual plan.

        Each ``expected_decomposition`` is the question's baseline plan, so
        a single mismatch is a regression. Callers (CI runner, admin UI,
        the accuracy-wave gate) MUST treat ``regressed is True`` as a FAILED
        eval run — a green status count is never sufficient evidence, since
        a change that silently swaps a measure or drops a filter still
        produces status "ok". This is the gate the accuracy wave depends on."""
        return self.decomposition_regressions > 0


def _comparison_to_dict(comp: DecompositionComparison) -> dict[str, Any]:
    """Serialise a DecompositionComparison to a JSON-safe dict."""
    result: dict[str, Any] = {
        "matched": comp.matched,
        "skipped": comp.skipped,
    }
    if comp.skip_reason:
        result["skip_reason"] = comp.skip_reason
    if comp.fields:
        result["fields"] = [
            {
                "field_name": f.field_name,
                "matched": f.matched,
                "expected": f.expected,
                "actual": f.actual,
                "detail": f.detail,
                "skipped": f.skipped,
            }
            for f in comp.fields
        ]
    return result


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
    # Track decomposition match stats separately so accuracy_score is
    # computed only over questions that carry an expected decomposition.
    decomp_total = 0
    decomp_matched = 0

    for ctx in contexts:
        questions = ctx.example_questions or []
        for q in questions:
            if not isinstance(q, dict):
                continue
            text = (q.get("q") or "").strip()
            decomp = q.get("decomposition")
            if not text:
                continue

            # Bug-6334 — meter eval turns against the daily budget, exactly like
            # chat turns. Eval spend is real LLM spend; without this an admin
            # could exhaust (or blow past) the project budget by looping the
            # harness. Fail CLOSED: check_budget returns a reason on a DB error
            # too, so we stop rather than spend blind.
            budget_reason = await check_budget(db, cfg)
            if budget_reason:
                report.budget_stopped = budget_reason
                logger.info(
                    "Eval for project %s stopped early: %s", project_id, budget_reason
                )
                _finalise_accuracy(report, decomp_total, decomp_matched)
                return report

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
                # Bug-6334 — record the eval turn's LLM spend in the same ledger
                # (AgentCostEntry) chat turns use, so daily_token_budget /
                # daily_cost_budget_usd enforcement and the /cost report see it.
                # turn_id is null (eval never persists AgentTurn rows).
                await record_turn_cost(
                    db=db,
                    project_id=project_id,
                    turn_id=None,
                    llm_config_id=cfg.answer_llm_config_id,
                    provider=outcome.provider or "",
                    input_tokens=outcome.usage_input_tokens,
                    output_tokens=outcome.usage_output_tokens,
                )
                await db.commit()
            except Exception as exc:
                logger.exception("Eval row failed")
                await db.rollback()
                row.status = "error"
                row.error = str(exc)

            # --- Decomposition comparison ---
            # context_model_id: each example question belongs to ONE model
            # context, which is the authoritative baseline model — the
            # plan's model_id is a UUID that can never string-match an
            # authored model name. turn_status: a refused/clarify/error
            # turn on a baselined question is a regression even when the
            # rejected plan text matches.
            comparison = compare_decomposition(
                decomp,
                row.plan,
                context_model_id=str(ctx.model_id),
                turn_status=row.status,
            )
            if not comparison.skipped:
                row.decomposition_match = comparison.matched
                row.decomposition_comparison = _comparison_to_dict(comparison)
                decomp_total += 1
                if comparison.matched:
                    decomp_matched += 1
                else:
                    report.decomposition_regressions += 1
            else:
                # Skipped: either no expected decomposition was authored
                # (not counted), or one was authored but is unparseable —
                # surfaced loudly so the gate is never silently off.
                row.decomposition_comparison = _comparison_to_dict(comparison)
                if comparison.skip_reason == "unparseable_decomposition":
                    report.decomposition_unparseable += 1
                    logger.warning(
                        "Eval question %r (model %s) has an expected"
                        " decomposition that could not be parsed for"
                        " comparison: %r",
                        text,
                        ctx.model_id,
                        decomp,
                    )

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

    _finalise_accuracy(report, decomp_total, decomp_matched)
    return report


def _finalise_accuracy(
    report: EvalReport,
    decomp_total: int,
    decomp_matched: int,
) -> None:
    """Compute and set the accuracy_score on the report."""
    report.decomposition_compared = decomp_total
    if decomp_total > 0:
        report.accuracy_score = round(decomp_matched / decomp_total, 4)
    else:
        report.accuracy_score = None
