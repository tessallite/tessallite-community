"""Output guardrails — Phase D2.

Applied to the final ``answer_text`` after narration succeeds. Three
passes:

  1. Banned-phrase filter from ``brand_guidelines``. Lines starting with
     ``banned:`` (case-insensitive) are stripped from the answer.
  2. Brand-voice prefix: if ``brand_guidelines`` contains a line starting
     with ``voice:``, the rest of that line is appended as a footer (so
     it does not hide the answer).
  3. Disclosure: ``disclosure_text`` is appended verbatim if non-empty
     and not already present.

This is a deliberately small implementation — full brand-voice
rewriting needs an LLM pass and lands as a future feature.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from shared.db.models import ProjectAgentConfig


@dataclass
class OutputResult:
    text: str
    actions: list[dict]


def _parse_directives(brand: Optional[str]) -> tuple[list[str], list[str]]:
    banned: list[str] = []
    voice: list[str] = []
    if not brand:
        return banned, voice
    for raw in brand.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("banned:"):
            phrase = line[len("banned:"):].strip()
            if phrase:
                banned.append(phrase)
        elif low.startswith("voice:"):
            phrase = line[len("voice:"):].strip()
            if phrase:
                voice.append(phrase)
    return banned, voice


def apply_output_guardrails(
    cfg: ProjectAgentConfig,
    answer_text: str,
) -> OutputResult:
    actions: list[dict] = []
    text = answer_text or ""

    banned, voice = _parse_directives(cfg.brand_guidelines)

    for phrase in banned:
        if phrase.lower() in text.lower():
            # Case-insensitive replace; ignore overlapping cases.
            lowered = text.lower()
            start = lowered.find(phrase.lower())
            while start >= 0:
                text = text[:start] + text[start + len(phrase):]
                lowered = text.lower()
                start = lowered.find(phrase.lower())
            actions.append(
                {"layer": "output", "action": "strip_banned_phrase", "phrase": phrase}
            )

    if voice:
        footer = " ".join(voice).strip()
        if footer and footer not in text:
            text = text.rstrip() + f"\n\n_{footer}_"
            actions.append({"layer": "output", "action": "append_voice_note"})

    disclosure = (cfg.disclosure_text or "").strip()
    if disclosure and disclosure not in text:
        text = text.rstrip() + f"\n\n{disclosure}"
        actions.append({"layer": "output", "action": "append_disclosure"})

    return OutputResult(text=text, actions=actions)
