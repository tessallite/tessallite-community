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

# R5 (F6) — providers whose /chat/completions endpoint accepts the OpenAI
# ``response_format: {"type": "json_object"}`` structured-output field. Ollama
# and unknown OpenAI-compatible endpoints are excluded: they may reject the
# field with a 400, so for them the JSON-in-text parser stays the (documented)
# path rather than risking a broken request. json_object mode guarantees valid
# JSON, eliminating the dominant parse-failure class (markdown fences / prose).
_JSON_OBJECT_PROVIDERS = frozenset({"openai", "deepseek", "glm"})


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

    def _maybe_json_mode(
        self, payload: dict, system: str, user: str, response_json: bool
    ) -> None:
        """Set ``response_format: json_object`` in-place when R5 JSON mode is
        requested and the provider supports it.

        OpenAI's json_object mode 400s unless the literal token "json" appears
        somewhere in the messages; the planner tool-spec preamble always says
        "respond with exactly one JSON object", but guard defensively so a
        future caller cannot trip the 400 — if neither message mentions json,
        skip the field and let the JSON-in-text parser handle the response.
        """
        if not response_json:
            return
        if self.config.provider not in _JSON_OBJECT_PROVIDERS:
            return
        if "json" not in system.lower() and "json" not in user.lower():
            logger.debug(
                "response_json requested but prompt lacks 'json'; skipping "
                "json_object mode for %s to avoid a 400.", self.config.provider,
            )
            return
        payload["response_format"] = {"type": "json_object"}

    async def complete(
        self, system: str, user: str, on_thinking=None, response_json: bool = False,
        cache_system_prefix: bool = False,
    ) -> str:
        # R1 (F1) — OpenAI-compatible endpoints apply implicit prefix caching
        # (or none) with no caller-placed breakpoint. Explicit no-op: accept the
        # flag for interface uniformity without altering the request. Byte-stable
        # section ordering (Lane B) is what feeds any implicit cache here.
        _ = cache_system_prefix
        payload = {
            "model": self.config.model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        self._maybe_json_mode(payload, system, user, response_json)
        url = f"{self._base_url()}/chat/completions"
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            resp = await client.post(url, headers=self._headers(), json=payload)
            if resp.status_code == 400 and "response_format" in payload:
                # R5 fail-open: some models on supported providers reject
                # json_object mode (e.g. deepseek-reasoner). A hard 400 here
                # would fail the whole turn — strictly worse than the parse-
                # failure class json mode prevents. Retry once WITHOUT the
                # field and fall back to the documented JSON-in-text parser.
                logger.warning(
                    "%s/%s returned 400 with response_format json_object set; "
                    "retrying once without it to rule out a json-mode "
                    "rejection — falling back to JSON-in-text parsing for "
                    "this call. If the retry also fails, the 400 was "
                    "unrelated to json mode and its detail is raised.",
                    self.config.provider, self.config.model_name,
                )
                payload.pop("response_format", None)
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

    async def stream_complete(
        self, system: str, user: str, on_thinking=None, response_json: bool = False,
        cache_system_prefix: bool = False,
    ) -> AsyncGenerator[str, None]:
        # R1 (F1) — documented no-op (implicit prefix caching); see ``complete``.
        _ = cache_system_prefix
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
        self._maybe_json_mode(payload, system, user, response_json)
        url = f"{self._base_url()}/chat/completions"
        input_tokens = 0
        output_tokens = 0
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            # R5 fail-open: at most two attempts — the second only when the
            # first is a 400 with json_object mode set (some models, e.g.
            # deepseek-reasoner, reject response_format). No answer token has
            # been emitted at that point, so the retry cannot duplicate output.
            for attempt in (1, 2):
                async with client.stream(
                    "POST", url, headers=self._headers(), json=payload
                ) as resp:
                    if (
                        resp.status_code == 400
                        and "response_format" in payload
                        and attempt == 1
                    ):
                        await resp.aread()
                        logger.warning(
                            "%s/%s returned 400 with response_format "
                            "json_object set on stream; retrying once without "
                            "it to rule out a json-mode rejection — falling "
                            "back to JSON-in-text parsing for this call. If "
                            "the retry also fails, the 400 was unrelated to "
                            "json mode and is raised.",
                            self.config.provider, self.config.model_name,
                        )
                        payload.pop("response_format", None)
                        continue
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
                break
        self.last_usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
