"""Unit guards for R1/F1 — Anthropic prompt-cache breakpoint plumbing.

``cache_system_prefix=True`` must mark the Anthropic ``system`` block cacheable
WITHOUT changing the rendered prompt text (a cache marker can never alter what
the model reads), must be a documented no-op on non-caching providers, and must
be gated by the ``llm.prompt_cache_enabled`` operator kill-switch. The adapter
interface (base + RetryingAdapter) must accept and forward the flag.

Test escape: no test exercised the cache-breakpoint request shape; a misplaced
or missing breakpoint bills full price silently (never wrong results). Guard:
these assert the per-provider payload and the flag plumbing. Tier: T1
(producer/consumer contract — request payload + usage capture per provider).
"""
import inspect

import httpx
import pytest

from shared.llm.adapter import LLMAdapter, LLMConfig, RetryingAdapter
from shared.llm.providers.anthropic import AnthropicAdapter
from shared.llm.providers.google import GoogleAdapter
from shared.llm.providers.openai_compat import OpenAICompatibleAdapter


def _mock_client_factory(handler):
    real_client_cls = httpx.AsyncClient

    def _factory(*_args, **kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("transport", None)
        return real_client_cls(transport=httpx.MockTransport(handler), **kwargs)

    return _factory


def _cfg(provider: str, **kw) -> LLMConfig:
    base = dict(
        provider=provider,
        display_name=f"{provider}_test",
        base_url=None,
        api_key="key",
        model_name="m",
        max_tokens=64,
        temperature=0.0,
        timeout_seconds=60,
        config={},
    )
    base.update(kw)
    return LLMConfig(**base)


def _patch_anthropic_snapshot(monkeypatch, cache_enabled=True):
    def _snap(key):
        if "endpoints" in key:
            return {"anthropic": "https://api.anthropic.com"}
        if key == "llm.anthropic_api_version":
            return "2023-06-01"
        if key == "llm.prompt_cache_enabled":
            return cache_enabled
        return None

    monkeypatch.setattr(
        "shared.llm.providers.anthropic.system_snapshot_get", _snap
    )


# ── Anthropic: cache_control marker on the system block ─────────────────────

def test_anthropic_marks_system_cacheable_when_flag_set(monkeypatch):
    _patch_anthropic_snapshot(monkeypatch)
    adapter = AnthropicAdapter(_cfg("anthropic"))
    _url, _headers, payload = adapter._build_request(
        "SYSTEM_PREFIX", "user", stream=False, thinking=False,
        cache_system_prefix=True,
    )
    # system becomes a single text block carrying cache_control.
    assert isinstance(payload["system"], list)
    assert len(payload["system"]) == 1
    block = payload["system"][0]
    assert block["type"] == "text"
    # The rendered text is byte-identical to the plain-string form — the
    # marker can never change what the model reads.
    assert block["text"] == "SYSTEM_PREFIX"
    assert block["cache_control"] == {"type": "ephemeral"}


def test_anthropic_plain_system_when_flag_unset(monkeypatch):
    _patch_anthropic_snapshot(monkeypatch)
    adapter = AnthropicAdapter(_cfg("anthropic"))
    _url, _headers, payload = adapter._build_request(
        "SYSTEM_PREFIX", "user", stream=False, thinking=False,
        cache_system_prefix=False,
    )
    # Default: plain string system, no cache_control (existing behaviour).
    assert payload["system"] == "SYSTEM_PREFIX"


def test_anthropic_kill_switch_disables_marker(monkeypatch):
    # llm.prompt_cache_enabled == False must fall back to the plain string even
    # when the caller asks for the marker (operator safety valve).
    _patch_anthropic_snapshot(monkeypatch, cache_enabled=False)
    adapter = AnthropicAdapter(_cfg("anthropic"))
    _url, _headers, payload = adapter._build_request(
        "SYSTEM_PREFIX", "user", stream=False, thinking=False,
        cache_system_prefix=True,
    )
    assert payload["system"] == "SYSTEM_PREFIX"


def test_anthropic_missing_kill_switch_defaults_enabled(monkeypatch):
    # A missing snapshot key (None) must NOT silently disable the marker — the
    # feature is correctness-neutral, so it defaults ON.
    def _snap(key):
        if "endpoints" in key:
            return {"anthropic": "https://api.anthropic.com"}
        if key == "llm.anthropic_api_version":
            return "2023-06-01"
        return None  # prompt_cache_enabled absent

    monkeypatch.setattr(
        "shared.llm.providers.anthropic.system_snapshot_get", _snap
    )
    adapter = AnthropicAdapter(_cfg("anthropic"))
    _url, _headers, payload = adapter._build_request(
        "S", "u", cache_system_prefix=True,
    )
    assert isinstance(payload["system"], list)
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_anthropic_blank_system_never_wrapped(monkeypatch):
    # An empty system has no cacheable prefix to mark — leave it as-is.
    _patch_anthropic_snapshot(monkeypatch)
    adapter = AnthropicAdapter(_cfg("anthropic"))
    _url, _headers, payload = adapter._build_request(
        "", "user", cache_system_prefix=True,
    )
    assert payload["system"] == ""


# ── Provider no-op: cache flag never alters google / openai requests ────────

def test_google_complete_accepts_cache_flag():
    sig = inspect.signature(GoogleAdapter.complete)
    assert "cache_system_prefix" in sig.parameters
    assert sig.parameters["cache_system_prefix"].default is False


def test_openai_complete_accepts_cache_flag():
    sig = inspect.signature(OpenAICompatibleAdapter.complete)
    assert "cache_system_prefix" in sig.parameters
    assert sig.parameters["cache_system_prefix"].default is False


# ── Adapter interface: flag is plumbed through the base + retrying wrapper ──

def test_base_adapter_declares_cache_flag():
    for meth in (LLMAdapter.complete, LLMAdapter.stream_complete):
        sig = inspect.signature(meth)
        assert "cache_system_prefix" in sig.parameters
        assert sig.parameters["cache_system_prefix"].default is False


class _RecordingAdapter(LLMAdapter):
    def __init__(self) -> None:
        super().__init__(
            LLMConfig(
                provider="test", display_name="t", base_url=None, api_key="k",
                model_name="m", max_tokens=10, temperature=0, timeout_seconds=5,
            )
        )
        self.complete_cache_flags: list[bool] = []
        self.stream_cache_flags: list[bool] = []

    async def complete(self, system, user, on_thinking=None,
                       response_json=False, cache_system_prefix=False):
        self.complete_cache_flags.append(cache_system_prefix)
        self.last_usage = {"input_tokens": 1, "output_tokens": 1}
        return "ok"

    async def stream_complete(self, system, user, on_thinking=None,
                              response_json=False, cache_system_prefix=False):
        self.stream_cache_flags.append(cache_system_prefix)
        yield "tok"
        self.last_usage = {"input_tokens": 1, "output_tokens": 1}


@pytest.mark.asyncio
async def test_retrying_adapter_forwards_cache_flag_complete():
    primary = _RecordingAdapter()
    wrapper = RetryingAdapter(primary)
    await wrapper.complete("s", "u", cache_system_prefix=True)
    assert primary.complete_cache_flags == [True]
    # default stays False when the caller does not opt in
    await wrapper.complete("s", "u")
    assert primary.complete_cache_flags == [True, False]


@pytest.mark.asyncio
async def test_retrying_adapter_forwards_cache_flag_stream():
    primary = _RecordingAdapter()
    wrapper = RetryingAdapter(primary)
    tokens = [t async for t in wrapper.stream_complete("s", "u", cache_system_prefix=True)]
    assert tokens == ["tok"]
    assert primary.stream_cache_flags == [True]


# ── Anthropic: cache usage captured into last_usage (Section 7 signal) ──────

@pytest.mark.asyncio
async def test_anthropic_complete_captures_cache_usage(monkeypatch):
    _patch_anthropic_snapshot(monkeypatch)

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "ok"}],
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 5,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 6656,
                },
            },
        )

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(_handler))
    adapter = AnthropicAdapter(_cfg("anthropic", base_url="https://api.anthropic.test"))
    out = await adapter.complete("SYS", "u", cache_system_prefix=True)
    assert out == "ok"
    # Existing billing fields unchanged; cache fields surfaced for observability.
    assert adapter.last_usage["input_tokens"] == 12
    assert adapter.last_usage["output_tokens"] == 5
    assert adapter.last_usage["cache_read_input_tokens"] == 6656
    assert adapter.last_usage["cache_creation_input_tokens"] == 0


@pytest.mark.asyncio
async def test_anthropic_stream_captures_cache_usage(monkeypatch):
    _patch_anthropic_snapshot(monkeypatch)

    _sse = (
        'data: {"type": "message_start", "message": {"usage": '
        '{"input_tokens": 3, "cache_creation_input_tokens": 100, '
        '"cache_read_input_tokens": 200}}}\n\n'
        'data: {"type": "content_block_delta", "delta": {"type": "text_delta", '
        '"text": "hi"}}\n\n'
        'data: {"type": "message_delta", "usage": {"output_tokens": 4}}\n\n'
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_sse.encode(),
            headers={"Content-Type": "text/event-stream"},
        )

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(_handler))
    adapter = AnthropicAdapter(_cfg("anthropic", base_url="https://api.anthropic.test"))
    tokens = [
        t async for t in adapter.stream_complete("SYS", "u", cache_system_prefix=True)
    ]
    assert "".join(tokens) == "hi"
    assert adapter.last_usage["input_tokens"] == 3
    assert adapter.last_usage["output_tokens"] == 4
    assert adapter.last_usage["cache_creation_input_tokens"] == 100
    assert adapter.last_usage["cache_read_input_tokens"] == 200
