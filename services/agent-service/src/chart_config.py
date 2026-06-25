"""Shared chart configuration resolution for prompt assembly and runtime."""
from __future__ import annotations

from typing import Any


def effective_chart_selector(cfg: Any) -> str:
    """Return the effective chart selector, applying rich_html override."""
    selector = getattr(cfg, "chart_type_selector", "auto")
    output_format = getattr(cfg, "agent_output_format", "json")
    if output_format == "rich_html" and selector == "none":
        return "llm"
    return selector
