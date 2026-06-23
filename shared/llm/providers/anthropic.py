"""Anthropic Claude adapter."""
from __future__ import annotations

import json
from typing import AsyncGenerator

import httpx

from shared.config.bootstrap import system_snapshot_get
from shared.llm.adapter import LLMAdapter, ThinkingCallback


class AnthropicAdapter(LLMAdapter):
    supports_thinking: bool = True

    def _thinking_budget(self) -> int:
        return max(200, min(8000, self.config.max_tokens // 2))

    def _build_request(
        self, system: str, user: str, stream: bool = False, thinking: bool = False
    ) -> tuple[str, dict, dict]:
        endpoints = system_snapshot_get("llm.provider_endpoints") or {}
        anthropic_version = str(system_snapshot_get("llm.anthropic_api_version"))
        headers = {
            "x-api-key": self.config.api_key,
            "anthropic-version": anthropic_version,
            "content-type": "application/json",
        }
        payload: dict = {
            "model": self.config.model_name,
            "max_tokens": self.config.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "stream": stream,
        }
        if thinking:
            payload["thinking"] = {
                "type": "enabled",
                "budget_tokens": self._thinking_budget(),
            }
        else:
            payload["temperature"] = self.config.temperature
        default_base = endpoints.get("anthropic") or "https://api.anthropic.com"
        base = (self.config.base_url or default_base).rstrip("/")
        url = f"{base}/v1/messages"
        return url, headers, payload

    async def complete(self, system: str, user: str, on_thinking: ThinkingCallback = None) -> str:
        thinking = on_thinking is not None
        url, headers, payload = self._build_request(system, user, stream=False, thinking=thinking)
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
            self.last_usage = {
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
            }
            if not text:
                stop_reason = data.get("stop_reason", "unknown")
                raise ValueError(
                    f"Anthropic returned empty text (stop_reason={stop_reason!r})."
                )
            return text

    async def stream_complete(self, system: str, user: str, on_thinking: ThinkingCallback = None) -> AsyncGenerator[str, None]:
        thinking = on_thinking is not None
        url, headers, payload = self._build_request(system, user, stream=True, thinking=thinking)
        input_tokens = 0
        output_tokens = 0
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
                    elif etype == "message_delta":
                        usage = event.get("usage") or {}
                        output_tokens = int(usage.get("output_tokens") or 0)
        self.last_usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
