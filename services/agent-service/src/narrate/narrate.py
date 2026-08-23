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

# R10 — when only the narration sample is capped (the result IS complete and
# every returned row is delivered to the user, but the narrator sees fewer
# rows), the narrator must not claim the query was truncated or that data is
# missing. Instead it must acknowledge it is summarising from a sample and
# never present any aggregate, extreme, or total derived from that sample as
# exact/complete.
# NOTE: distinct wording from _GUARD_TRUNCATED on purpose — saying the data was
# "limited by a row cap" here would be false, since every row IS in the result.
# NOTE: says "returned to the user", not "in the data table" — the rendered
# table/chart is configuration-dependent (include_data_table, chart_max_rows),
# so a table promise could itself be a false disclosure.
_GUARD_NARRATOR_SAMPLED = (
    " You are shown only the first {showing} of {total} rows of a COMPLETE "
    "result — every returned row is delivered to the user even though you "
    "cannot see them all. Do not claim the data was truncated, capped, or "
    "limited, and do not imply rows are missing. Your summary is based on this "
    "sample: never present any total, sum, average, maximum, minimum, or count "
    "derived from the sample as an exact figure for the whole result; describe "
    "such readings as based on the shown rows only."
)

# R10 (combined case) — the DB row cap (100 default / 1000 trend floor) is far
# ABOVE the 25-row narration cap, so a truncated result usually still has
# 100-1000 materialized rows the user receives while the narrator sees 25.
# _GUARD_TRUNCATED only discloses that rows beyond the DB cap are missing; it
# does NOT scope the narrator's 25-row view against rows 26..cap, which DO
# exist and ARE delivered to the user. Without this addendum the narrator may
# state a peak/largest-item from its 25 rows that the user's own table
# contradicts. Fired IN ADDITION to _GUARD_TRUNCATED when the sample is also
# capped. Deliberately does not say "do not claim the data was truncated"
# (it was) nor "COMPLETE result" (it is not).
_GUARD_TRUNCATED_SAMPLED = (
    " Additionally, you are shown only the first {showing} of the {total} "
    "returned rows. Never present a maximum, minimum, largest or smallest "
    "item, total, sum, average, or count read from the rows shown to you as "
    "the extreme or total of the returned result — the returned rows extend "
    "beyond those you can see."
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

# R10 — sampled variant of the trend rules, used whenever the narrator sees
# fewer rows than were materialized (both the complete-result and the
# DB-truncated case). Commanding "state the absolute lowest and highest
# values" (rule 3 above) would order the narrator to present a sample-derived
# extreme as the true extreme — the exact silent-wrong-number path the R10
# guards forbid. This variant scopes extremes and trajectory to the shown
# rows. Wording notes: it says "returned rows/result", never "complete
# result" (false in the truncated case); it makes no claim about WHICH part
# of the period the shown rows are (the first rows in RESULT ORDER are the
# period's end for newest-first sorts, and nothing in particular for
# unordered output); and it does not promise a rendered table or complete
# series (both are configuration-dependent).
_TREND_SUMMARIZATION_INSTRUCTION_SAMPLED = (
    "\n\nTREND DATA SUMMARY RULES (Crucial for time-series/large datasets):\n"
    "If the data is a time-series trend containing more than 5 rows:\n"
    "1. Do NOT list out every single date/time and its corresponding value in prose.\n"
    "2. State the total range spanned using the \"Date range in the data\" line "
    "— it is computed from ALL returned rows, not just the rows shown to you.\n"
    "3. You are shown only the first rows of the returned result: describe the "
    "lowest and highest values AMONG THE ROWS SHOWN and say so explicitly — "
    "never call them the absolute peak or valley, because the true extremes "
    "may lie in rows you cannot see. If deterministic shape facts provide "
    "peak or trough values, state those instead — they cover all returned rows.\n"
    "4. Describe the trajectory of the rows shown in natural prose, making "
    "clear it covers only the portion of the series you can see.\n"
    "5. Direct the user's attention to the returned results for the detailed "
    "day-to-day values."
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
    sample_capped: bool = False,
    sample_showing: int = 0,
    sample_total: int = 0,
    row_security_denied: bool = False,
) -> str:
    """Return task description + format instruction + concrete example for this turn.

    ``row_security_denied`` (Bug-8453) means the router reported the deny-all
    row-security sentinel for this execution. It takes priority over
    ``is_empty``: both produce zero usable rows, but only one of them is true
    to say out loud.

    ``truncated`` means the DB execution hit a row cap (Bug-5351): rows beyond
    the cap do not exist in the result and the complete data extends further.
    ``sample_capped`` means the narrator sees fewer rows than were materialized
    (R10): the narration prompt is capped at ``_MAX_ROWS_FOR_NARRATION``.
    Guard selection (R10):
      * truncated only            -> ``_GUARD_TRUNCATED``
      * sample_capped only        -> ``_GUARD_NARRATOR_SAMPLED`` (denies a row
        cap; result is complete)
      * truncated + sample_capped -> ``_GUARD_TRUNCATED`` plus
        ``_GUARD_TRUNCATED_SAMPLED`` (row cap IS real, and additionally the
        narrator must not present shown-row readings as extremes of the
        returned rows, which extend to the cap)
    ``sample_showing`` / ``sample_total`` fill the "first N of M" disclosures.
    """
    if row_security_denied:
        # Bug-8453: a row-security deny-all is a fact about the CALLER'S
        # PERMISSIONS, not about the business. Telling a user "there is no data
        # for that" when their policy grants them no rows is a false assertion,
        # and it also hides a misconfigured policy from the person best placed
        # to report it. Name the restriction; never guess at or describe the
        # rule contents (the agent is given rule IDS only, no predicate SQL).
        task = (
            "Tell the user that no answer can be shown because their "
            "row-level security permissions do not grant them access to any "
            "of the underlying rows for this question. Make clear this is a "
            "permissions restriction, NOT a statement that the data does not "
            "exist or that the value is zero. Suggest they contact their "
            "Tessallite administrator if they believe they should have access. "
            "Do not speculate about what the data would show, do not invent a "
            "number, and do not describe the security rules themselves. "
            "Use the tone and language specified in the system prompt."
        )
        if output_format == "markup":
            return task + "\nFormat your answer using markdown."
        if output_format in ("html", "rich_html"):
            return task + "\nFormat your answer as HTML."
        return task + "\nWrite in natural prose without markdown symbols or HTML tags."

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
        + (
            _GUARD_TRUNCATED_SAMPLED.format(
                showing=sample_showing, total=sample_total
            )
            if truncated and sample_capped
            else ""
        )
        + (
            _GUARD_NARRATOR_SAMPLED.format(
                showing=sample_showing, total=sample_total
            )
            if sample_capped and not truncated
            else ""
        )
    )
    task = (
        "Write a proper conversational answer to the user's question based "
        "only on the answer information above. "
        + guard + " "
        "Use the tone and language specified in the system prompt."
    )

    if has_dates and len(sample_rows) > 5:
        # R10 — whenever the narrator sees fewer rows than were materialized
        # (regardless of DB truncation), it must not be ordered to state
        # absolute extremes it cannot see; use the shown-rows-scoped variant.
        # Only when the narrator sees every materialized row are the plain
        # rules safe: extremes over the shown rows ARE extremes of the
        # returned result.
        task += (
            _TREND_SUMMARIZATION_INSTRUCTION_SAMPLED
            if sample_capped
            else _TREND_SUMMARIZATION_INSTRUCTION
        )

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
    sample_capped: bool = False,
    sample_showing: int = 0,
    sample_total: int = 0,
    steps_truncated: bool = False,
) -> str:
    """Return task description + format instruction + example for a compound-query result.

    R10 (compound scope — Lane D review add): the multi-row case renders
    ``computed.result_rows`` to the narrator, and the producer (pipeline.py)
    caps that list at its ``_MAX_NARRATE_ROWS`` (env-configurable, default 25);
    the disclosure numbers below are derived from the payload itself, so they
    stay correct under any override. When more per-dimension rows were
    computed than the narrator sees, telling it to "highlight the highest and
    lowest" would order it to present a sample-derived extreme as the true
    extreme — the same wrong-numbers-by-omission path the primary path closes.

    Guard selection mirrors Lane D's three-case design on the primary path
    (``steps_truncated`` = any underlying sub-query hit the DB row cap, so the
    COMPUTED result derives from partial step data):
      * steps_truncated only            -> ``_GUARD_TRUNCATED`` (figures are a
        partial view; complete data extends further)
      * sample_capped only              -> ``_GUARD_NARRATOR_SAMPLED`` (result
        IS complete; narrator sees a sample — never claim a cap)
      * steps_truncated + sample_capped -> ``_GUARD_TRUNCATED`` plus
        ``_GUARD_TRUNCATED_SAMPLED`` (cap is real AND shown-row readings must
        not be presented as extremes of the computed rows)
    The scalar case is a single computed value with no sampling; it carries
    ``_GUARD_TRUNCATED`` when steps were truncated (the value derives from
    partial data).
    """
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
        if sample_capped:
            # Shown-rows-scoped extreme instruction + the applicable partial-
            # view guard (reused from Lane D's primary-path machinery). Never
            # orders the narrator to name THE highest/lowest — only the
            # extremes AMONG THE SHOWN rows, said explicitly.
            extreme_instruction = (
                "Present these values clearly — summarise key figures from the "
                "rows shown, note the highest and lowest AMONG THE ROWS SHOWN "
                "(say so explicitly — do not call them the overall highest or "
                "lowest), and describe the overall pattern of the shown rows. "
            )
            if steps_truncated:
                # "COMPLETE result" would be FALSE here — the computed rows
                # derive from row-capped sub-query data. Disclose the real cap
                # AND scope shown-row readings (Lane D combined case).
                sampled_guard = _GUARD_TRUNCATED + _GUARD_TRUNCATED_SAMPLED.format(
                    showing=sample_showing, total=sample_total
                )
            else:
                sampled_guard = _GUARD_NARRATOR_SAMPLED.format(
                    showing=sample_showing, total=sample_total
                )
        else:
            extreme_instruction = (
                "Present these values clearly — summarise key figures, highlight "
                "the highest and lowest, and note the overall pattern. "
            )
            # Narrator sees every computed row; extremes over the shown rows
            # ARE the extremes of the computed result. When the underlying
            # steps were truncated the computed result itself is partial —
            # disclose with the row-cap guard (Lane D truncated-only case).
            sampled_guard = _GUARD_TRUNCATED if steps_truncated else ""
        task = (
            "Write a proper conversational answer. "
            "The computed results contain per-dimension values in result_rows. "
            + extreme_instruction
            + "Use the exact computed values — do not recalculate or approximate. "
            + guard + sampled_guard + pct_rule + " "
            "Use the tone and language specified in the system prompt."
        )
    else:
        # Scalar computed value — no sampling, but a value computed from
        # row-capped sub-query data is a partial-data figure (R10 review R1-2).
        scalar_truncation_guard = _GUARD_TRUNCATED if steps_truncated else ""
        task = (
            "Write a proper conversational answer. State the computed result first, "
            "then provide brief context without citing internal step values. "
            "Use the exact computed value — do not recalculate or approximate. "
            + guard + scalar_truncation_guard + pct_rule + " "
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
    full_row_count = len(execution.rows)
    showing = len(sample)

    # R10 — narrator truncation alignment. Two independent conditions can make
    # the narrator's view partial, and each needs its OWN, factually-accurate
    # disclosure (conflating them tells the user a false story either way):
    #   * execution_truncated — the DB hit a row cap; rows beyond it do NOT
    #     exist in the result. _GUARD_TRUNCATED: "limited by a row cap,
    #     complete data extends further" (TRUE only here).
    #   * sample_capped — the narration prompt shows fewer rows than were
    #     materialized (_MAX_ROWS_FOR_NARRATION). Alone, it fires
    #     _GUARD_NARRATOR_SAMPLED ("first N of M of a COMPLETE result; do not
    #     claim a cap; never present sample-derived aggregates as exact").
    #     COMBINED with execution_truncated it fires _GUARD_TRUNCATED_SAMPLED
    #     instead: the DB caps (100/1000) sit far above the 25-row narration
    #     cap, so rows 26..cap DO exist and DO reach the user — the row-cap
    #     disclosure alone would not stop the narrator presenting a shown-row
    #     reading as the extreme of the returned rows.
    # Every partial view therefore carries a disclosure whose wording matches
    # reality, closing the wrong-numbers-by-omission path in all three cases.
    #
    # sample_total: today rows_returned == len(rows) at the single
    # QueryExecution construction site (query.py), but this function's whole
    # job is partial-view disclosure, so it must not silently trust that
    # invariant. Taking the max means a future producer that pre-caps the
    # rows list while reporting a larger rows_returned still triggers the
    # sampled disclosure instead of an unguarded "summarise the sample" path.
    execution_truncated = bool(getattr(execution, "truncated", False))
    sample_total = max(full_row_count, total)
    sample_capped = showing < sample_total
    effective_truncated = execution_truncated or sample_capped

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

    # Bug-8453: the router reported the deny-all row-security sentinel for this
    # execution. Read defensively so an older/partial QueryExecution still works.
    row_security_denied = bool(getattr(execution, "row_security_denied", False))
    if row_security_denied:
        # Bug-8453: never state "no data" for a permissions denial.
        row_count = (
            "No rows are available to this user: row-level security denied "
            "access to every row for this query. This is a permissions "
            "restriction, not an absence of data."
        )
    elif total == 0:
        row_count = "No data was found for this query."
    else:
        parts = [f"{total} rows returned"]
        if execution_truncated:
            parts.append(
                "this is the row cap — more rows exist beyond it, so the figures "
                "below are only the first part of the full set"
            )
        if sample_capped:
            parts.append(
                f"showing {showing} of {sample_total} rows to the narrator"
            )
        elif not effective_truncated:
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
        truncated=execution_truncated,
        sample_capped=sample_capped,
        sample_showing=showing, sample_total=sample_total,
        row_security_denied=row_security_denied,
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

    # ── narrator-sample disclosure (R10 — compound scope) ─────────────────
    # The multi-row producer caps computed.result_rows at _MAX_ROWS_FOR_NARRATION
    # but the full per-dimension result reaches the user (compound table/chart).
    # result_total_rows carries the true count so a >25-row result triggers the
    # narrator-sampled disclosure instead of an unguarded "summarise the rows"
    # path. max() over the shown length is defensive: if a producer ever reports
    # a smaller total than what it rendered, never claim a cap that isn't real.
    is_multi_row = bool(computed.get("is_multi_row") and computed.get("result_rows"))
    sample_showing = len(computed["result_rows"]) if is_multi_row else 0
    result_total = computed.get("result_total_rows")
    if not isinstance(result_total, int) or result_total < 0:
        result_total = sample_showing
    sample_total = max(sample_showing, result_total)
    sample_capped = is_multi_row and sample_showing < sample_total
    # Review R1-2 — any underlying sub-query hit the DB row cap: the computed
    # result derives from partial step data, so "COMPLETE result" wording is
    # forbidden and the row-cap guard applies instead.
    steps_truncated = bool(computed.get("steps_truncated"))

    # ── answer information block ──────────────────────────────────────────
    # ── date ranges ───────────────────────────────────────────────────────
    date_ranges = aggregate_date_ranges(step_summaries)

    # ── instruction block (first) ─────────────────────────────────────────
    fmt_block = _build_compound_format_block(
        computed, output_format, has_dates=bool(date_ranges),
        sample_capped=sample_capped,
        sample_showing=sample_showing, sample_total=sample_total,
        steps_truncated=steps_truncated,
    )
    instr_parts = [fmt_block]
    shape_facts = _shape_facts_payload(computed.get("shape") if isinstance(computed, dict) else None)
    if shape_facts:
        instr_parts.append(_SHAPE_FACT_RULES)
    pq = _prior_questions_line(prior_questions or [])
    if pq:
        instr_parts.append(pq)
    instruction_block = "## Instructions\n\n" + "\n\n".join(instr_parts)

    # ── data block (second) ───────────────────────────────────────────────
    # is_multi_row / sample_* already computed above for the format block.
    data_parts = [
        f"User question: {user_message}",
        "This answer was computed across multiple query steps.",
    ]
    if is_multi_row:
        n = len(computed["result_rows"])
        if sample_capped:
            # Review R1-4 — assert result SIZE, never delivery: what the user
            # actually receives (table rows, chart points) is configuration-
            # dependent, so a delivery promise could itself be false.
            data_parts.append(
                f"Computed results ({label}): the computed result contains "
                f"{sample_total} rows of per-dimension values; you are shown "
                f"only the first {n} (server-calculated — do not recalculate). "
                f"See computed.result_rows below."
            )
        else:
            data_parts.append(
                f"Computed results ({label}): {n} rows of per-dimension values "
                f"— see computed.result_rows below (server-calculated — do not recalculate)."
            )
        if steps_truncated:
            data_parts.append(
                "Note: one or more underlying sub-queries hit a row cap, so "
                "the computed rows cover only the returned part of the data."
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
