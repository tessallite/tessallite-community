"""Google Gemini adapter (google-genai SDK).

Supports two modes selected via ``config.google_mode`` on the
``LLMProviderConfig`` / ``LLMConfig`` record:

* ``"api_key"`` (default, also when absent) — uses the plain API key to
  reach ``generativelanguage.googleapis.com`` (AI Studio / prepaid
  billing).
* ``"vertex_ai"`` — uses Google Cloud Application Default Credentials
  (ADC) via the Vertex AI endpoint.  ``project`` and ``location``
  are read from ``config.google_project`` and ``config.google_location``
  (both required — Bug-5282: missing config raises ``ValueError``).
"""
from __future__ import annotations

import asyncio
from typing import AsyncGenerator

from google import genai
from google.genai import types

from shared.llm.adapter import LLMAdapter, ThinkingCallback


def _client(config) -> genai.Client:
    mode = (config.config or {}).get("google_mode")
    if mode == "vertex_ai":
        # Bug-5282 — fail clearly when Vertex AI config is incomplete.
        # The previous hard-coded defaults ("tessallite-io" / "global") were
        # internal dev values that would silently fail on any other deployment.
        project = (config.config or {}).get("google_project")
        location = (config.config or {}).get("google_location")
        if not project:
            raise ValueError(
                "Vertex AI mode requires 'google_project' in the LLM config. "
                "Set it to your GCP project ID in Project Settings > LLM "
                "Configurations > config JSON (e.g. "
                '"google_project": "my-gcp-project").'
            )
        if not location:
            raise ValueError(
                "Vertex AI mode requires 'google_location' in the LLM config. "
                "Set it to your GCP region in Project Settings > LLM "
                "Configurations > config JSON (e.g. "
                '"google_location": "us-central1").'
            )
        return genai.Client(
            vertexai=True,
            project=project,
            location=location,
        )
    # default / api_key / missing
    return genai.Client(api_key=config.api_key)


class GoogleAdapter(LLMAdapter):
    supports_thinking: bool = True

    async def complete(self, system: str, user: str, on_thinking: ThinkingCallback = None) -> str:
        thinking, text = await asyncio.to_thread(
            self._complete_sync, system, user, on_thinking is not None
        )
        if thinking and on_thinking:
            await on_thinking(thinking)
        return text

    def _complete_sync(self, system: str, user: str, include_thoughts: bool = False) -> tuple[str, str]:
        client = _client(self.config)
        config_kwargs: dict = {
            "system_instruction": system,
            "max_output_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        if include_thoughts:
            config_kwargs["thinking_config"] = types.ThinkingConfig(include_thoughts=True)
        config = types.GenerateContentConfig(**config_kwargs)
        response = client.models.generate_content(
            model=self.config.model_name,
            contents=user,
            config=config,
        )
        usage_meta = getattr(response, "usage_metadata", None)
        self.last_usage = {
            "input_tokens": int(getattr(usage_meta, "prompt_token_count", 0) or 0),
            "output_tokens": int(getattr(usage_meta, "candidates_token_count", 0) or 0),
        }

        thinking_parts: list[str] = []
        answer_parts: list[str] = []
        candidates = getattr(response, "candidates", None) or []
        if candidates and getattr(candidates[0], "content", None):
            for part in candidates[0].content.parts:
                t = getattr(part, "text", "") or ""
                if getattr(part, "thought", False):
                    thinking_parts.append(t)
                elif t:
                    answer_parts.append(t)
        else:
            # fallback: use convenience .text property (no thinking separation)
            answer_parts.append(response.text or "")

        text = "".join(answer_parts)
        if not text:
            raise ValueError(
                "Google returned empty response text. The model may have been "
                "blocked by a safety filter."
            )
        return "".join(thinking_parts), text

    async def stream_complete(self, system: str, user: str, on_thinking: ThinkingCallback = None) -> AsyncGenerator[str, None]:
        client = _client(self.config)
        config_kwargs: dict = {
            "system_instruction": system,
            "max_output_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        if on_thinking is not None:
            config_kwargs["thinking_config"] = types.ThinkingConfig(include_thoughts=True)
        config = types.GenerateContentConfig(**config_kwargs)
        stream = await asyncio.to_thread(
            lambda: client.models.generate_content_stream(
                model=self.config.model_name,
                contents=user,
                config=config,
            )
        )
        input_tokens = 0
        output_tokens = 0
        for chunk in stream:
            candidates = getattr(chunk, "candidates", None) or []
            if candidates and getattr(candidates[0], "content", None):
                for part in candidates[0].content.parts:
                    t = getattr(part, "text", "") or ""
                    if getattr(part, "thought", False):
                        if t and on_thinking:
                            await on_thinking(t)
                    elif t:
                        yield t
            else:
                text = getattr(chunk, "text", None) or ""
                if text:
                    yield text
            meta = getattr(chunk, "usage_metadata", None)
            if meta:
                input_tokens = int(getattr(meta, "prompt_token_count", 0) or 0)
                output_tokens = int(getattr(meta, "candidates_token_count", 0) or 0)
        self.last_usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
