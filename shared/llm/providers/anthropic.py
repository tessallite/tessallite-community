"""Anthropic Claude adapter."""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncGenerator

import httpx

from shared.config.bootstrap import system_snapshot_get
from shared.llm.adapter import LLMAdapter, ThinkingCallback

logger = logging.getLogger(__name__)

# Anthropic API validity bounds for extended thinking:
#   1024 <= budget_tokens < max_tokens
# (requests violating either bound are rejected with a 400).
_MIN_THINKING_BUDGET = 1024

# Last-resort fallback mirroring the registry default for
# ``llm.anthropic_thinking_budget`` — only reachable if the snapshot/registry
# returns a non-int (broken deployment); keep the two values in sync.
_DEFAULT_THINKING_BUDGET = 4000


class AnthropicAdapter(LLMAdapter):
    supports_thinking: bool = True

    def _thinking_budget(self) -> int | None:
        """Resolve the extended-thinking budget (R6/F15).

        The budget is an INDEPENDENT knob, never derived from ``max_tokens`` —
        the old ``max(200, min(8000, max_tokens // 2))`` formula silently
        halved reasoning depth whenever an operator lowered ``max_tokens`` for
        cost control, a hidden accuracy regression on the accuracy-critical
        planner step.

        Resolution order:
          1. per-row override ``config.config['thinking_budget']`` (int > 0)
          2. system default ``llm.anthropic_thinking_budget`` (registry: 4000)

        ``max_tokens`` influences the result ONLY as an API-validity ceiling:
        the API requires ``1024 <= budget_tokens < max_tokens``.
          * ``max_tokens <= 1024`` — NO valid budget exists; return ``None``
            so the caller disables thinking for this request (loud warning)
            instead of sending a guaranteed-400 payload.
          * budget >= max_tokens — clamp to ``max_tokens - 1`` (guaranteed
            >= 1024 given the previous rule) with a warning so the operator
            sees the coupling their low output cap created.
          * budget < 1024 — raised to the API minimum (cannot violate the
            ceiling: max_tokens > 1024 is guaranteed at this point).
        A high ``max_tokens`` (the default case) never alters the configured
        budget.
        """
        override = (self.config.config or {}).get("thinking_budget")
        budget: int | None = None
        if isinstance(override, int) and not isinstance(override, bool) and override > 0:
            budget = override
        if budget is None:
            snap = system_snapshot_get("llm.anthropic_thinking_budget")
            if isinstance(snap, int) and not isinstance(snap, bool) and snap > 0:
                budget = snap
        if budget is None:
            budget = _DEFAULT_THINKING_BUDGET

        max_tokens = self.config.max_tokens
        if isinstance(max_tokens, int) and max_tokens >= 1:
            if max_tokens <= _MIN_THINKING_BUDGET:
                logger.warning(
                    "Anthropic max_tokens %d cannot fit any valid extended-"
                    "thinking budget (API requires %d <= budget_tokens < "
                    "max_tokens); disabling thinking for this request. Raise "
                    "max_tokens above %d to restore planner reasoning.",
                    max_tokens, _MIN_THINKING_BUDGET, _MIN_THINKING_BUDGET,
                )
                return None
            if budget >= max_tokens:
                clamped = max_tokens - 1  # >= 1024 given max_tokens > 1024
                logger.warning(
                    "Anthropic thinking_budget %d >= max_tokens %d; clamped to "
                    "%d to satisfy the API constraint. Raise max_tokens to "
                    "restore the full reasoning budget.",
                    budget, max_tokens, clamped,
                )
                budget = clamped
        if budget < _MIN_THINKING_BUDGET:
            logger.warning(
                "Anthropic thinking_budget %d is below the API minimum %d; "
                "raised to the minimum.",
                budget, _MIN_THINKING_BUDGET,
            )
            budget = _MIN_THINKING_BUDGET
        return budget

    def _log_cache_usage(self, usage: dict) -> None:
        """R1 (F1) — in-product observer for the Section-7 acceptance signal.

        ``cache_read_input_tokens > 0`` on a repeat call proves the breakpoint
        landed; logging it makes the check possible from service logs instead
        of only the provider console. INFO on activity, silent otherwise.
        """
        read = usage.get("cache_read_input_tokens") or 0
        written = usage.get("cache_creation_input_tokens") or 0
        if read or written:
            logger.info(
                "[LLM_CACHE] anthropic/%s cache_read_input_tokens=%d "
                "cache_creation_input_tokens=%d uncached_input_tokens=%d",
                self.config.model_name, read, written,
                usage.get("input_tokens") or 0,
            )

    def _cache_prefix_enabled(self) -> bool:
        """R1 (F1) — operator kill-switch for the ``cache_control`` breakpoint.

        A misplaced or unexpected breakpoint bills full price (never wrong
        results — the rendered text is identical), so an operator can disable
        the marker system-wide without a redeploy. Defaults ON; only a stored
        ``False`` disables it.
        """
        flag = system_snapshot_get("llm.prompt_cache_enabled")
        # Treat anything other than an explicit False as enabled (missing key /
        # broken snapshot must not silently disable a correctness-neutral perf
        # feature).
        return flag is not False

    def _build_request(
        self,
        system: str,
        user: str,
        stream: bool = False,
        thinking: bool = False,
        cache_system_prefix: bool = False,
    ) -> tuple[str, dict, dict]:
        endpoints = system_snapshot_get("llm.provider_endpoints") or {}
        anthropic_version = str(system_snapshot_get("llm.anthropic_api_version"))
        headers = {
            "x-api-key": self.config.api_key,
            "anthropic-version": anthropic_version,
            "content-type": "application/json",
        }
        # R1 (F1) — when the caller marks the stable prefix cacheable, send the
        # system prompt as a single text block carrying ``cache_control``. The
        # block's ``text`` is byte-identical to the plain-string form, so the
        # model reads exactly the same prompt — this only lets Anthropic serve a
        # repeated stable prefix from cache. A blank system is left absent (no
        # cacheable prefix to mark). Disabled providers / kill-switch fall back
        # to the plain string.
        system_field: Any = system
        if cache_system_prefix and system and self._cache_prefix_enabled():
            system_field = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        payload: dict = {
            "model": self.config.model_name,
            "max_tokens": self.config.max_tokens,
            "system": system_field,
            "messages": [{"role": "user", "content": user}],
            "stream": stream,
        }
        budget = self._thinking_budget() if thinking else None
        if budget is not None:
            payload["thinking"] = {
                "type": "enabled",
                "budget_tokens": budget,
            }
        else:
            # Either thinking was not requested, or max_tokens is too low to
            # fit any valid budget (R6 — _thinking_budget returned None with a
            # warning): degrade to a non-thinking request rather than sending
            # a payload the API is guaranteed to reject with a 400.
            payload["temperature"] = self.config.temperature
        default_base = endpoints.get("anthropic") or "https://api.anthropic.com"
        base = (self.config.base_url or default_base).rstrip("/")
        url = f"{base}/v1/messages"
        return url, headers, payload

    async def complete(
        self, system: str, user: str, on_thinking: ThinkingCallback = None,
        response_json: bool = False, cache_system_prefix: bool = False,
    ) -> str:
        # R5 (F6) — Anthropic has no plain JSON-object output mode; its only
        # native structured output is forced tool_use / json_schema, which is
        # subset-risky for the planner's recursive 8-tool union and could
        # over-constrain into a wrong plan. Per the spec this is the DOCUMENTED
        # fallback: ignore ``response_json`` and rely on the JSON-in-text
        # parser — never a silent downgrade to a broken request.
        _ = response_json
        thinking = on_thinking is not None
        url, headers, payload = self._build_request(
            system, user, stream=False, thinking=thinking,
            cache_system_prefix=cache_system_prefix,
        )
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                try:
                    body = resp.json()
                    detail = body.get("error", {}).get("message", resp.text[:500])
                except Exception:
                    detail = resp.text[:500]
                raise RuntimeError(
                    f"anthropic/{self.config.model_name} "
                    f"returned {resp.status_code}: {detail}"
                )
            data = resp.json()

            text = ""
            for block in data.get("content", []):
                btype = block.get("type")
                if btype == "thinking":
                    if on_thinking:
                        t = block.get("thinking", "")
                        if t:
                            await on_thinking(t)
                elif btype == "text":
                    text += block.get("text", "")

            usage = data.get("usage") or {}
            # R1 (F1) — surface cache activity for observability. Anthropic
            # reports ``input_tokens`` as the UNCACHED remainder only, so we
            # keep it as-is for existing billing (summed by callers) and add
            # the cache fields alongside; ``cache_read_input_tokens > 0`` on a
            # second turn is the Section-7 acceptance signal. NOTE for cost
            # accounting: once caching engages, per-turn ``input_tokens``
            # totals drop while the real bill adds cache reads (~10% of list
            # input price) and cache writes (~125%) — the cost ledger reflects
            # the uncached remainder by design until per-stage cache-aware
            # accounting lands (spec section 7 token-accounting follow-up).
            self.last_usage = {
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "cache_creation_input_tokens": int(
                    usage.get("cache_creation_input_tokens") or 0
                ),
                "cache_read_input_tokens": int(
                    usage.get("cache_read_input_tokens") or 0
                ),
            }
            self._log_cache_usage(self.last_usage)
            if not text:
                stop_reason = data.get("stop_reason", "unknown")
                raise ValueError(
                    f"Anthropic returned empty text (stop_reason={stop_reason!r})."
                )
            return text

    async def stream_complete(
        self, system: str, user: str, on_thinking: ThinkingCallback = None,
        response_json: bool = False, cache_system_prefix: bool = False,
    ) -> AsyncGenerator[str, None]:
        # R5 (F6) — see ``complete``: Anthropic keeps the JSON-in-text path as
        # the documented fallback; ``response_json`` is intentionally ignored.
        _ = response_json
        thinking = on_thinking is not None
        url, headers, payload = self._build_request(
            system, user, stream=True, thinking=thinking,
            cache_system_prefix=cache_system_prefix,
        )
        input_tokens = 0
        output_tokens = 0
        cache_creation_input_tokens = 0
        cache_read_input_tokens = 0
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise RuntimeError(
                        f"anthropic/{self.config.model_name} returned {resp.status_code}"
                    )
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type", "")
                    if etype == "content_block_delta":
                        delta = event.get("delta", {})
                        dtype = delta.get("type", "")
                        if dtype == "thinking_delta":
                            if on_thinking:
                                t = delta.get("thinking", "")
                                if t:
                                    await on_thinking(t)
                        else:
                            text = delta.get("text") or delta.get("value", "")
                            if text:
                                yield text
                    elif etype == "message_start":
                        usage = event.get("message", {}).get("usage") or {}
                        input_tokens = int(usage.get("input_tokens") or 0)
                        # R1 (F1) — cache fields arrive on message_start.
                        cache_creation_input_tokens = int(
                            usage.get("cache_creation_input_tokens") or 0
                        )
                        cache_read_input_tokens = int(
                            usage.get("cache_read_input_tokens") or 0
                        )
                    elif etype == "message_delta":
                        usage = event.get("usage") or {}
                        output_tokens = int(usage.get("output_tokens") or 0)
        self.last_usage = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_creation_input_tokens,
            "cache_read_input_tokens": cache_read_input_tokens,
        }
        self._log_cache_usage(self.last_usage)
