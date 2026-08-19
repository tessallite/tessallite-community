"""Turn orchestration — the single entry point used by the messages
endpoint.

Phase B1 flow (single-model only):
  1. assemble three-layer prompt + history + tool spec
  2. call answer LLM -> JSON tool call
  3. dispatch:
       query   -> SQL build -> query-router -> narration LLM call -> answer
       clarify -> echo question to user
       refuse  -> echo refusal message to user
  4. write the full F2 turn row.

Recipe execution (run_recipe) lands in B3. Async judge (B4) wraps this
pipeline at the api layer.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx

from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select as sa_select

from shared.db.models import (
    KPI,
    NamedSet,
    AgentConversation,
    AgentTurn,
    Dimension,
    Measure,
    Model,
    ProjectAgentConfig,
)
from shared.semantic.kpi_expression import _collect_references, parse_kpi_expression
from src.chart_config import effective_chart_selector
from src.citations.builder import build_citations, describe_filter_grain
from src.charts.selector import select_chart_type
from src.exec.query import (
    ModelNotAllowListedError,
    PersonaScopeViolationError,
    QueryExecution,
    QueryExecutionError,
    RowSecurityDeniedQueryError,
    execute_query,
)
from src.exec.recipe import (
    RecipeExecution,
    RecipeExecutionError,
    execute_recipe,
)
from src.guardrails.budget import check_budget, check_budget_post_turn, check_query_complexity, record_turn_cost
from src.guardrails.input import scan_input_message
from src.guardrails.output import apply_output_guardrails
from src.guardrails.refuse import render_refusal
from shared.config.settings import get_settings
from shared.llm.adapter import build_adapter, RetryingAdapter
from shared.middleware.internal_bypass import internal_request_headers
from shared.llm.config_resolution import resolve_agent_llm_failover_configs
from src.narrate.narrate import (
    narrate_answer,
    narrate_answer_stream,
    narrate_compound_answer,
    narrate_compound_answer_stream,
)
from src.planning.validation import (
    apply_shape_contract_repairs,
    apply_pre_validation_repairs,
    invalid_plan_message,
    validate_shape_contract_before_execution,
    validate_tool_call_against_bundle,
    validation_feedback_for_correction,
    validation_trace,
)
from src.planning.contracts import ShapeLimits
from src.planning.intent import detect_analytical_intent
from src.planning.matcher import match_shape_contract
from src.planning.roles import infer_field_roles
from src.planning.shape import normalize_result_shape
from src.prompt.assembler import assemble_prompt
from src.charts.renderer import (
    render_chart,
    render_compound_result,
    render_table,
    render_visual_artifact,
)
from src.recipes.eval import (
    CombineEvalError,
    evaluate_combine_aligned,
    validate_expression,
)
from src.sse.events import EventPublisher
from src.tools.spec import (
    ClarifyToolCall,
    CompoundQueryToolCall,
    CompoundStep,
    CreateAggregateToolCall,
    EvaluateKpiToolCall,
    PreviewNamedSetToolCall,
    QueryToolCall,
    RefuseToolCall,
    RunRecipeToolCall,
    ToolCallParseError,
    make_tool_spec,
    parse_tool_call,
)


async def _emit(publisher: EventPublisher | None, name: str, **data) -> None:
    if publisher is not None:
        await publisher.emit(name, **data)


def _narration_publisher(
    cfg: "ProjectAgentConfig", publisher: EventPublisher | None
) -> EventPublisher | None:
    """F-023-01/F-023-03 — in sync ("gateway") judge mode no answer token
    may reach the client before the verdict, so narration is buffered:
    the non-streaming narrate path runs and the (possibly blocked)
    answer is delivered only via the post-judge ``turn.completed`` event.
    Phase events (plan.tool, query.rows, ...) still stream."""
    # F-023-29 / Bug-8148 — the default is validated-first ("sync"); a cfg
    # missing the attribute entirely falls closed to buffered narration.
    # (Keyed on ``== "sync"`` to match the answer-delivery boundary in
    # conversations.py; uniform malformed-value normalisation at ingress is
    # deferred — intake 2026-07-22-judge-mode-ingress-validation.md.)
    if getattr(cfg, "judge_mode", "sync") == "sync":
        return None
    return publisher


# F-023-03 (round 2) — pre-verdict event allowlist for sync judge mode.
# Key = event name; value = the payload fields that may stream before the
# verdict (None = the whole payload is safe). Safe fields are counts,
# identifiers, and plan-derived labels — NEVER result values: step
# ``first_row`` and the computed combine ``value`` are answer-derived and
# stay withheld until the post-verdict ``turn.completed``/``turn.judged``.
# Events NOT listed here (``narration.delta``, ``thought.delta``, and any
# event added in the future) are dropped entirely pre-verdict — the gate
# fails closed so a new emission cannot silently reopen the leak.
_SYNC_PREVERDICT_SAFE_EVENTS: dict[str, frozenset[str] | None] = {
    "turn.started": None,        # echoes the user's own message
    "plan.tool": None,           # plan spec derived from the question
    "turn.plan": None,           # create_aggregate plan spec
    "query.rows": None,          # row count + route label only
    "compound.step": frozenset({"step_name", "model_id", "rows_returned"}),
    "recipe.step": frozenset({"step_name", "model_id", "rows_returned"}),
    "compound.expression": frozenset({"expression", "label"}),
    "turn.blocked": None,        # refusals are never judged — no verdict to pre-empt
    "turn.error": None,
}


class _SyncVerdictGate:
    """F-023-03 (round 2) — wraps the SSE publisher while ``run_turn``
    executes in sync judge mode so no answer-derived data reaches the
    wire before the verdict.

    Applied once at ``run_turn`` entry; the API layer keeps the raw
    publisher and emits the fully redacted ``turn.completed`` /
    ``turn.judged`` events after the judge has ruled. Side effect kept
    deliberately: a ``clarify`` turn's ``narration.delta`` is also
    withheld in sync mode — the question still reaches the client via
    the immediate ``turn.completed`` event, consistent with how every
    other sync answer is delivered."""

    def __init__(self, inner: EventPublisher) -> None:
        self._inner = inner

    async def emit(self, name: str, **data: Any) -> None:
        if name not in _SYNC_PREVERDICT_SAFE_EVENTS:
            return
        allowed = _SYNC_PREVERDICT_SAFE_EVENTS[name]
        if allowed is not None:
            data = {k: v for k, v in data.items() if k in allowed}
        await self._inner.emit(name, **data)

    async def close(self) -> None:
        await self._inner.close()

logger = logging.getLogger(__name__)


def _is_retryable_llm_error(exc: Exception) -> bool:
    detail = str(exc).lower()
    if "429" in detail or "rate" in detail:
        return True
    if "timeout" in detail or "timed out" in detail:
        return True
    if "connection" in detail or "connect" in detail:
        return True
    if "503" in detail or "502" in detail or "service unavailable" in detail:
        return True
    return False


def _int_env(name: str, default: int, *, minimum: int = 1) -> int:
    """Read a positive-integer tuning knob from the environment.

    CLAUDE.md forbids hard-coding config values in source. These narration /
    correction limits are operational tuning knobs, so they are read from the
    environment (with the historical value as the default) rather than baked in.
    A missing, blank, non-numeric, or below-``minimum`` value falls back to the
    default so a misconfiguration can never silently zero out a limit.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        logger.warning("Ignoring non-integer %s=%r; using default %d", name, raw, default)
        return default
    if value < minimum:
        logger.warning("Ignoring %s=%d below minimum %d; using default %d", name, value, minimum, default)
        return default
    return value


# Bug-7354 — the number of sample rows handed to the narration LLM was a
# hard-coded 25, so on a wide result the intermediate rows between row 25 and the
# end were invisible to the narrator. It is now an environment-tunable knob
# (default 25 preserves prior behaviour). Raising it lets the narrator see more
# of the result set; the effective upper bound is the query's own SQL LIMIT
# (rows already fetched), so a large value cannot pull more rows than the query
# returned. This knob only affects the narration prompt sample, not the persisted
# result_sample (capped independently by _RESULT_SAMPLE_CAP).
_MAX_NARRATE_ROWS = _int_env("AGENT_MAX_NARRATE_ROWS", 25)
_RESULT_SAMPLE_CAP = _int_env("AGENT_RESULT_SAMPLE_CAP", 50)

# Bug-7353 — the malformed-tool-call correction prompt fed only the last 2
# conversation turns, which can be too little context when a follow-up
# correction depends on an earlier turn. The window is now env-tunable (default
# 4, up from the previous hard-coded 2) so operators can widen it without a code
# change, and follow-up corrections carry more of the recent dialogue.
_TOOL_CORRECTION_CONTEXT_TURNS = _int_env("AGENT_TOOL_CORRECTION_CONTEXT_TURNS", 4)

# Bug-3587: a router-side security denial (persona gate rejection, column-level
# security, row security, restricted column) comes back to the agent as a generic
# QueryExecutionError. Without a dedicated pattern it would fall through to
# _QUERY_ERROR_FALLBACK ("try rephrasing — name a specific metric…"), which
# misleads the user into thinking it is a phrasing problem rather than an access
# restriction. These keyword patterns (matched before the generic ones) convert a
# gateway/router security error into clear conversational language, per the
# agent's documented contract: enforcement happens at the router chokepoint, and
# the narrator surfaces the refusal conversationally.
_SECURITY_DENIAL_MESSAGE = (
    "That request needs data your current access level or persona is not "
    "permitted to see. This is a security restriction, not a phrasing problem — "
    "ask your administrator if you believe you should have access, or try a "
    "question that uses the data available to you."
)

_QUERY_ERROR_PATTERNS: list[tuple[str, str]] = [
    # Full-phrase keys only: a bare "restricted" substring would mislabel a
    # benign field error (e.g. "column restricted_sales does not exist") as a
    # security denial (Phase 7 review F-P7-01). The genuine router security
    # details — "COLUMN_RESTRICTED", "restricted column", "is not included in
    # persona", "not permitted", "row security" — are all still caught by these
    # specific phrases.
    ("persona", _SECURITY_DENIAL_MESSAGE),
    ("column_restricted", _SECURITY_DENIAL_MESSAGE),
    ("restricted column", _SECURITY_DENIAL_MESSAGE),
    ("is restricted", _SECURITY_DENIAL_MESSAGE),
    ("not permitted", _SECURITY_DENIAL_MESSAGE),
    ("access denied", _SECURITY_DENIAL_MESSAGE),
    ("row security", _SECURITY_DENIAL_MESSAGE),
    ("neither grouped nor aggregated",
     "I could not apply that threshold filter. "
     "Try rephrasing with 'top N by...' or 'where ... is greater than' instead."),
    ("does not exist",
     "One of the fields in my query plan does not exist in the model. "
     "Please rephrase your question."),
    ("syntax error",
     "I generated a query the system could not process. "
     "Please try rephrasing your question."),
    ("ambiguous column",
     "The query references a field that exists in multiple places. "
     "Please be more specific about which metric you mean."),
]

_QUERY_ERROR_FALLBACK = (
    "I could not run that query against the data model. "
    "Please try rephrasing — name a specific metric and time window."
)

# Bug-5356: a missing source RELATION (table) is a data-availability /
# provisioning fault, NOT a model-field error. PostgreSQL reports it as
# 'relation "<schema.table>" does not exist' (asyncpg UndefinedTableError),
# whereas a genuine missing FIELD is 'column "<name>" does not exist'. The
# generic "does not exist" pattern below conflated the two, so when a model's
# underlying source tables were absent the agent told users "one of the fields
# in my query plan does not exist — please rephrase", sending them in circles
# (rephrasing can never fix unprovisioned source data). This message names the
# real cause so the user escalates instead of rephrasing.
_SOURCE_TABLE_MISSING_MESSAGE = (
    "The data behind this model is currently unavailable — one of its source "
    "tables could not be found. This is a data-availability problem, not a "
    "phrasing problem, so rephrasing the question will not help. Please ask "
    "your administrator to check the model's source connection and confirm its "
    "tables have been loaded."
)

_USER_CHART_PATTERNS: list[tuple[str, str]] = [
    (r"\bhorizontal\s+bar\b", "h_bar"),
    (r"\bstacked\s+bar\b", "stacked_bar"),
    (r"\bgrouped\s+bar\b", "grouped_bar"),
    (r"\bbar\s+chart\b", "bar"),
    (r"\bcolumn\s+chart\b", "bar"),
    (r"\bline\s+(?:chart|graph|plot)\b", "line"),
    (r"\bline\s+graph\b", "line"),
    (r"\bpie\s+chart\b", "pie"),
    (r"\bkpi\b", "kpi"),
    (r"\bscorecard\b", "kpi"),
]
_USER_CHART_RE = [(re.compile(p, re.IGNORECASE), t) for p, t in _USER_CHART_PATTERNS]


def _shape_limits_from_config(cfg: ProjectAgentConfig) -> ShapeLimits:
    return ShapeLimits(max_chart_rows=getattr(cfg, "chart_max_rows", 500) or 500)


def _extract_user_chart_type(user_message: str) -> str | None:
    for pattern, chart_type in _USER_CHART_RE:
        if pattern.search(user_message):
            return chart_type
    return None


_KPI_EVALUATION_INTENT_RE = re.compile(
    r"\b(evaluate|goal|target|status|trend|perform(?:ance|ing)?|on\s+track|"
    r"health|threshold)\b",
    re.IGNORECASE,
)
_FIELD_SUFFIX_TOKENS = {"code", "id", "key", "name", "number", "no"}
_COUNT_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_RAW_RECORD_REFUSAL_REASON = "raw_records_not_supported_by_aggregate_tools"
_RAW_RECORD_REFUSAL_I18N_KEY = "turn.rawRecordsNotSupported"


def _norm_label(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _repair_evaluate_kpi_presentation_call(
    call: EvaluateKpiToolCall,
    bundle: Any,
    user_message: str,
) -> QueryToolCall | None:
    """Route metric-card presentation prompts through shaped query execution.

    ``evaluate_kpi`` returns model-service KPI health metadata (goal/status/
    trend). That is correct for explicit KPI evaluation, but wrong for ordinary
    analytical presentation prompts such as "show revenue as a KPI card".
    """
    if _KPI_EVALUATION_INTENT_RE.search(user_message):
        return None

    profile = None
    for item in getattr(bundle, "model_profiles", []) or []:
        if str(getattr(item, "id", "")) == str(call.model_id):
            profile = item
            break
    if profile is None:
        return None

    kpi = None
    for item in getattr(profile, "kpis", []) or []:
        if str(getattr(item, "id", "")) == str(call.kpi_id):
            kpi = item
            break
    if kpi is None:
        return None

    kpi_labels = {
        _norm_label(getattr(kpi, "display_name", None)),
        _norm_label(getattr(kpi, "name", None)),
    }
    for measure in getattr(profile, "measure_names", []) or []:
        if _norm_label(measure) in kpi_labels:
            return QueryToolCall(
                model_id=call.model_id,
                measures=[measure],
                dimensions=[],
                where=[],
                having=[],
                sort=[],
                limit=100,
                limit_explicit=False,
                chart_type=_extract_user_chart_type(user_message) or "kpi",
            )
    return None


def _repair_preview_named_set_ranking_call(
    call: PreviewNamedSetToolCall,
    bundle: Any,
    user_message: str,
) -> QueryToolCall | None:
    """Route accidental named-set previews for ranking prompts to query.

    Named-set preview is correct only when the user asks to inspect a named set.
    For prompts like "Which five countries have the highest revenue?", the
    business intent is a ranked aggregate query. The LLM can occasionally pick
    ``preview_named_set`` when the model happens to expose named sets; this
    deterministic repair keeps the ranking contract on the query path.
    """
    intent = detect_analytical_intent(user_message)
    if not intent.wants_ranking:
        return None

    profile = None
    for item in getattr(bundle, "model_profiles", []) or []:
        if str(getattr(item, "id", "")) == str(call.model_id):
            profile = item
            break
    if profile is None:
        return None

    measures = [
        name
        for name in getattr(profile, "measure_names", []) or []
        if _field_mentioned(user_message, name)
    ]
    dimensions = [
        name
        for name in getattr(profile, "dimension_names", []) or []
        if _field_mentioned(user_message, name)
    ]
    if len(measures) != 1 or len(dimensions) != 1:
        return None

    limit = intent.requested_limit or _requested_count_word(user_message) or 10
    return QueryToolCall(
        model_id=call.model_id,
        measures=[measures[0]],
        dimensions=[dimensions[0]],
        where=[],
        having=[],
        sort=[{"name": measures[0], "direction": intent.ranking_direction or "desc"}],
        limit=limit,
        limit_explicit=True,
        chart_type=_extract_user_chart_type(user_message) or "h_bar",
    )


def _field_mentioned(user_message: str, field_name: str) -> bool:
    message_tokens = {
        _singularize(token)
        for token in re.findall(r"[a-z0-9]+", (user_message or "").lower())
    }
    field_tokens = [
        _singularize(token)
        for token in re.findall(r"[a-z0-9]+", (field_name or "").lower())
    ]
    while len(field_tokens) > 1 and field_tokens[-1] in _FIELD_SUFFIX_TOKENS:
        field_tokens.pop()
    return bool(field_tokens) and all(token in message_tokens for token in field_tokens)


def _singularize(token: str) -> str:
    if len(token) > 3 and token.endswith("ies"):
        return f"{token[:-3]}y"
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def _requested_count_word(user_message: str) -> int | None:
    tokens = re.findall(r"[a-z0-9]+", (user_message or "").lower())
    for token in tokens:
        if token.isdigit():
            value = int(token)
            if 1 <= value <= 1000:
                return value
        value = _COUNT_WORDS.get(token)
        if value is not None:
            return value
    return None


def _round_for_display(value: Any, sig: int = 4) -> Any:
    """F-023-14 — presentation-only rounding that preserves small ratios.

    The previous ``_round_computed`` forced ``round(value, 2)`` at the data
    layer, so a computed ratio of ``0.0042`` (0.42%) collapsed to ``0.0``
    before narration, charts, and persistence ever saw it. The exact value
    must survive to the narration LLM ("use the exact computed value") and
    the stored turn; only the formatted strings shown in a chart cell or a
    calculation-step label should be rounded — and even then with
    significant-figure rounding so sub-1.0 magnitudes keep their precision.

    Integers and large floats round to 2 decimals as before; magnitudes
    below 1 keep ``sig`` significant figures (0.004238 -> 0.004238 at 4 sig,
    not 0.0)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if value == 0:
            return 0.0
        magnitude = abs(value)
        if magnitude >= 1:
            return round(value, 2)
        # Significant-figure rounding for sub-1.0 values.
        digits = sig - 1 - int(math.floor(math.log10(magnitude)))
        return round(value, max(2, digits))
    if isinstance(value, dict):
        return {k: _round_for_display(v, sig) for k, v in value.items()}
    if isinstance(value, list):
        return [_round_for_display(v, sig) for v in value]
    return value


def _humanize_query_error(detail: str) -> str:
    lower = detail.lower()
    if "aggregation path" in lower:
        return detail
    # Bug-5356: discriminate a missing source TABLE from a missing model FIELD
    # before the generic "does not exist" pattern. A relation/table-not-found is
    # an infrastructure fault (source data not provisioned); only a missing
    # column is an actual field error the user could rephrase around.
    if "does not exist" in lower and (
        "relation" in lower or "undefinedtable" in lower
    ):
        return _SOURCE_TABLE_MISSING_MESSAGE
    for pattern, message in _QUERY_ERROR_PATTERNS:
        if pattern in lower:
            return message
    return _QUERY_ERROR_FALLBACK


def _chart_renderer(cfg: ProjectAgentConfig) -> str:
    renderer = getattr(cfg, "chart_renderer", "echarts") or "echarts"
    return renderer if renderer in {"echarts", "html"} else "echarts"


def _render_shaped_output(
    cfg: ProjectAgentConfig,
    chart_type: str | None,
    columns: list[str],
    rows: list[list[Any]],
    *,
    include_table: bool | None = None,
    legacy_html: str | None = None,
) -> str | None:
    should_include_table = (
        getattr(cfg, "include_data_table", True)
        if include_table is None else include_table
    )
    if _chart_renderer(cfg) == "html":
        if chart_type:
            return render_chart(
                chart_type,
                columns,
                rows,
                palette=getattr(cfg, "chart_color_palette", "default"),
                size=getattr(cfg, "chart_size", "md"),
                include_table=False,
            ) or None
        if should_include_table:
            return render_table(columns, rows) or None
        return None
    result = render_visual_artifact(
        chart_type,
        columns,
        rows,
        palette=getattr(cfg, "chart_color_palette", "default"),
        size=getattr(cfg, "chart_size", "md"),
        include_table=should_include_table,
        legacy_html=legacy_html,
    )
    # Bug-7350 -- log when a chart was requested but the renderer returned None.
    if result is None and chart_type is not None:
        logger.debug(
            "render_visual_artifact returned None for chart_type=%s "
            "(columns=%d, rows=%d, renderer=echarts)",
            chart_type, len(columns), len(rows),
        )
    return result or None


def _numeric_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return numeric


def _scalar_contribution_pie_shape(
    user_message: str,
    result_label: str | None,
    combine_value: Any,
    user_chart: str | None,
) -> tuple[str, list[str], list[list[Any]], dict[str, Any]] | None:
    """Represent a scalar percent-of-total answer as selected vs remaining.

    Compound scalar expressions often compute a single percentage such as
    "Cairo base amount / global base amount". If the user asks for a pie or
    contribution view, a KPI-only scalar loses the requested whole/part shape.
    The pie must use the selected contribution and the remaining global total;
    using "Cairo" and "global" as two slices would double-count Cairo.
    """
    intent = detect_analytical_intent(user_message)
    if user_chart != "pie" and not intent.wants_composition:
        return None

    percentage = _numeric_float(combine_value)
    if percentage is None or percentage < 0 or percentage > 100:
        return None
    # Bug-7349 -- detect ratio values (0.0..1.0 exclusive) that were not
    # multiplied by 100.  A value like 0.42 would produce a nonsensical
    # 0.42% slice with 99.58% remaining.  Suppress the pie whenever the
    # label signals a ratio/fraction/proportion (the value is almost
    # certainly 0-1 scaled).  For labels that do NOT signal a ratio (e.g.
    # "Cairo share (%)" = 0.6 meaning a genuine sub-1% share), allow
    # through -- legitimate sub-1% percentages are rare but possible.
    if 0 < percentage < 1.0:
        label_lower = (result_label or "").lower()
        if any(kw in label_lower for kw in ("ratio", "fraction", "proportion", "index", "factor")):
            return None

    label = (result_label or "Selected contribution").strip()
    label = re.sub(r"\s*\(%\)\s*$", "", label).strip()
    if not re.search(r"\b(share|contribution|percent|percentage)\b", label, re.I):
        label = f"{label} contribution" if label else "Selected contribution"

    value_col = "Contribution (%)"
    selected = _round_for_display(percentage)
    remaining = _round_for_display(max(0.0, 100.0 - percentage))
    columns = ["segment", value_col]
    rows = [
        [label, selected],
        ["Remaining global total", remaining],
    ]
    shape_trace = {
        "shape": "stacked_composition",
        "chart_type": "pie",
        "output_mode": "chart_table",
        "columns": columns,
        "row_sample": rows,
        "narration_facts": {
            "row_count": len(rows),
            "composition": {
                "total": 100.0,
                "selected_part": selected,
                "remaining_part": remaining,
                "denominator_scope": "global_total",
            },
        },
        "notes": [
            "registry_entry=percent_of_total_by_category",
            "registry_entry=compound_ratio_kpi",
            "scalar_contribution_pie",
        ],
    }
    return "pie", columns, rows, shape_trace


@dataclass
class TurnOutcome:
    answer_text: str
    status: str
    plan: dict[str, Any] | None
    semantic_query: dict[str, Any] | None
    routed_sql: str | None
    route: str | None
    rows_returned: int
    guardrail_actions: list[dict[str, Any]]
    citations: list[dict[str, Any]] | None = None
    thought_summary: str | None = None
    usage_input_tokens: int = 0
    usage_output_tokens: int = 0
    prompt_messages: dict[str, Any] | None = None
    llm_raw_response: str | None = None
    rendered_output: str | None = None
    chart_type: str | None = None
    result_sample: list[dict[str, Any]] | None = None
    result_row_count: int | None = None
    provider: str | None = None
    calculation_steps: list[dict[str, Any]] | None = None
    # F-023-24 — recipe turns populate the dedicated schema columns.
    recipe_id: UUID | None = None
    recipe_steps_executed: list[dict[str, Any]] | None = None


def _raw_record_refusal_outcome(
    call: Any,
    intent: Any,
    *,
    prompt_messages: dict[str, Any] | None = None,
    llm_raw_response: str | None = None,
    provider: str | None = None,
) -> TurnOutcome | None:
    if "raw_records_requested" not in getattr(intent, "notes", []):
        return None
    return TurnOutcome(
        answer_text=_RAW_RECORD_REFUSAL_REASON,
        status="refused",
        plan={
            "tool": "refuse",
            "reason": _RAW_RECORD_REFUSAL_REASON,
            "message_i18n_key": _RAW_RECORD_REFUSAL_I18N_KEY,
            "source_tool": _plan_dict(call),
        },
        semantic_query=None,
        routed_sql=None,
        route=None,
        rows_returned=0,
        guardrail_actions=[
            {
                "layer": "shape",
                "action": "refuse",
                "reason": _RAW_RECORD_REFUSAL_REASON,
                "message_i18n_key": _RAW_RECORD_REFUSAL_I18N_KEY,
            }
        ],
        prompt_messages=prompt_messages,
        llm_raw_response=llm_raw_response,
        provider=provider,
    )


def _accumulate_usage(adapter: Any, totals: dict[str, int]) -> None:
    usage = getattr(adapter, "last_usage", None)
    if not isinstance(usage, dict):
        return
    totals["input"] += int(usage.get("input_tokens") or 0)
    totals["output"] += int(usage.get("output_tokens") or 0)


async def _attempt_tool_call_correction(
    adapter: Any,
    failing_output: str,
    error_message: str,
    db: AsyncSession,
    conversation_id: UUID,
    usage_totals: dict[str, int],
) -> str | None:
    """Single-shot LLM correction for a malformed tool call.

    Sends the tool schema, failing output, error, and the most recent
    conversation turns to the LLM for one correction attempt.  Returns the
    corrected raw response or ``None`` if the correction call itself fails.

    Bug-7353 — the context window is ``_TOOL_CORRECTION_CONTEXT_TURNS``
    (env-tunable, default 4) rather than a hard-coded 2, so a follow-up
    correction that depends on an earlier turn keeps that context.
    """
    q = await db.execute(
        sa_select(AgentTurn.user_message, AgentTurn.answer_text)
        .where(AgentTurn.conversation_id == conversation_id)
        .order_by(AgentTurn.turn_index.desc())
        .limit(_TOOL_CORRECTION_CONTEXT_TURNS)
    )
    recent = list(reversed(q.all()))
    context_lines: list[str] = []
    for row in recent:
        context_lines.append(f"User: {row.user_message}")
        if row.answer_text:
            context_lines.append(f"Assistant: {row.answer_text[:300]}")
    context_block = "\n".join(context_lines) if context_lines else "(first turn)"

    tool_spec = make_tool_spec()

    system = (
        "You are correcting a malformed JSON tool call. "
        "Output ONLY the corrected JSON object — no prose, no markdown "
        "fences, no explanation.\n\n"
        f"## REQUIRED SCHEMA\n{tool_spec}"
    )
    user = (
        f"The following tool call was rejected:\n"
        f"```\n{failing_output[:2000]}\n```\n\n"
        f"Error: {error_message}\n\n"
        f"Recent conversation (for context):\n{context_block}\n\n"
        f"Fix the error and output ONLY the corrected JSON."
    )

    try:
        # R5 (F6) — the correction step also produces a tool-call JSON object;
        # request native JSON-output mode so a second malformed (non-JSON)
        # response is prevented at source where the provider supports it.
        corrected = await adapter.complete(system, user, response_json=True)
        _accumulate_usage(adapter, usage_totals)
        return corrected
    except Exception:
        logger.warning("Tool-call correction LLM call failed", exc_info=True)
        return None


async def _load_measure_formats(
    db: AsyncSession,
    model_id: UUID,
) -> dict[str, str | None]:
    """Return {measure_name: format_token} for all measures in a model."""
    q = await db.execute(
        sa_select(Measure.name, Measure.format).where(
            Measure.model_id == model_id,
            Measure.is_invalid.is_(False),
        )
    )
    return {name: fmt for name, fmt in q.all()}


def _plan_dict(call: Any) -> dict[str, Any]:
    if isinstance(call, QueryToolCall):
        # Bug-5349 Phase 2/3 — structured predicates live in *_refs, not the
        # flat where/having lists. Re-merge their raw forms so a follow-up plan
        # replay reconstructs them (otherwise the structured filter would be
        # silently lost on the next turn).
        where_out = list(call.where) + [r.raw for r in (call.where_refs or [])]
        having_out = list(call.having) + [r.raw for r in (call.having_refs or [])]
        inner_q: dict[str, Any] = {
            "model_id": call.model_id,
            "measures": call.measures,
            "dimensions": call.dimensions,
            "where": where_out,
            "having": having_out,
            "sort": call.sort,
            "limit": call.limit,
        }
        # Bug-5349 / decision D2 — when any dimension is grained/expression,
        # carry the original entries so follow-ups can reuse the user-facing
        # grain. The key is OMITTED for plain bare-dimension queries, keeping
        # legacy plan dicts byte-for-byte identical.
        refs = call.dimension_refs or []
        if any(not r.is_bare for r in refs):
            inner_q["dimension_exprs"] = [r.raw for r in refs]
        # Phase 3 — preserve computed projection columns for follow-up replay.
        if call.projection_refs:
            inner_q["projections"] = [p.raw for p in call.projection_refs]
        return {"query": inner_q}
    if isinstance(call, RunRecipeToolCall):
        return {"run_recipe": {
            "recipe_id": call.recipe_id,
            "parameters": call.parameters,
        }}
    if isinstance(call, CompoundQueryToolCall):
        inner: dict[str, Any] = {
            "steps": [],
            "expression": call.expression,
            "result_label": call.result_label,
        }
        for s in call.steps:
            step_where = list(s.where) + [r.raw for r in (s.where_refs or [])]
            step_having = list(s.having) + [r.raw for r in (s.having_refs or [])]
            step: dict[str, Any] = {
                "name": s.name,
                "model_id": s.model_id,
                "measures": s.measures,
                "dimensions": s.dimensions,
                "where": step_where,
                "having": step_having,
                "sort": s.sort,
                "limit": s.limit,
            }
            refs = s.dimension_refs or []
            if any(not r.is_bare for r in refs):
                step["dimension_exprs"] = [r.raw for r in refs]
            if s.projection_refs:
                step["projections"] = [p.raw for p in s.projection_refs]
            inner["steps"].append(step)
        if call.chart_type:
            inner["chart_type"] = call.chart_type
        return {"compound_query": inner}
    if isinstance(call, EvaluateKpiToolCall):
        return {"evaluate_kpi": {"model_id": call.model_id, "kpi_id": call.kpi_id}}
    if isinstance(call, PreviewNamedSetToolCall):
        return {"preview_named_set": {"model_id": call.model_id, "named_set_id": call.named_set_id}}
    if isinstance(call, ClarifyToolCall):
        return {"clarify": {"question": call.question}}
    if isinstance(call, RefuseToolCall):
        return {"refuse": {"reason": call.reason, "message": call.message}}
    return {"tool": "unknown"}


def _dedupe_compound_alignment_alias(alias: str, taken: set[str]) -> str:
    candidate = alias
    n = 2
    while candidate in taken:
        candidate = f"{alias}_{n}"
        n += 1
    taken.add(candidate)
    return candidate


def _compound_alignment_key(ref: Any) -> str:
    """Semantic key used for compound row alignment.

    Display aliases are intentionally excluded from the key. Two branches can
    align when their normalized expression renders identically; two unrelated
    expressions that merely reused the same alias cannot align accidentally.
    """
    bases = ",".join(ref.base_fields)
    return f"{ref.render_group_by()}|bases={bases}"


def _prepare_compound_alignment(
    step_executions: list[tuple[CompoundStep, QueryExecution]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[str]], list[dict[str, Any]]]:
    semantic_to_alias: dict[str, str] = {}
    taken_aliases: set[str] = set()
    trace: list[dict[str, Any]] = []

    for step, _execution in step_executions:
        for ref in step.dimension_refs or []:
            key = _compound_alignment_key(ref)
            if key not in semantic_to_alias:
                semantic_to_alias[key] = _dedupe_compound_alignment_alias(
                    ref.alias, taken_aliases,
                )
            trace.append({
                "step": step.name,
                "dimension": ref.alias,
                "alignment_alias": semantic_to_alias[key],
                "alignment_key": key,
                "base_fields": list(ref.base_fields),
            })

    aligned_rows: dict[str, list[dict[str, Any]]] = {}
    aligned_dimensions: dict[str, list[str]] = {}
    for step, execution in step_executions:
        refs = step.dimension_refs or []
        aliases = [semantic_to_alias[_compound_alignment_key(ref)] for ref in refs]
        aligned_dimensions[step.name] = aliases
        rows: list[dict[str, Any]] = []
        for row in execution.rows:
            aligned = dict(row)
            for ref, alias in zip(refs, aliases, strict=True):
                if alias != ref.alias:
                    aligned[alias] = row.get(ref.alias)
            rows.append(aligned)
        aligned_rows[step.name] = rows

    return aligned_rows, aligned_dimensions, trace


def _field_roles_for_query_call(
    call: QueryToolCall,
    bundle: Any,
    *,
    result_columns: list[str] | None = None,
    result_rows: list[dict[str, Any]] | None = None,
) -> list[Any]:
    profile = None
    for item in getattr(bundle, "model_profiles", []) or []:
        if str(getattr(item, "id", "")) == str(call.model_id):
            profile = item
            break
    measure_metadata = dict(getattr(profile, "measure_metadata", {}) or {}) if profile else {}
    return infer_field_roles(
        selected_measures=call.measures,
        selected_dimensions=call.dimensions,
        dimension_refs=call.dimension_refs or [],
        result_columns=result_columns,
        result_rows=result_rows,
        model_profiles=profile,
        measure_metadata=measure_metadata,
    )


def _field_roles_for_compound_result(
    call: CompoundQueryToolCall,
    result_columns: list[str],
    result_rows: list[dict[str, Any]],
) -> list[Any]:
    dimensions = [col for col in result_columns if col != call.result_label]
    return infer_field_roles(
        selected_measures=[],
        selected_dimensions=dimensions,
        dimension_refs=[],
        result_columns=result_columns,
        result_rows=result_rows,
        model_profiles={},
        measure_metadata={},
        computed_label=call.result_label,
    )


async def _allow_list_refusal_outcome(
    call: Any,
    bundle: Any,
    prompt_messages: dict[str, Any] | None,
    llm_raw_response: str | None,
    db: AsyncSession | None = None,
) -> "TurnOutcome | None":
    """F-023-07 / Bug-5279 / Bug-6329 — shared allow-list AND persona
    field-scope gate for tool branches that do not flow through
    ``execute_query`` (create_aggregate, evaluate_kpi, preview_named_set).

    Returns a refusal outcome when ``call.model_id`` is invalid, outside
    the agent allow-list, or when the tool references measures or
    dimensions outside the active persona's field scope.  Previously only
    the model-level allow-list was checked; the field-level persona scope
    was bypassed, so a restricted persona could evaluate any KPI or create
    aggregates on hidden fields (Bug-5279).

    Bug-6329 (F-023-01): the KPI / named-set checks formerly compared the
    requested id against the model profile's KPI / named-set list, which
    is NEVER filtered by persona at assembly time — so every KPI and named
    set was always "exposed" and a restricted persona could evaluate a KPI
    built on a hidden measure.  The gate now resolves the KPI's measure
    lineage (and the named set's dimension lineage) from the database and
    refuses when any dependency falls outside the persona's visible field
    set.  It is fail-closed: an unresolvable KPI / named set, or one whose
    lineage cannot be confirmed in scope, is refused."""
    try:
        model_uuid = UUID(call.model_id)
    except (ValueError, TypeError):
        model_uuid = None
    if model_uuid is None or model_uuid not in bundle.allow_list_model_ids:
        if isinstance(call, CreateAggregateToolCall):
            plan = {"tool": "create_aggregate", "model_id": call.model_id}
        else:
            plan = _plan_dict(call)
        return TurnOutcome(
            answer_text="The requested model is not allow-listed for this project.",
            status="refused",
            plan=plan,
            semantic_query=None, routed_sql=None, route=None, rows_returned=0,
            guardrail_actions=[{"layer": "tool", "action": "refuse", "reason": "model_not_allow_listed"}],
            prompt_messages=prompt_messages, llm_raw_response=llm_raw_response,
        )

    # Bug-5279 — enforce persona field scope on non-query tool paths.
    # The query path enforces via enforce_execution_scope(); these tool
    # branches bypassed it entirely. A persona that restricts field
    # visibility must not be able to evaluate a KPI whose underlying
    # measure is hidden, create an aggregate on hidden fields, or preview
    # a named set outside their model scope.
    persona_scopes = getattr(bundle, "persona_scopes", None)
    if persona_scopes is not None:
        scope = persona_scopes.get(model_uuid)
        if scope is None:
            plan = _plan_dict(call)
            return TurnOutcome(
                answer_text=(
                    "That model is not available under your current persona."
                ),
                status="refused",
                plan=plan,
                semantic_query=None, routed_sql=None, route=None, rows_returned=0,
                guardrail_actions=[{
                    "layer": "tool", "action": "refuse",
                    "reason": "persona_scope_violation",
                    "detail": f"model {call.model_id} not in persona",
                }],
                prompt_messages=prompt_messages, llm_raw_response=llm_raw_response,
            )

        violations: list[str] = []
        # create_aggregate carries explicit measures + dimensions
        if isinstance(call, CreateAggregateToolCall):
            violations += [m for m in (call.measures or []) if m not in scope.measures]
            violations += [d for d in (call.dimensions or []) if d not in scope.dimensions]
        # evaluate_kpi — Bug-6329: resolve the KPI's measure lineage from
        # the DB and refuse when any underlying measure is outside the
        # persona's visible measure set. Fail-closed: an unresolvable KPI
        # (or one we cannot confirm is fully in scope) is refused.
        if isinstance(call, EvaluateKpiToolCall):
            kpi_id = getattr(call, "kpi_id", None)
            if await _kpi_outside_persona_scope(
                db, kpi_id, model_uuid, scope.measures, scope.dimensions
            ):
                violations.append(f"kpi:{kpi_id}")
        # preview_named_set — Bug-6329: resolve the named set's dimension
        # lineage from the DB and refuse when it references a real model
        # dimension the persona cannot see.
        if isinstance(call, PreviewNamedSetToolCall):
            ns_id = getattr(call, "named_set_id", None)
            if await _named_set_outside_persona_scope(
                db, ns_id, model_uuid, scope.dimensions
            ):
                violations.append(f"named_set:{ns_id}")

        if violations:
            plan = _plan_dict(call)
            return TurnOutcome(
                answer_text=(
                    "That request references fields or items outside your "
                    "current persona scope."
                ),
                status="refused",
                plan=plan,
                semantic_query=None, routed_sql=None, route=None, rows_returned=0,
                guardrail_actions=[{
                    "layer": "tool", "action": "refuse",
                    "reason": "persona_scope_violation",
                    "detail": f"violations: {', '.join(violations)}",
                }],
                prompt_messages=prompt_messages, llm_raw_response=llm_raw_response,
            )

    return None


# Mirrors the MDX bracket-token extraction in model-service
# ``named_sets.py`` (Bug-5963): member keys ``&[key]`` are stripped first
# so source values are never mistaken for dimension-name references, then
# structural tokens that can never name a dimension are dropped.
_NS_MEMBER_KEY_RE = re.compile(r"&\[[^\]]*\]")
_NS_BRACKET_REF_RE = re.compile(r"\[([^\]]+)\]")
_NS_NON_DIMENSION_TOKENS = frozenset({"measures", "model", "members", "all"})


def _named_set_referenced_dimension_names(expression: str | None) -> set[str]:
    """Candidate dimension-name references in an MDX set expression."""
    if not expression:
        return set()
    expr_without_keys = _NS_MEMBER_KEY_RE.sub("", expression)
    refs = _NS_BRACKET_REF_RE.findall(expr_without_keys)
    return {r for r in refs if r.lower() not in _NS_NON_DIMENSION_TOKENS}


def _kpi_expr_refs(expr: str) -> tuple[set[str], set[str], set[str]]:
    """Return ``(measure_names, kpi_names, dimension_names)`` referenced by
    a KPI expression.

    Raises on a parse failure so the caller can fail closed rather than
    silently treating an unparseable expression as having no lineage."""
    ast = parse_kpi_expression(expr)
    measures, kpis, dims = _collect_references(ast)
    return set(measures), set(kpis), set(dims)


async def _kpi_outside_persona_scope(
    db: AsyncSession | None,
    kpi_id: Any,
    model_uuid: UUID,
    visible_measures: frozenset[str],
    visible_dimensions: frozenset[str],
) -> bool:
    """True when the KPI cannot be evaluated under the persona's visible
    field set (Bug-6329 / F-023-01).

    Resolves the KPI's FULL lineage — transitively through ``kpi("...")``
    references (composite KPIs):

    * MEASURE lineage: ``measure()`` refs in the expression / target
      expression plus the legacy value/goal/target measure-id bindings;
      every referenced measure must be in ``visible_measures``.
    * DIMENSION lineage: ``dimension()`` refs in the expression / target
      expression plus the KPI's ``time_dimension_id`` binding; any
      referenced dimension that is a real model dimension the persona
      cannot see (not in ``visible_dimensions``) blocks evaluation — a
      restricted persona must not evaluate a KPI whose value is computed
      over a hidden dimension (Codex round-2 finding).

    Fail-closed (returns ``True`` → refuse) on any lineage uncertainty: a
    missing DB session, an unparseable id, a KPI that does not belong to
    the model, an expression that fails to parse, a ``kpi()`` dependency
    that does not resolve to a KPI in the model, or a legacy measure-id
    that cannot be resolved to a model measure."""
    if db is None:
        return True
    try:
        kid = kpi_id if isinstance(kpi_id, UUID) else UUID(str(kpi_id))
    except (TypeError, ValueError):
        return True
    kpi = await db.get(KPI, kid)
    if kpi is None or kpi.model_id != model_uuid:
        return True

    # Resolve the whole model's measure/dimension/kpi maps once so legacy
    # id bindings and transitive kpi() refs resolve without per-node
    # round-trips.
    meas_rows = await db.execute(
        sa_select(Measure.id, Measure.name).where(Measure.model_id == model_uuid)
    )
    measure_id_to_name = {mid: name for mid, name in meas_rows.all()}
    dim_rows = await db.execute(
        sa_select(Dimension.id, Dimension.name).where(Dimension.model_id == model_uuid)
    )
    dim_id_to_name: dict[Any, str] = {did: dname for did, dname in dim_rows.all()}
    kpi_rows = await db.execute(
        sa_select(KPI).where(KPI.model_id == model_uuid)
    )
    kpis_by_name = {row.name: row for row in kpi_rows.scalars().all()}

    visible_dims_lower = {d.lower() for d in visible_dimensions}
    referenced_measures: set[str] = set()
    # Lowercased dimension names the KPI depends on. Every entry must be a
    # visible dimension; an unresolved dimension() ref (no matching model
    # dimension) is left here too so the final check fails closed on it —
    # symmetric with measure handling, per the round-2 fail-closed goal.
    referenced_dimensions: set[str] = set()
    seen: set[Any] = set()
    stack: list[Any] = [kpi]
    while stack:
        cur = stack.pop()
        cur_id = getattr(cur, "id", None)
        if cur_id in seen:
            continue
        seen.add(cur_id)

        for expr in (
            getattr(cur, "expression", None),
            getattr(cur, "target_expression", None),
        ):
            if not expr or not str(expr).strip():
                continue
            try:
                m_names, k_names, d_names = _kpi_expr_refs(expr)
            except Exception:
                return True  # unparseable expression → indeterminate → refuse
            referenced_measures.update(m_names)
            # Add every dimension() ref: a hidden or unresolved name will
            # fall outside visible_dims_lower and fail closed below.
            referenced_dimensions.update(d.lower() for d in d_names)
            for kname in k_names:
                child = kpis_by_name.get(kname)
                if child is None:
                    return True  # unresolved kpi() dependency → refuse
                if getattr(child, "id", None) not in seen:
                    stack.append(child)

        for attr in ("value_measure_id", "goal_measure_id", "target_measure_id"):
            raw = getattr(cur, attr, None)
            if raw is None:
                continue
            try:
                muid = raw if isinstance(raw, UUID) else UUID(str(raw))
            except (TypeError, ValueError):
                return True
            name = measure_id_to_name.get(muid)
            if name is None:
                return True  # legacy binding to unknown measure → refuse
            referenced_measures.add(name)

        # time_dimension_id binds the KPI to a (possibly hidden) dimension.
        # Fail closed when it is set but does not resolve to a model
        # dimension (dangling/deleted binding = indeterminate lineage).
        tdid = getattr(cur, "time_dimension_id", None)
        if tdid is not None:
            try:
                tduid = tdid if isinstance(tdid, UUID) else UUID(str(tdid))
            except (TypeError, ValueError):
                return True
            tdname = dim_id_to_name.get(tduid)
            if tdname is None:
                return True  # dangling time-dimension binding → refuse
            referenced_dimensions.add(tdname.lower())

    if any(name not in visible_measures for name in referenced_measures):
        return True
    if any(dl not in visible_dims_lower for dl in referenced_dimensions):
        return True
    return False


async def _named_set_outside_persona_scope(
    db: AsyncSession | None,
    ns_id: Any,
    model_uuid: UUID,
    visible_dimensions: frozenset[str],
) -> bool:
    """True when the named set references a real model dimension the
    persona cannot see (Bug-6329 / F-023-01).

    Mirrors model-service ``_named_set_visible_to_persona``: an MDX
    expression's bracket tokens also cover hierarchy/level names and
    literal member captions, so this restricts only on a confident match
    to a real model dimension outside the persona's visible dimension set.
    Fail-closed on a missing DB session, an unparseable id, or a named set
    that does not belong to the model.

    Bug-7348 -- accepted risk: bracket tokens that are NOT real model
    dimension names (hierarchy names, level names, member captions) pass
    through this heuristic silently.  This is intentional: the true
    enforcement is at the query-router binder, which refuses any dimension
    reference outside the persona's visible set at SQL generation time."""
    if db is None:
        return True
    try:
        nid = ns_id if isinstance(ns_id, UUID) else UUID(str(ns_id))
    except (TypeError, ValueError):
        return True
    ns = await db.get(NamedSet, nid)
    if ns is None or ns.model_id != model_uuid:
        return True

    # Two lineage signals: (1) the authoritative persisted ``dimensions``
    # field (comma/semicolon-separated dimension names, when the builder
    # populated it) and (2) confident dimension-name references extracted
    # from the MDX expression. Refuse when EITHER names a real model
    # dimension the persona cannot see.
    referenced = _named_set_referenced_dimension_names(getattr(ns, "expression", None))
    raw_dims = getattr(ns, "dimensions", None)
    if raw_dims:
        for part in re.split(r"[;,]", str(raw_dims)):
            token = part.strip()
            if token:
                referenced.add(token)
    if not referenced:
        return False
    rows = await db.execute(
        sa_select(Dimension.name).where(Dimension.model_id == model_uuid)
    )
    all_dim_lower = {name.lower() for (name,) in rows.all()}
    visible_lower = {d.lower() for d in visible_dimensions}
    for ref in referenced:
        rl = ref.lower()
        if rl in all_dim_lower and rl not in visible_lower:
            return True
    return False


async def run_turn(
    db: AsyncSession,
    cfg: ProjectAgentConfig,
    conversation: AgentConversation,
    user_message: str,
    jwt_token: str,
    publisher: EventPublisher | None = None,
    persona_id: UUID | None = None,
    embed_model_ids: list[str] | None = None,
    budget_reservation_id: UUID | None = None,
) -> TurnOutcome:
    # F-023-03 (round 2) — in sync judge mode every emission from the
    # pipeline (including the compound/recipe branches and execute_recipe)
    # goes through the fail-closed pre-verdict gate.
    # F-023-29 / Bug-8148 — the default is validated-first ("sync"); a cfg
    # missing the attribute falls closed to the pre-verdict gate. (Keyed on
    # ``== "sync"`` to match the answer-delivery boundary in conversations.py;
    # uniform malformed-value normalisation at ingress is deferred — intake
    # 2026-07-22-judge-mode-ingress-validation.md.)
    if publisher is not None and getattr(cfg, "judge_mode", "sync") == "sync":
        publisher = _SyncVerdictGate(publisher)

    await _emit(
        publisher,
        "turn.started",
        conversation_id=str(conversation.id),
        user_message=user_message,
    )

    # D2.1 — input guardrails (cheap, pre-LLM).
    scan = scan_input_message(cfg, user_message)
    if not scan.ok:
        refusal = render_refusal(
            scan.reason or "internal_error", detail=scan.matched_topic
        )
        await _emit(
            publisher, "turn.blocked", reason=refusal.reason, message=refusal.message
        )
        return TurnOutcome(
            answer_text=refusal.message,
            status="refused",
            plan={"tool": "refuse", "reason": refusal.reason},
            semantic_query=None,
            routed_sql=None,
            route=None,
            rows_returned=0,
            guardrail_actions=[
                {"layer": "input", "action": "refuse", "reason": refusal.reason}
            ],
        )

    bundle = await assemble_prompt(
        db, cfg, conversation.id, user_message,
        persona_id=persona_id or getattr(conversation, "persona_id", None),
        pinned_model_id=getattr(conversation, "pinned_model_id", None),
        embed_model_ids=embed_model_ids,
    )
    _prompt_msgs = {"system": bundle.system, "user": bundle.user}

    if not bundle.allow_list_model_ids:
        return TurnOutcome(
            answer_text=(
                "This project's agent has no allow-listed models. Ask a "
                "tenant admin or modeller to allow-list at least one "
                "published model in the Project Agent settings."
            ),
            status="refused",
            plan={"tool": "refuse", "reason": "no_allow_list"},
            semantic_query=None,
            routed_sql=None,
            route=None,
            rows_returned=0,
            guardrail_actions=[
                {"layer": "input", "action": "refuse", "reason": "no_allow_list"}
            ],
            prompt_messages=_prompt_msgs,
        )

    try:
        llm_configs = await resolve_agent_llm_failover_configs(cfg.project_id, "answer", db)
    except ValueError as exc:
        return TurnOutcome(
            answer_text=str(exc),
            status="error",
            plan={"tool": "refuse", "reason": "no_llm_config"},
            semantic_query=None,
            routed_sql=None,
            route=None,
            rows_returned=0,
            guardrail_actions=[
                {"layer": "config", "action": "refuse", "reason": "no_llm_config"}
            ],
            prompt_messages=_prompt_msgs,
        )

    llm_config = llm_configs[0]

    # Budget check — before spending tokens.
    # Bug-7777 — exclude the pessimistic reservation written by reserve_budget
    # so it does not count against its own turn's budget check.  Without this,
    # projects with daily_token_budget <= 8096 refuse every turn because the
    # reservation's estimated tokens already fill or exceed the budget.
    budget_reason = await check_budget(
        db, cfg, exclude_reservation_id=budget_reservation_id,
    )
    if budget_reason:
        budget_messages = {
            "daily_token_budget_exceeded": (
                "The project's daily token budget has been reached. "
                "Ask your administrator to raise the limit or wait until tomorrow."
            ),
            "daily_cost_budget_exceeded": (
                "The project's daily cost budget has been reached. "
                "Ask your administrator to raise the limit or wait until tomorrow."
            ),
            # Bug-5754 — fail-closed: DB error during budget verification.
            "budget_check_unavailable": (
                "Unable to verify budget status at the moment. "
                "Please try again shortly. If the issue persists, "
                "contact your tenant administrator."
            ),
        }
        msg = budget_messages.get(budget_reason, "Budget exceeded.")
        await _emit(publisher, "turn.blocked", reason=budget_reason, message=msg)
        return TurnOutcome(
            answer_text=msg,
            status="refused",
            plan={"tool": "refuse", "reason": budget_reason},
            semantic_query=None,
            routed_sql=None,
            route=None,
            rows_returned=0,
            guardrail_actions=[
                {"layer": "budget", "action": "refuse", "reason": budget_reason}
            ],
            prompt_messages=_prompt_msgs,
        )

    usage_totals = {"input": 0, "output": 0}
    thinking_parts: list[str] = []

    async def _on_thinking(token: str) -> None:
        thinking_parts.append(token)
        # Bug-7376 / Bug-7545 — gate streaming thought tokens on
        # show_thought_process.  Persisted turns already strip the field
        # in _redact_trace, but the live SSE path was an unguarded
        # parallel channel.  Suppressing here prevents the tokens from
        # ever crossing the API boundary.
        if publisher is not None and getattr(cfg, "show_thought_process", True):
            await publisher.emit("thought.delta", text=token)

    raw = None
    last_exc: Exception | None = None
    for i, llm_config in enumerate(llm_configs):
        try:
            adapter = RetryingAdapter(build_adapter(llm_config))
        except ValueError:
            continue
        try:
            # R5 (F6) — the planner emits exactly one JSON tool-call object.
            # Request native JSON-output mode (response_json) so providers that
            # support it (OpenAI-family json_object, Gemini application/json)
            # guarantee valid JSON, eliminating the markdown-fence / prose parse
            # failure that otherwise burns the single correction round. Anthropic
            # ignores the flag and relies on the JSON-in-text parser (documented
            # fallback).
            raw = await adapter.complete(
                bundle.system, bundle.user, on_thinking=_on_thinking,
                response_json=True,
                # R1 (F1) — mark the stable planner prefix cacheable. Lane B
                # kept per-turn content out of bundle.system and R1 moved the
                # daily CURRENT_DATE into bundle.user, so bundle.system is now
                # byte-stable across turns AND day boundaries — the big cache
                # win. The marker never changes the rendered prompt text.
                cache_system_prefix=True,
            )
            _accumulate_usage(adapter, usage_totals)
            if i > 0:
                logger.warning(
                    "[AGENT] provider %s/%s failed, fell back to %s/%s",
                    llm_configs[0].provider, llm_configs[0].model_name,
                    llm_config.provider, llm_config.model_name,
                )
            break
        except Exception as exc:
            last_exc = exc
            if _is_retryable_llm_error(exc) and i < len(llm_configs) - 1:
                logger.warning(
                    "[AGENT] provider %s failed (%s), falling back to %s",
                    llm_config.provider, exc, llm_configs[i + 1].provider,
                )
                thinking_parts.clear()
                continue
            break

    if raw is None:
        exc = last_exc or ValueError("No LLM provider available")
        logger.exception("Answer LLM call failed")
        detail = str(exc)
        # Bug-5957 — user-facing messages must NOT expose provider names,
        # model names, API key hints, timeout configs, or raw exception
        # strings.  Log the full detail server-side and return a safe,
        # generic message that guides the user without leaking internals.
        if "401" in detail or "authentication" in detail.lower() or "unauthorized" in detail.lower():
            user_msg = (
                "The language-model service could not authenticate. "
                "Please ask your administrator to verify the LLM "
                "configuration in Project Settings."
            )
        elif "404" in detail or "not found" in detail.lower():
            user_msg = (
                "The configured language model could not be found. "
                "Please ask your administrator to check the LLM "
                "configuration in Project Settings."
            )
        elif "429" in detail or "rate" in detail.lower():
            user_msg = (
                "The language-model service is temporarily rate-limited. "
                "Please wait a moment and try again."
            )
        elif "timeout" in detail.lower() or "timed out" in detail.lower():
            user_msg = (
                "The language-model service did not respond in time. "
                "The service may be temporarily overloaded — "
                "please try again shortly."
            )
        else:
            user_msg = (
                "The language-model service is currently unavailable. "
                "Please try again shortly or contact your administrator "
                "if the issue persists."
            )
        logger.error(
            "[AGENT] LLM error detail (not sent to user): %s/%s — %s",
            llm_config.provider, llm_config.model_name, detail,
        )
        return TurnOutcome(
            answer_text=user_msg,
            status="error",
            plan={"tool": "refuse", "reason": "llm_unavailable", "detail": detail},
            semantic_query=None,
            routed_sql=None,
            route=None,
            rows_returned=0,
            guardrail_actions=[
                {"layer": "answer_llm", "action": "error", "reason": "llm_unavailable"}
            ],
            prompt_messages=_prompt_msgs,
        )

    try:
        call = parse_tool_call(raw)
    except ToolCallParseError as exc:
        corrected = await _attempt_tool_call_correction(
            adapter, raw, str(exc), db, conversation.id, usage_totals,
        )
        if corrected is not None:
            try:
                call = parse_tool_call(corrected)
                raw = corrected
                logger.info("Tool-call correction succeeded on retry")
            except ToolCallParseError:
                logger.info("Tool-call correction also failed")
                corrected = None
        if corrected is None:
            return TurnOutcome(
                answer_text=(
                    "I could not produce a structured plan for that question. "
                    "Please rephrase, ideally naming a metric and a time window."
                ),
                status="refused",
                plan={"tool": "refuse", "reason": "tool_call_parse_error", "detail": str(exc), "raw": raw[:1000]},
                semantic_query=None,
                routed_sql=None,
                route=None,
                rows_returned=0,
                guardrail_actions=[
                    {"layer": "answer_llm", "action": "refuse", "reason": "tool_call_parse_error"}
                ],
                prompt_messages=_prompt_msgs,
                llm_raw_response=raw,
            )

    if isinstance(call, QueryToolCall):
        pre_validation_intent = detect_analytical_intent(
            user_message,
            previous_plan=getattr(bundle, "previous_plan", None),
        )
        if apply_pre_validation_repairs(call, pre_validation_intent, bundle):
            await _emit(
                publisher,
                "plan.pre_validation_repaired",
                intent=pre_validation_intent.as_trace(),
                plan=_plan_dict(call),
            )

    validation_issues = validate_tool_call_against_bundle(call, bundle)
    if validation_issues:
        raw_plan = _plan_dict(call)
        raw_validation = validation_trace(validation_issues)
        await _emit(
            publisher,
            "plan.raw",
            plan=raw_plan,
            validation=raw_validation,
        )
        corrected = None
        if any(issue.repairable for issue in validation_issues):
            corrected = await _attempt_tool_call_correction(
                adapter,
                json.dumps(raw_plan, indent=2),
                validation_feedback_for_correction(validation_issues, bundle),
                db,
                conversation.id,
                usage_totals,
            )
        if corrected is not None:
            try:
                corrected_call = parse_tool_call(corrected)
                corrected_issues = validate_tool_call_against_bundle(
                    corrected_call, bundle
                )
                if not corrected_issues:
                    call = corrected_call
                    raw = corrected
                    validation_issues = []
                    await _emit(
                        publisher,
                        "plan.repaired",
                        plan=_plan_dict(call),
                        validation=validation_trace([]),
                    )
                else:
                    validation_issues = corrected_issues
                    await _emit(
                        publisher,
                        "plan.repair_failed",
                        plan=_plan_dict(corrected_call),
                        validation=validation_trace(corrected_issues),
                    )
            except ToolCallParseError as exc:
                await _emit(
                    publisher,
                    "plan.repair_failed",
                    plan=raw_plan,
                    validation={
                        "status": "invalid",
                        "issues": raw_validation["issues"],
                        "repair_parse_error": str(exc),
                    },
                )
        if validation_issues:
            msg = invalid_plan_message(validation_issues)
            await _emit(
                publisher,
                "turn.blocked",
                reason="tool_validation_failed",
                message=msg,
            )
            plan = _plan_dict(call)
            plan["validation"] = validation_trace(validation_issues)
            return TurnOutcome(
                answer_text=msg,
                status="refused",
                plan=plan,
                semantic_query=None,
                routed_sql=None,
                route=None,
                rows_returned=0,
                guardrail_actions=[
                    {
                        "layer": "tool",
                        "action": "refuse",
                        "reason": "tool_validation_failed",
                        "issues": [
                            issue.as_trace() for issue in validation_issues
                        ],
                    }
                ],
                prompt_messages=_prompt_msgs,
                llm_raw_response=raw,
                provider=llm_config.provider,
            )

    if isinstance(call, EvaluateKpiToolCall):
        repaired_call = _repair_evaluate_kpi_presentation_call(
            call,
            bundle,
            user_message,
        )
        if repaired_call is not None:
            call = repaired_call
            await _emit(
                publisher,
                "plan.repaired",
                reason="evaluate_kpi_presentation_routed_to_query",
                plan=_plan_dict(call),
            )

    if isinstance(call, PreviewNamedSetToolCall):
        repaired_call = _repair_preview_named_set_ranking_call(
            call,
            bundle,
            user_message,
        )
        if repaired_call is not None:
            call = repaired_call
            await _emit(
                publisher,
                "plan.repaired",
                reason="preview_named_set_ranking_routed_to_query",
                plan=_plan_dict(call),
            )

    raw_record_intent = detect_analytical_intent(
        user_message,
        previous_plan=getattr(bundle, "previous_plan", None),
    )
    raw_record_refusal = _raw_record_refusal_outcome(
        call,
        raw_record_intent,
        prompt_messages=_prompt_msgs,
        llm_raw_response=raw,
        provider=llm_config.provider,
    )
    if raw_record_refusal is not None:
        await _emit(
            publisher,
            "turn.blocked",
            reason="raw_records_not_supported_by_aggregate_tools",
            message=raw_record_refusal.answer_text,
        )
        return raw_record_refusal

    if isinstance(call, QueryToolCall):
        analytical_intent = detect_analytical_intent(
            user_message,
            previous_plan=getattr(bundle, "previous_plan", None),
        )
        field_roles = _field_roles_for_query_call(call, bundle)
        shape_issues = validate_shape_contract_before_execution(
            call,
            analytical_intent,
            field_roles,
        )
        if shape_issues and any(issue.repairable for issue in shape_issues):
            if apply_shape_contract_repairs(
                call,
                analytical_intent,
                field_roles,
                bundle,
            ):
                field_roles = _field_roles_for_query_call(call, bundle)
                shape_issues = validate_shape_contract_before_execution(
                    call,
                    analytical_intent,
                    field_roles,
                )
                await _emit(
                    publisher,
                    "plan.shape_repaired",
                    intent=analytical_intent.as_trace(),
                    validation=validation_trace(shape_issues),
                )
        if shape_issues:
            msg = invalid_plan_message(shape_issues)
            await _emit(
                publisher,
                "turn.blocked",
                reason="shape_validation_failed",
                message=msg,
            )
            plan = _plan_dict(call)
            plan["shape_validation"] = {
                "intent": analytical_intent.as_trace(),
                "field_roles": [role.as_trace() for role in field_roles],
                "validation": validation_trace(shape_issues),
            }
            return TurnOutcome(
                answer_text=msg,
                status="refused",
                plan=plan,
                semantic_query=None,
                routed_sql=None,
                route=None,
                rows_returned=0,
                guardrail_actions=[
                    {
                        "layer": "shape",
                        "action": "refuse",
                        "reason": "shape_validation_failed",
                        "issues": [issue.as_trace() for issue in shape_issues],
                    }
                ],
                prompt_messages=_prompt_msgs,
                llm_raw_response=raw,
                provider=llm_config.provider,
            )

    await _emit(publisher, "plan.tool", plan=_plan_dict(call))

    if isinstance(call, ClarifyToolCall):
        await _emit(publisher, "narration.delta", text=call.question)
        return TurnOutcome(
            answer_text=call.question,
            status="clarify",
            plan=_plan_dict(call),
            semantic_query=None,
            routed_sql=None,
            route=None,
            rows_returned=0,
            guardrail_actions=[],
            prompt_messages=_prompt_msgs,
            llm_raw_response=raw,
            provider=llm_config.provider,
        )

    if isinstance(call, RunRecipeToolCall):
        outcome = await _run_recipe_branch(
            db, cfg, adapter, bundle, user_message, call, jwt_token, publisher,
            usage_totals, prompt_messages=_prompt_msgs, llm_raw_response=raw,
        )
        outcome.thought_summary = "".join(thinking_parts) or None
        # F-023-04 — stamp the answer-LLM provider so persist_turn can cost the turn.
        outcome.provider = outcome.provider or llm_config.provider
        return outcome

    if isinstance(call, CompoundQueryToolCall):
        outcome = await _run_compound_query_branch(
            db, cfg, adapter, bundle, user_message, call, jwt_token, publisher,
            usage_totals, prompt_messages=_prompt_msgs, llm_raw_response=raw,
            conversation_id=conversation.id,
            on_thinking=_on_thinking,
        )
        outcome.thought_summary = "".join(thinking_parts) or None
        # F-023-04 — stamp the answer-LLM provider so persist_turn can cost the turn.
        outcome.provider = outcome.provider or llm_config.provider
        return outcome

    if isinstance(call, EvaluateKpiToolCall):
        # F-023-07 — the read tools must honour the agent allow-list too.
        refusal = await _allow_list_refusal_outcome(call, bundle, _prompt_msgs, raw, db=db)
        if refusal is not None:
            return refusal
        outcome = await _run_evaluate_kpi_branch(
            cfg, call, jwt_token, publisher, usage_totals,
            prompt_messages=_prompt_msgs, llm_raw_response=raw,
            project_id=cfg.project_id,
        )
        outcome.thought_summary = "".join(thinking_parts) or None
        # F-023-04 — stamp the answer-LLM provider so persist_turn can cost the turn.
        outcome.provider = outcome.provider or llm_config.provider
        return outcome

    if isinstance(call, PreviewNamedSetToolCall):
        # F-023-07 — the read tools must honour the agent allow-list too.
        refusal = await _allow_list_refusal_outcome(call, bundle, _prompt_msgs, raw, db=db)
        if refusal is not None:
            return refusal
        outcome = await _run_preview_named_set_branch(
            cfg, call, jwt_token, publisher, usage_totals,
            prompt_messages=_prompt_msgs, llm_raw_response=raw,
            project_id=cfg.project_id,
        )
        outcome.thought_summary = "".join(thinking_parts) or None
        # F-023-04 — stamp the answer-LLM provider so persist_turn can cost the turn.
        outcome.provider = outcome.provider or llm_config.provider
        return outcome

    if isinstance(call, CreateAggregateToolCall):
        refusal = await _allow_list_refusal_outcome(call, bundle, _prompt_msgs, raw, db=db)
        if refusal is not None:
            return refusal
        outcome = await _run_create_aggregate_branch(
            cfg, call, jwt_token, publisher, usage_totals,
            prompt_messages=_prompt_msgs, llm_raw_response=raw,
        )
        outcome.thought_summary = "".join(thinking_parts) or None
        # F-023-04 — stamp the answer-LLM provider so persist_turn can cost the turn.
        outcome.provider = outcome.provider or llm_config.provider
        return outcome

    if isinstance(call, RefuseToolCall):
        await _emit(
            publisher,
            "turn.blocked",
            reason=call.reason,
            message=call.message,
        )
        return TurnOutcome(
            answer_text=call.message,
            status="refused",
            plan=_plan_dict(call),
            semantic_query=None,
            routed_sql=None,
            route=None,
            rows_returned=0,
            guardrail_actions=[
                {"layer": "answer_llm", "action": "refuse", "reason": call.reason}
            ],
            prompt_messages=_prompt_msgs,
            llm_raw_response=raw,
            provider=llm_config.provider,
        )

    # query tool call
    assert isinstance(call, QueryToolCall)

    try:
        _model_uuid = UUID(call.model_id)
    except ValueError:
        return TurnOutcome(
            answer_text=(
                "I produced an invalid model reference. "
                "Please rephrase your question."
            ),
            status="refused",
            plan=_plan_dict(call),
            semantic_query=None,
            routed_sql=None,
            route=None,
            rows_returned=0,
            guardrail_actions=[
                {"layer": "tool", "action": "refuse", "reason": "invalid_model_id"}
            ],
            prompt_messages=_prompt_msgs,
            llm_raw_response=raw,
        )

    complexity_reason = check_query_complexity(cfg, call)
    if complexity_reason:
        msg = (
            "That query is too complex for this project's configuration. "
            "Try asking for fewer measures or dimensions at once."
        )
        await _emit(publisher, "turn.blocked", reason=complexity_reason, message=msg)
        return TurnOutcome(
            answer_text=msg,
            status="refused",
            plan=_plan_dict(call),
            semantic_query=None,
            routed_sql=None,
            route=None,
            rows_returned=0,
            guardrail_actions=[
                {"layer": "budget", "action": "refuse", "reason": complexity_reason}
            ],
            prompt_messages=_prompt_msgs,
            llm_raw_response=raw,
        )

    try:
        # F-023-07 / F-023-08 — the allow-list and persona scope are
        # enforced inside execute_query (the single chokepoint shared
        # with compound steps and recipe steps).
        execution: QueryExecution = await execute_query(
            db, call, jwt_token,
            allowed_model_ids=bundle.allow_list_model_ids,
            persona_scopes=bundle.persona_scopes,
            # This path RENDERS a row-security denial (narrate.py branches on
            # execution.row_security_denied and tells the user their access is
            # restricted), so it opts out of the chokepoint's refusal. The
            # compound and recipe paths deliberately do NOT opt out.
            allow_row_security_denial=True,
        )
        await _emit(
            publisher,
            "query.rows",
            model_id=call.model_id,
            rows_returned=execution.rows_returned,
            route=execution.route_type,
        )
    except ModelNotAllowListedError as exc:
        return TurnOutcome(
            answer_text=(
                "I tried to query a model that is not allow-listed for this "
                "project. Please rephrase, or ask a modeller to allow-list "
                "the right model."
            ),
            status="refused",
            plan=_plan_dict(call),
            semantic_query=None,
            routed_sql=None,
            route=None,
            rows_returned=0,
            guardrail_actions=[
                {"layer": "tool", "action": "refuse",
                 "reason": "model_not_allow_listed", "detail": str(exc)}
            ],
            prompt_messages=_prompt_msgs,
            llm_raw_response=raw,
        )
    except PersonaScopeViolationError as exc:
        return TurnOutcome(
            answer_text=(
                "That question needs data that is not available to your "
                "current persona. Please rephrase using the fields available "
                "to you, or ask an administrator about your persona scope."
            ),
            status="refused",
            plan=_plan_dict(call),
            semantic_query=None,
            routed_sql=None,
            route=None,
            rows_returned=0,
            guardrail_actions=[
                {"layer": "tool", "action": "refuse",
                 "reason": "persona_scope_violation", "detail": str(exc)}
            ],
            prompt_messages=_prompt_msgs,
            llm_raw_response=raw,
        )
    except QueryExecutionError as exc:
        return TurnOutcome(
            answer_text=_humanize_query_error(str(exc)),
            status="refused",
            plan=_plan_dict(call),
            semantic_query=None,
            routed_sql=None,
            route=None,
            rows_returned=0,
            guardrail_actions=[
                {"layer": "exec", "action": "refuse", "reason": "query_rejected", "detail": str(exc)}
            ],
            prompt_messages=_prompt_msgs,
            llm_raw_response=raw,
        )

    plan = _plan_dict(call)

    queried_model = await db.get(Model, _model_uuid)
    model_display_name = queried_model.display_name if queried_model else None
    measure_formats = await _load_measure_formats(db, _model_uuid)

    chart_type_selector = effective_chart_selector(cfg)
    rendered_output: str | None = None
    chart_type: str | None = None
    shape_trace: dict[str, Any] | None = None

    if execution.columns and execution.rows is not None:
        analytical_intent = detect_analytical_intent(
            user_message,
            previous_plan=getattr(bundle, "previous_plan", None),
        )
        field_roles = _field_roles_for_query_call(
            call,
            bundle,
            result_columns=execution.columns,
            result_rows=execution.rows,
        )
        user_chart = _extract_user_chart_type(user_message)
        requested_chart = (
            "none" if chart_type_selector == "none"
            else user_chart
            or (getattr(call, "chart_type", None) if chart_type_selector == "llm" else None)
            or chart_type_selector
        )
        shape_limits = _shape_limits_from_config(cfg)
        contract = match_shape_contract(
            intent=analytical_intent,
            field_roles=field_roles,
            call=call,
            chart_type_selector=requested_chart,
            limits=shape_limits,
        )
        shaped = normalize_result_shape(
            execution.columns,
            execution.rows,
            analytical_intent,
            field_roles,
            contract,
            shape_limits,
        )
        shape_trace = shaped.as_trace()
        chart_type = shaped.chart_type

        if chart_type or getattr(cfg, "include_data_table", True):
            rendered_output = _render_shaped_output(
                cfg,
                chart_type,
                shaped.columns,
                shaped.rows,
            )

    output_fmt = getattr(cfg, "agent_output_format", "plain")
    prior_qs = getattr(bundle, "prior_questions", [])
    narr_publisher = _narration_publisher(cfg, publisher)
    try:
        if narr_publisher is not None:
            narration = await narrate_answer_stream(
                adapter, bundle.narration_system, user_message, execution,
                narr_publisher, measure_formats=measure_formats,
                output_format=output_fmt,
                prior_questions=prior_qs,
                shape_trace=shape_trace,
                on_thinking=_on_thinking,
            )
        else:
            narration = await narrate_answer(
                adapter, bundle.narration_system, user_message, execution,
                measure_formats=measure_formats,
                output_format=output_fmt,
                prior_questions=prior_qs,
                shape_trace=shape_trace,
                on_thinking=_on_thinking,
            )
        _accumulate_usage(adapter, usage_totals)
    except Exception as exc:
        # Bug-5957 — do not expose route type, row count, or raw exception
        # in the user-facing answer.  Log the full detail server-side.
        logger.exception(
            "Narration LLM call failed (route=%s, rows=%d)",
            execution.route_type, execution.rows_returned,
        )
        narration = (
            "The query executed successfully but the narration service "
            "could not summarise the result. Please try again."
        )

    output = apply_output_guardrails(cfg, narration)
    narration = output.text
    # In streaming mode narration.delta tokens already emitted above; in non-streaming
    # mode publisher is None and _emit is a no-op — either way nothing more to emit here.

    # Bug-8181 (L3) — thread the route that served this answer and a
    # human-readable filter/grain summary of THIS call onto every citation, so
    # a citation chip is checkable (definition + route + exact slice), not
    # just a semantic label. See citations/builder.py for the field contract.
    # F-L3-R1-02 (round 2): where_refs/having/having_refs are threaded too —
    # a structured predicate (function-on-column, OR/NOT) is an equally
    # supported call shape as the flat `where` list, and omitting it here
    # made a genuinely filtered query render as "unfiltered" in the citation
    # dialog.
    citations = await build_citations(
        db,
        UUID(call.model_id),
        call.measures,
        call.dimensions,
        execution.rows,
        route_type=execution.route_type or None,
        filter_grain=describe_filter_grain(
            call.where,
            call.dimensions,
            where_refs=call.where_refs,
            having=call.having,
            having_refs=call.having_refs,
        ),
    )

    return TurnOutcome(
        answer_text=narration,
        status="ok",
        plan=plan,
        semantic_query={
            "model_id": call.model_id,
            "measures": call.measures,
            "dimensions": call.dimensions,
            "where": call.where,
            "having": call.having,
            "sort": call.sort,
            "limit": call.limit,
            "executed_sql": execution.sql,
            "shape": shape_trace,
        },
        routed_sql=execution.routed_sql,
        route=execution.route_type,
        rows_returned=execution.rows_returned,
        guardrail_actions=output.actions,
        citations=citations,
        thought_summary="".join(thinking_parts) or None,
        usage_input_tokens=usage_totals["input"],
        usage_output_tokens=usage_totals["output"],
        prompt_messages=_prompt_msgs,
        llm_raw_response=raw,
        rendered_output=rendered_output,
        chart_type=chart_type,
        result_sample=(execution.rows or [])[:_RESULT_SAMPLE_CAP],
        result_row_count=execution.rows_returned,
        provider=llm_config.provider,
    )


async def _run_recipe_branch(
    db: AsyncSession,
    cfg: ProjectAgentConfig,
    adapter,
    bundle,
    user_message: str,
    call: RunRecipeToolCall,
    jwt_token: str,
    publisher: EventPublisher | None = None,
    usage_totals: dict[str, int] | None = None,
    prompt_messages: dict[str, Any] | None = None,
    llm_raw_response: str | None = None,
) -> TurnOutcome:
    if usage_totals is None:
        usage_totals = {"input": 0, "output": 0}
    plan = _plan_dict(call)
    try:
        # F-023-07 — recipe steps execute through the same chokepoint as
        # the direct query path; allow-list and persona scope enforced
        # inside execute_query for every step.
        recipe_exec: RecipeExecution = await execute_recipe(
            db, cfg.project_id, call, jwt_token, publisher=publisher,
            allowed_model_ids=bundle.allow_list_model_ids,
            persona_scopes=bundle.persona_scopes,
        )
    except RecipeExecutionError as exc:
        cause = exc.__cause__
        if isinstance(cause, ModelNotAllowListedError):
            reason = "model_not_allow_listed"
        elif isinstance(cause, PersonaScopeViolationError):
            reason = "persona_scope_violation"
        elif isinstance(cause, RowSecurityDeniedQueryError):
            # R3 finding B-3: the chokepoint refusal reaches here wrapped in a
            # RecipeExecutionError. Without this branch it fell to
            # "recipe_failed" -> "please try again", telling a user to retry
            # something that did not fail and will never succeed, and hiding
            # the permissions truth. Same misattribution the compound branch
            # got a dedicated handler to avoid.
            reason = "row_security_denied"
        else:
            reason = "recipe_failed"
        # Bug-5957 — do not expose raw exception in user-facing answer.
        logger.warning("Recipe execution failed: %s", exc)
        if reason == "persona_scope_violation":
            safe_msg = (
                "That recipe needs data that is not available to your "
                "current persona. Please ask an administrator about "
                "your persona scope."
            )
        elif reason == "model_not_allow_listed":
            safe_msg = (
                "That recipe references a model that is not allow-listed "
                "for this project."
            )
        elif reason == "row_security_denied":
            safe_msg = (
                "That recipe cannot be run because your row-level security "
                "permissions grant you access to none of the underlying rows. "
                "This is a permissions restriction, not an absence of data, "
                "and retrying will not change it. Contact your administrator "
                "if you believe you should have access."
            )
        else:
            safe_msg = (
                "The recipe could not be executed. Please try again "
                "or contact your administrator if the issue persists."
            )
        return TurnOutcome(
            answer_text=safe_msg,
            status="refused",
            plan=plan,
            semantic_query=None,
            routed_sql=None,
            route=None,
            rows_returned=0,
            guardrail_actions=[
                {"layer": "recipe", "action": "refuse", "reason": reason, "detail": str(exc)}
            ],
            prompt_messages=prompt_messages,
            llm_raw_response=llm_raw_response,
        )

    # F-023-18 — route recipe results through the same LLM narration the
    # compound branch uses, instead of emitting raw Python dict repr. The
    # step summaries and computed result feed narrate_compound_answer, so a
    # recipe answer reads as prose (and honours the project output format).
    total_rows = 0
    step_summaries: list[dict[str, Any]] = []
    for step in recipe_exec.steps:
        total_rows += step.execution.rows_returned
        step_summaries.append({
            "name": step.name,
            "columns": step.execution.columns,
            "rows_returned": step.execution.rows_returned,
            "sample_rows": step.execution.rows[:_MAX_NARRATE_ROWS],
            # R10 (compound scope, review R1-3) — full rows for date-range
            # aggregation only; never rendered into the prompt.
            "all_rows": step.execution.rows,
        })

    computed: dict[str, Any] = {
        "expression": recipe_exec.combine_expression,
        "label": recipe_exec.recipe_name,
        "value": recipe_exec.combine_value,
        "is_multi_row": False,
        "alignment_mode": "recipe",
        # R10 (compound scope, review R1-2) — a recipe value computed from
        # row-capped step data is a partial-data figure; the narration prompt
        # must disclose it.
        "steps_truncated": any(
            bool(getattr(s.execution, "truncated", False))
            for s in recipe_exec.steps
        ),
    }

    output_fmt = getattr(cfg, "agent_output_format", "plain")
    prior_qs = getattr(bundle, "prior_questions", [])
    narr_publisher = _narration_publisher(cfg, publisher)
    try:
        if narr_publisher is not None:
            narration = await narrate_compound_answer_stream(
                adapter, bundle.narration_system, user_message,
                step_summaries, computed, narr_publisher,
                output_format=output_fmt,
                prior_questions=prior_qs,
            )
        else:
            narration = await narrate_compound_answer(
                adapter, bundle.narration_system, user_message,
                step_summaries, computed,
                output_format=output_fmt,
                prior_questions=prior_qs,
            )
        _accumulate_usage(adapter, usage_totals)
    except Exception as exc:
        # Bug-5957 — do not expose recipe name, step count, combine
        # values, or raw exception in the user-facing answer.
        logger.exception(
            "Recipe narration LLM call failed (recipe=%s, steps=%d)",
            recipe_exec.recipe_name, len(recipe_exec.steps),
        )
        narration = (
            "The recipe executed successfully but the narration service "
            "could not summarise the result. Please try again."
        )
    output = apply_output_guardrails(cfg, narration)
    narration = output.text

    semantic = {
        "tool": "run_recipe",
        "recipe_id": str(recipe_exec.recipe_id),
        "recipe_name": recipe_exec.recipe_name,
        "parameters": recipe_exec.parameters_resolved,
        "steps": [
            {
                "name": s.name,
                "executed_sql": s.execution.sql,
                "rows_returned": s.execution.rows_returned,
                "first_row": s.first_row,
            }
            for s in recipe_exec.steps
        ],
        "combine": recipe_exec.combine_expression,
        "combine_value": recipe_exec.combine_value,
    }
    routed_sql = "\n\n".join(
        f"-- step: {s.name}\n{s.execution.routed_sql or s.execution.sql}"
        for s in recipe_exec.steps
    )
    routes = sorted({s.execution.route_type for s in recipe_exec.steps if s.execution.route_type})

    # F-023-24 — recipe steps land in the dedicated schema column rather
    # than only inside semantic_query.
    recipe_steps_executed = [
        {
            "name": s.name,
            "rows_returned": s.execution.rows_returned,
            "route": s.execution.route_type,
            "first_row": s.first_row,
        }
        for s in recipe_exec.steps
    ]

    calc_steps: list[dict[str, Any]] = []
    for s in recipe_exec.steps:
        row = s.first_row or {}
        value_col = s.execution.columns[0] if s.execution.columns else None
        val = row.get(value_col) if value_col and row else None
        calc_steps.append({
            "step_number": len(calc_steps) + 1,
            "description": s.name.replace("_", " ").title(),
            "name": s.name,
            "measure": value_col,
            "value": val,
            "formatted_value": str(_round_for_display(val)) if val is not None else "",
        })
    if recipe_exec.combine_expression:
        calc_steps.append({
            "step_number": len(calc_steps) + 1,
            "description": "Computed result",
            "name": recipe_exec.recipe_name,
            "value": recipe_exec.combine_value,
            "formatted_value": str(_round_for_display(recipe_exec.combine_value)),
            "formula": recipe_exec.combine_expression,
        })

    return TurnOutcome(
        answer_text=narration,
        status="ok",
        plan=plan,
        semantic_query=semantic,
        routed_sql=routed_sql or None,
        route=",".join(routes) if routes else None,
        rows_returned=total_rows,
        guardrail_actions=output.actions,
        citations=None,
        thought_summary=None,
        usage_input_tokens=usage_totals["input"],
        usage_output_tokens=usage_totals["output"],
        prompt_messages=prompt_messages,
        llm_raw_response=llm_raw_response,
        result_sample=[
            s.first_row for s in recipe_exec.steps
            if s.first_row
        ][:_RESULT_SAMPLE_CAP],
        result_row_count=total_rows,
        recipe_id=recipe_exec.recipe_id,
        recipe_steps_executed=recipe_steps_executed,
        calculation_steps=calc_steps or None,
    )


async def _run_compound_query_branch(
    db: AsyncSession,
    cfg,
    adapter,
    bundle,
    user_message: str,
    call: CompoundQueryToolCall,
    jwt_token: str,
    publisher: EventPublisher | None = None,
    usage_totals: dict[str, int] | None = None,
    prompt_messages: dict[str, Any] | None = None,
    llm_raw_response: str | None = None,
    conversation_id: UUID | None = None,
    on_thinking=None,
) -> TurnOutcome:
    if usage_totals is None:
        usage_totals = {"input": 0, "output": 0}
    plan = _plan_dict(call)

    max_steps = getattr(cfg, "max_compound_steps", 3)
    if len(call.steps) > max_steps:
        msg = (
            f"This question requires {len(call.steps)} sub-queries, which "
            f"exceeds the configured limit of {max_steps}."
        )
        await _emit(publisher, "turn.blocked", reason="compound_step_limit", message=msg)
        return TurnOutcome(
            answer_text=msg,
            status="refused",
            plan=plan,
            semantic_query=None,
            routed_sql=None,
            route=None,
            rows_returned=0,
            guardrail_actions=[
                {"layer": "compound", "action": "refuse", "reason": "step_limit_exceeded"}
            ],
            prompt_messages=prompt_messages,
            llm_raw_response=llm_raw_response,
        )

    # Bug-5284 — enforce complexity at the TURN level, not just per step.
    # A compound query with 3 steps of complexity 9 each totals 27, which
    # is far beyond what a single-query max_query_complexity of 10 intends.
    if getattr(cfg, "max_query_complexity", 0) > 0:
        turn_complexity = 0
        for step in call.steps:
            turn_complexity += (
                len(step.measures or [])
                + len(step.dimensions or [])
                + len(step.where or [])
                + len(step.having or [])
                + len(step.sort or [])
            )
        if turn_complexity > cfg.max_query_complexity:
            msg = (
                f"This compound query is too complex for this project's "
                f"configuration (total complexity {turn_complexity} across "
                f"{len(call.steps)} steps exceeds the limit of "
                f"{cfg.max_query_complexity}). Try asking for fewer "
                f"measures or dimensions."
            )
            await _emit(publisher, "turn.blocked", reason="query_too_complex", message=msg)
            return TurnOutcome(
                answer_text=msg,
                status="refused",
                plan=plan,
                semantic_query=None,
                routed_sql=None,
                route=None,
                rows_returned=0,
                guardrail_actions=[
                    {"layer": "budget", "action": "refuse",
                     "reason": "query_too_complex",
                     "detail": f"turn_complexity={turn_complexity}"}
                ],
                prompt_messages=prompt_messages,
                llm_raw_response=llm_raw_response,
            )

    for step in call.steps:
        try:
            UUID(step.model_id)
        except ValueError:
            msg = (
                f"Compound query step '{step.name}' has an invalid model "
                f"reference. Please rephrase your question."
            )
            await _emit(publisher, "turn.blocked", reason="invalid_model_id", message=msg)
            return TurnOutcome(
                answer_text=msg,
                status="refused",
                plan=plan,
                semantic_query=None,
                routed_sql=None,
                route=None,
                rows_returned=0,
                guardrail_actions=[
                    {"layer": "compound", "action": "refuse",
                     "reason": "invalid_model_id", "detail": f"step={step.name}"}
                ],
                prompt_messages=prompt_messages,
                llm_raw_response=llm_raw_response,
            )
        # F-023-07 — the allow-list (and persona scope) for each step is
        # enforced inside execute_query, the single chokepoint shared
        # with the direct query path. Only structural validation stays
        # up here.
        step_call_for_check = QueryToolCall(
            model_id=step.model_id,
            measures=step.measures,
            dimensions=step.dimensions,
            where=step.where,
            having=step.having,
            sort=step.sort,
            limit=step.limit,
            limit_explicit=step.limit_explicit,
            dimension_refs=step.dimension_refs,
            # Bug-5349 Phase 2/3 — carry the structured expression refs so the
            # compound step's projections / structured predicates survive into
            # SQL composition AND so their base fields reach persona-scope
            # validation (Codex-2 — they were silently dropped before).
            projection_refs=step.projection_refs,
            where_refs=step.where_refs,
            having_refs=step.having_refs,
        )
        complexity_reason = check_query_complexity(cfg, step_call_for_check)
        if complexity_reason:
            msg = (
                f"Compound query step '{step.name}' is too complex for this "
                f"project's configuration. Try asking for fewer measures or "
                f"dimensions."
            )
            await _emit(publisher, "turn.blocked", reason=complexity_reason, message=msg)
            return TurnOutcome(
                answer_text=msg,
                status="refused",
                plan=plan,
                semantic_query=None,
                routed_sql=None,
                route=None,
                rows_returned=0,
                guardrail_actions=[
                    {"layer": "budget", "action": "refuse",
                     "reason": complexity_reason,
                     "detail": f"step={step.name}"}
                ],
                prompt_messages=prompt_messages,
                llm_raw_response=llm_raw_response,
            )

    validation_errors = validate_expression(call.expression, call.steps)
    if validation_errors and conversation_id is not None:
        # Bug-5349 — reuse the canonical plan-dict serialiser so the failing
        # JSON shown to the correction LLM preserves every structured form
        # (dimension_exprs, projections, structured where/having). A hand-rolled
        # rebuild from s.where/s.having only would silently drop the structured
        # filters/projections, so the corrected retry could lose them (Codex-R2).
        failing_json = json.dumps(_plan_dict(call), indent=2)
        corrected = await _attempt_tool_call_correction(
            adapter, failing_json, "; ".join(validation_errors),
            db, conversation_id, usage_totals,
        )
        if corrected is not None:
            try:
                new_call = parse_tool_call(corrected)
                if isinstance(new_call, CompoundQueryToolCall):
                    new_errors = validate_expression(
                        new_call.expression, new_call.steps,
                    )
                    # Bug-5349 Phase 2/3 (Codex-R3) — also re-run the pre-execution
                    # bundle validator on the corrected steps. A correction that
                    # fixes the combine expression could still introduce an
                    # invented field inside a structured projection / where /
                    # having; without this re-check it would bypass the metadata
                    # gate and only surface as a raw binder error downstream.
                    new_bundle_issues = validate_tool_call_against_bundle(
                        new_call, bundle,
                    )
                    if not new_errors and not new_bundle_issues:
                        call = new_call
                        plan = _plan_dict(call)
                        validation_errors = []
                        logger.info(
                            "Compound expression correction succeeded on retry"
                        )
            except ToolCallParseError:
                pass
    if validation_errors:
        msg = (
            f"I tried to compute '{call.result_label}' but the expression "
            f"has errors: {'; '.join(validation_errors)}. Could you rephrase?"
        )
        await _emit(publisher, "turn.blocked", reason="expression_invalid", message=msg)
        return TurnOutcome(
            answer_text=msg,
            status="refused",
            plan=plan,
            semantic_query=None,
            routed_sql=None,
            route=None,
            rows_returned=0,
            guardrail_actions=[
                {"layer": "compound", "action": "refuse", "reason": "expression_invalid",
                 "detail": "; ".join(validation_errors)}
            ],
            prompt_messages=prompt_messages,
            llm_raw_response=llm_raw_response,
        )

    step_executions: list[tuple[CompoundStep, QueryExecution]] = []
    combine_ctx: dict[str, Any] = {}
    total_rows = 0

    for step in call.steps:
        step_call = QueryToolCall(
            model_id=step.model_id,
            measures=step.measures,
            dimensions=step.dimensions,
            where=step.where,
            having=step.having,
            sort=step.sort,
            limit=step.limit,
            limit_explicit=step.limit_explicit,
            dimension_refs=step.dimension_refs,
            # Bug-5349 Phase 2/3 / Codex-2 — structured refs flow into execution
            # so compound-step projections/predicates render and their base
            # fields are persona-scope checked at the execute_query chokepoint.
            projection_refs=step.projection_refs,
            where_refs=step.where_refs,
            having_refs=step.having_refs,
        )
        try:
            execution = await execute_query(
                db, step_call, jwt_token,
                allowed_model_ids=bundle.allow_list_model_ids,
                persona_scopes=bundle.persona_scopes,
            )
        except ModelNotAllowListedError:
            msg = (
                f"Compound query step '{step.name}' references a model that "
                f"is not allow-listed. Please rephrase, or ask a modeller to "
                f"allow-list the right model."
            )
            await _emit(publisher, "turn.blocked", reason="model_not_allow_listed", message=msg)
            return TurnOutcome(
                answer_text=msg,
                status="refused",
                plan=plan,
                semantic_query=None,
                routed_sql=None,
                route=None,
                rows_returned=0,
                guardrail_actions=[
                    {"layer": "compound", "action": "refuse",
                     "reason": "model_not_allow_listed",
                     "detail": f"step={step.name}"}
                ],
                prompt_messages=prompt_messages,
                llm_raw_response=llm_raw_response,
            )
        except PersonaScopeViolationError as exc:
            msg = (
                f"Compound query step '{step.name}' needs data that is not "
                f"available to your current persona. Please rephrase using "
                f"the fields available to you."
            )
            await _emit(publisher, "turn.blocked", reason="persona_scope_violation", message=msg)
            return TurnOutcome(
                answer_text=msg,
                status="refused",
                plan=plan,
                semantic_query=None,
                routed_sql=None,
                route=None,
                rows_returned=0,
                guardrail_actions=[
                    {"layer": "compound", "action": "refuse",
                     "reason": "persona_scope_violation",
                     "detail": f"step={step.name}: {exc}"}
                ],
                prompt_messages=prompt_messages,
                llm_raw_response=llm_raw_response,
            )
        except RowSecurityDeniedQueryError:
            # R2 finding B2: a denied step must refuse with the TRUTH, not with
            # "the step failed" (nothing failed) and above all not by feeding a
            # WHERE 0 = 1 zero into the combine expression, which would have
            # the agent state a fabricated business figure.
            msg = (
                f"Compound query step '{step.name}' cannot be answered because "
                f"your row-level security permissions grant you access to none "
                f"of the underlying rows. This is a permissions restriction, "
                f"not an absence of data — no figure can be calculated from "
                f"it. Contact your administrator if you believe you should have "
                f"access."
            )
            await _emit(
                publisher, "turn.blocked",
                reason="row_security_denied", message=msg,
            )
            return TurnOutcome(
                answer_text=msg,
                status="refused",
                plan=plan,
                semantic_query=None,
                routed_sql=None,
                route=None,
                rows_returned=0,
                guardrail_actions=[
                    {"layer": "compound", "action": "refuse",
                     "reason": "row_security_denied",
                     "detail": f"step={step.name}"}
                ],
                prompt_messages=prompt_messages,
                llm_raw_response=llm_raw_response,
            )
        except QueryExecutionError as exc:
            human_msg = _humanize_query_error(str(exc))
            await _emit(publisher, "turn.blocked", reason="step_failed", message=human_msg)
            return TurnOutcome(
                answer_text=human_msg,
                status="refused",
                plan=plan,
                semantic_query=None,
                routed_sql=None,
                route=None,
                rows_returned=0,
                guardrail_actions=[
                    {"layer": "compound", "action": "refuse",
                     "reason": "step_failed", "detail": str(exc)}
                ],
                prompt_messages=prompt_messages,
                llm_raw_response=llm_raw_response,
            )

        step_executions.append((step, execution))
        total_rows += execution.rows_returned

        await _emit(
            publisher,
            "compound.step",
            step_name=step.name,
            model_id=step.model_id,
            rows_returned=execution.rows_returned,
            first_row=execution.rows[0] if execution.rows else {},
        )

    step_rows, step_dimensions, alignment_trace = _prepare_compound_alignment(
        step_executions,
    )

    try:
        result_rows, result_columns, is_multi_row, alignment_mode = evaluate_combine_aligned(
            call.expression, step_rows, step_dimensions, call.result_label,
        )
    except CombineEvalError as exc:
        msg = (
            f"I ran all the sub-queries but the expression for "
            f"'{call.result_label}' failed to evaluate: {exc}"
        )
        await _emit(publisher, "turn.blocked", reason="expression_eval_failed", message=msg)
        return TurnOutcome(
            answer_text=msg,
            status="refused",
            plan=plan,
            semantic_query=None,
            routed_sql=None,
            route=None,
            rows_returned=total_rows,
            guardrail_actions=[
                {"layer": "compound", "action": "refuse",
                 "reason": "expression_eval_failed", "detail": str(exc)}
            ],
            prompt_messages=prompt_messages,
            llm_raw_response=llm_raw_response,
        )

    # Bug-7361 -- when alignment falls back to a stacked mode, the derived
    # metric cannot be computed.  Surface a diagnostic refusal instead of
    # returning an unlabelled stacked table with no computed answer.
    # Covers all three stacked modes: no_shared_dims, no_overlap, and
    # ambiguous_grain (where multiple steps have non-unique keys on the
    # shared dimensions).
    _STACKED_MESSAGES = {
        "stacked_no_shared_dims": (
            "I ran all the sub-queries but could not align them to compute "
            f"'{call.result_label}' -- the sub-queries returned data on "
            "different dimensions with no shared grouping. Please rephrase "
            "so both parts of the question use the same grouping dimension."
        ),
        "stacked_no_overlap": (
            "I ran all the sub-queries but could not align them to compute "
            f"'{call.result_label}' -- the sub-queries share dimensions "
            "but returned non-overlapping values. Please rephrase so both "
            "parts of the question cover the same data range."
        ),
        "stacked_ambiguous_grain": (
            "I ran all the sub-queries but could not align them to compute "
            f"'{call.result_label}' -- the sub-queries returned rows at "
            "different levels of detail (ambiguous grain). Please rephrase "
            "so both parts of the question use the same grouping level."
        ),
    }
    if alignment_mode in _STACKED_MESSAGES:
        _stacked_reason = _STACKED_MESSAGES[alignment_mode]
        await _emit(publisher, "turn.blocked", reason="alignment_failed", message=_stacked_reason)
        return TurnOutcome(
            answer_text=_stacked_reason,
            status="refused",
            plan=plan,
            semantic_query=None,
            routed_sql=None,
            route=None,
            rows_returned=total_rows,
            guardrail_actions=[
                {"layer": "compound", "action": "refuse",
                 "reason": "alignment_failed",
                 "detail": f"alignment_mode={alignment_mode}"}
            ],
            prompt_messages=prompt_messages,
            llm_raw_response=llm_raw_response,
        )

    if not is_multi_row and result_rows:
        first = result_rows[0]
        if call.result_label in first:
            combine_value = first[call.result_label]
        else:
            combine_value = {k: v for k, v in first.items() if k in result_columns}
    else:
        combine_value = result_rows

    # F-023-14 — keep the exact computed values for narration, charts, and
    # persistence. Rounding is applied only to display strings (chart cells
    # and calculation-step labels) so small ratios are not destroyed here.

    await _emit(
        publisher,
        "compound.expression",
        expression=call.expression,
        label=call.result_label,
        value=combine_value if not is_multi_row else f"{len(result_rows)} rows",
    )

    step_summaries = []
    for step, execution in step_executions:
        sample = execution.rows[:_MAX_NARRATE_ROWS]
        step_summaries.append({
            "name": step.name,
            "columns": execution.columns,
            "rows_returned": execution.rows_returned,
            "sample_rows": sample,
            # R10 (compound scope, review R1-3) — full rows for DATE-RANGE
            # aggregation only (never rendered into the prompt). Computing the
            # range from the 25-row sample would understate min/max on sorted
            # results while the narrator is ordered to state it as exact.
            "all_rows": execution.rows,
        })

    computed: dict[str, Any] = {
        "expression": call.expression,
        "label": call.result_label,
        "value": combine_value if not is_multi_row else None,
        "is_multi_row": is_multi_row,
        "alignment_mode": alignment_mode,
        "alignment": alignment_trace,
        # R10 (compound scope, review R1-2) — when any step hit the DB row cap,
        # the combined result derives from PARTIAL step data. The narration
        # prompt must never claim a "COMPLETE result" in that case.
        "steps_truncated": any(
            bool(getattr(execution, "truncated", False))
            for _, execution in step_executions
        ),
    }
    if is_multi_row:
        # R10 (compound scope) — cap the narrator's view at _MAX_NARRATE_ROWS
        # but record the TRUE per-dimension row count so the narration prompt
        # can disclose "showing N of M" and forbid presenting shown-row
        # extremes as the overall extreme. The full result reaches the user via
        # the compound table / chart; only the narrator's sample is capped.
        computed["result_total_rows"] = len(result_rows)
        computed["result_rows"] = result_rows[:_MAX_NARRATE_ROWS]
        computed["result_columns"] = result_columns

    chart_html = ""
    shaped_visual: tuple[str | None, list[str], list[list[Any]]] | None = None
    chart_type = call.chart_type
    chart_type_selector = effective_chart_selector(cfg)
    chart_max_rows = getattr(cfg, "chart_max_rows", 500)
    shape_trace: dict[str, Any] | None = None
    if chart_type == "none":
        chart_type = None

    user_chart = _extract_user_chart_type(user_message)

    if chart_type_selector != "none":
        if is_multi_row and result_rows and result_columns:
            compound_intent = detect_analytical_intent(
                user_message,
                previous_plan=getattr(bundle, "previous_plan", None),
            )
            field_roles = _field_roles_for_compound_result(
                call,
                result_columns,
                result_rows,
            )
            requested_chart = (
                user_chart
                or (chart_type if chart_type_selector == "llm" else None)
                or chart_type_selector
            )
            shape_limits = _shape_limits_from_config(cfg)
            contract = match_shape_contract(
                intent=compound_intent,
                field_roles=field_roles,
                call=call,
                chart_type_selector=requested_chart,
                limits=shape_limits,
            )
            shaped = normalize_result_shape(
                result_columns,
                result_rows,
                compound_intent,
                field_roles,
                contract,
                shape_limits,
            )
            shape_trace = shaped.as_trace()
            computed["shape"] = shape_trace
            chart_type = shaped.chart_type
            if chart_type and chart_type != "kpi":
                if _chart_renderer(cfg) == "html":
                    chart_html = render_chart(
                        chart_type,
                        shaped.columns,
                        shaped.rows,
                        palette=getattr(cfg, "chart_color_palette", "default"),
                        size=getattr(cfg, "chart_size", "md"),
                        include_table=False,
                    ) or ""
                else:
                    shaped_visual = (chart_type, shaped.columns, shaped.rows)
            elif getattr(cfg, "include_data_table", True):
                if _chart_renderer(cfg) == "html":
                    chart_html = render_table(shaped.columns, shaped.rows) or ""
                else:
                    shaped_visual = (None, shaped.columns, shaped.rows)
        elif not is_multi_row and isinstance(combine_value, dict) and len(combine_value) > 1:
            col_order = ["Label", "Value"]
            row_lists = [[label, _round_for_display(val)] for label, val in combine_value.items()]
            if not user_chart:
                chart_type = select_chart_type(
                    {"columns": col_order, "rows": row_lists},
                    max_rows=chart_max_rows,
                )
            else:
                chart_type = user_chart
            if chart_type and chart_type != "kpi":
                if _chart_renderer(cfg) == "html":
                    chart_html = render_chart(
                        chart_type,
                        col_order,
                        row_lists,
                        palette=getattr(cfg, "chart_color_palette", "default"),
                        size=getattr(cfg, "chart_size", "md"),
                        include_table=False,
                    ) or ""
                else:
                    shaped_visual = (chart_type, col_order, row_lists)
        elif not is_multi_row:
            scalar_shape = _scalar_contribution_pie_shape(
                user_message,
                call.result_label,
                combine_value,
                user_chart,
            )
            if scalar_shape is not None:
                visual_chart_type, visual_columns, visual_rows, shape_trace = scalar_shape
                computed["shape"] = shape_trace
                chart_type = visual_chart_type
                shaped_visual = (visual_chart_type, visual_columns, visual_rows)

    output_fmt = getattr(cfg, "agent_output_format", "plain")
    prior_qs = getattr(bundle, "prior_questions", [])
    narr_publisher = _narration_publisher(cfg, publisher)
    try:
        if narr_publisher is not None:
            narration = await narrate_compound_answer_stream(
                adapter, bundle.narration_system, user_message,
                step_summaries, computed, narr_publisher,
                output_format=output_fmt,
                prior_questions=prior_qs,
                on_thinking=on_thinking,
            )
        else:
            narration = await narrate_compound_answer(
                adapter, bundle.narration_system, user_message,
                step_summaries, computed,
                output_format=output_fmt,
                prior_questions=prior_qs,
                on_thinking=on_thinking,
            )
        _accumulate_usage(adapter, usage_totals)
    except Exception as exc:
        # Bug-5957 — do not expose step count, result label, combine
        # value, or raw exception in the user-facing answer.
        logger.exception(
            "Compound narration LLM call failed (steps=%d, label=%s)",
            len(call.steps), call.result_label,
        )
        narration = (
            "The compound query executed successfully but the narration "
            "service could not summarise the result. Please try again."
        )

    output = apply_output_guardrails(cfg, narration)
    narration = output.text

    step_result_dicts = []
    for step, execution in step_executions:
        step_result_dicts.append({
            "name": step.name,
            "columns": execution.columns,
            "rows": execution.rows,
        })

    compound_html = render_compound_result(
        step_result_dicts,
        computed,
        include_step_tables=getattr(cfg, "include_data_table", True),
    )

    if shaped_visual is not None:
        visual_chart_type, visual_columns, visual_rows = shaped_visual
        rendered_output = _render_shaped_output(
            cfg,
            visual_chart_type,
            visual_columns,
            visual_rows,
            legacy_html=compound_html,
        )
    else:
        rendered_output: str | None = (chart_html + compound_html) or None

    semantic_combine_value = (
        combine_value if not is_multi_row
        else [
            {c: r.get(c) for c in result_columns}
            for r in result_rows[:20]
        ]
    )

    semantic = {
        "tool": "compound_query",
        "steps": [
            {
                "name": step.name,
                "model_id": step.model_id,
                "executed_sql": execution.sql,
                "rows_returned": execution.rows_returned,
                "first_row": execution.rows[0] if execution.rows else {},
            }
            for step, execution in step_executions
        ],
        "expression": call.expression,
        "result_label": call.result_label,
        "combine_value": semantic_combine_value,
        "alignment_mode": alignment_mode,
        "alignment": alignment_trace,
        "shape": shape_trace,
    }

    routed_sql = "\n\n".join(
        f"-- step: {step.name}\n{execution.routed_sql or execution.sql}"
        for step, execution in step_executions
    )
    routes = sorted({
        execution.route_type
        for _, execution in step_executions
        if execution.route_type
    })

    # ── Build calculation_steps for frontend accordion ──────────────────
    calc_steps: list[dict[str, Any]] = []
    for step, execution in step_executions:
        row = execution.rows[0] if execution.rows else {}
        # F-023-23(b) — column 0 is the first dimension when a step is
        # grouped; show the step's measure, not a dimension label. Prefer
        # the declared measure name, else the first non-dimension column.
        dim_names = set(step.dimensions or [])
        value_col: str | None = None
        for m in (step.measures or []):
            if m in execution.columns:
                value_col = m
                break
        if value_col is None:
            for c in execution.columns:
                if c not in dim_names:
                    value_col = c
                    break
        if value_col is None and execution.columns:
            value_col = execution.columns[0]
        desc = step.name.replace("_", " ").title()
        val = row.get(value_col) if value_col and row else None
        calc_steps.append({
            "step_number": len(calc_steps) + 1,
            "description": desc,
            "name": step.name,
            "measure": value_col,
            "value": val,
            "formatted_value": str(_round_for_display(val)) if val is not None else "",
        })
    if call.expression:
        calc_steps.append({
            "step_number": len(calc_steps) + 1,
            "description": "Computed result",
            "name": call.result_label,
            "value": combine_value if not is_multi_row else None,
            "formatted_value": (
                str(_round_for_display(combine_value))
                if not is_multi_row else "(multi-row)"
            ),
            "formula": call.expression,
        })

    return TurnOutcome(
        answer_text=narration,
        status="ok",
        plan=plan,
        semantic_query=semantic,
        routed_sql=routed_sql or None,
        route=",".join(routes) if routes else None,
        rows_returned=total_rows,
        guardrail_actions=output.actions,
        citations=None,
        thought_summary=None,  # caller (run_turn) sets this from the shared thinking_parts
        usage_input_tokens=usage_totals["input"],
        usage_output_tokens=usage_totals["output"],
        prompt_messages=prompt_messages,
        llm_raw_response=llm_raw_response,
        rendered_output=rendered_output,
        chart_type=chart_type,
        result_sample=(
            result_rows[:_RESULT_SAMPLE_CAP] if is_multi_row and result_rows
            else [{
                "label": call.result_label,
                "expression": call.expression,
                "value": combine_value,
            }] if combine_value is not None
            else None
        ),
        result_row_count=(
            len(result_rows) if is_multi_row and result_rows
            else 1 if not is_multi_row and combine_value is not None
            else total_rows
        ),
        calculation_steps=calc_steps or None,
    )


async def persist_turn(
    db: AsyncSession,
    conversation: AgentConversation,
    turn_index: int,
    user_message: str,
    outcome: TurnOutcome,
    started_monotonic: float,
    cfg: ProjectAgentConfig | None = None,
    llm_provider: str = "",
    budget_reservation_id: UUID | None = None,
) -> AgentTurn:
    latency_ms = int((time.monotonic() - started_monotonic) * 1000)

    existing = (await db.execute(
        sa_select(AgentTurn).where(
            AgentTurn.conversation_id == conversation.id,
            AgentTurn.turn_index == turn_index,
        )
    )).scalar_one_or_none()

    if existing is not None:
        existing.user_message = user_message
        existing.answer_text = outcome.answer_text
        existing.llm_plan = outcome.plan
        existing.thought_summary = outcome.thought_summary
        existing.semantic_query = outcome.semantic_query
        existing.routed_sql = outcome.routed_sql
        existing.route = outcome.route
        existing.query_result_rows = outcome.rows_returned or None
        existing.query_result_sample = outcome.result_sample or None
        existing.citations = outcome.citations
        existing.guardrail_actions = outcome.guardrail_actions
        existing.status = outcome.status
        existing.latency_ms = latency_ms
        existing.usage_input_tokens = outcome.usage_input_tokens
        existing.usage_output_tokens = outcome.usage_output_tokens
        existing.prompt_messages = outcome.prompt_messages
        existing.llm_raw_response = outcome.llm_raw_response
        existing.rendered_output = outcome.rendered_output
        existing.chart_type = outcome.chart_type
        existing.calculation_steps = outcome.calculation_steps
        existing.recipe_id = outcome.recipe_id
        existing.recipe_steps_executed = outcome.recipe_steps_executed
        turn = existing
    else:
        turn = AgentTurn(
            conversation_id=conversation.id,
            turn_index=turn_index,
            user_message=user_message,
            answer_text=outcome.answer_text,
            llm_plan=outcome.plan,
            thought_summary=outcome.thought_summary,
            semantic_query=outcome.semantic_query,
            routed_sql=outcome.routed_sql,
            route=outcome.route,
            query_result_rows=outcome.rows_returned or None,
            query_result_sample=outcome.result_sample or None,
            citations=outcome.citations,
            guardrail_actions=outcome.guardrail_actions,
            status=outcome.status,
            latency_ms=latency_ms,
            usage_input_tokens=outcome.usage_input_tokens,
            usage_output_tokens=outcome.usage_output_tokens,
            prompt_messages=outcome.prompt_messages,
            llm_raw_response=outcome.llm_raw_response,
            rendered_output=outcome.rendered_output,
            chart_type=outcome.chart_type,
            calculation_steps=outcome.calculation_steps,
            recipe_id=outcome.recipe_id,
            recipe_steps_executed=outcome.recipe_steps_executed,
        )
        db.add(turn)
    if cfg is not None and (outcome.usage_input_tokens > 0 or outcome.usage_output_tokens > 0):
        # Bug-5283 — post-turn budget enforcement. Check BEFORE writing the
        # ledger row to avoid double-counting via SQLAlchemy auto-flush
        # (review R1 5283-F1). The pre-turn check gates new turns; the
        # post-turn check detects when one expensive request pushes spend
        # past the budget within a single turn.
        # Bug-7777 — exclude this turn's pessimistic reservation row. On the
        # happy path the callers reconciled it away before persist_turn, so
        # this is a no-op; when reconcile failed (swallowed warning) it stops
        # the stale reservation from being counted on top of real spend.
        budget_breach = await check_budget_post_turn(
            db, cfg,
            turn_input_tokens=outcome.usage_input_tokens,
            turn_output_tokens=outcome.usage_output_tokens,
            provider=llm_provider,
            exclude_reservation_id=budget_reservation_id,
        )
        await record_turn_cost(
            db=db,
            project_id=cfg.project_id,
            turn_id=turn.id,
            llm_config_id=cfg.answer_llm_config_id,
            provider=llm_provider,
            input_tokens=outcome.usage_input_tokens,
            output_tokens=outcome.usage_output_tokens,
        )
        if budget_breach:
            actions = list(turn.guardrail_actions or [])
            actions.append({
                "layer": "budget",
                "action": "warning",
                "reason": budget_breach,
                "detail": "budget exceeded after this turn completed",
            })
            turn.guardrail_actions = actions
    return turn


async def _run_evaluate_kpi_branch(
    cfg: ProjectAgentConfig,
    call: EvaluateKpiToolCall,
    jwt_token: str,
    publisher: EventPublisher | None,
    usage_totals: dict[str, int] | None = None,
    prompt_messages: dict[str, Any] | None = None,
    llm_raw_response: str | None = None,
    project_id: UUID | None = None,
) -> TurnOutcome:
    if usage_totals is None:
        usage_totals = {"input": 0, "output": 0}
    plan = _plan_dict(call)
    settings = get_settings()
    # F-023-09 — thread the real project id; model-service validates the
    # path segment as a UUID, so the old literal `_` always 422'd.
    project_seg = str(project_id) if project_id is not None else str(cfg.project_id)
    url = (
        f"{settings.MODEL_SERVICE_URL}/api/v1/projects/{project_seg}/models/"
        f"{call.model_id}/kpis/{call.kpi_id}/evaluate"
    )
    # internal_request_headers: agent -> model-service metadata reads are
    # internal pipeline traffic — exempt from the per-tenant rate limiter.
    headers = (
        {"Authorization": f"Bearer {jwt_token}", **internal_request_headers()}
        if jwt_token
        else dict(internal_request_headers())
    )
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, headers=headers)
    except httpx.HTTPError as exc:
        # Bug-5957 — do not expose raw httpx exception to the user.
        logger.warning("KPI evaluation HTTP error: %s", exc)
        return TurnOutcome(
            answer_text=(
                "The KPI evaluation service is temporarily unavailable. "
                "Please try again shortly."
            ),
            status="error",
            plan=plan,
            semantic_query=None,
            routed_sql=None,
            route=None,
            rows_returned=0,
            guardrail_actions=[],
            prompt_messages=prompt_messages,
            llm_raw_response=llm_raw_response,
            usage_input_tokens=usage_totals["input"],
            usage_output_tokens=usage_totals["output"],
        )

    if resp.status_code >= 400:
        # Bug-5957 — do not expose HTTP status or internal response body.
        logger.warning(
            "KPI evaluation failed (HTTP %d): %s",
            resp.status_code, resp.text[:500],
        )
        return TurnOutcome(
            answer_text=(
                "The KPI could not be evaluated at this time. "
                "Please try again or contact your administrator."
            ),
            status="error",
            plan=plan,
            semantic_query=None,
            routed_sql=None,
            route=None,
            rows_returned=0,
            guardrail_actions=[],
            prompt_messages=prompt_messages,
            llm_raw_response=llm_raw_response,
            usage_input_tokens=usage_totals["input"],
            usage_output_tokens=usage_totals["output"],
        )

    data = resp.json()
    parts: list[str] = []
    if data.get("formatted_value") is not None:
        parts.append(f"Value: {data['formatted_value']}")
    if data.get("formatted_goal") is not None:
        parts.append(f"Goal: {data['formatted_goal']}")
    if data.get("status_label"):
        parts.append(f"Status: {data['status_label']}")
    if data.get("trend_label"):
        parts.append(f"Trend: {data['trend_label']}")

    narration = " | ".join(parts) if parts else "KPI evaluation returned no data."
    output = apply_output_guardrails(cfg, narration)
    narration = output.text
    await _emit(_narration_publisher(cfg, publisher), "narration.delta", text=narration)

    return TurnOutcome(
        answer_text=narration,
        status="ok",
        plan=plan,
        semantic_query={"tool": "evaluate_kpi", "model_id": call.model_id, "kpi_id": call.kpi_id, "result": data},
        routed_sql=None,
        route=None,
        rows_returned=1,
        guardrail_actions=output.actions,
        prompt_messages=prompt_messages,
        llm_raw_response=llm_raw_response,
        usage_input_tokens=usage_totals["input"],
        usage_output_tokens=usage_totals["output"],
    )


async def _run_preview_named_set_branch(
    cfg: ProjectAgentConfig,
    call: PreviewNamedSetToolCall,
    jwt_token: str,
    publisher: EventPublisher | None,
    usage_totals: dict[str, int] | None = None,
    prompt_messages: dict[str, Any] | None = None,
    llm_raw_response: str | None = None,
    project_id: UUID | None = None,
) -> TurnOutcome:
    if usage_totals is None:
        usage_totals = {"input": 0, "output": 0}
    plan = _plan_dict(call)
    settings = get_settings()
    # F-023-09 — thread the real project id; the literal `_` segment failed
    # model-service UUID path validation (HTTP 422) before any handler ran.
    project_seg = str(project_id) if project_id is not None else str(cfg.project_id)
    # Bug-8712: the conversational agent is a CONSUMPTION surface, so it previews
    # the DEPLOYED definition. Without the flag a modeller's unsaved expression
    # edit changed the members the agent quoted back to every user, with no
    # Deploy — the same leak as TESSALLITE.LISTBYID, on a different transport.
    # Root contract: the deployed snapshot is the contract, the live state is
    # editor-only (F-013-01).
    url = (
        f"{settings.MODEL_SERVICE_URL}/api/v1/projects/{project_seg}/models/"
        f"{call.model_id}/named-sets/{call.named_set_id}/preview"
        f"?deployed_only=true"
    )
    # internal_request_headers: agent -> model-service metadata reads are
    # internal pipeline traffic — exempt from the per-tenant rate limiter.
    headers = (
        {"Authorization": f"Bearer {jwt_token}", **internal_request_headers()}
        if jwt_token
        else dict(internal_request_headers())
    )
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, headers=headers)
    except httpx.HTTPError as exc:
        # Bug-5957 — do not expose raw httpx exception to the user.
        logger.warning("Named set preview HTTP error: %s", exc)
        return TurnOutcome(
            answer_text=(
                "The named set preview service is temporarily unavailable. "
                "Please try again shortly."
            ),
            status="error",
            plan=plan,
            semantic_query=None,
            routed_sql=None,
            route=None,
            rows_returned=0,
            guardrail_actions=[],
            prompt_messages=prompt_messages,
            llm_raw_response=llm_raw_response,
            usage_input_tokens=usage_totals["input"],
            usage_output_tokens=usage_totals["output"],
        )

    if resp.status_code >= 400:
        # Bug-5957 — do not expose HTTP status or internal response body.
        logger.warning(
            "Named set preview failed (HTTP %d): %s",
            resp.status_code, resp.text[:500],
        )
        return TurnOutcome(
            answer_text=(
                "The named set could not be previewed at this time. "
                "Please try again or contact your administrator."
            ),
            status="error",
            plan=plan,
            semantic_query=None,
            routed_sql=None,
            route=None,
            rows_returned=0,
            guardrail_actions=[],
            prompt_messages=prompt_messages,
            llm_raw_response=llm_raw_response,
            usage_input_tokens=usage_totals["input"],
            usage_output_tokens=usage_totals["output"],
        )

    data = resp.json()
    items = data.get("items", [])
    total = data.get("total_count", len(items))
    truncated = data.get("truncated", False)

    if not items:
        narration = "The named set is empty or could not be resolved."
    else:
        captions = [item.get("caption", str(item)) for item in items[:20]]
        narration = f"Named set contains {total} member{'s' if total != 1 else ''}"
        if truncated:
            narration += f" (showing first {len(captions)})"
        narration += ": " + ", ".join(captions)

    output = apply_output_guardrails(cfg, narration)
    narration = output.text
    await _emit(_narration_publisher(cfg, publisher), "narration.delta", text=narration)

    return TurnOutcome(
        answer_text=narration,
        status="ok",
        plan=plan,
        semantic_query={"tool": "preview_named_set", "model_id": call.model_id, "named_set_id": call.named_set_id, "total_count": total},
        routed_sql=None,
        route=None,
        rows_returned=total,
        guardrail_actions=output.actions,
        prompt_messages=prompt_messages,
        llm_raw_response=llm_raw_response,
        usage_input_tokens=usage_totals["input"],
        usage_output_tokens=usage_totals["output"],
    )


async def _resolve_default_target(
    project_id: str, model_id: str, jwt_token: str, settings,
) -> str | None:
    """Fetch the model's default target_id from the model-service."""
    url = f"{settings.MODEL_SERVICE_URL}/api/v1/projects/{project_id}/models/{model_id}"
    # internal_request_headers: agent -> model-service metadata reads are
    # internal pipeline traffic — exempt from the per-tenant rate limiter.
    headers = (
        {"Authorization": f"Bearer {jwt_token}", **internal_request_headers()}
        if jwt_token
        else dict(internal_request_headers())
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            return resp.json().get("target_id")
    except Exception:
        pass
    return None


async def _run_create_aggregate_branch(
    cfg: ProjectAgentConfig,
    call: CreateAggregateToolCall,
    jwt_token: str,
    publisher: EventPublisher | None,
    usage_totals: dict[str, int],
    *,
    prompt_messages: list | None = None,
    llm_raw_response: str | None = None,
) -> TurnOutcome:
    """Call the optimizer service to create an aggregate from agent chat."""
    settings = get_settings()
    optimizer_url = f"{settings.OPTIMIZER_URL}/api/v1/optimize/run"
    plan = {
        "tool": "create_aggregate",
        "model_id": call.model_id,
        "measures": call.measures,
        "dimensions": call.dimensions,
        "description": call.description,
    }
    await _emit(publisher, "turn.plan", plan=plan)

    target_id = await _resolve_default_target(
        str(cfg.project_id), call.model_id, jwt_token, settings,
    )

    try:
        payload: dict[str, Any] = {
            "model_id": call.model_id,
            "target_id": target_id,
            "max_creates": 1,
            "dry_run": False,
        }
        if call.dimensions:
            payload["requested_grain"] = call.dimensions
        if call.measures:
            payload["requested_measures"] = call.measures
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                optimizer_url,
                json=payload,
                headers={"Authorization": f"Bearer {jwt_token}"},
            )
        if resp.status_code != 200:
            # Bug-5957 — do not expose HTTP status or optimizer response.
            logger.warning(
                "create_aggregate failed (HTTP %d): %s",
                resp.status_code, resp.text[:500],
            )
            narration = (
                "The aggregate could not be created at this time. "
                "Please try again or contact your administrator."
            )
        else:
            data = resp.json()
            created = data.get("aggregates_created", 0)
            ids = data.get("created_aggregate_ids", [])
            if created > 0:
                narration = (
                    f"Created {created} aggregate(s). "
                    f"Aggregate IDs: {', '.join(str(i) for i in ids)}."
                )
            else:
                errors = data.get("errors", [])
                candidates = data.get("candidates_found", 0)
                if errors:
                    # Bug-5957 — log optimizer errors; show safe message.
                    logger.warning("No aggregates created: %s", "; ".join(errors))
                    narration = (
                        "No aggregates were created. The optimizer could not "
                        "find suitable candidates. Try running more queries "
                        "first or contact your administrator."
                    )
                elif candidates == 0:
                    narration = (
                        "No aggregate candidates found. The optimizer requires "
                        "query miss patterns before it can create aggregates. "
                        "Try running some queries first."
                    )
                else:
                    narration = "Optimizer ran but did not create any new aggregates."
    except Exception as exc:
        # Bug-5957 — do not expose raw exception to the user.
        logger.exception("create_aggregate branch failed")
        narration = (
            "The aggregate creation service is temporarily unavailable. "
            "Please try again shortly."
        )

    # Review R1 5279-F1 — create_aggregate must apply output guardrails
    # (disclosure text, content rules) like every other tool branch.
    output = apply_output_guardrails(cfg, narration)
    narration = output.text
    await _emit(_narration_publisher(cfg, publisher), "narration.delta", text=narration)

    return TurnOutcome(
        answer_text=narration,
        status="ok",
        plan=plan,
        semantic_query=plan,
        routed_sql=None,
        route=None,
        rows_returned=0,
        guardrail_actions=output.actions,
        prompt_messages=prompt_messages,
        llm_raw_response=llm_raw_response,
        usage_input_tokens=usage_totals["input"],
        usage_output_tokens=usage_totals["output"],
    )
