"""OpenAI-compatible adapter — handles OpenAI, DeepSeek, GLM, and Ollama."""
from __future__ import annotations

import json
import logging
import time
from typing import AsyncGenerator

import httpx

from shared.config.bootstrap import system_snapshot_get
from shared.llm.adapter import LLMAdapter

logger = logging.getLogger(__name__)


def _glm_jwt(api_key: str) -> str:
    """Zhipu GLM keys are ``{kid}.{secret}``; the API expects a signed JWT."""
    import jwt as pyjwt

    kid, secret = api_key.split(".", 1)
    now = int(time.time())
    payload = {"api_key": kid, "exp": now + 3600, "timestamp": now}
    return pyjwt.encode(
        payload,
        secret,
        algorithm="HS256",
        headers={"alg": "HS256", "sign_type": "SIGN"},
    )


class OpenAICompatibleAdapter(LLMAdapter):
    def _base_url(self) -> str:
        if self.config.base_url:
            return self.config.base_url.rstrip("/")
        endpoints = system_snapshot_get("llm.provider_endpoints") or {}
        url = endpoints.get(self.config.provider) or endpoints.get("openai")
        return str(url).rstrip("/")

    def _auth_token(self) -> str | None:
        if not self.config.api_key:
            return None
        if self.config.provider == "glm" and "." in self.config.api_key:
            base = self._base_url()
            if "api.z.ai" not in base:
                try:
                    return _glm_jwt(self.config.api_key)
                except Exception:
                    logger.warning("GLM JWT generation failed; falling back to raw key")
        return self.config.api_key

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept-Language": "en-US,en",
        }
        token = self._auth_token()
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    async def complete(self, system: str, user: str, on_thinking=None) -> str:
        payload = {
            "model": self.config.model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        url = f"{self._base_url()}/chat/completions"
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            resp = await client.post(url, headers=self._headers(), json=payload)
            if resp.status_code >= 400:
                try:
                    body = resp.json()
                    detail = body.get("error", {}).get("message", resp.text[:500])
                except Exception:
                    detail = resp.text[:500]
                raise RuntimeError(
                    f"{self.config.provider}/{self.config.model_name} "
                    f"returned {resp.status_code}: {detail}"
                )
            data = resp.json()
            msg = data["choices"][0]["message"]
            content = msg.get("content") or ""
            if not content:
                content = msg.get("reasoning_content") or ""
            usage = data.get("usage") or {}
            self.last_usage = {
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
            }
            if not content:
                finish_reason = data["choices"][0].get("finish_reason", "unknown")
                raise ValueError(
                    f"LLM returned empty content (finish_reason={finish_reason!r})."
                )
            return content

    async def stream_complete(self, system: str, user: str, on_thinking=None) -> AsyncGenerator[str, None]:
        payload = {
            "model": self.config.model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        url = f"{self._base_url()}/chat/completions"
        input_tokens = 0
        output_tokens = 0
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            async with client.stream("POST", url, headers=self._headers(), json=payload) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise RuntimeError(
                        f"{self.config.provider}/{self.config.model_name} returned {resp.status_code}"
                    )
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    usage = chunk.get("usage")
                    if usage:
                        input_tokens = int(usage.get("prompt_tokens") or 0)
                        output_tokens = int(usage.get("completion_tokens") or 0)
                    choices = chunk.get("choices") or []
                    for choice in choices:
                        delta = choice.get("delta") or {}
                        text = delta.get("content")
                        if text:
                            yield text
        self.last_usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
