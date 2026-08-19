"""Agent budget and complexity guardrails.

Checks run before the LLM call (budget) and before query dispatch
(complexity). When a limit is exceeded the pipeline returns a refusal
without consuming tokens.

Cost ledger entries are written after a successful turn via
``record_turn_cost()``.

Bug-5755 — multi-replica safety note: the module-level state in this
file (_COST_PER_1K, _FALLBACK_PER_1K, _warned_providers) is all
per-process and read-only after initialization (except _warned_providers
which is append-only for log deduplication). All budget enforcement
reads from the database (AgentCostEntry rows), so it is inherently
safe across multiple replicas. _warned_providers only controls whether
a log line is emitted; each replica independently logs the first
occurrence per unmapped provider.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from uuid import UUID

from sqlalchemy import ColumnElement, and_, delete as sa_delete, func, select
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

# Bug-5755 — this set deduplicates log warnings for unmapped providers.
# It is per-process (each replica has its own copy) and does NOT affect
# functional behaviour: the fallback rate is applied regardless of whether
# the warning has been emitted. In a multi-replica deployment each replica
# independently logs the first occurrence per provider, which is the
# correct and expected behaviour. No cross-replica state is needed.
#
# Bug-7351 — pre-seed with providers that are missing from the cost config
# so the startup warning (below) and the runtime warning (in
# estimate_cost_usd) do not duplicate for the same provider.
_warned_providers: set[str] = set()

for _default_provider in _DEFAULT_COST_PER_1K:
    if _default_provider not in _COST_PER_1K:
        logger.warning(
            "Built-in provider '%s' has no entry in cost config — "
            "the unmapped-provider fallback rate will apply",
            _default_provider,
        )
        _warned_providers.add(_default_provider)


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


# ---------------------------------------------------------------------------
# Reservation identity and orphan detection
#
# ``reserve_budget`` writes a pessimistic estimate row and
# ``reconcile_budget_reservation`` deletes it. Every CODE exit path reconciles,
# but a process death between the two (SIGKILL, OOM, container restart,
# uvicorn's post-grace-period task cancellation) leaves the estimate row behind
# forever. Such an ORPHAN is not spend: it charges ~8096 tokens and the
# fallback USD rate against the project's daily budget until UTC midnight, and
# it inflates the /cost per-provider report for the whole lookback window with
# money nobody spent.
#
# The row is stamped with a reserved provider string so an orphan is identified
# EXACTLY. The obvious alternative — inferring the shape from "turn_id AND
# llm_config_id are both NULL" — is unsafe: the eval runner deliberately writes
# ``turn_id=None`` (eval never persists AgentTurn rows) and
# ``llm_config_id=cfg.answer_llm_config_id``, which is a nullable column. A
# project whose ``answer_llm_config_id`` is NULL would have its real eval spend
# classified as an orphan and dropped from the budget sum — a budget BYPASS.
#
# The sentinel cannot collide with real spend: ``record_turn_cost`` only runs
# after a successful LLM call, and ``shared/llm/adapter.build_adapter`` raises
# ``Unknown LLM provider`` for anything outside
# {openai, deepseek, glm, ollama, google, anthropic}. No turn can complete
# carrying this provider string, whatever a tenant admin types into the
# (unconstrained) ``LLMProviderConfig.provider`` column.
# ---------------------------------------------------------------------------
_RESERVATION_PROVIDER = "__reservation__"

_DEFAULT_RESERVATION_MAX_AGE_MINUTES = 15

# Suppress the unmapped-provider warning for the sentinel: a reservation is
# deliberately costed at the conservative fallback rate, which is exactly the
# behaviour that warning exists to flag for REAL providers.
_warned_providers.add(_RESERVATION_PROVIDER)


def _reservation_max_age_minutes() -> int:
    """Age past which an unreconciled reservation is presumed orphaned.

    A live turn's reservation must keep counting against the budget for the
    whole turn — that is the point of Bug-7366 — so this bound has to exceed
    the slowest legitimate turn. 15 minutes is far beyond any observed turn
    (LLM call + query execution) while still releasing a crashed turn's hold
    the same hour rather than at UTC midnight.

    Overridable with AGENT_RESERVATION_MAX_AGE_MINUTES. Read per call so a
    deployment can change it without a code change; a non-numeric or
    non-positive value falls back to the default rather than disabling
    detection.
    """
    raw = os.environ.get("AGENT_RESERVATION_MAX_AGE_MINUTES")
    if raw is None:
        return _DEFAULT_RESERVATION_MAX_AGE_MINUTES
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "AGENT_RESERVATION_MAX_AGE_MINUTES=%r is not an integer — "
            "using the default of %d minutes",
            raw, _DEFAULT_RESERVATION_MAX_AGE_MINUTES,
        )
        return _DEFAULT_RESERVATION_MAX_AGE_MINUTES
    if value <= 0:
        logger.warning(
            "AGENT_RESERVATION_MAX_AGE_MINUTES=%d is not positive — "
            "using the default of %d minutes",
            value, _DEFAULT_RESERVATION_MAX_AGE_MINUTES,
        )
        return _DEFAULT_RESERVATION_MAX_AGE_MINUTES
    return value


def is_reservation_row() -> ColumnElement[bool]:
    """SQL predicate: this ledger row is a pessimistic reservation, not spend.

    NULL-safe by construction. A plain ``provider == sentinel`` yields NULL for
    the rows whose provider is NULL (pre-migration rows, and the empty-string
    writes ``record_turn_cost`` normalises to None); negating that NULL drops
    those rows from the WHERE clause entirely, which would silently delete real
    spend from both the budget sum and the cost report. ``IS NOT DISTINCT
    FROM`` is two-valued, so the negation is well-defined for every row.
    """
    return AgentCostEntry.provider.is_not_distinct_from(_RESERVATION_PROVIDER)


def is_orphaned_reservation(now: Optional[datetime] = None) -> ColumnElement[bool]:
    """SQL predicate: this row is a reservation whose owner never reconciled it."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(
        minutes=_reservation_max_age_minutes()
    )
    return and_(is_reservation_row(), AgentCostEntry.created_at < cutoff)


async def _today_usage(
    db: AsyncSession,
    project_id: UUID,
    exclude_reservation_id: Optional[UUID] = None,
) -> tuple[int, float]:
    """Return (total_tokens, total_cost_usd) for today UTC for this project.

    Bug-7777 — when ``exclude_reservation_id`` is provided, that single row
    is excluded from the sum so the pessimistic reservation written by
    ``reserve_budget`` does not count against the budget check that runs
    inside the same turn.  Without this exclusion, a project whose
    ``daily_token_budget`` is at or below the reservation estimate (8096
    tokens) refuses every turn including the first on a fresh day.

    An ORPHANED reservation (see ``is_orphaned_reservation``) is excluded too.
    A LIVE reservation still counts — holding budget for the duration of a turn
    is the whole point of Bug-7366 — but one whose owning process died can
    never be reconciled, and leaving it in the sum lets a single crash eat the
    project's allowance until UTC midnight. A restart under load strands one
    per in-flight turn, which is enough to refuse every subsequent turn for the
    rest of the day on a modest ``daily_token_budget``.
    """
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    query = select(
        func.coalesce(func.sum(AgentCostEntry.input_tokens), 0).label("in_tok"),
        func.coalesce(func.sum(AgentCostEntry.output_tokens), 0).label("out_tok"),
        func.coalesce(
            func.sum(AgentCostEntry.estimated_cost_usd), 0.0
        ).label("cost"),
    ).where(
        AgentCostEntry.project_id == project_id,
        AgentCostEntry.created_at >= today_start,
    )
    if exclude_reservation_id is not None:
        query = query.where(AgentCostEntry.id != exclude_reservation_id)
    query = query.where(~is_orphaned_reservation())
    result = await db.execute(query)
    row = result.one()
    total_tokens = int(row.in_tok) + int(row.out_tok)
    return total_tokens, float(row.cost)


async def check_budget(
    db: AsyncSession,
    cfg: ProjectAgentConfig,
    exclude_reservation_id: Optional[UUID] = None,
) -> Optional[str]:
    """Return a refusal reason string if budget is exceeded, else None.

    Checks daily_token_budget and daily_cost_budget_usd.  0 means no limit.

    Bug-7777 — when ``exclude_reservation_id`` is provided, that row is
    excluded from the usage sum so the pessimistic reservation written by
    ``reserve_budget`` does not count against the budget check that runs
    inside the same turn.  Without this, projects with small budgets
    (daily_token_budget <= 8096) brick on every turn because the reservation
    itself exceeds the limit before the LLM call even starts.
    """
    if cfg.daily_token_budget <= 0 and cfg.daily_cost_budget_usd <= 0:
        return None

    try:
        total_tokens, total_cost = await _today_usage(
            db, cfg.project_id,
            exclude_reservation_id=exclude_reservation_id,
        )
    except Exception:
        # Bug-5754 — fail CLOSED: deny the request when we cannot verify
        # whether the budget is exhausted. A DB error must never silently
        # bypass spending limits.
        logger.exception("Budget check DB query failed — denying turn (fail-closed)")
        return "budget_check_unavailable"

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


# Bug-7366 -- pessimistic budget reservation.
_RESERVATION_INPUT_ESTIMATE = 4000


async def reserve_budget(
    tenant_id: str,
    project_id: UUID,
    provider: str,
    max_output_tokens: int,
) -> Optional[UUID]:
    """Write a pessimistic budget reservation in its own committed transaction.

    Bug-7366 -- the daily budget check is a non-atomic read-then-spend
    sequence: concurrent turns all observe the same remaining allowance.
    This writes an estimated cost row in a SHORT, COMMITTED transaction so
    concurrent requests see the reservation via ``_today_usage``.  The
    caller must ensure ``reconcile_budget_reservation`` is called on every
    exit path (success, failure, crash recovery).

    The row is stamped with the reserved ``_RESERVATION_PROVIDER`` string so a
    reservation the owning process never reconciled is identifiable — see the
    "Reservation identity" block above. ``cost`` is still estimated from the
    CALLER's ``provider``, so the amount held is unchanged.
    """
    from shared.db.session import get_tenant_db

    estimated_input = _RESERVATION_INPUT_ESTIMATE
    estimated_output = max(max_output_tokens, 500)
    cost = estimate_cost_usd(provider, estimated_input, estimated_output)
    reservation_id = uuid.uuid4()
    try:
        async for res_db in get_tenant_db(tenant_id):
            entry = AgentCostEntry(
                id=reservation_id,
                project_id=project_id,
                turn_id=None,
                llm_config_id=None,
                provider=_RESERVATION_PROVIDER,
                input_tokens=estimated_input,
                output_tokens=estimated_output,
                estimated_cost_usd=cost,
            )
            res_db.add(entry)
            await res_db.commit()
            break
    except Exception:
        logger.warning("Failed to write budget reservation", exc_info=True)
        return None
    return reservation_id


async def reconcile_budget_reservation(
    tenant_id: str,
    reservation_id: Optional[UUID],
) -> None:
    """Delete the pessimistic reservation in its own committed transaction.

    Bug-7366 -- record_turn_cost writes the real cost entry; the
    reservation must be removed so the ledger is not double-counted.
    Uses a separate session so the delete is committed regardless of the
    caller's transaction state.
    """
    if reservation_id is None:
        return
    from shared.db.session import get_tenant_db

    try:
        async for res_db in get_tenant_db(tenant_id):
            entry = await res_db.get(AgentCostEntry, reservation_id)
            if entry is not None:
                await res_db.delete(entry)
                await res_db.commit()
            break
    except Exception:
        logger.warning("Failed to reconcile budget reservation %s", reservation_id, exc_info=True)


async def sweep_orphaned_reservations(
    db: AsyncSession,
    project_id: Optional[UUID] = None,
) -> int:
    """Physically reclaim reservation rows orphaned by a process death.

    ``reserve_budget`` commits a pessimistic estimate row and
    ``reconcile_budget_reservation`` deletes it on every code exit path. A
    process death between the two (SIGKILL, OOM, container restart, uvicorn's
    post-grace-period task cancellation) strands the estimate row forever.

    ``_today_usage`` and the ``/cost`` report already EXCLUDE such rows from
    their sums (``is_orphaned_reservation``), so an orphan no longer holds the
    daily budget or inflates the report — but the physical row still
    accumulates in ``agent_cost_ledger`` on every crash. This is the
    housekeeping half: it DELETEs rows the exclusion predicate already treats
    as orphaned, bounding ledger growth.

    The orphan window is the SAME config-driven bound the exclusion uses
    (``_reservation_max_age_minutes`` / ``AGENT_RESERVATION_MAX_AGE_MINUTES``),
    so a LIVE reservation is never reaped — holding budget for the duration of
    a turn is the whole point of Bug-7366. The predicate is NULL-safe
    (``is_reservation_row`` uses ``IS NOT DISTINCT FROM``), so a real-spend row
    carrying a NULL provider is never matched and never deleted.

    Optionally scoped to one ``project_id`` (the caller in the admin
    retention-cleanup endpoint is project-scoped); tenant-wide when omitted.
    Returns the number of rows deleted. Does not commit — the caller owns the
    transaction (mirrors ``shared/agent/retention.py``).
    """
    stmt = sa_delete(AgentCostEntry).where(is_orphaned_reservation())
    if project_id is not None:
        stmt = stmt.where(AgentCostEntry.project_id == project_id)
    result = await db.execute(stmt)
    return int(result.rowcount or 0)


def _predicate_leaf_count(node: object) -> int:
    """Number of leaf comparisons in a structured predicate tree (Bug-6331).

    A nested ``AND``/``OR``/``NOT`` contributes the count of its leaf
    comparisons, so a compound boolean predicate is scored by how many
    conditions it actually carries rather than as a single flat clause. Any
    non-boolean node (a ``Comparison``) counts as one leaf."""
    # Imported lazily to keep the budget module free of a hard dependency on the
    # tools package at import time (and to avoid any import-order coupling).
    from src.tools.expressions import BoolOp, NotPred

    if isinstance(node, BoolOp):
        return sum(_predicate_leaf_count(a) for a in node.args)
    if isinstance(node, NotPred):
        return _predicate_leaf_count(node.arg)
    return 1


def check_query_complexity(cfg: ProjectAgentConfig, call: object) -> Optional[str]:
    """Return a refusal reason if the query exceeds max_query_complexity.

    Complexity = measures + dimensions + flat where/having + sort + the
    structured predicate/projection refs. 0 means no limit.

    Bug-6331 — structured predicates (OR/NOT groups, function-on-column and
    column-to-column comparisons) and computed projections do NOT live in the
    flat ``where``/``having`` lists; the parser routes them into the typed
    ``where_refs``/``having_refs``/``projection_refs`` companions. Counting only
    the flat clauses left a query built entirely from structured forms invisible
    to ``max_query_complexity``, so the guardrail could be bypassed. We now count
    every projection ref plus the leaf-comparison count of each structured
    predicate (a deeply nested boolean tree is not scored as one clause)."""
    if cfg.max_query_complexity <= 0:
        return None

    measures = getattr(call, "measures", []) or []
    dimensions = getattr(call, "dimensions", []) or []
    where = getattr(call, "where", []) or []
    having = getattr(call, "having", []) or []
    sort = getattr(call, "sort", []) or []
    projection_refs = getattr(call, "projection_refs", None) or []
    where_refs = getattr(call, "where_refs", None) or []
    having_refs = getattr(call, "having_refs", None) or []
    structured = (
        len(projection_refs)
        + sum(_predicate_leaf_count(r.node) for r in where_refs)
        + sum(_predicate_leaf_count(r.node) for r in having_refs)
    )
    complexity = (
        len(measures) + len(dimensions) + len(where) + len(having) + len(sort)
        + structured
    )
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
    exclude_reservation_id: Optional[UUID] = None,
) -> Optional[str]:
    """Bug-5283 — post-turn budget check so one expensive request that
    sneaks past the pre-turn gate is detected after recording its cost.

    Returns the exceeded budget reason string, or None if still within
    limits.  The caller (persist_turn) appends a guardrail warning action
    so the audit trail records that this turn pushed the budget over.

    Bug-7777 — ``exclude_reservation_id`` excludes this turn's pessimistic
    reservation row from the usage sum, exactly as in ``check_budget``.
    The callers reconcile (delete) the reservation before ``persist_turn``
    runs, so on the happy path the row is already gone and the exclusion is
    a no-op.  It matters on two real paths: (1) when
    ``reconcile_budget_reservation`` fails (its exception is swallowed with
    a warning), the stale 8096-token reservation would otherwise be counted
    ON TOP of the turn's real spend, spuriously flagging a budget breach;
    (2) it makes this check independent of the reconcile/persist call
    ordering, so a future reorder cannot silently reintroduce the
    double-count.
    """
    if cfg.daily_token_budget <= 0 and cfg.daily_cost_budget_usd <= 0:
        return None

    try:
        total_tokens, total_cost = await _today_usage(
            db, cfg.project_id,
            exclude_reservation_id=exclude_reservation_id,
        )
    except Exception:
        # Bug-5754 — fail CLOSED on the post-turn check too, so the audit
        # trail records a budget warning even when the DB is degraded.
        logger.exception("Post-turn budget check DB query failed — denying (fail-closed)")
        return "budget_check_unavailable"

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
