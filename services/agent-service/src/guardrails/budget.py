"""Agent budget and complexity guardrails.

Checks run before the LLM call (budget) and before query dispatch
(complexity). When a limit is exceeded the pipeline returns a refusal
without consuming tokens.

Cost ledger entries are written after a successful turn via
``record_turn_cost()``.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import AgentCostEntry, ProjectAgentConfig

logger = logging.getLogger(__name__)

_DEFAULT_COST_PER_1K: dict[str, dict[str, float]] = {
    "anthropic": {"input": 0.003, "output": 0.015},
    "openai": {"input": 0.005, "output": 0.015},
    # Keys must match the LLMProviderConfig.provider string. Google configs
    # store provider="google" (not "gemini"); deepseek/glm route through the
    # OpenAI-compatible adapter and report their own provider names.
    "gemini": {"input": 0.001, "output": 0.002},
    "google": {"input": 0.001, "output": 0.002},
    "deepseek": {"input": 0.00027, "output": 0.0011},
    "glm": {"input": 0.0006, "output": 0.0022},
}

# Bug-1103 — a provider absent from the cost table must NEVER silently zero the
# ledger (which would let daily_cost_budget_usd stop enforcing for any
# unmapped/mis-typed/future provider). When a provider is unlisted we estimate
# its spend with a conservative non-zero fallback rate (the highest known rate,
# so we never under-charge an unknown model) and surface a durable operator
# warning. The fallback rate is configurable via the "_fallback" key in
# config/llm_costs.json; the built-in default mirrors the priciest known model
# so an unmapped provider is over-, never under-, counted.
_FALLBACK_PROVIDER_KEY = "_fallback"
_DEFAULT_FALLBACK_PER_1K: dict[str, float] = {"input": 0.005, "output": 0.015}

_COST_CONFIG_PATH = os.environ.get(
    "LLM_COSTS_CONFIG",
    str(Path(__file__).resolve().parent.parent.parent / "config" / "llm_costs.json"),
)


def _load_cost_table() -> dict[str, dict[str, float]]:
    """Load cost rates from config file, fall back to built-in defaults."""
    try:
        with open(_COST_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        table: dict[str, dict[str, float]] = {}
        for provider, rates in data.items():
            table[provider] = {
                "input": float(rates.get("input_per_1k", 0)),
                "output": float(rates.get("output_per_1k", 0)),
            }
        return table
    except FileNotFoundError:
        return dict(_DEFAULT_COST_PER_1K)
    except Exception as exc:
        logger.warning("Failed to load LLM cost config from %s: %s", _COST_CONFIG_PATH, exc)
        return dict(_DEFAULT_COST_PER_1K)


_COST_PER_1K: dict[str, dict[str, float]] = _load_cost_table()


def _resolve_fallback_rate() -> dict[str, float]:
    """The rate applied to providers absent from the cost table (Bug-1103).

    Sourced from the "_fallback" key in the config file when present; falls
    back to the built-in default. The "_fallback" pseudo-provider is removed
    from the active rate table so it can never be selected by name.
    """
    cfg_fallback = _COST_PER_1K.pop(_FALLBACK_PROVIDER_KEY, None)
    if cfg_fallback and (cfg_fallback.get("input") or cfg_fallback.get("output")):
        return {
            "input": float(cfg_fallback.get("input", 0.0)),
            "output": float(cfg_fallback.get("output", 0.0)),
        }
    return dict(_DEFAULT_FALLBACK_PER_1K)


_FALLBACK_PER_1K: dict[str, float] = _resolve_fallback_rate()

logger.info("LLM cost table loaded with providers: %s", sorted(_COST_PER_1K.keys()))
for _default_provider in _DEFAULT_COST_PER_1K:
    if _default_provider not in _COST_PER_1K:
        logger.warning(
            "Built-in provider '%s' has no entry in cost config — "
            "the unmapped-provider fallback rate will apply",
            _default_provider,
        )

_warned_providers: set[str] = set()


def estimate_cost_usd(provider: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost for a turn.

    Bug-1103 — an unmapped provider is charged at the conservative fallback
    rate, never silently zeroed, so a budget can never be bypassed by an
    unlisted/mis-typed/future provider string. A durable (de-duplicated)
    warning is logged so operators can add the missing rate.
    """
    rates = _COST_PER_1K.get(provider)
    if not rates:
        # Atomic check-and-add: set.add is a single operation; check length
        # change to determine if this provider was already warned about.
        prev_len = len(_warned_providers)
        _warned_providers.add(provider)
        if len(_warned_providers) > prev_len:
            logger.warning(
                "No cost rates for provider %r — applying fallback rate "
                "(input=%.5f, output=%.5f per 1k). Add this provider to "
                "config/llm_costs.json to cost it accurately.",
                provider,
                _FALLBACK_PER_1K["input"],
                _FALLBACK_PER_1K["output"],
            )
        rates = _FALLBACK_PER_1K
    in_rate = rates.get("input", 0.0)
    out_rate = rates.get("output", 0.0)
    return (input_tokens * in_rate + output_tokens * out_rate) / 1000.0


async def _today_usage(
    db: AsyncSession, project_id: UUID
) -> tuple[int, float]:
    """Return (total_tokens, total_cost_usd) for today UTC for this project."""
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    result = await db.execute(
        select(
            func.coalesce(func.sum(AgentCostEntry.input_tokens), 0).label("in_tok"),
            func.coalesce(func.sum(AgentCostEntry.output_tokens), 0).label("out_tok"),
            func.coalesce(
                func.sum(AgentCostEntry.estimated_cost_usd), 0.0
            ).label("cost"),
        ).where(
            AgentCostEntry.project_id == project_id,
            AgentCostEntry.created_at >= today_start,
        )
    )
    row = result.one()
    total_tokens = int(row.in_tok) + int(row.out_tok)
    return total_tokens, float(row.cost)


async def check_budget(
    db: AsyncSession, cfg: ProjectAgentConfig
) -> Optional[str]:
    """Return a refusal reason string if budget is exceeded, else None.

    Checks daily_token_budget and daily_cost_budget_usd.  0 means no limit.
    """
    if cfg.daily_token_budget <= 0 and cfg.daily_cost_budget_usd <= 0:
        return None

    try:
        total_tokens, total_cost = await _today_usage(db, cfg.project_id)
    except Exception:
        logger.exception("Budget check DB query failed — allowing turn through")
        return None

    if cfg.daily_token_budget > 0 and total_tokens >= cfg.daily_token_budget:
        logger.info(
            "Token budget exceeded for project %s: %d >= %d",
            cfg.project_id,
            total_tokens,
            cfg.daily_token_budget,
        )
        return "daily_token_budget_exceeded"

    if cfg.daily_cost_budget_usd > 0 and total_cost >= float(cfg.daily_cost_budget_usd):
        logger.info(
            "Cost budget exceeded for project %s: %.4f >= %.4f",
            cfg.project_id,
            total_cost,
            float(cfg.daily_cost_budget_usd),
        )
        return "daily_cost_budget_exceeded"

    return None


def check_query_complexity(cfg: ProjectAgentConfig, call: object) -> Optional[str]:
    """Return a refusal reason if the query exceeds max_query_complexity.

    Complexity = len(measures) + len(dimensions) + len(filters).
    0 means no limit.
    """
    if cfg.max_query_complexity <= 0:
        return None

    measures = getattr(call, "measures", []) or []
    dimensions = getattr(call, "dimensions", []) or []
    where = getattr(call, "where", []) or []
    having = getattr(call, "having", []) or []
    sort = getattr(call, "sort", []) or []
    complexity = len(measures) + len(dimensions) + len(where) + len(having) + len(sort)
    if complexity > cfg.max_query_complexity:
        logger.info(
            "Query complexity %d exceeds limit %d for project %s",
            complexity,
            cfg.max_query_complexity,
            cfg.project_id,
        )
        return "query_too_complex"
    return None


async def record_turn_cost(
    db: AsyncSession,
    project_id: UUID,
    turn_id: Optional[UUID],
    llm_config_id: Optional[UUID],
    provider: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Write a ledger row. Errors are logged but never raised.

    Bug-1103 — the cost is always written as a concrete number, never NULL.
    estimate_cost_usd applies a fallback rate for unmapped providers, so spend
    is never silently lost from the budget sum; a genuine 0-token turn writes a
    real 0.0 (still non-NULL), keeping daily_cost_budget_usd enforceable.
    """
    try:
        cost = estimate_cost_usd(provider, input_tokens, output_tokens)
        entry = AgentCostEntry(
            id=uuid.uuid4(),
            project_id=project_id,
            turn_id=turn_id,
            llm_config_id=llm_config_id,
            # F-023-28 — persist the provider the spend was costed against so
            # the /cost report can split per provider from data. Empty provider
            # strings are normalised to None ("unknown" in reporting).
            provider=(provider or None),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=cost,
        )
        db.add(entry)
    except Exception:
        logger.exception("Failed to write agent cost ledger entry")


async def check_budget_post_turn(
    db: AsyncSession,
    cfg: ProjectAgentConfig,
    turn_input_tokens: int,
    turn_output_tokens: int,
    provider: str,
) -> Optional[str]:
    """Bug-5283 — post-turn budget check so one expensive request that
    sneaks past the pre-turn gate is detected after recording its cost.

    Returns the exceeded budget reason string, or None if still within
    limits.  The caller (persist_turn) appends a guardrail warning action
    so the audit trail records that this turn pushed the budget over.
    """
    if cfg.daily_token_budget <= 0 and cfg.daily_cost_budget_usd <= 0:
        return None

    try:
        total_tokens, total_cost = await _today_usage(db, cfg.project_id)
    except Exception:
        logger.exception("Post-turn budget check DB query failed")
        return None

    # Add the current turn's spend (which may not yet be committed)
    turn_cost = estimate_cost_usd(provider, turn_input_tokens, turn_output_tokens)
    total_tokens += turn_input_tokens + turn_output_tokens
    total_cost += turn_cost

    if cfg.daily_token_budget > 0 and total_tokens >= cfg.daily_token_budget:
        logger.warning(
            "Post-turn: token budget exceeded for project %s: %d >= %d",
            cfg.project_id, total_tokens, cfg.daily_token_budget,
        )
        return "daily_token_budget_exceeded"

    if cfg.daily_cost_budget_usd > 0 and total_cost >= float(cfg.daily_cost_budget_usd):
        logger.warning(
            "Post-turn: cost budget exceeded for project %s: %.4f >= %.4f",
            cfg.project_id, total_cost, float(cfg.daily_cost_budget_usd),
        )
        return "daily_cost_budget_exceeded"

    return None
