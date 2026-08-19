"""Judge LLM call — evaluates agent answers against a rubric.

The judge runs after the answer is produced. It receives the planner's
full payload (system prompt + conversation history + question) and the
narration output (plan, data, answer). It returns:
  {"verdict": "pass|warn|fail", "reasoning": "...", "metrics": {...}}

If the judge LLM call fails or returns malformed JSON, we mark the turn
verdict as `unknown` rather than blocking the user — the answer has
already been delivered.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import AgentJudgeRubric, ProjectAgentConfig
from shared.llm.adapter import build_adapter, RetryingAdapter
from shared.llm.config_resolution import resolve_agent_llm_config
logger = logging.getLogger(__name__)


# R2 (F2) — distillation is a STRIP-list, not a keep-list (review R1 finding 1).
# Only the headings below — non-evidential planner instructions the judge never
# scores against (task examples, trust-hierarchy meta-rules, output schemas) —
# are removed from the judge's evidence pack. EVERYTHING ELSE IS KEPT, so a
# renamed or newly added planner section degrades to extra distractor tokens,
# never to lost evidence (fail-safe in the correctness direction). The
# evidential set below documents the sections we expect to keep; a
# producer-derived parity test asserts the assembler's headings are exactly the
# union of the two sets, so any assembler heading change breaks a test instead
# of silently changing what the judge sees.
_JUDGE_NON_EVIDENTIAL_HEADINGS: frozenset[str] = frozenset(
    {
        "## TASK",
        "## RUNTIME INPUT ROBUSTNESS",
        "## OUTPUT FORMAT",
    }
)

_JUDGE_EVIDENTIAL_HEADINGS: frozenset[str] = frozenset(
    {
        "## PROJECT CONTEXT",
        "## AVAILABLE MODELS",
        "## GROUNDING",
        "## CROSS-MODEL RECIPES",
    }
)


def build_judge_evidence(
    system_prompt: str,
    system_sections: list[tuple[str, str]] | None,
    grounding_matches: str | None,
    mode: str,
) -> str:
    """Assemble the judge's view of what the planner saw (R2/F2 + intake fix).

    ``mode == "full"`` returns the planner system prompt verbatim (the pre-R2
    behaviour, kept as the project-level escape hatch).

    ``mode == "distilled"`` strips ONLY the known non-evidential boilerplate
    sections (``_JUDGE_NON_EVIDENTIAL_HEADINGS``) and keeps every other
    section — including any heading it does not recognise, so planner-prompt
    drift can only ADD distractor tokens, never remove evidence. It ALWAYS
    appends the per-turn GROUNDING MATCHES block so the judge sees the
    retrieved glossary cards the planner used for term resolution in
    two-tier / retrieval-only glossary modes (intake fix — those cards live in
    the planner's user suffix, not the system prompt).

    Correctness guards — the judge must never score on LESS evidence than the
    planner had:
      * ``system_sections`` missing/empty (an older caller that only passed the
        joined string) → fall back to the full ``system_prompt``.
      * distillation that would keep NO section at all (every heading in the
        strip-list — degenerate input) → fall back to full + warn, rather than
        hand the judge an empty evidence view.
    """
    grounding_block = (grounding_matches or "").strip()

    def _with_grounding(body: str) -> str:
        if grounding_block:
            return f"{body}\n\n## GROUNDING MATCHES\n{grounding_block}"
        return body

    if mode == "full":
        # Full mode still surfaces the retrieved cards — they are evidence the
        # planner acted on, and in full-glossary mode they are already inside
        # the verbatim system prompt so grounding_block is empty (no dup).
        return _with_grounding(system_prompt)

    if not system_sections:
        # No section breakdown available — never distil blindly; use the full
        # prompt so the judge keeps every piece of evidence.
        logger.warning(
            "Judge evidence: distilled mode requested but no system_sections "
            "provided; falling back to the full planner system prompt."
        )
        return _with_grounding(system_prompt)

    kept: list[str] = []
    for heading, body in system_sections:
        if heading in _JUDGE_NON_EVIDENTIAL_HEADINGS:
            continue  # known boilerplate — the only thing distillation removes
        if heading not in _JUDGE_EVIDENTIAL_HEADINGS:
            # Unknown heading — keep it (fail-safe: cost tokens, never
            # evidence) and warn so the drift is noticed and classified.
            logger.warning(
                "Judge evidence: unrecognised planner section %r kept in the "
                "distilled pack (fail-safe). Classify it in judge.py's "
                "strip/keep sets.",
                heading,
            )
        kept.append(f"{heading}\n{body}")

    if not kept:
        # Every section was in the strip-list (degenerate input) — fail loud
        # and fall back to the full prompt (never judge on nothing).
        logger.warning(
            "Judge evidence: distilled mode kept no sections at all; falling "
            "back to the full planner system prompt so no evidence is lost."
        )
        return _with_grounding(system_prompt)

    return _with_grounding("\n\n".join(kept))


JUDGE_INSTRUCTIONS = """\
## TASK

You are an EVALUATION JUDGE for a conversational analytics agent.

The conversational agent works as follows: it receives a user question,
creates a semantic query plan against a data model, a program executes
that plan against the semantic layer and returns result rows, and the
agent then converts those results into conversational language, optionally
suggesting a chart type for data presentation.

You will be provided with:
1. A RUBRIC to assess the correctness and compliance of the agent's
   outcome.
2. The inputs the agent received:
   A) The human user's current question.
   B) Previous questions in the conversation (to resolve references to
      prior subjects — e.g. "break that down", "same period", "show me
      that by region").
   C) The agent's CONTEXT — the evidential parts of the agent's prompt:
      project context (role, locale, safety policy), the available models
      with their field lists and per-attribute semantics, the grounding
      rules with the glossary / term index / alias maps, the cross-model
      recipes, and — when present — a GROUNDING MATCHES section holding the
      glossary cards retrieved for THIS question. This is the same model and
      grounding evidence the agent planned against. (Non-evidential planner
      boilerplate — task examples, output-format schemas, expression rules —
      is intentionally omitted; do not treat its absence as missing context.)
   D) The DATE ANCHOR — the current date the agent resolved relative dates
      ("last month", "this quarter", "YTD") against, plus the same
      relative-date rules the agent saw. Use this as the authoritative
      "today" for date-range verification. If no DATE ANCHOR section is
      present, do not assume any particular current date — verify date
      ranges only against the question and the returned data.
3. The agent's outputs:
   A) The semantic query plan (QUERY PLAN).
   B) The result rows from query execution (DATA RETURNED).
   C) The conversational answer text (ASSISTANT ANSWER).

Your job:
i)   Check the LOGIC of the semantic query plan against the semantic
     model — did the agent select the correct model, measures, dimensions,
     and filters for the question asked?
ii)  Assess the QUALITY of the narration — did the agent convert query
     results into accurate, clear conversational language that follows the
     narration rules?
iii) Verify FOLLOW-UP RESOLUTION — if the question references a prior
     turn, did the agent correctly interpret the reference using
     conversation history?
iv)  Check SAFETY AND COMPLIANCE — does the answer respect the project's
     safety policy, content rules, and brand guidelines?
v)   Score the agent's overall outcome against each RUBRIC section.

You do NOT answer the user's question.
You do NOT rewrite the query.
You do NOT suggest improvements to the answer.
You produce EXACTLY one JSON verdict.

EVALUATION STEPS:
1. Read the RUBRIC. Each section title becomes a key in your metrics dict.
   IMPORTANT: The examples below use illustrative metric keys ("Factual
   accuracy", "Query correctness", etc.) for demonstration only. When a
   custom rubric is provided, use the actual rubric section titles as your
   metric keys — do NOT copy the example keys.
2. Read the QUERY PLAN and verify:
   - Every measure, dimension, and filter field exists in AVAILABLE MODELS.
   - The selected model is appropriate for the question.
   - Filters and date ranges match the question's intent. Resolve any
     relative dates in the question ("last month", "this quarter", "YTD")
     against the DATE ANCHOR (2D) — that is the current date the agent used.
     Do not assume today's date from your own clock.
   - If the question is a follow-up, verify the plan correctly carries
     forward or modifies the prior query's scope using CONVERSATION HISTORY.
3. Compare the ASSISTANT ANSWER to DATA RETURNED:
   - Check that every number, label, and ranking in the answer matches
     the evidence rows.
   - Check that the answer does not fabricate values absent from the rows.
4. Check narration rule compliance:
   - The answer must NOT compute, calculate, or estimate derived values
     (percentages, ratios, growth rates, differences) from the data.
     It should only cite values directly present in the result rows.
     If derived values appear in the answer but not in the rows, this
     is a narration rule violation.
   - Time periods must state the exact date range present in the data.
     The answer must not generalise to full years, full months, or full
     quarters unless the data actually covers the entire period.
   - If the answer includes pre-formatted currency or numeric values,
     they should be quoted as-is from the data, not re-rounded or
     reformatted.
   - The answer MUST NOT invent a currency symbol or code (e.g. "USD",
     "$", "£", "GBP") unless that symbol or code appears verbatim in
     the DATA RETURNED rows or in the agent's CONTEXT (2C). Transactions
     may be in mixed currencies; adding any currency label not present
     in the data is a factual violation.
5. Check compliance — does the answer respect the safety policy, brand
   guidelines, and content rules defined in PROJECT CONTEXT?
6. Score each rubric section 0.0 to 1.0 and determine the verdict.

SCORING GUIDE:
- 1.0 = fully meets the rubric section
- 0.5 = partially meets with minor gaps
- 0.0 = fails the section or contradicts the data
- verdict "pass": every metric >= 0.8
- verdict "warn": any metric between 0.5 and 0.8 (and none below 0.5)
- verdict "fail": any metric < 0.5

EDGE CASES:
- Empty result set: if DATA RETURNED has 0 rows, the answer should say so
  explicitly. Score factual accuracy on whether the answer acknowledges the
  empty result, not on data matching.
- Truncated sample: DATA RETURNED may show fewer rows than the total
  result set. The header states "N total, showing M". If M < N, claims
  about data beyond the shown sample (e.g. peaks, tails, outliers, or
  rankings that depend on unseen rows) CANNOT be verified. Do not treat
  such claims as confirmed. Note each unverifiable claim explicitly in
  your reasoning and reduce the relevant metric score proportionally to
  the verification gap. Only claims grounded in the visible rows may
  receive full credit.
- Follow-up without history: if the question appears to reference a prior
  turn but no CONVERSATION HISTORY is provided, note this in reasoning.
  Do not penalise the query plan for missing context you also lack.
- Clarifications and refusals: if the agent used "clarify" or "refuse"
  instead of querying, score based on whether the reason was valid given
  the available models and the safety policy.
- Disclosure text: the answer may include an auto-appended disclosure
  footer (e.g. "This response was generated by an AI assistant...").
  This is added by the platform, not by the agent. Do not penalise or
  credit the agent for its presence or content.
- Fuzzy Numeric Rule: Standard formatting changes (adding thousands
  separators like `1,090,764,614.56` instead of `1090764614.56`,
  appending currency symbols like `£` or `$`, or rounding to two
  decimal places) are considered standard, correct representations
  of the numerical data in the rows. This is NOT a fabrication or a
  narration violation. If the system lists a value under
  "FORMAT-EQUIVALENT VALUES CONFIRMED BY SYSTEM", the value is
  pre-verified as format-equivalent — do NOT flag it.
- Chart type: if the QUERY PLAN includes a chart_type, assess whether
  it is reasonable for the data shape. Rules (violations reduce Presentation score):
    * ANY chart type other than kpi when the result is a single value or
      single row → always wrong. kpi is the only valid type for a single value.
    * bar, h_bar, line, pie, grouped_bar, stacked_bar for a no-dimension
      aggregate query → wrong; kpi is correct.
    * pie with more than 7 categories → wrong; bar or h_bar is correct.
    * pie with any zero or negative value → wrong.
    * pie with a time-based dimension → wrong; line is correct.
    * line without a date or time dimension → wrong.
  If no chart_type is present, do not penalise — chart suggestion is optional.

EXAMPLES:

Example A — PASS (all metrics >= 0.8):
Question: "What was total revenue last month?"
Answer: "Acme Corp's total base amount for April 2026 was 2,847,391.00."
Plan: {"query": {"model_id": "...", "measures": ["base_amount"], "where": [{"name": "business_date", "op": "between", "value": ["2026-04-01","2026-04-30"]}], "limit": 100}}
Data: [{"base_amount": 2847391.00}]
Verdict:
{"verdict": "pass", "reasoning": "The answer correctly reports the single base_amount value from the data. The query selects the right measure with an appropriate date filter for 'last month'. No derived values computed. Time range stated. No currency label invented — the data contained no currency column.", "metrics": {"Factual accuracy": 1.0, "Query correctness": 1.0, "Completeness": 0.9, "Presentation": 1.0, "Narration rules": 1.0, "Compliance": 1.0}}

Example B — WARN (one metric between 0.5 and 0.8):
Question: "Show me transaction count by country"
Answer: "The UK leads with 45,231 transactions, followed by the US at 28,102."
Plan: {"query": {"model_id": "...", "measures": ["transaction_count"], "dimensions": ["country_code"], "limit": 100}}
Data: [{"country_code": "GB", "transaction_count": 45231}, {"country_code": "US", "transaction_count": 28102}, {"country_code": "DE", "transaction_count": 12847}, {"country_code": "FR", "transaction_count": 9433}]
Verdict:
{"verdict": "warn", "reasoning": "Numbers for GB and US match the data exactly, and the query is correct. However, the answer omits DE (12,847) and FR (9,433) from the 4-row result set — all returned data should be narrated. No time range stated. These gaps reduce completeness.", "metrics": {"Factual accuracy": 1.0, "Query correctness": 1.0, "Completeness": 0.6, "Presentation": 0.8, "Narration rules": 1.0, "Compliance": 1.0}}

Example C — FAIL (one metric < 0.5):
Question: "What is the chargeback rate?"
Answer: "The chargeback rate is 3.2%, representing USD 91,117 in chargebacks."
Plan: {"query": {"model_id": "...", "measures": ["chargeback_amount"], "limit": 100}}
Data: [{"chargeback_amount": 142850.75}]
Verdict:
{"verdict": "fail", "reasoning": "The answer claims USD 91,117 but the data shows USD 142,850.75 — factual mismatch. The 3.2% rate is a derived value computed by the agent, violating the narration rule against computing derived metrics. The query only fetches chargeback_amount without a denominator measure. Multiple failures across accuracy and narration compliance.", "metrics": {"Factual accuracy": 0.0, "Query correctness": 0.4, "Completeness": 0.3, "Presentation": 0.8, "Narration rules": 0.0, "Compliance": 0.9}}

Example D — FOLLOW-UP with conversation history:
History: [User: "Show me revenue for April", Assistant: "Total base amount for April 2026 was 2,847,391.00."]
Question: "Break that down by payment method"
Plan: {"query": {"model_id": "...", "measures": ["base_amount"], "dimensions": ["payment_method"], "where": [{"name": "business_date", "op": "between", "value": ["2026-04-01","2026-04-30"]}], "limit": 100}}
Data: [{"payment_method": "Credit Card", "base_amount": 1283451.25}, {"payment_method": "Debit Card", "base_amount": 842190.5}, ...]
Verdict:
{"verdict": "pass", "reasoning": "The follow-up 'break that down' correctly references the prior turn's revenue query. The plan preserves the April 2026 date filter and adds payment_method as a dimension. The base_amount measure is carried forward. Follow-up resolution is correct.", "metrics": {"Factual accuracy": 1.0, "Query correctness": 1.0, "Completeness": 0.9, "Presentation": 1.0, "Narration rules": 1.0, "Compliance": 1.0}}

OUTPUT FORMAT:
Reply with EXACTLY one JSON object — no prose, no markdown fences, no explanation outside the JSON:

{
  "verdict": "pass" | "warn" | "fail",
  "reasoning": "<one short paragraph>",
  "metrics": { "<rubric section title>": <0.0 to 1.0>, ... }
}
"""


_RAW_OUTPUT_CAP = 500
"""Max chars of raw LLM output kept for diagnostics on malformed judge responses."""

_ABBREV_MULTIPLIERS = {
    "k": 1_000, "K": 1_000,
    "M": 1_000_000,
    "B": 1_000_000_000, "bn": 1_000_000_000, "Bn": 1_000_000_000,
    "T": 1_000_000_000_000, "tn": 1_000_000_000_000, "Tn": 1_000_000_000_000,
}


def _normalize_number(value: Any) -> str:
    """Strip formatting from a string/int/float, returning a canonical decimal string."""
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return f"{round(value, 2):.2f}"
    text = str(value)
    text = re.sub(r'[£$€¥₹]', '', text)
    text = re.sub(r'\b(GBP|USD|EUR|JPY|INR|AUD|CAD|CNY|CHF)\b', '', text, flags=re.IGNORECASE)
    text = text.replace(',', '')
    text = re.sub(r'\s', '', text)
    match = re.search(r'([-]?\d+\.?\d*)\s*([kKMBT](?:n)?)?', text)
    if not match:
        return text.strip()
    try:
        num = float(match.group(1))
        suffix = match.group(2)
        if suffix and suffix in _ABBREV_MULTIPLIERS:
            num *= _ABBREV_MULTIPLIERS[suffix]
        return f"{round(num, 2):.2f}"
    except ValueError:
        return text.strip()


def _extract_all_numbers(data: Any) -> list[str]:
    """Recursively extract all normalized numbers from nested data structures."""
    if isinstance(data, (int, float)):
        return [_normalize_number(data)]
    if isinstance(data, str):
        n = _normalize_number(data)
        return [n] if n and re.match(r'[-]?\d+\.?\d*', n) else []
    if isinstance(data, list):
        results: list[str] = []
        for item in data:
            results.extend(_extract_all_numbers(item))
        return results
    if isinstance(data, dict):
        results: list[str] = []
        for v in data.values():
            results.extend(_extract_all_numbers(v))
        return results
    return []


_NUM_PATTERN = (
    r'(?:[£$€¥₹]\s*)?'
    r'\d{1,3}(?:,\d{3})*(?:\.\d+)?'
    r'(?:\s*[kKMBT](?:n)?)?'
    r'(?:\s*(?:GBP|USD|EUR|JPY|INR|AUD|CAD|CNY|CHF))?'
)


def _extract_answer_numbers(answer_text: str) -> list[str]:
    """Extract all normalized number tokens from the answer text."""
    matches = re.findall(_NUM_PATTERN, answer_text)
    return [_normalize_number(m) for m in matches if _normalize_number(m)]


def _find_format_equivalents(
    answer_text: str,
    sample_rows: list[dict[str, Any]],
) -> list[str]:
    """Find numbers in the answer that are format-equivalent to data values.

    Returns a list of answer substrings (as they appear in the answer)
    that correspond to format-equivalent values in the data source.
    These should be excluded from fabrication checks by the judge.
    """
    answer_nums = _extract_answer_numbers(answer_text)
    source_nums = set(_extract_all_numbers(sample_rows))

    verified: list[str] = []
    for match in re.finditer(_NUM_PATTERN, answer_text):
        original = match.group()
        normalized = _normalize_number(original)
        if normalized and normalized in source_nums:
            verified.append(original)
    return verified


@dataclass
class JudgeOutcome:
    verdict: str
    reasoning: str
    metrics: dict[str, Any]
    # Debug-only diagnostic (Bug-6336): truncated raw LLM text captured when
    # the judge response could not be parsed. Never persisted to a
    # user-visible column and never copied into ``reasoning`` — callers must
    # surface it only to logs or an admin-only trace, never to end users.
    raw_output: str | None = None
    usage_input_tokens: int = 0
    usage_output_tokens: int = 0
    provider: str = ""


def _format_rubric(rubric: AgentJudgeRubric | None) -> str:
    if rubric is None or not rubric.sections:
        return "(no rubric configured — apply your default judgement.)"
    lines: list[str] = []
    for s in rubric.sections:
        if not isinstance(s, dict):
            continue
        title = (s.get("title") or s.get("name") or "Section").strip()
        body = (s.get("body") or s.get("text") or "").strip()
        bullets = s.get("bullets") or []
        lines.append(f"### {title}")
        if body:
            lines.append(body)
        if isinstance(bullets, list):
            for b in bullets:
                if isinstance(b, str) and b.strip():
                    lines.append(f"- {b.strip()}")
    return "\n".join(lines) if lines else "(rubric is empty)"


def _format_conversation_history(
    history: list[dict[str, str]] | None,
) -> str:
    if not history:
        return "(first turn — no prior context)"
    lines: list[str] = []
    for entry in history:
        role = entry.get("role", "")
        content = entry.get("content", "")
        if role == "user":
            lines.append(f"User: {content}")
        elif role == "assistant":
            lines.append(f"Assistant: {content}")
    return "\n".join(lines) if lines else "(first turn — no prior context)"


def _build_judge_user_prompt(
    rubric: AgentJudgeRubric | None,
    user_message: str,
    conversation_history: list[dict[str, str]] | None,
    evidence: str,
    plan: dict[str, Any] | None,
    sample_rows: list[dict[str, Any]],
    total_rows: int,
    answer_text: str,
    verified_matches: list[str] | None = None,
    date_anchor: str | None = None,
) -> str:
    # ``evidence`` is the judge's view of what the planner saw (R2/F2): either
    # the distilled evidence pack (model layer + grounding + retrieved cards,
    # boilerplate stripped) or, in full mode / on a fallback, the verbatim
    # planner system prompt. Either way it is the SAME evidence the planner
    # acted on — the judge never sees less.
    #
    # ``date_anchor`` (integration fix) is the per-turn ``## DATE ANCHOR`` block
    # Lane G moved OUT of the cacheable planner system prefix and into the
    # planner's user suffix. It is NOT part of ``evidence`` (built only from the
    # system sections / grounding cards), so without threading it here the judge
    # would run with no CURRENT_DATE and JUDGE_INSTRUCTIONS step 2 (date-range
    # verification) would be unanchored. It renders in the per-turn user prompt
    # (never in JUDGE_INSTRUCTIONS, which is cache_system_prefix=True — a daily
    # date there would break that cache) so the judge anchors on the SAME date
    # the planner did, in both judge_context_mode values.
    anchor_block = (date_anchor or "").strip()
    parts: list[str] = [
        "## 1. RUBRIC\n",
        f"Score each section 0.0-1.0:\n\n{_format_rubric(rubric)}",
        "\n\n## 2. AGENT INPUT\n",
        f"### A) Current question\n\n{user_message}",
        f"\n\n### B) Conversation history\n\n"
        f"{_format_conversation_history(conversation_history)}",
        f"\n\n### C) Agent context (models, grounding, glossary the planner "
        f"saw)\n\n{evidence}",
    ]
    if anchor_block:
        parts.append(
            f"\n\n### D) Date anchor (the current date the planner resolved "
            f"relative dates against)\n\n## DATE ANCHOR\n{anchor_block}"
        )
    parts += [
        "\n\n## 3. AGENT OUTPUT\n",
        f"### A) Query plan\n\n{json.dumps(plan or {}, default=str)}",
        f"\n\n### B) Data returned ({total_rows} total, "
        f"showing {len(sample_rows)})\n\n"
        f"{json.dumps(sample_rows, default=str)}"
        + (f"\n\n**NOTE — PARTIAL SAMPLE:** Only {len(sample_rows)} of "
           f"{total_rows} total result rows are shown above. Claims in the "
           f"answer about data beyond this sample (e.g. peaks, tails, "
           f"outliers, rankings depending on unseen rows) cannot be verified. "
           f"Do NOT treat such claims as confirmed — note each unverifiable "
           f"claim in your reasoning and reduce metric scores proportionally."
           if total_rows > len(sample_rows) else ""),
    ]
    if verified_matches:
        parts.append(
            "\n\n### FORMAT-EQUIVALENT VALUES CONFIRMED BY SYSTEM\n"
            f"The following numbers in the assistant's answer are "
            f"pre-verified by the system as format-equivalent to values "
            f"in the DATA RETURNED (standard formatting differences only — "
            f"thousands separators, currency symbols, rounding). Do NOT flag "
            f"these as fabrications:\n"
            f"{', '.join(verified_matches)}"
        )
    parts.append(f"\n\n### C) Assistant answer\n\n{answer_text}")
    return "".join(parts)


def _parse_judge_json(raw: str) -> JudgeOutcome:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise
        obj = json.loads(match.group(0))
    if not isinstance(obj, dict):
        raise ValueError("Judge did not return a JSON object")
    verdict = str(obj.get("verdict") or "unknown").lower()
    if verdict not in ("pass", "warn", "fail"):
        verdict = "unknown"
    reasoning = str(obj.get("reasoning") or "")
    metrics = obj.get("metrics") or {}
    if not isinstance(metrics, dict):
        metrics = {}
    return JudgeOutcome(verdict=verdict, reasoning=reasoning, metrics=metrics)


async def _resolve_judge_context_mode(db: AsyncSession, project_id) -> str:
    """R2 (F2) PER-PROJECT escape hatch — 'distilled' (default) or 'full'.

    Resolved through the project-level setting ``agent.judge_context_mode``
    (spec section 7: a judge-quality regression observed in one project's
    judge-block-rate KPI must be revertible for THAT project without a deploy
    and without touching other projects). Falls back to the registry default
    ('distilled') when unset. Any resolver failure or unknown value resolves
    to 'distilled' — the accuracy-neutral-to-positive default — with a
    warning, never an exception (the judge must still run).
    """
    try:
        from shared.config.resolver import get_setting

        # Review R2 finding 1 / R3 finding 1 — run the settings SELECT under a
        # SAVEPOINT. A DB-level failure would otherwise leave the shared
        # AsyncSession in an aborted-transaction state (later commit/get →
        # PendingRollbackError, verdict discarded). A full ``db.rollback()``
        # is NOT the answer: rollback expires every loaded ORM instance
        # (rubric/cfg/turn), and the next lazy attribute read raises
        # MissingGreenlet under AsyncSession — same discarded verdict, plus a
        # regression for non-DB resolver failures on a healthy session. The
        # savepoint confines the failure: on error only the savepoint rolls
        # back, the outer transaction and all loaded instances stay intact.
        async with db.begin_nested():
            mode = await get_setting(
                "agent.judge_context_mode",
                tenant_session=db,
                project_id=project_id,
            )
    except Exception:
        logger.warning(
            "Judge context mode could not be resolved for project %s; "
            "defaulting to 'distilled'.",
            project_id,
            exc_info=True,
        )
        return "distilled"
    return "full" if mode == "full" else "distilled"


async def run_judge(
    db: AsyncSession,
    cfg: ProjectAgentConfig,
    system_prompt: str,
    user_message: str,
    plan: dict[str, Any] | None,
    answer_text: str,
    sample_rows: list[dict[str, Any]] | None,
    result_row_count: int | None = None,
    conversation_history: list[dict[str, str]] | None = None,
    system_sections: list[tuple[str, str]] | None = None,
    grounding_matches: str | None = None,
    date_anchor: str | None = None,
) -> JudgeOutcome:
    """Run the judge LLM. Caller decides sync vs background dispatch.

    R2 (F2): ``system_sections`` (the planner prompt's ordered (heading, body)
    list) and ``grounding_matches`` (the per-turn retrieved glossary cards) let
    the judge receive a DISTILLED evidence pack — all evidential context, none
    of the non-evidential planner boilerplate. ``system_prompt`` remains the
    full-mode value and the fallback whenever distillation cannot run, so the
    judge never scores on less evidence than the planner had. Callers that do
    not pass ``system_sections`` get the full prompt (back-compatible).

    Integration fix: ``date_anchor`` is the per-turn ``## DATE ANCHOR`` block
    Lane G moved out of the cacheable system prefix into the planner user
    suffix. It is threaded through here (not part of ``system_sections`` /
    ``grounding_matches``) and rendered in the judge user prompt so the judge
    anchors relative-date verification on the SAME current date the planner
    used. Callers that do not pass it simply omit the anchor block.
    """

    # Scoped to the project that owns this config. PUT/PATCH /agent/config
    # prove a SUBMITTED judge_rubric_id belongs to the path project, but a
    # ``project_agent_configs`` row bound to another project's rubric before
    # that guard existed still resolved through a bare ``db.get``, and the
    # rubric's ``sections`` are rendered into the judge prompt — another
    # project's rubric TEXT deciding how this project's answers are judged.
    # A rubric this project does not own resolves to nothing, which is exactly
    # what an unset judge_rubric_id already means: no rubric configured.
    rubric: AgentJudgeRubric | None = None
    if cfg.judge_rubric_id is not None:
        rubric = (
            await db.execute(
                select(AgentJudgeRubric)
                .where(AgentJudgeRubric.project_id == cfg.project_id)
                .where(AgentJudgeRubric.id == cfg.judge_rubric_id)
            )
        ).scalars().one_or_none()

    try:
        llm_config = await resolve_agent_llm_config(cfg.project_id, "judge", db)
    except ValueError as exc:
        logger.warning("Judge LLM not resolvable: %s", exc)
        return JudgeOutcome(
            verdict="unknown",
            # Bug-5957 — do not expose raw exception in reasoning; it
            # can reach end users via TurnResponse.judge_reasoning.
            reasoning="Judge could not run: LLM configuration issue.",
            metrics={},
        )

    provider = llm_config.provider or ""

    try:
        adapter = RetryingAdapter(build_adapter(llm_config))
    except ValueError as exc:
        logger.warning("Judge adapter build failed: %s", exc)
        return JudgeOutcome(
            verdict="unknown",
            # Bug-5957 — do not expose raw exception in reasoning.
            reasoning="Judge could not run: LLM configuration issue.",
            metrics={},
            provider=provider,
        )

    _JUDGE_SAMPLE_CAP = 50
    rows_sample = (sample_rows or [])[:_JUDGE_SAMPLE_CAP]
    total_rows = (
        result_row_count
        if result_row_count is not None
        else len(sample_rows or [])
    )

    # ── Fuzzy numeric normalisation ──────────────────────────────────────
    verified_matches = (
        _find_format_equivalents(answer_text, rows_sample)
        if answer_text and rows_sample
        else []
    )

    # R2 (F2) — build the judge's evidence view (distilled by default; verbatim
    # in 'full' mode / on any distillation fallback) and thread in the retrieved
    # glossary cards the planner saw (intake fix).
    evidence = build_judge_evidence(
        system_prompt=system_prompt,
        system_sections=system_sections,
        grounding_matches=grounding_matches,
        mode=await _resolve_judge_context_mode(db, cfg.project_id),
    )

    judge_user = _build_judge_user_prompt(
        rubric=rubric,
        user_message=user_message,
        conversation_history=conversation_history,
        evidence=evidence,
        plan=plan,
        sample_rows=rows_sample,
        total_rows=total_rows,
        answer_text=answer_text,
        verified_matches=verified_matches or None,
        date_anchor=date_anchor,
    )

    try:
        # R1 (F1) — JUDGE_INSTRUCTIONS is a fixed ~2k-token block identical on
        # every judge call; mark it cacheable so repeated judge invocations
        # (async every-turn default) serve it from the provider cache. The
        # marker never changes the rendered text.
        raw = await adapter.complete(
            JUDGE_INSTRUCTIONS, judge_user, cache_system_prefix=True
        )
    except Exception as exc:
        logger.exception("Judge LLM call failed")
        return JudgeOutcome(
            verdict="unknown",
            # Bug-5957 — do not expose raw exception in reasoning.
            reasoning="Judge could not run: evaluation service error.",
            metrics={},
            provider=provider,
        )

    usage = getattr(adapter, "last_usage", None) or {}
    in_tok = int(usage.get("input_tokens") or 0)
    out_tok = int(usage.get("output_tokens") or 0)

    try:
        outcome = _parse_judge_json(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning(
            "Judge produced malformed JSON: %s — raw output: %s",
            exc, raw[:_RAW_OUTPUT_CAP],
        )
        return JudgeOutcome(
            verdict="unknown",
            reasoning="Judge evaluation could not be completed: the judge "
                      "output was not in the expected format.",
            metrics={},
            raw_output=raw[:_RAW_OUTPUT_CAP],
            usage_input_tokens=in_tok,
            usage_output_tokens=out_tok,
            provider=provider,
        )
    outcome.usage_input_tokens = in_tok
    outcome.usage_output_tokens = out_tok
    outcome.provider = provider
    return outcome
