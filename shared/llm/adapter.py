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
    async def complete(
        self,
        system: str,
        user: str,
        on_thinking: ThinkingCallback = None,
        response_json: bool = False,
        cache_system_prefix: bool = False,
    ) -> str:
        """Send a completion request and return the raw text response.

        If ``on_thinking`` is provided and the provider supports extended
        thinking, thinking tokens are passed to the callback as they are
        received; only the answer text is returned.

        R5 (F6) — when ``response_json`` is True the adapter requests the
        provider's native JSON-output mode where supported (OpenAI-family
        ``response_format: json_object``, Gemini ``response_mime_type:
        application/json``), guaranteeing the response is valid JSON and
        eliminating the dominant parse-failure class (markdown fences /
        prose-wrapped output). Providers that genuinely lack a plain
        JSON-object mode (Anthropic) ignore the flag and rely on the
        JSON-in-text parser as the documented fallback — never a silent
        downgrade to a broken request. Defaults False so narration, judge,
        correction, and test calls are unchanged.

        R1 (F1) — when ``cache_system_prefix`` is True the adapter marks the
        ``system`` string as a cacheable prefix where the provider supports
        native prompt caching (Anthropic ``cache_control: ephemeral`` on the
        system block). This is a MARKER only: the rendered system text is
        byte-identical with or without the flag, so it can never change what
        the model reads — it only lets the provider serve a repeated stable
        prefix from cache. Providers without native caching ignore the flag
        (explicit no-op — implicit prefix caching or none). Defaults False so
        every existing caller is unchanged.
        """
        ...

    @abstractmethod
    async def stream_complete(
        self,
        system: str,
        user: str,
        on_thinking: ThinkingCallback = None,
        response_json: bool = False,
        cache_system_prefix: bool = False,
    ) -> AsyncGenerator[str, None]:
        """Stream answer tokens. Yields text deltas as they arrive.
        Thinking tokens (if any) are routed to ``on_thinking`` instead.
        Must update self.last_usage when the stream ends.

        See ``complete`` for ``response_json`` (R5/F6) and
        ``cache_system_prefix`` (R1/F1) semantics."""
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
        response_json: bool = False,
        cache_system_prefix: bool = False,
    ) -> str:
        """Complete with retry on primary, then failover to fallbacks.

        Bug-7587 -- suppress thinking callback on retry/failover so
        previously-delivered thinking tokens are not duplicated in the
        persisted thought_summary or streamed SSE events.  Uses a tracking
        wrapper (matching stream_complete) that only sets the flag when a
        thinking token is actually delivered, so retries after a pre-token
        failure preserve thinking on the successful attempt.
        """
        adapters = [self._primary] + self._build_fallback_adapters()
        last_error: Exception | None = None
        thinking_emitted = False

        for idx, adapter in enumerate(adapters):
            for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
                # Bug-7587 -- suppress thinking on retry/failover after
                # thinking has already been delivered.
                effective_thinking: ThinkingCallback = (
                    None if thinking_emitted else on_thinking
                )

                async def _tracking_complete_thinking(token: str) -> None:
                    nonlocal thinking_emitted
                    thinking_emitted = True
                    if effective_thinking is not None:
                        await effective_thinking(token)

                wrapped: ThinkingCallback = (
                    _tracking_complete_thinking if on_thinking is not None else None
                )
                try:
                    result = await adapter.complete(
                        system, user, on_thinking=wrapped,
                        response_json=response_json,
                        cache_system_prefix=cache_system_prefix,
                    )
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
                        break  # non-retryable -- skip to next adapter
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
        response_json: bool = False,
        cache_system_prefix: bool = False,
    ) -> AsyncGenerator[str, None]:
        """Stream with retry on primary only before any user-visible token.

        Bug-7587 -- thinking-token callbacks are user-visible output: once
        emitted they cannot be retracted.  On retry we suppress the callback
        so previously-delivered thinking tokens are not duplicated.  If
        thinking was emitted before a failure we also treat it as "emitted"
        for the retry-gate, preventing a retry that would duplicate the
        thinking prefix.
        """
        last_error: Exception | None = None
        thinking_emitted = False

        for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
            emitted = False

            # Bug-7587 -- after a failed attempt that already delivered
            # thinking tokens, suppress the callback on retry so the same
            # thinking content is not sent twice.
            effective_thinking: ThinkingCallback = (
                None if thinking_emitted else on_thinking
            )

            # Track whether this attempt emits thinking tokens.
            thinking_emitted_this_attempt = False

            async def _tracking_thinking(token: str) -> None:
                nonlocal thinking_emitted, thinking_emitted_this_attempt
                thinking_emitted = True
                thinking_emitted_this_attempt = True
                if effective_thinking is not None:
                    await effective_thinking(token)

            wrapped_thinking: ThinkingCallback = (
                _tracking_thinking if on_thinking is not None else None
            )

            try:
                async for token in self._primary.stream_complete(
                    system, user, on_thinking=wrapped_thinking,
                    response_json=response_json,
                    cache_system_prefix=cache_system_prefix,
                ):
                    emitted = True
                    yield token
                self.last_usage = self._primary.last_usage
                return
            except Exception as exc:
                last_error = exc
                if self._primary.last_usage is not None:
                    self.last_usage = self._primary.last_usage
                # Bug-7587 -- thinking tokens are user-visible; treat them
                # the same as emitted answer tokens for the retry gate.
                if emitted or thinking_emitted_this_attempt:
                    logger.warning(
                        "LLM stream %s failed after emitting output "
                        "(answer=%s, thinking=%s); not retrying "
                        "to avoid duplicate streamed text",
                        self._primary.config.provider,
                        emitted,
                        thinking_emitted_this_attempt,
                    )
                    break
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


_ENV_API_KEY_VARS = {
    "google": "GOOGLE_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "glm": "GLM_API_KEY",
}


def _env_api_key(provider: str) -> Optional[str]:
    """Operator-configured bring-your-own API key for ``provider`` from the
    process environment.

    Used as a fallback when an LLM config carries no stored key, so a
    self-hosted deployment can supply keys via ``.env`` rather than requiring a
    per-config key. Returns ``None`` when the provider has no env mapping or the
    variable is unset/empty.
    """
    import os

    var = _ENV_API_KEY_VARS.get(provider)
    value = os.environ.get(var) if var else None
    return value or None


def build_adapter(config: LLMConfig) -> LLMAdapter:
    """Factory: build the right adapter based on provider name.

    Bug-7108 — defensively validates ``config.base_url`` against the
    shared SSRF allowlist before constructing the adapter.  This is the
    last-resort enforcement point; API-layer validators should reject
    earlier where possible.
    """
    from shared.llm.base_url_validator import validate_llm_base_url

    validate_llm_base_url(config.base_url)

    if not config.api_key:
        # Fall back to an operator-configured bring-your-own key from the
        # environment when the config carries no stored key.
        env_key = _env_api_key(config.provider)
        is_google_vertex = (
            config.provider == "google"
            and (config.config or {}).get("google_mode") == "vertex_ai"
        )
        if is_google_vertex:
            # A seeded/imported Vertex-AI config is unusable on a deployment
            # where service-account auth is disabled. When the operator has
            # supplied a bring-your-own GOOGLE_API_KEY, transparently switch to
            # the api_key path instead of failing the turn; otherwise leave the
            # config untouched so the Google adapter raises its clear
            # SA-disabled / ADC error.
            from shared.llm.sa_auth import service_account_auth_allowed

            if not service_account_auth_allowed() and env_key:
                from dataclasses import replace

                downgraded = {**(config.config or {}), "google_mode": "api_key"}
                config = replace(config, api_key=env_key, config=downgraded)
        elif env_key:
            from dataclasses import replace

            config = replace(config, api_key=env_key)

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
