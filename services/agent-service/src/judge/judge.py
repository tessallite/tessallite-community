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

from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import AgentJudgeRubric, ProjectAgentConfig
from shared.llm.adapter import build_adapter, RetryingAdapter
from shared.llm.config_resolution import resolve_agent_llm_config
logger = logging.getLogger(__name__)


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
   C) The agent's complete system prompt — copied verbatim. This
      includes the agent's task definition, runtime robustness rules,
      project context, available models with field lists and source
      statistics, grounding rules with glossary, cross-model recipes,
      and output format schemas.
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
2. Read the QUERY PLAN and verify:
   - Every measure, dimension, and filter field exists in AVAILABLE MODELS.
   - The selected model is appropriate for the question.
   - Filters and date ranges match the question's intent.
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
     the DATA RETURNED rows or in the agent's system prompt. Transactions
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
- Truncated sample: DATA RETURNED may show fewer rows than the total.
  If the answer references values outside the sample, you cannot verify them.
  Do not penalise unverifiable claims — note the limitation in reasoning.
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
    system_prompt: str,
    plan: dict[str, Any] | None,
    sample_rows: list[dict[str, Any]],
    total_rows: int,
    answer_text: str,
    verified_matches: list[str] | None = None,
) -> str:
    parts: list[str] = [
        "## 1. RUBRIC\n",
        f"Score each section 0.0-1.0:\n\n{_format_rubric(rubric)}",
        "\n\n## 2. AGENT INPUT\n",
        f"### A) Current question\n\n{user_message}",
        f"\n\n### B) Conversation history\n\n"
        f"{_format_conversation_history(conversation_history)}",
        f"\n\n### C) Agent system prompt\n\n{system_prompt}",
        "\n\n## 3. AGENT OUTPUT\n",
        f"### A) Query plan\n\n{json.dumps(plan or {}, default=str)}",
        f"\n\n### B) Data returned ({total_rows} total, "
        f"showing {len(sample_rows)})\n\n"
        f"{json.dumps(sample_rows, default=str)}"
        + (f"\n\n**NOTE:** Only {len(sample_rows)} of {total_rows} rows are "
           f"shown above. The agent's answer may reference data from rows not "
           f"included in this sample. Do NOT penalise the answer for "
           f"referencing values that could exist in the unseen rows."
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
) -> JudgeOutcome:
    """Run the judge LLM. Caller decides sync vs background dispatch."""

    rubric: AgentJudgeRubric | None = None
    if cfg.judge_rubric_id is not None:
        rubric = await db.get(AgentJudgeRubric, cfg.judge_rubric_id)

    try:
        llm_config = await resolve_agent_llm_config(cfg.project_id, "judge", db)
    except ValueError as exc:
        logger.warning("Judge LLM not resolvable: %s", exc)
        return JudgeOutcome(
            verdict="unknown",
            reasoning=f"Judge LLM not configured: {exc}",
            metrics={},
        )

    provider = llm_config.provider or ""

    try:
        adapter = RetryingAdapter(build_adapter(llm_config))
    except ValueError as exc:
        logger.warning("Judge adapter build failed: %s", exc)
        return JudgeOutcome(
            verdict="unknown",
            reasoning=f"Judge LLM API key missing: {exc}",
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

    judge_user = _build_judge_user_prompt(
        rubric=rubric,
        user_message=user_message,
        conversation_history=conversation_history,
        system_prompt=system_prompt,
        plan=plan,
        sample_rows=rows_sample,
        total_rows=total_rows,
        answer_text=answer_text,
        verified_matches=verified_matches or None,
    )

    try:
        raw = await adapter.complete(JUDGE_INSTRUCTIONS, judge_user)
    except Exception as exc:
        logger.exception("Judge LLM call failed")
        return JudgeOutcome(
            verdict="unknown",
            reasoning=f"Judge LLM call failed: {exc}",
            metrics={},
            provider=provider,
        )

    usage = getattr(adapter, "last_usage", None) or {}
    in_tok = int(usage.get("input_tokens") or 0)
    out_tok = int(usage.get("output_tokens") or 0)

    try:
        outcome = _parse_judge_json(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("Judge produced malformed JSON: %s", exc)
        return JudgeOutcome(
            verdict="unknown",
            reasoning=f"Judge output malformed: {raw[:300]}",
            metrics={},
            usage_input_tokens=in_tok,
            usage_output_tokens=out_tok,
            provider=provider,
        )
    outcome.usage_input_tokens = in_tok
    outcome.usage_output_tokens = out_tok
    outcome.provider = provider
    return outcome
