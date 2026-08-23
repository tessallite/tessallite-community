"""Bug-7108 / Bug-5634: shared SSRF allowlist for LLM base_url values.

Every consumer that builds an LLM adapter from a persisted or ad-hoc
``base_url`` must validate it through ``validate_llm_base_url`` before
the request is sent.  The canonical enforcement point is
``build_adapter`` in ``shared.llm.adapter``; API-layer validators
(optimizer ad-hoc route, model-service config CRUD) add a second
layer that rejects early.

Raises ``ValueError`` for framework-independent callers.  HTTP-layer
wrappers should catch ``ValueError`` and translate to a 400 response.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---- Known LLM provider domains (remote) --------------------------------

_ALLOWED_REMOTE_DOMAINS: frozenset[str] = frozenset({
    "api.openai.com",
    "api.anthropic.com",
    "api.deepseek.com",
    "api.z.ai",
    "open.bigmodel.cn",
    "generativelanguage.googleapis.com",
    "aiplatform.googleapis.com",
})

# ---- Loopback ----

_LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})
_ALLOWED_LOOPBACK_PORTS: frozenset[int] = frozenset({
    11434,  # Ollama default
})


def validate_llm_base_url(base_url: str | None) -> str | None:
    """Return *base_url* unchanged when it passes the SSRF allowlist, else raise.

    ``None`` passes through (adapter will use the system default).

    Allowed origins:
    1. Known LLM provider domains (hardcoded allowlist).
    2. Loopback addresses on known LLM-server ports (Ollama 11434).
    3. Any origin whose scheme+host+port matches a server-configured
       provider endpoint (``llm.provider_endpoints`` system setting).

    Raises:
        ValueError: if the URL is not allowed.
    """
    if base_url is None:
        return None

    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    scheme = (parsed.scheme or "").lower()

    if scheme not in ("https", "http"):
        raise ValueError(
            f"Invalid base_url scheme: {scheme!r}. Only https and http are allowed."
        )

    # 1. Known remote providers -- require HTTPS to prevent API keys
    # and tenant telemetry from travelling over plaintext.
    if host in _ALLOWED_REMOTE_DOMAINS:
        if scheme != "https":
            raise ValueError(
                f"Remote LLM provider {host!r} requires HTTPS. "
                f"HTTP is not allowed for remote provider endpoints."
            )
        return base_url

    # 2. Loopback: only allow known LLM-server ports.
    if host in _LOOPBACK_HOSTS:
        port = parsed.port or (443 if scheme == "https" else 80)
        if port in _ALLOWED_LOOPBACK_PORTS:
            return base_url
        raise ValueError(
            f"Loopback base_url port {port} is not allowed. "
            f"Permitted loopback ports: {sorted(_ALLOWED_LOOPBACK_PORTS)}. "
            f"Register other loopback LLM endpoints in System Settings "
            f"'llm.provider_endpoints' to use them."
        )

    # 3. Server-configured provider endpoints (scheme+host+port match).
    try:
        from shared.config.bootstrap import system_snapshot_get
        configured_endpoints = system_snapshot_get("llm.provider_endpoints") or {}
    except Exception:
        configured_endpoints = {}

    for endpoint_url in configured_endpoints.values():
        ep_parsed = urlparse(str(endpoint_url))
        ep_host = (ep_parsed.hostname or "").lower()
        ep_scheme = (ep_parsed.scheme or "").lower()
        ep_port = ep_parsed.port or (443 if ep_scheme == "https" else 80)
        req_port = parsed.port or (443 if scheme == "https" else 80)
        if ep_host and host == ep_host and scheme == ep_scheme and req_port == ep_port:
            return base_url

    raise ValueError(
        f"base_url host {host!r} is not in the allowed LLM provider "
        f"domain list. Use a known provider endpoint or omit base_url "
        f"to use the server-configured default."
    )
