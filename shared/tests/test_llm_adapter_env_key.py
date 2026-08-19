"""Unit guards for ``build_adapter``'s environment bring-your-own-key fallback.

Protects the fix for the demo-agent LLM blocker: a Google LLM config seeded in
``vertex_ai`` mode is unusable on a deployment where service-account auth is
disabled (``LLM_ALLOW_SERVICE_ACCOUNT_AUTH=false``). ``build_adapter`` must fall
back to the operator-supplied ``GOOGLE_API_KEY`` (switching to ``api_key`` mode)
when one is present, while preserving the Vertex path when SA auth is allowed and
the clear "no key configured" error for genuinely unconfigured providers.
"""
import pytest

from shared.llm.adapter import LLMConfig, build_adapter


def _google_vertex_cfg(api_key=None):
    return LLMConfig(
        provider="google",
        display_name="Gemini_test",
        base_url=None,
        api_key=api_key,
        model_name="gemini-x",
        max_tokens=64,
        temperature=0.2,
        timeout_seconds=60,
        config={"google_mode": "vertex_ai", "google_project": "p", "google_location": "l"},
    )


def test_vertex_downgrades_to_api_key_when_sa_off_and_env_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-test-key")
    monkeypatch.setattr("shared.llm.sa_auth.service_account_auth_allowed", lambda: False)
    adapter = build_adapter(_google_vertex_cfg())
    assert adapter.config.config["google_mode"] == "api_key"
    assert adapter.config.api_key == "AIza-test-key"


def test_vertex_preserved_when_sa_allowed(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-test-key")
    monkeypatch.setattr("shared.llm.sa_auth.service_account_auth_allowed", lambda: True)
    adapter = build_adapter(_google_vertex_cfg())
    # SA auth is on: keep ADC/Vertex, do not consume the BYO key.
    assert adapter.config.config["google_mode"] == "vertex_ai"


def test_vertex_preserved_when_no_env_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr("shared.llm.sa_auth.service_account_auth_allowed", lambda: False)
    adapter = build_adapter(_google_vertex_cfg())
    # No key to fall back to: stay vertex; the Google adapter raises its clear
    # SA-disabled error only at call time.
    assert adapter.config.config["google_mode"] == "vertex_ai"


def test_api_key_mode_uses_env_key_when_unset(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-env")
    cfg = LLMConfig(
        provider="google",
        display_name="g",
        base_url=None,
        api_key=None,
        model_name="gemini-x",
        max_tokens=64,
        temperature=0.2,
        timeout_seconds=60,
        config={"google_mode": "api_key"},
    )
    adapter = build_adapter(cfg)
    assert adapter.config.api_key == "AIza-env"


def test_stored_key_is_not_overridden_by_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-env")
    cfg = LLMConfig(
        provider="google",
        display_name="g",
        base_url=None,
        api_key="stored-key",
        model_name="gemini-x",
        max_tokens=64,
        temperature=0.2,
        timeout_seconds=60,
        config={"google_mode": "api_key"},
    )
    adapter = build_adapter(cfg)
    assert adapter.config.api_key == "stored-key"


def test_missing_key_still_raises_for_non_vertex(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = LLMConfig(
        provider="openai",
        display_name="o",
        base_url=None,
        api_key=None,
        model_name="gpt",
        max_tokens=64,
        temperature=0.2,
        timeout_seconds=60,
        config={},
    )
    with pytest.raises(ValueError, match="No API key configured"):
        build_adapter(cfg)
