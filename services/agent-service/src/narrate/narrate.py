"""Narration: take rows + the original question, produce the user-facing answer.

This is the second LLM call per turn. The system prompt carries the project
context, brand guidelines, content rules, and safety policy. The user prompt
frames the task conversationally: what the user asked, the answer data, any
prior question history, and a concrete output format example for this turn.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from shared.llm.adapter import LLMAdapter
from src.exec.query import QueryExecution
from src.narrate.formatting import (
    aggregate_date_ranges,
    build_format_hints,
    extract_date_ranges,
    format_rows,
)

if TYPE_CHECKING:
    from src.sse.events import EventPublisher

logger = logging.getLogger(__name__)

_MAX_ROWS_FOR_NARRATION = 25

_GUARD_NO_DERIVE = (
    "Never compute or state percentages, ratios, growth rates, sums, or "
    "differences unless they appear verbatim in the rows. "
    "You may note which item is largest or smallest and how many items are "
    "shown — these are readings of the data, not computations."
)

_GUARD_DATE_RANGE = (
    " State the date range exactly as the \"Date range in the data\" line gives "
    "it — read the boundaries from there, never infer or round them. "
    "If the data spans January to August, say \"from January to August\", "
    "not \"throughout the year\" or the full period."
)

# Bug-5351 — when the result hit the query's row cap, the data shown is only the
# first part of the full set. The narrator must say so instead of implying the
# answer is complete.
_GUARD_TRUNCATED = (
    " The results were limited to the first rows by a row cap and do not cover "
    "the full set — say plainly that the figures shown are a partial view "
    "limited to the first N rows and the complete data extends further."
)

# Combined form kept for tests that assert on its substrings.
_NARRATION_GUARD = _GUARD_NO_DERIVE + _GUARD_DATE_RANGE

# Compound queries: computed.value IS the derived figure — the single-query guard
# would forbid stating it. This guard explicitly permits it and blocks anything beyond.
_GUARD_COMPOUND = (
    "The only derived figure you may state is the server-calculated result in "
    "computed.value — do not introduce any other arithmetic. "
    "For compound answers, user-visible facts are limited to computed.value "
    "and computed.result_rows. Do not cite step row values, numerator values, "
    "denominator values, or intermediate totals unless those same values are "
    "also present in computed.value or computed.result_rows. "
    "Do not calculate or state the remaining, rest-of-total, complement, or "
    "non-selected share for a percentage result unless that row is explicitly "
    "present in computed.result_rows. "
    "Do not claim a chart type or visual breakdown unless it is explicitly "
    "present in the computed result rows or deterministic shape facts. "
    "You may describe direction (higher, lower, unchanged) based on the sign "
    "of the computed value — this is a reading, not a computation. "
    "Never add percentages, ratios, or growth rates not present in the data."
)

_TREND_SUMMARIZATION_INSTRUCTION = (
    "\n\nTREND DATA SUMMARY RULES (Crucial for time-series/large datasets):\n"
    "If the data is a time-series trend containing more than 5 rows:\n"
    "1. Do NOT list out every single date/time and its corresponding value in prose.\n"
    "2. State the total range spanned (e.g., 'From May 2025 to June 2026').\n"
    "3. Identify and state the absolute lowest and highest values along with their dates/times (peaks and valleys).\n"
    "4. Describe the overall trajectory in natural prose (e.g., whether the values generally increased, decreased, fluctuated, or remained stable over the period).\n"
    "5. Direct the user's attention to the rendered chart or data table for the detailed day-to-day values."
)

_SHAPE_FACT_RULES = (
    "Use the deterministic shape facts below as the contract for summary claims. "
    "Do not claim a series ends early unless its last_period says so. "
    "Do not compare months as bare numbers when full period keys are present. "
    "Do not claim top or bottom rankings unless ranking facts are present. "
    "Disclose table-only output when chart_notes or table facts say the requested chart was rejected. "
    "Allowed insight statements are readings of provided facts only: peak, trough, first, last, largest, smallest, row count, missing periods, and truncation."
)


def _prior_questions_line(prior_questions: list[str]) -> str:
    if not prior_questions:
        return ""
    quoted = " / ".join(f'"{q}"' for q in prior_questions)
    return (
        f"The user previously asked: {quoted}. "
        "Reference prior context where it adds value to the answer."
    )


def _build_format_block(
    columns: list[str],
    sample_rows: list[dict],
    output_format: str,
    is_empty: bool = False,
    has_dates: bool = False,
    currency_symbol: str = "",
    currency_code: str = "",
    truncated: bool = False,
) -> str:
    """Return task description + format instruction + concrete example for this turn."""
    if is_empty:
        task = (
            "Tell the user clearly that no data was found for their question. "
            "Do not speculate about the reason. "
            "Use the tone and language specified in the system prompt."
        )
        if output_format == "markup":
            return task + "\nFormat your answer using markdown."
        if output_format in ("html", "rich_html"):
            return task + "\nFormat your answer as HTML."
        return task + "\nWrite in natural prose without markdown symbols or HTML tags."

    guard = (
        _GUARD_NO_DERIVE
        + (_GUARD_DATE_RANGE if has_dates else "")
        + (_GUARD_TRUNCATED if truncated else "")
    )
    task = (
        "Write a proper conversational answer to the user's question based "
        "only on the answer information above. "
        + guard + " "
        "Use the tone and language specified in the system prompt."
    )

    if has_dates and len(sample_rows) > 5:
        task += _TREND_SUMMARIZATION_INSTRUCTION

    if currency_symbol or currency_code:
        task += (
            f"\n\nCURRENCY FORMATTING RULES:\n"
            f"When presenting monetary amounts, always prepend the currency symbol '{currency_symbol}' and/or append the currency code '{currency_code}' (e.g., '{currency_symbol}1,234.56 {currency_code}'). "
            "Never cite plain raw numbers for currency values."
        )

    rows = sample_rows[:3]

    def _label(row: dict) -> str:
        return str(row.get(columns[0], "")) if columns else ""

    def _values(row: dict) -> str:
        return ", ".join(str(row.get(c, "")) for c in columns[1:]) if len(columns) > 1 else ""

    if output_format == "markup":
        lines = []
        for row in rows[:2]:
            lbl, vals = _label(row), _values(row)
            lines.append(f"**{lbl}:** {vals}" if vals else f"**{lbl}**")
        example = "\n".join(lines)
        return (
            task + "\n"
            "Format your answer using markdown — bold key figures with **text**, "
            "use bullet lists for item breakdowns.\n"
            f"Example:\n{example}"
        )

    if output_format == "html":
        lines = []
        for row in rows[:2]:
            lbl, vals = _label(row), _values(row)
            lines.append(
                f"<p><strong>{lbl}:</strong> {vals}</p>" if vals
                else f"<p><strong>{lbl}</strong></p>"
            )
        example = "\n".join(lines)
        return (
            task + "\n"
            "Format your answer as HTML — use <p> for paragraphs, "
            "<strong> for key figures, <ul>/<li> for lists.\n"
            f"Example:\n{example}"
        )

    if output_format == "rich_html":
        first = rows[0] if rows else {}
        lbl, vals = _label(first), _values(first)
        header = f"<p><strong>{lbl}:</strong> {vals}</p>" if vals else f"<p><strong>{lbl}</strong></p>"
        if len(rows) > 1:
            li = "".join(
                f"<li><strong>{_label(r)}:</strong> {_values(r)}</li>" if _values(r)
                else f"<li>{_label(r)}</li>"
                for r in rows[1:]
            )
            example = f"{header}\n<ul>{li}</ul>"
        else:
            example = header
        return (
            task + "\n"
            "Format your answer as rich HTML — use <p>, <strong>, "
            "<ul>/<li> for lists, <table>/<thead>/<tbody>/<tr>/<th>/<td> for tabular data.\n"
            f"Example:\n{example}"
        )

    # plain / json — full-coverage prose sentence using all available sample rows
    prose_rows = sample_rows  # no cap: show the complete set so the model learns full coverage
    if prose_rows:
        def _prose_item(row: dict) -> str:
            lbl = _label(row)
            vals = _values(row)
            return f"{lbl} at {vals}" if vals else lbl

        items = [_prose_item(r) for r in prose_rows]
        if len(items) == 1:
            example = items[0] + "."
        elif len(items) == 2:
            example = f"{items[0]}, and {items[1]}."
        else:
            dim = columns[0].replace("_", " ") if columns else ""
            lead = f"The breakdown by {dim}: " if dim else ""
            example = lead + ", ".join(items[:-1]) + f", and {items[-1]}."
        return (
            task + "\n"
            "Write in natural prose without markdown symbols or HTML tags. "
            "Use short paragraphs; do not return one dense block of text.\n"
            f"Example:\n{example}"
        )
    return (
        task + "\n"
        "Write in natural prose without markdown symbols or HTML tags. "
        "Use short paragraphs; do not return one dense block of text."
    )


def _build_compound_format_block(
    computed: dict,
    output_format: str,
    has_dates: bool = False,
) -> str:
    """Return task description + format instruction + example for a compound-query result."""
    label = str(computed.get("label", "result"))
    value = computed.get("value")
    val_str = str(value) if value is not None else ""

    guard = _GUARD_COMPOUND + (_GUARD_DATE_RANGE if has_dates else "")
    label_lower = label.lower()
    is_pct = any(k in label_lower for k in ("%", "percentage", "percent", "share", "rate", "ratio"))
    pct_rule = (
        "\nWhen presenting percentage or rate values, use the % symbol "
        "(e.g., '27.27%'), not the word 'percent'. "
        "The % symbol is a unit notation, not a currency symbol."
    ) if is_pct else ""
    is_multi_row = bool(computed.get("is_multi_row") and computed.get("result_rows"))
    if is_multi_row:
        task = (
            "Write a proper conversational answer. "
            "The computed results contain per-dimension values in result_rows. "
            "Present these values clearly — summarise key figures, highlight "
            "the highest and lowest, and note the overall pattern. "
            "Use the exact computed values — do not recalculate or approximate. "
            + guard + pct_rule + " "
            "Use the tone and language specified in the system prompt."
        )
    else:
        task = (
            "Write a proper conversational answer. State the computed result first, "
            "then provide brief context without citing internal step values. "
            "Use the exact computed value — do not recalculate or approximate. "
            + guard + pct_rule + " "
            "Use the tone and language specified in the system prompt."
        )

    def _directional_example(fmt: str = "plain") -> str:
        """Build a sign-aware, comma-formatted example sentence."""
        if isinstance(value, (int, float)):
            if value < 0:
                num = f"{abs(value):,.2f}"
                direction = "down"
            elif value > 0:
                num = f"{value:,.2f}"
                direction = "up"
            else:
                num = "0"
                direction = "unchanged at"
            prose = f"{label}: {direction} {num}."
        else:
            prose = f"{label}: {val_str}."

        if fmt == "markup":
            return f"**{label}:** {direction} **{num}**." if isinstance(value, (int, float)) else f"**{label}:** {val_str}."
        if fmt in ("html", "rich_html"):
            return f"<p><strong>{label}:</strong> {prose}</p>"
        return prose

    if output_format == "markup":
        return (
            task + "\n"
            "Format your answer using markdown — bold key figures with **text**, "
            "use bullet lists for step context.\n"
            f"Example:\n{_directional_example('markup')}"
        )
    if output_format == "html":
        return (
            task + "\n"
            "Format your answer as HTML — use <p>, <strong>, <ul>/<li>.\n"
            f"Example:\n{_directional_example('html')}"
        )
    if output_format == "rich_html":
        return (
            task + "\n"
            "Format your answer as rich HTML — use <p>, <strong>, "
            "<ul>/<li>, <table> elements.\n"
            f"Example:\n{_directional_example('rich_html')}"
        )
    # plain
    example = _directional_example()
    return (
        task + "\n"
        "Write in natural prose without markdown symbols or HTML tags. "
        "Use short paragraphs; do not return one dense block of text.\n"
        f"Example:\n{example}"
    )


def _build_narrate_prompt(
    project_system_prompt: str,
    user_message: str,
    execution: QueryExecution,
    measure_formats: dict[str, str | None] | None = None,
    output_format: str = "plain",
    currency_symbol: str = "",
    currency_code: str = "",
    prior_questions: list[str] | None = None,
    shape_trace: dict[str, Any] | None = None,
) -> tuple[str, str]:
    sample = execution.rows[:_MAX_ROWS_FOR_NARRATION]
    total = execution.rows_returned
    showing = len(sample)

    # ── answer information block ──────────────────────────────────────────
    mf = measure_formats or {}
    if mf and total > 0:
        formatted_sample = format_rows(
            sample, execution.columns, mf,
            currency_symbol=currency_symbol, currency_code=currency_code,
        )
        format_hint = build_format_hints(
            mf, currency_symbol=currency_symbol, currency_code=currency_code,
        )
    else:
        formatted_sample = sample
        format_hint = ""

    truncated = bool(getattr(execution, "truncated", False))
    if total == 0:
        row_count = "No data was found for this query."
    else:
        parts = [f"{total} rows returned"]
        if truncated:
            parts.append(
                "this is the row cap — more rows exist beyond it, so the figures "
                "below are only the first part of the full set"
            )
        if showing < total:
            parts.append(f"{showing} shown — summarise key patterns from the sample")
        elif not truncated:
            parts.append("all shown")
        row_count = "; ".join(parts) + "."

    # ── date ranges ───────────────────────────────────────────────────────
    # Bug-5350 — compute the date span from the FULL result, not the truncated
    # narration sample, so the stated range is the data's real min/max rather
    # than the boundaries of the first N sorted rows.
    date_ranges: dict = {}
    if total > 0:
        date_ranges = extract_date_ranges(execution.rows, execution.columns)

    # ── instruction block (first) ─────────────────────────────────────────
    fmt_block = _build_format_block(
        execution.columns, formatted_sample, output_format,
        is_empty=(total == 0), has_dates=bool(date_ranges),
        currency_symbol=currency_symbol, currency_code=currency_code,
        truncated=truncated,
    )
    instr_parts = [fmt_block]
    if shape_trace:
        instr_parts.append(_SHAPE_FACT_RULES)
    pq = _prior_questions_line(prior_questions or [])
    if pq:
        instr_parts.append(pq)
    instruction_block = "## Instructions\n\n" + "\n\n".join(instr_parts)

    # ── data block (second) ───────────────────────────────────────────────
    data_parts = [f"User question: {user_message}", row_count]

    if date_ranges:
        dr_lines = [f"  {col}: {mn} to {mx}" for col, (mn, mx) in date_ranges.items()]
        data_parts.append("Date range in the data:\n" + "\n".join(dr_lines))

    shape_facts = _shape_facts_payload(shape_trace)
    if shape_facts:
        data_parts.append(
            "Deterministic shape facts for narration:\n"
            + json.dumps(shape_facts, default=str)
        )

    body: dict[str, Any] = {
        "columns": execution.columns,
        "rows_returned": total,
        "sample_rows": formatted_sample,
    }
    data_parts.append(json.dumps(body, default=str))

    if format_hint:
        data_parts.append(
            format_hint + "\n"
            "Note: measure values in sample_rows are pre-formatted — "
            "quote them exactly, do not reformat or round."
        )

    data_block = "## Data\n\n" + "\n\n".join(data_parts)

    return project_system_prompt, "\n\n".join([instruction_block, data_block])


async def narrate_answer(
    adapter: LLMAdapter,
    project_system_prompt: str,
    user_message: str,
    execution: QueryExecution,
    measure_formats: dict[str, str | None] | None = None,
    output_format: str = "plain",
    currency_symbol: str = "",
    currency_code: str = "",
    prior_questions: list[str] | None = None,
    shape_trace: dict[str, Any] | None = None,
    on_thinking=None,
) -> str:
    system, user_prompt = _build_narrate_prompt(
        project_system_prompt, user_message, execution, measure_formats,
        output_format=output_format,
        currency_symbol=currency_symbol,
        currency_code=currency_code,
        prior_questions=prior_questions,
        shape_trace=shape_trace,
    )
    return (await adapter.complete(system, user_prompt, on_thinking=on_thinking)).strip()


async def narrate_answer_stream(
    adapter: LLMAdapter,
    project_system_prompt: str,
    user_message: str,
    execution: QueryExecution,
    publisher: "EventPublisher",
    measure_formats: dict[str, str | None] | None = None,
    output_format: str = "plain",
    currency_symbol: str = "",
    currency_code: str = "",
    prior_questions: list[str] | None = None,
    shape_trace: dict[str, Any] | None = None,
    on_thinking=None,
) -> str:
    """Stream narration tokens through publisher, return full assembled text."""
    system, user_prompt = _build_narrate_prompt(
        project_system_prompt, user_message, execution, measure_formats,
        output_format=output_format,
        currency_symbol=currency_symbol,
        currency_code=currency_code,
        prior_questions=prior_questions,
        shape_trace=shape_trace,
    )
    chunks: list[str] = []
    async for token in adapter.stream_complete(system, user_prompt, on_thinking=on_thinking):
        chunks.append(token)
        await publisher.emit("narration.delta", text=token)
    return "".join(chunks).strip()


def _build_compound_narrate_prompt(
    project_system_prompt: str,
    user_message: str,
    step_summaries: list[dict],
    computed: dict,
    output_format: str = "plain",
    prior_questions: list[str] | None = None,
) -> tuple[str, str]:
    label = str(computed.get("label", "result"))
    value = computed.get("value")

    # ── answer information block ──────────────────────────────────────────
    # ── date ranges ───────────────────────────────────────────────────────
    date_ranges = aggregate_date_ranges(step_summaries)

    # ── instruction block (first) ─────────────────────────────────────────
    fmt_block = _build_compound_format_block(computed, output_format, has_dates=bool(date_ranges))
    instr_parts = [fmt_block]
    shape_facts = _shape_facts_payload(computed.get("shape") if isinstance(computed, dict) else None)
    if shape_facts:
        instr_parts.append(_SHAPE_FACT_RULES)
    pq = _prior_questions_line(prior_questions or [])
    if pq:
        instr_parts.append(pq)
    instruction_block = "## Instructions\n\n" + "\n\n".join(instr_parts)

    # ── data block (second) ───────────────────────────────────────────────
    is_multi_row = bool(computed.get("is_multi_row") and computed.get("result_rows"))
    data_parts = [
        f"User question: {user_message}",
        "This answer was computed across multiple query steps.",
    ]
    if is_multi_row:
        n = len(computed["result_rows"])
        data_parts.append(
            f"Computed results ({label}): {n} rows of per-dimension values "
            f"— see computed.result_rows below (server-calculated — do not recalculate)."
        )
    else:
        data_parts.append(
            f"Computed result ({label}): {value} (server-calculated — do not recalculate)."
        )

    if date_ranges:
        dr_lines = [f"  {col}: {mn} to {mx}" for col, (mn, mx) in date_ranges.items()]
        data_parts.append("Date range in the step data:\n" + "\n".join(dr_lines))

    if shape_facts:
        data_parts.append(
            "Deterministic shape facts for narration:\n"
            + json.dumps(shape_facts, default=str)
        )

    computed_body: dict[str, Any] = {
        "expression": computed.get("expression"),
        "label": computed.get("label"),
        "value": computed.get("value"),
    }
    if is_multi_row:
        computed_body["result_rows"] = computed["result_rows"]
        computed_body["result_columns"] = computed.get("result_columns", [])

    step_context = [
        {
            "name": step.get("name"),
            "columns": step.get("columns"),
            "rows_returned": step.get("rows_returned"),
        }
        for step in step_summaries
    ]
    body: dict[str, Any] = {
        "steps": step_context,
        "computed": computed_body,
    }
    data_parts.append(json.dumps(body, default=str))

    data_block = "## Data\n\n" + "\n\n".join(data_parts)

    return project_system_prompt, "\n\n".join([instruction_block, data_block])


def _shape_facts_payload(shape_trace: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(shape_trace, dict):
        return None
    facts = shape_trace.get("narration_facts")
    if not isinstance(facts, dict):
        return None
    return {
        "shape": shape_trace.get("shape"),
        "chart_type": shape_trace.get("chart_type"),
        "output_mode": shape_trace.get("output_mode"),
        "quality_findings": shape_trace.get("quality_findings") or [],
        "notes": shape_trace.get("notes") or [],
        "narration_facts": facts,
    }


async def narrate_compound_answer(
    adapter: LLMAdapter,
    project_system_prompt: str,
    user_message: str,
    step_summaries: list[dict],
    computed: dict,
    output_format: str = "plain",
    prior_questions: list[str] | None = None,
    on_thinking=None,
) -> str:
    system, user_prompt = _build_compound_narrate_prompt(
        project_system_prompt, user_message, step_summaries, computed,
        output_format=output_format,
        prior_questions=prior_questions,
    )
    return (await adapter.complete(system, user_prompt, on_thinking=on_thinking)).strip()


async def narrate_compound_answer_stream(
    adapter: LLMAdapter,
    project_system_prompt: str,
    user_message: str,
    step_summaries: list[dict],
    computed: dict,
    publisher: "EventPublisher",
    output_format: str = "plain",
    prior_questions: list[str] | None = None,
    on_thinking=None,
) -> str:
    system, user_prompt = _build_compound_narrate_prompt(
        project_system_prompt, user_message, step_summaries, computed,
        output_format=output_format,
        prior_questions=prior_questions,
    )
    chunks: list[str] = []
    async for token in adapter.stream_complete(system, user_prompt, on_thinking=on_thinking):
        chunks.append(token)
        await publisher.emit("narration.delta", text=token)
    return "".join(chunks).strip()
