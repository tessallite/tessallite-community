"""Input guardrails — Phase D2.

Cheap heuristics that run before any LLM call.

Three layers:
  1. Unicode normalisation (NFKC) — defeats homoglyph substitution.
  2. Prompt-injection patterns:
     a. Structural markers (prompt template tags, role-switching).
     b. English-language injection phrases.
     c. Non-English injection phrases (Chinese, Spanish, Arabic, French).
  3. Denied-topic match against ProjectAgentConfig.safety_policy lines.

Safety-policy format (F-023-17): one topic per line. Each line names a
banned subject — a single word ("gambling") or a short phrase ("medical
advice"). Lines are matched on **word boundaries**, so "revenue" blocks
the word *revenue* but not the substring inside "revenues" or
"overrevenue", and "medical advice" matches only when those two words
appear adjacently. Prose sentences ("Do not discuss medical or legal
topics") are NOT topic lines — they will not match user input; split them
into per-topic lines instead. Lines starting with ``#`` are comments;
leading bullet markers (``-`` ``*`` ``•``) are stripped.

Returned ``InputScanResult`` carries either ``ok=True`` or a refusal
reason ready to be fed into ``render_refusal``.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

from shared.db.models import ProjectAgentConfig


# Structural markers — prompt template tags and role-switching patterns
# that indicate injection regardless of language.
_STRUCTURAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"</?system>", re.I),
    re.compile(r"</?assistant>", re.I),
    re.compile(r"</?user>", re.I),
    re.compile(r"\[INST\]", re.I),
    re.compile(r"<<SYS>>", re.I),
    re.compile(r"\[/INST\]", re.I),
    re.compile(r"###\s*(System|Human|Assistant)\s*:", re.I),
    re.compile(r"<\|im_start\|>", re.I),
    re.compile(r"<\|im_end\|>", re.I),
    re.compile(r"<\|endoftext\|>", re.I),
)

# English injection phrases
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bignore\s+(all\s+)?(the\s+)?(previous\s+|prior\s+)?instructions?\b", re.I),
    re.compile(r"\bdisregard\s+(all\s+)?(the\s+)?(previous\s+|prior\s+)?instructions?\b", re.I),
    re.compile(r"\bforget\s+(everything|the\s+rules?|your\s+instructions?)\b", re.I),
    re.compile(r"\byou\s+are\s+now\s+[a-z]", re.I),
    re.compile(r"\bact\s+as\s+(if\s+you\s+were|a)\b", re.I),
    re.compile(r"\bsystem\s*prompt\b", re.I),
    re.compile(r"\bjailbreak\b", re.I),
    re.compile(r"\bdo\s+not\s+follow\s+(your|the)\s+rules?\b", re.I),
    re.compile(r"\boverride\s+(your|the|all)\s+instructions?\b", re.I),
)

# Non-English injection phrases
_NON_ENGLISH_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Chinese
    re.compile(r"忽略.*指令"),
    re.compile(r"忽略.*规则"),
    re.compile(r"无视.*指示"),
    re.compile(r"你现在是"),
    # Spanish
    re.compile(r"\bignora\s+(las|todas\s+las)\s+instrucciones\b", re.I),
    re.compile(r"\bolivida\s+(las|todas\s+las)\s+instrucciones\b", re.I),
    re.compile(r"\bahora\s+eres\b", re.I),
    # Arabic
    re.compile(r"تجاهل.*التعليمات"),
    re.compile(r"أنت\s+الآن"),
    # French
    re.compile(r"\bignore[zr]?\s+(les|toutes\s+les)\s+instructions\b", re.I),
    re.compile(r"\boublie[zr]?\s+(les|toutes\s+les)\s+instructions\b", re.I),
    re.compile(r"\btu\s+es\s+maintenant\b", re.I),
)


@dataclass
class InputScanResult:
    ok: bool
    reason: Optional[str] = None
    matched_topic: Optional[str] = None


# Bug-5280 — prose policy lines such as "Do not discuss medical or legal
# topics" previously fell through because the whole sentence never matched
# as one topic string.  When a line looks like a prose instruction (contains
# a directive verb), we extract the noun/adjective subjects from "discuss X",
# "answer Y", etc. and add each as an individual topic.  Pure topic lines
# ("gambling", "competitor pricing") still work as before.
_PROSE_INDICATOR_RE = re.compile(
    r"\b(do\s+not|don't|never|avoid|refuse|must\s+not|should\s+not|"
    r"do\s+not\s+respond|no\s+questions?\s+about|"
    r"not\s+allowed|prohibited|forbidden)\b",
    re.IGNORECASE,
)
# Verbs that introduce a list of banned subjects.
_PROSE_SUBJECT_RE = re.compile(
    r"\b(?:discuss(?:ing)?|answer(?:ing)?|respond(?:ing)?\s+to|"
    r"talk(?:ing)?\s+about|address(?:ing)?|provid(?:e|ing)|"
    r"mention(?:ing)?|shar(?:e|ing)|disclos(?:e|ing)|"
    r"engag(?:e|ing)\s+(?:with|in)|handl(?:e|ing)|entertain(?:ing)?|"
    r"questions?\s+about|topics?\s+(?:about|like|such\s+as)|"
    r"related\s+to|regarding|involving)\s+(.+)",
    re.IGNORECASE,
)


def _extract_prose_topics(line: str) -> list[str]:
    """Extract individual topic words from a prose policy instruction.

    Given "Do not discuss medical or legal topics", returns
    ["medical", "legal"].  Given "Never answer questions about gambling,
    drugs, or weapons", returns ["gambling", "drugs", "weapons"].
    """
    m = _PROSE_SUBJECT_RE.search(line)
    if not m:
        return []
    tail = m.group(1)
    # Strip leading noise words (e.g. "questions about", "topics like")
    tail = re.sub(
        r"^(questions?\s+about|topics?\s+(?:about|like|such\s+as)|"
        r"requests?\s+(?:about|for|related\s+to)|anything\s+(?:about|related\s+to))\s+",
        "", tail, flags=re.IGNORECASE,
    ).strip()
    # Strip trailing noise words
    tail = re.sub(
        r"\b(topics?|questions?|requests?|queries|content|information|data|"
        r"advice|details?|matters?|subjects?|issues?|areas?)\b\.?\s*$",
        "", tail, flags=re.IGNORECASE,
    ).strip().rstrip(".")

    # Split on conjunctions and commas: "medical or legal", "gambling, drugs, or weapons"
    parts = re.split(r"\s*,\s*|\s+(?:or|and|&)\s+", tail, flags=re.IGNORECASE)
    topics = [p.strip() for p in parts if p.strip()]
    return topics


def _denied_topics(policy: Optional[str]) -> list[str]:
    """Parse the safety policy into individual denied-topic strings.

    Bug-5280 — prose sentences are now decomposed into individual topic
    words so the guardrail actually enforces them.  Pure topic lines
    ("gambling", "competitor pricing") still work as before.
    """
    if not policy:
        return []
    out: list[str] = []
    for raw in policy.splitlines():
        line = raw.strip().lstrip("-*•").strip()
        if not line or line.startswith("#"):
            continue
        # If the line is a prose instruction, extract individual topics.
        if _PROSE_INDICATOR_RE.search(line):
            extracted = _extract_prose_topics(line)
            if extracted:
                out.extend(extracted)
                continue
        # Otherwise, treat the whole line as a topic (existing behaviour).
        out.append(line)
    return out


def _topic_matches(topic: str, text: str) -> bool:
    """F-023-17 — match a denied-topic line against the user message on
    word boundaries rather than as a bare substring.

    ``re.escape`` keeps the topic literal (so punctuation in a topic is not
    treated as a pattern); internal whitespace in a multi-word topic is
    normalised to ``\\s+`` so "medical advice" matches one or more spaces
    between the words. Word boundaries are anchored only where the topic's
    own edge characters are word characters, so a topic that starts or ends
    with punctuation still matches."""
    topic = topic.strip()
    if not topic:
        return False
    words = topic.split()
    pattern = r"\s+".join(re.escape(w) for w in words)
    left = r"\b" if words and words[0][:1].isalnum() else ""
    right = r"\b" if words and words[-1][-1:].isalnum() else ""
    try:
        return re.search(left + pattern + right, text, re.IGNORECASE) is not None
    except re.error:
        # Defensive: fall back to a case-insensitive substring check.
        return topic.lower() in text.lower()


def _normalize(text: str) -> str:
    """NFKC normalization defeats homoglyph attacks (e.g. fullwidth chars)."""
    return unicodedata.normalize("NFKC", text)


def scan_input_message(
    cfg: ProjectAgentConfig,
    text: str,
) -> InputScanResult:
    """Run cheap pre-LLM checks. Returns ``ok=True`` on pass."""
    if not text or not text.strip():
        return InputScanResult(ok=False, reason="empty_input")

    normalized = _normalize(text)

    for pat in _STRUCTURAL_PATTERNS:
        if pat.search(normalized):
            return InputScanResult(ok=False, reason="prompt_injection")

    for pat in _INJECTION_PATTERNS:
        if pat.search(normalized):
            return InputScanResult(ok=False, reason="prompt_injection")

    for pat in _NON_ENGLISH_PATTERNS:
        if pat.search(normalized):
            return InputScanResult(ok=False, reason="prompt_injection")

    for topic in _denied_topics(cfg.safety_policy):
        if _topic_matches(topic, normalized):
            return InputScanResult(
                ok=False,
                reason="policy_denied_topic",
                matched_topic=topic,
            )

    return InputScanResult(ok=True)
