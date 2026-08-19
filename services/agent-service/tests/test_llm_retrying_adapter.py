"""RetryingAdapter streaming retry contract tests."""
from __future__ import annotations

import pytest

from shared.llm.adapter import LLMAdapter, LLMConfig, RetryingAdapter

pytestmark = pytest.mark.unit


def _config() -> LLMConfig:
    return LLMConfig(
        provider="test",
        display_name="Test",
        base_url=None,
        api_key="test-key",
        model_name="test-model",
        max_tokens=100,
        temperature=0,
        timeout_seconds=30,
    )


class _PartialThenSuccessAdapter(LLMAdapter):
    def __init__(self) -> None:
        super().__init__(_config())
        self.stream_attempts = 0

    async def complete(self, system: str, user: str, on_thinking=None, response_json: bool = False, cache_system_prefix: bool = False) -> str:
        return "ok"

    async def stream_complete(self, system: str, user: str, on_thinking=None, response_json: bool = False, cache_system_prefix: bool = False):
        self.stream_attempts += 1
        if self.stream_attempts == 1:
            self.last_usage = {"input_tokens": 2, "output_tokens": 1}
            yield "Hello "
            raise ConnectionError("connection reset")
        yield "Hello world"
        self.last_usage = {"input_tokens": 2, "output_tokens": 2}


class _FailsBeforeTokenThenSuccessAdapter(LLMAdapter):
    def __init__(self) -> None:
        super().__init__(_config())
        self.stream_attempts = 0

    async def complete(self, system: str, user: str, on_thinking=None, response_json: bool = False, cache_system_prefix: bool = False) -> str:
        return "ok"

    async def stream_complete(self, system: str, user: str, on_thinking=None, response_json: bool = False, cache_system_prefix: bool = False):
        self.stream_attempts += 1
        if self.stream_attempts == 1:
            raise ConnectionError("connection reset")
        yield "Hello world"
        self.last_usage = {"input_tokens": 2, "output_tokens": 2}


async def _collect(adapter: RetryingAdapter) -> str:
    chunks: list[str] = []
    async for token in adapter.stream_complete("system", "user"):
        chunks.append(token)
    return "".join(chunks)


@pytest.mark.asyncio
async def test_stream_retry_stops_after_partial_output():
    primary = _PartialThenSuccessAdapter()
    adapter = RetryingAdapter(primary)

    chunks: list[str] = []
    with pytest.raises(ConnectionError):
        async for token in adapter.stream_complete("system", "user"):
            chunks.append(token)

    assert "".join(chunks) == "Hello "
    assert primary.stream_attempts == 1
    assert adapter.last_usage == {"input_tokens": 2, "output_tokens": 1}


@pytest.mark.asyncio
async def test_stream_retry_still_retries_before_first_output():
    primary = _FailsBeforeTokenThenSuccessAdapter()
    adapter = RetryingAdapter(primary)

    result = await _collect(adapter)

    assert result == "Hello world"
    assert primary.stream_attempts == 2
    assert adapter.last_usage == {"input_tokens": 2, "output_tokens": 2}


# ---------------------------------------------------------------------------
# R5/F6 (review R2-1) — response_json passthrough contract. The planner's
# JSON mode flows exclusively through RetryingAdapter; if the passthrough in
# adapter.py were dropped, JSON mode would silently vanish on the planner
# path with every other test still green — the exact silent-downgrade class
# this lane closes. Tier: T1 (producer/consumer contract).
# ---------------------------------------------------------------------------

class _RecordingAdapter(LLMAdapter):
    def __init__(self, fail_complete: bool = False) -> None:
        super().__init__(_config())
        self.complete_response_json: list[bool] = []
        self.stream_response_json: list[bool] = []
        self._fail_complete = fail_complete

    async def complete(self, system: str, user: str, on_thinking=None, response_json: bool = False, cache_system_prefix: bool = False) -> str:
        self.complete_response_json.append(response_json)
        if self._fail_complete:
            # message deliberately avoids retryable keywords so RetryingAdapter
            # fails over immediately without backoff sleeps
            raise ValueError("provider rejected the request")
        return "ok"

    async def stream_complete(self, system: str, user: str, on_thinking=None, response_json: bool = False, cache_system_prefix: bool = False):
        self.stream_response_json.append(response_json)
        yield "tok"
        self.last_usage = {"input_tokens": 1, "output_tokens": 1}


@pytest.mark.asyncio
async def test_complete_forwards_response_json_to_primary():
    primary = _RecordingAdapter()
    adapter = RetryingAdapter(primary)

    assert await adapter.complete("system", "user", response_json=True) == "ok"
    assert primary.complete_response_json == [True]

    # and the default stays False when the caller does not opt in
    assert await adapter.complete("system", "user") == "ok"
    assert primary.complete_response_json == [True, False]


@pytest.mark.asyncio
async def test_complete_forwards_response_json_to_failover_adapter():
    primary = _RecordingAdapter(fail_complete=True)
    fallback = _RecordingAdapter()
    adapter = RetryingAdapter(primary)
    # inject the fallback directly — build_adapter needs a real provider config
    adapter._build_fallback_adapters = lambda: [fallback]

    assert await adapter.complete("system", "user", response_json=True) == "ok"
    assert primary.complete_response_json == [True]
    assert fallback.complete_response_json == [True]


@pytest.mark.asyncio
async def test_stream_complete_forwards_response_json_to_primary():
    primary = _RecordingAdapter()
    adapter = RetryingAdapter(primary)

    tokens = [t async for t in adapter.stream_complete("system", "user", response_json=True)]
    assert tokens == ["tok"]
    assert primary.stream_response_json == [True]
