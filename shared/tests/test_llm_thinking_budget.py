"""Unit guards for R6/F15 — thinking_budget decoupled from max_tokens.

The Anthropic extended-thinking budget must be an INDEPENDENT knob: lowering
``max_tokens`` for cost control must not silently shrink reasoning depth on the
accuracy-critical planner step. ``max_tokens`` may only act as an API-validity
ceiling (``budget_tokens < max_tokens``), never as the source of the value.

Test escape: no test pinned the budget's independence from max_tokens (the old
``max(200, min(8000, max_tokens // 2))`` formula halved reasoning silently).
Guard: these tests. Tier: T2 (fixed-bug regression guard).
"""
import pytest

from shared.llm.adapter import LLMConfig
from shared.llm.providers.anthropic import AnthropicAdapter


def _cfg(max_tokens: int, config: dict | None = None) -> LLMConfig:
    return LLMConfig(
        provider="anthropic",
        display_name="Claude_test",
        base_url=None,
        api_key="sk-test",
        model_name="claude-x",
        max_tokens=max_tokens,
        temperature=0.0,
        timeout_seconds=60,
        config=config or {},
    )


def test_default_budget_is_4000_regardless_of_high_max_tokens(monkeypatch):
    # No snapshot configured -> registry-default fallback (4000). A generous
    # max_tokens must leave the budget at its configured default.
    monkeypatch.setattr(
        "shared.llm.providers.anthropic.system_snapshot_get", lambda *_: None
    )
    adapter = AnthropicAdapter(_cfg(max_tokens=64000))
    assert adapter._thinking_budget() == 4000


def test_low_max_tokens_does_not_shrink_budget_below_default_when_room_exists(monkeypatch):
    # Section-7 criterion: a low-ish max_tokens that still exceeds the budget
    # leaves the budget at the configured default (NOT halved to max_tokens//2).
    monkeypatch.setattr(
        "shared.llm.providers.anthropic.system_snapshot_get", lambda *_: 4000
    )
    adapter = AnthropicAdapter(_cfg(max_tokens=8000))
    # Old formula would have returned min(8000, 8000//2)=4000 by coincidence;
    # use max_tokens=6000 where the old formula returns 3000 to prove decoupling.
    adapter2 = AnthropicAdapter(_cfg(max_tokens=6000))
    assert adapter._thinking_budget() == 4000
    assert adapter2._thinking_budget() == 4000  # old formula would give 3000


def test_budget_clamps_below_max_tokens_only_as_api_ceiling(monkeypatch):
    # When the configured budget >= max_tokens (and max_tokens can still fit a
    # valid budget), clamp to max_tokens-1 so the request cannot 400 on
    # budget_tokens >= max_tokens. The clamp never violates the API's 1024
    # minimum because max_tokens > 1024 is guaranteed on this branch.
    monkeypatch.setattr(
        "shared.llm.providers.anthropic.system_snapshot_get", lambda *_: 4000
    )
    adapter = AnthropicAdapter(_cfg(max_tokens=2000))
    b = adapter._thinking_budget()
    assert b == 1999
    assert 1024 <= b < 2000


def test_max_tokens_too_low_disables_thinking_instead_of_invalid_payload(monkeypatch):
    # Anthropic requires 1024 <= budget_tokens < max_tokens. With
    # max_tokens <= 1024 NO valid budget exists — the adapter must disable
    # thinking (None) rather than emit a guaranteed-400 payload (e.g. 999).
    monkeypatch.setattr(
        "shared.llm.providers.anthropic.system_snapshot_get", lambda *_: 4000
    )
    for mt in (1, 2, 1000, 1024):
        adapter = AnthropicAdapter(_cfg(max_tokens=mt))
        assert adapter._thinking_budget() is None


def test_build_request_degrades_to_non_thinking_when_budget_cannot_fit(monkeypatch):
    # End-to-end payload check: thinking requested but max_tokens too low —
    # the request must carry NO thinking block (and a temperature instead),
    # never an invalid budget_tokens.
    def _snap(key):
        if key == "llm.anthropic_thinking_budget":
            return 4000
        if key == "llm.provider_endpoints":
            return {"anthropic": "https://api.anthropic.com"}
        return "2023-06-01"

    monkeypatch.setattr(
        "shared.llm.providers.anthropic.system_snapshot_get", _snap
    )
    adapter = AnthropicAdapter(_cfg(max_tokens=1000))
    _url, _headers, payload = adapter._build_request(
        "sys", "user", stream=False, thinking=True
    )
    assert "thinking" not in payload
    assert "temperature" in payload


def test_build_request_carries_valid_budget_when_it_fits(monkeypatch):
    def _snap(key):
        if key == "llm.anthropic_thinking_budget":
            return 4000
        if key == "llm.provider_endpoints":
            return {"anthropic": "https://api.anthropic.com"}
        return "2023-06-01"

    monkeypatch.setattr(
        "shared.llm.providers.anthropic.system_snapshot_get", _snap
    )
    adapter = AnthropicAdapter(_cfg(max_tokens=64000))
    _url, _headers, payload = adapter._build_request(
        "sys", "user", stream=False, thinking=True
    )
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 4000}


def test_sub_minimum_override_raised_to_api_minimum(monkeypatch):
    # An operator override below the API minimum (1024) would 400; it is
    # raised to the minimum, never sent as-is.
    monkeypatch.setattr(
        "shared.llm.providers.anthropic.system_snapshot_get", lambda *_: 4000
    )
    adapter = AnthropicAdapter(_cfg(max_tokens=64000, config={"thinking_budget": 500}))
    assert adapter._thinking_budget() == 1024


def test_per_row_config_override_takes_precedence(monkeypatch):
    monkeypatch.setattr(
        "shared.llm.providers.anthropic.system_snapshot_get", lambda *_: 4000
    )
    adapter = AnthropicAdapter(_cfg(max_tokens=64000, config={"thinking_budget": 6500}))
    assert adapter._thinking_budget() == 6500


def test_system_default_used_when_no_override(monkeypatch):
    # A deployment-tuned system default (e.g. 5000) is honoured over the
    # hard-coded fallback.
    monkeypatch.setattr(
        "shared.llm.providers.anthropic.system_snapshot_get", lambda *_: 5000
    )
    adapter = AnthropicAdapter(_cfg(max_tokens=64000))
    assert adapter._thinking_budget() == 5000


def test_invalid_override_ignored_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(
        "shared.llm.providers.anthropic.system_snapshot_get", lambda *_: 4000
    )
    # bool is not a valid int budget; negative/zero ignored too.
    for bad in (True, 0, -100, "4000", None):
        adapter = AnthropicAdapter(_cfg(max_tokens=64000, config={"thinking_budget": bad}))
        assert adapter._thinking_budget() == 4000
