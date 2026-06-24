"""Provider-agnostic LLM client interface and factory.

Shared across all services. Each provider has its own module under
``shared.llm.providers``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import AsyncGenerator, Optional

ThinkingCallback = Optional[Callable[[str], Awaitable[None]]]

logger = logging.getLogger(__name__)

_RETRY_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_BASE = 1.0  # seconds
_RETRYABLE_ERRORS = (
    ConnectionError,
    TimeoutError,
    OSError,  # covers [Errno 101] Network is unreachable and similar
)
_RETRYABLE_HTTP_STATUSES = {429, 502, 503}


def _is_retryable(exc: Exception) -> bool:
    """Check if an exception warrants a retry."""
    if isinstance(exc, _RETRYABLE_ERRORS):
        return True
    msg = str(exc).lower()
    if any(kw in msg for kw in ("network", "timeout", "unreachable", "reset", "refused")):
        return True
    if hasattr(exc, "code"):
        code = getattr(exc, "code", None)
        if isinstance(code, int) and code in _RETRYABLE_HTTP_STATUSES:
            return True
    if hasattr(exc, "status_code"):
        sc = getattr(exc, "status_code", None)
        if isinstance(sc, int) and sc in _RETRYABLE_HTTP_STATUSES:
            return True
    return False


@dataclass
class LLMConfig:
    """Resolved LLM configuration ready to build an adapter from."""

    provider: str
    display_name: str
    base_url: Optional[str]
    api_key: Optional[str]
    model_name: str
    max_tokens: int
    temperature: float
    timeout_seconds: int
    config: dict = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.config is None:
            object.__setattr__(self, 'config', {})


class LLMAdapter(ABC):
    """Base class for all LLM provider adapters."""

    supports_thinking: bool = False

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self.last_usage: Optional[dict] = None

    @abstractmethod
    async def complete(self, system: str, user: str, on_thinking: ThinkingCallback = None) -> str:
        """Send a completion request and return the raw text response.

        If ``on_thinking`` is provided and the provider supports extended
        thinking, thinking tokens are passed to the callback as they are
        received; only the answer text is returned.
        """
        ...

    @abstractmethod
    async def stream_complete(self, system: str, user: str, on_thinking: ThinkingCallback = None) -> AsyncGenerator[str, None]:
        """Stream answer tokens. Yields text deltas as they arrive.
        Thinking tokens (if any) are routed to ``on_thinking`` instead.
        Must update self.last_usage when the stream ends."""
        ...

    async def test(self) -> dict:
        """Send a minimal ping prompt and return latency/success info."""
        try:
            start = time.monotonic()
            await self.complete(
                system="You are a test assistant.",
                user="Reply with exactly: OK",
            )
            elapsed = int((time.monotonic() - start) * 1000)
            return {"ok": True, "latency_ms": elapsed, "error": None}
        except Exception as exc:
            return {"ok": False, "latency_ms": None, "error": str(exc)}


class RetryingAdapter:
    """Wraps a primary LLMAdapter with retry + optional provider failover.

    When ``fallback_configs`` is provided, the wrapper tries each fallback
    provider in order if the primary exhausts all retry attempts.

    Scope constraint: failover only applies at the start of a new call.
    It does NOT attempt to resume a partially-streamed response from a
    different provider.
    """

    def __init__(
        self,
        primary: LLMAdapter,
        fallback_configs: list[LLMConfig] | None = None,
    ) -> None:
        self._primary = primary
        self._fallback_configs = fallback_configs or []
        # Expose the primary adapter's attributes for usage tracking
        self.last_usage = primary.last_usage
        self.config = primary.config

    @property
    def supports_thinking(self) -> bool:
        return self._primary.supports_thinking

    def _build_fallback_adapters(self) -> list[LLMAdapter]:
        adapters: list[LLMAdapter] = []
        for cfg in self._fallback_configs:
            try:
                adapters.append(build_adapter(cfg))
            except Exception as exc:
                logger.warning(
                    "Cannot build fallback adapter for %s/%s: %s",
                    cfg.provider, cfg.display_name, exc,
                )
        return adapters

    async def complete(
        self,
        system: str,
        user: str,
        on_thinking: ThinkingCallback = None,
    ) -> str:
        """Complete with retry on primary, then failover to fallbacks."""
        adapters = [self._primary] + self._build_fallback_adapters()
        last_error: Exception | None = None

        for idx, adapter in enumerate(adapters):
            for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
                try:
                    result = await adapter.complete(system, user, on_thinking=on_thinking)
                    self.last_usage = adapter.last_usage
                    if idx > 0:
                        logger.info(
                            "LLM failover succeeded: primary=%s failed, "
                            "fallback=%s/%s",
                            self._primary.config.provider,
                            adapter.config.provider,
                            adapter.config.display_name,
                        )
                    return result
                except Exception as exc:
                    last_error = exc
                    if not _is_retryable(exc):
                        break  # non-retryable — skip to next adapter
                    if attempt < _RETRY_MAX_ATTEMPTS:
                        wait = _RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                        logger.debug(
                            "LLM call %s/%s attempt %d failed (%s), "
                            "retrying in %.1fs",
                            adapter.config.provider,
                            adapter.config.display_name,
                            attempt,
                            type(exc).__name__,
                            wait,
                        )
                        await asyncio.sleep(wait)

            logger.warning(
                "LLM adapter %s/%s exhausted retries: %s",
                adapter.config.provider,
                adapter.config.display_name,
                last_error,
            )

        raise last_error or RuntimeError("All LLM adapters failed")

    async def stream_complete(
        self,
        system: str,
        user: str,
        on_thinking: ThinkingCallback = None,
    ) -> AsyncGenerator[str, None]:
        """Stream with retry on primary only (no mid-stream failover)."""
        last_error: Exception | None = None
        for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
            try:
                async for token in self._primary.stream_complete(
                    system, user, on_thinking=on_thinking,
                ):
                    yield token
                self.last_usage = self._primary.last_usage
                return
            except Exception as exc:
                last_error = exc
                if not _is_retryable(exc):
                    break
                if attempt < _RETRY_MAX_ATTEMPTS:
                    wait = _RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                    logger.debug(
                        "LLM stream %s attempt %d failed (%s), retrying in %.1fs",
                        self._primary.config.provider,
                        attempt,
                        type(exc).__name__,
                        wait,
                    )
                    await asyncio.sleep(wait)
        raise last_error or RuntimeError("Primary LLM adapter streaming failed")

    async def test(self) -> dict:
        return await self._primary.test()


def build_adapter(config: LLMConfig) -> LLMAdapter:
    """Factory: build the right adapter based on provider name."""
    if not config.api_key:
        # Vertex AI mode on Google uses ADC, not an API key.
        if not (
            config.provider == "google"
            and (config.config or {}).get("google_mode") == "vertex_ai"
        ):
            raise ValueError(
                f"No API key configured for provider '{config.provider}' "
                f"({config.display_name}). Open the LLM Configurations tab in "
                f"Project Settings, edit this provider, and enter a valid API key."
            )
    if config.provider in ("openai", "deepseek", "glm", "ollama"):
        from shared.llm.providers.openai_compat import OpenAICompatibleAdapter

        return OpenAICompatibleAdapter(config)
    if config.provider == "google":
        from shared.llm.providers.google import GoogleAdapter

        return GoogleAdapter(config)
    if config.provider == "anthropic":
        from shared.llm.providers.anthropic import AnthropicAdapter

        return AnthropicAdapter(config)
    raise ValueError(f"Unknown LLM provider: {config.provider}")


def decrypt_api_key(encrypted: Optional[bytes]) -> Optional[str]:
    """Decrypt a Fernet-encrypted API key."""
    if encrypted is None:
        return None
    from cryptography.fernet import InvalidToken

    try:
        from shared.security.credential_crypto import decrypt_str
        return decrypt_str(encrypted)
    except InvalidToken:
        logger.error(
            "Failed to decrypt LLM API key — CREDENTIAL_ENCRYPTION_KEY may "
            "have changed since the key was saved. Re-enter the API key in "
            "Project Settings > LLM Configurations."
        )
        return None


def to_llm_config(record) -> LLMConfig:
    """Convert an ``LLMProviderConfig`` ORM record to an ``LLMConfig``."""
    return LLMConfig(
        provider=record.provider,
        display_name=record.display_name,
        base_url=record.base_url,
        api_key=decrypt_api_key(record.encrypted_api_key),
        model_name=record.model_name,
        max_tokens=record.max_tokens,
        temperature=record.temperature,
        timeout_seconds=record.timeout_seconds,
        config=getattr(record, 'config', None) or {},
    )
