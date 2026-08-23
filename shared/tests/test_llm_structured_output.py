"""Unit guards for R5/F6 — provider-native structured (JSON) output.

The planner emits exactly one JSON tool-call object. ``response_json=True``
must make each provider request its native JSON-output mode WHERE it exists,
and must NEVER add a JSON-mode field where the provider genuinely lacks one
(Anthropic — documented JSON-in-text fallback, not a silent broken request).

Test escape: no test exercised the planner's structured-output request shape.
Guard: these tests assert the per-provider request payload. Tier: T1
(producer/consumer contract — request payload per provider).
"""
import types

import httpx
import pytest

from shared.llm.adapter import LLMConfig
from shared.llm.providers.anthropic import AnthropicAdapter
from shared.llm.providers.openai_compat import OpenAICompatibleAdapter


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


# ── OpenAI-family json_object mode ─────────────────────────────────────────

@pytest.mark.parametrize("provider", ["openai", "deepseek", "glm"])
def test_json_object_added_for_supported_providers(provider):
    adapter = OpenAICompatibleAdapter(_cfg(provider))
    payload: dict = {}
    adapter._maybe_json_mode(payload, "respond with json", "question", True)
    assert payload["response_format"] == {"type": "json_object"}


def test_json_object_not_added_when_response_json_false():
    adapter = OpenAICompatibleAdapter(_cfg("openai"))
    payload: dict = {}
    adapter._maybe_json_mode(payload, "respond with json", "question", False)
    assert "response_format" not in payload


def test_json_object_not_added_for_ollama_and_unknown():
    # Ollama and unknown OpenAI-compatible endpoints may reject the field;
    # documented fallback keeps the JSON-in-text parser instead of a 400.
    for provider in ("ollama",):
        adapter = OpenAICompatibleAdapter(_cfg(provider))
        payload: dict = {}
        adapter._maybe_json_mode(payload, "respond with json", "q", True)
        assert "response_format" not in payload


def test_json_object_skipped_when_prompt_lacks_json_token():
    # OpenAI json_object mode 400s unless the messages mention "json".
    adapter = OpenAICompatibleAdapter(_cfg("openai"))
    payload: dict = {}
    adapter._maybe_json_mode(payload, "no keyword here", "plain question", True)
    assert "response_format" not in payload


# ── Anthropic: no JSON-mode field ever (documented fallback) ────────────────

def test_anthropic_build_request_never_adds_json_mode_field(monkeypatch):
    monkeypatch.setattr(
        "shared.llm.providers.anthropic.system_snapshot_get",
        lambda key: {"anthropic": "https://api.anthropic.com"} if "endpoints" in key else "2023-06-01",
    )
    adapter = AnthropicAdapter(_cfg("anthropic"))
    _url, _headers, payload = adapter._build_request("sys", "user", stream=False, thinking=False)
    # Anthropic has no plain JSON-object mode — the payload carries no
    # response_format / output_config JSON-enforcement field.
    assert "response_format" not in payload
    assert "output_config" not in payload


def test_anthropic_complete_accepts_response_json_kwarg():
    # Interface parity: the flag is accepted and simply ignored (no crash).
    adapter = AnthropicAdapter(_cfg("anthropic"))
    import inspect

    sig = inspect.signature(adapter.complete)
    assert "response_json" in sig.parameters
    assert sig.parameters["response_json"].default is False


# ── Google: response_mime_type set on the SDK config ────────────────────────

def test_google_sets_response_mime_type_when_response_json(monkeypatch):
    from shared.llm.providers import google as gmod

    captured: dict = {}

    class _FakeConfig:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class _FakeThinkingConfig:
        def __init__(self, **kwargs):
            pass

    fake_types = types.SimpleNamespace(
        GenerateContentConfig=_FakeConfig,
        ThinkingConfig=_FakeThinkingConfig,
    )

    class _FakePart:
        text = '{"query": {}}'
        thought = False

    class _FakeContent:
        parts = [_FakePart()]

    class _FakeCandidate:
        content = _FakeContent()

    class _FakeResponse:
        candidates = [_FakeCandidate()]
        usage_metadata = types.SimpleNamespace(
            prompt_token_count=1, candidates_token_count=1
        )
        text = '{"query": {}}'

    class _FakeModels:
        def generate_content(self, model, contents, config):
            return _FakeResponse()

    class _FakeClient:
        models = _FakeModels()

    monkeypatch.setattr(gmod, "_client", lambda cfg: _FakeClient())
    monkeypatch.setattr(gmod, "_genai", lambda: (None, fake_types))

    adapter = gmod.GoogleAdapter(_cfg("google"))
    _thinking, text = adapter._complete_sync("sys", "user", include_thoughts=False, response_json=True)
    assert text == '{"query": {}}'
    assert captured.get("response_mime_type") == "application/json"


def test_google_omits_response_mime_type_when_not_json(monkeypatch):
    from shared.llm.providers import google as gmod

    captured: dict = {}

    class _FakeConfig:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake_types = types.SimpleNamespace(
        GenerateContentConfig=_FakeConfig,
        ThinkingConfig=lambda **k: None,
    )

    class _FakePart:
        text = "hello"
        thought = False

    class _FakeContent:
        parts = [_FakePart()]

    class _FakeCandidate:
        content = _FakeContent()

    class _FakeResponse:
        candidates = [_FakeCandidate()]
        usage_metadata = types.SimpleNamespace(
            prompt_token_count=1, candidates_token_count=1
        )
        text = "hello"

    class _FakeModels:
        def generate_content(self, model, contents, config):
            return _FakeResponse()

    class _FakeClient:
        models = _FakeModels()

    monkeypatch.setattr(gmod, "_client", lambda cfg: _FakeClient())
    monkeypatch.setattr(gmod, "_genai", lambda: (None, fake_types))

    adapter = gmod.GoogleAdapter(_cfg("google"))
    adapter._complete_sync("sys", "user", include_thoughts=False, response_json=False)
    assert "response_mime_type" not in captured


# ── OpenAI-family fail-open: a 400 on json_object retries WITHOUT the field ──
#
# Some models on a supported provider (e.g. deepseek-reasoner) reject
# response_format json_object. A hard 400 would fail the whole turn — strictly
# worse than the parse-failure class json mode was meant to prevent. The
# adapter must retry once without the field and succeed, falling back to the
# JSON-in-text parser. This is the documented fallback, not a silent broken
# request. (Bug class: silent broken structured-output request.)


def _mock_client_factory(handler):
    """Return a drop-in for httpx.AsyncClient bound to a MockTransport.

    Captures the REAL AsyncClient class up front so the factory keeps working
    after ``httpx.AsyncClient`` is monkeypatched to this factory.
    """
    real_client_cls = httpx.AsyncClient

    def _factory(*_args, **kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("transport", None)
        return real_client_cls(transport=httpx.MockTransport(handler), **kwargs)

    return _factory


@pytest.mark.asyncio
async def test_openai_400_on_json_object_retries_without_it(monkeypatch):
    seen_payloads: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        body = _json.loads(request.content)
        seen_payloads.append(body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": {"message": "json mode unsupported"}})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"query": {}}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(_handler))
    adapter = OpenAICompatibleAdapter(
        _cfg("deepseek", base_url="https://api.deepseek.test/v1")
    )
    out = await adapter.complete("respond with json", "question", response_json=True)
    assert out == '{"query": {}}'
    # Exactly two attempts: first WITH response_format (rejected), second WITHOUT.
    assert len(seen_payloads) == 2
    assert "response_format" in seen_payloads[0]
    assert "response_format" not in seen_payloads[1]


@pytest.mark.asyncio
async def test_openai_non_400_error_is_not_retried_as_fallback(monkeypatch):
    # A 500 (or any non-400) must NOT trigger the json-mode fallback retry —
    # only a 400 with response_format set does. A 500 raises immediately.
    attempts = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(500, json={"error": {"message": "server error"}})

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(_handler))
    adapter = OpenAICompatibleAdapter(
        _cfg("openai", base_url="https://api.openai.test/v1")
    )
    with pytest.raises(RuntimeError):
        await adapter.complete("respond with json", "question", response_json=True)
    assert attempts["n"] == 1  # no fallback retry on a 500


@pytest.mark.asyncio
async def test_openai_stream_400_on_json_object_retries_without_duplication(monkeypatch):
    # Stream-path fail-open (review R1-2): the 400 is detected on response
    # status BEFORE any token is emitted, so the single retry without
    # response_format must yield each token exactly once — never doubled,
    # never dropped — and must make exactly two requests.
    seen_payloads: list[dict] = []

    _sse = (
        'data: {"choices": [{"delta": {"content": "{\\"query\\""}}]}\n\n'
        'data: {"choices": [{"delta": {"content": ": {}}"}}], '
        '"usage": {"prompt_tokens": 5, "completion_tokens": 4}}\n\n'
        "data: [DONE]\n\n"
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        body = _json.loads(request.content)
        seen_payloads.append(body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": {"message": "json mode unsupported"}})
        return httpx.Response(
            200,
            content=_sse.encode(),
            headers={"Content-Type": "text/event-stream"},
        )

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(_handler))
    adapter = OpenAICompatibleAdapter(
        _cfg("deepseek", base_url="https://api.deepseek.test/v1")
    )
    tokens = [
        t async for t in adapter.stream_complete(
            "respond with json", "question", response_json=True
        )
    ]
    assert "".join(tokens) == '{"query": {}}'
    assert len(tokens) == 2  # exactly the two deltas, un-duplicated
    assert len(seen_payloads) == 2
    assert "response_format" in seen_payloads[0]
    assert "response_format" not in seen_payloads[1]
    assert adapter.last_usage == {"input_tokens": 5, "output_tokens": 4}


@pytest.mark.asyncio
async def test_openai_stream_second_400_raises(monkeypatch):
    # A 400 that persists after dropping response_format was never a json-mode
    # rejection — it must raise, not loop or silently return an empty stream.
    attempts = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(_handler))
    adapter = OpenAICompatibleAdapter(
        _cfg("openai", base_url="https://api.openai.test/v1")
    )
    with pytest.raises(RuntimeError):
        async for _ in adapter.stream_complete(
            "respond with json", "q", response_json=True
        ):
            pass
    assert attempts["n"] == 2  # first with response_format, retry without, then raise
