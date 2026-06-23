"""Refusal taxonomy — Phase D2.

A single canonical set of refusal reasons, each with a templated user
message that ends with a "try rephrasing" suggestion. Internal callers
pass a short reason code; the user sees the full templated message.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


REFUSAL_REASONS: tuple[str, ...] = (
    "policy_denied_topic",
    "ambiguous_abbreviation",
    "out_of_scope",
    "query_rejected",
    "judge_blocked",
    "internal_error",
    "prompt_injection",
    "empty_input",
    "no_allow_list",
    "no_llm_config",
    "tool_call_parse_error",
    "model_not_allow_listed",
)


@dataclass
class RefusalRender:
    reason: str
    message: str


_TEMPLATES: dict[str, str] = {
    "policy_denied_topic": (
        "I cannot answer this — it touches a topic the project policy "
        "excludes ({detail}).\n\nTry asking about an in-scope metric "
        "(e.g. revenue, units, signups) for a specific time window."
    ),
    "ambiguous_abbreviation": (
        "The term {detail!r} could mean more than one thing in this "
        "project.\n\nName the metric or dimension explicitly — for "
        "example, \"revenue (gross)\" instead of \"rev\"."
    ),
    "out_of_scope": (
        "That question is outside the agent's scope for this project.\n\n"
        "Try asking about one of the allow-listed models — pick a "
        "specific measure and a time window.\n\n"
        "If you need connection instructions for Excel, Power BI, or "
        "another BI tool, open the model in Model Builder and click "
        "the Endpoints panel for ready-to-use connection strings."
    ),
    "query_rejected": (
        "I could not run that query: {detail}.\n\nTry simplifying — "
        "fewer filters, or a wider time window."
    ),
    "judge_blocked": (
        "Answer withheld for review. Try rephrasing the question, "
        "naming a specific metric, model, and time window."
    ),
    "internal_error": (
        "Something went wrong on my side. Please try again. If the issue "
        "persists, ask a tenant admin to check the agent service logs."
    ),
    "prompt_injection": (
        "The request looks like an attempt to override the project "
        "policy.\n\nIf this was unintentional, please rephrase as a data "
        "question and avoid meta-instructions."
    ),
    "empty_input": (
        "Please send a question — naming a metric and a time window "
        "works best."
    ),
    "no_allow_list": (
        "This project's agent has no allow-listed models. Ask a tenant "
        "admin or modeller to allow-list at least one published model in "
        "the Project Agent settings."
    ),
    "no_llm_config": (
        "The answer model is not configured for this project. Ask a "
        "tenant admin to set the answer LLM in the Project Agent "
        "settings."
    ),
    "tool_call_parse_error": (
        "I could not produce a structured plan for that question.\n\n"
        "Please rephrase, ideally naming a metric and a time window."
    ),
    "model_not_allow_listed": (
        "I tried to query a model that is not allow-listed for this "
        "project.\n\nPlease rephrase to refer to an allow-listed model, "
        "or ask a modeller to allow-list the right one."
    ),
}


def render_refusal(reason: str, detail: Optional[str] = None) -> RefusalRender:
    template = _TEMPLATES.get(reason, _TEMPLATES["internal_error"])
    try:
        message = template.format(detail=detail) if detail is not None else template
    except (KeyError, IndexError):
        message = template
    return RefusalRender(reason=reason, message=message)
