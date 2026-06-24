"""Policy for non-API-key (cloud service-account / OAuth) LLM auth.

Some LLM provider configs authenticate via cloud Application Default
Credentials (ADC) instead of a bring-your-own API key — currently only Google
Vertex AI (`config.google_mode == "vertex_ai"`, resolved through ADC in
`shared/llm/providers/google.py`). That path bills the *deployment's* cloud
project, not the customer's key. It is gated behind
`LLM_ALLOW_SERVICE_ACCOUNT_AUTH` (default off) so the platform cannot be billed
without an explicit operator opt-in.

Single source of truth for the detection + the allow check, shared by the
LLM-config CRUD guard (model-service), the import/rehydrate neutraliser, and the
Google adapter's use-time refusal — so every write/use path agrees.
"""
from __future__ import annotations

# Config keys that only make sense under Vertex AI (service-account) mode.
SERVICE_ACCOUNT_CONFIG_KEYS = ("google_mode", "google_project", "google_location")


def uses_service_account_auth(provider: str | None, config: dict | None) -> bool:
    """True when the config authenticates via cloud service-account / OAuth
    (ADC) rather than a bring-your-own API key. Extend when other providers
    add ADC/OAuth modes."""
    cfg = config or {}
    if (provider or "").lower() in ("google", "gemini"):
        return cfg.get("google_mode") == "vertex_ai"
    return False


def service_account_auth_allowed() -> bool:
    """Whether the deployment permits service-account / OAuth LLM auth."""
    from shared.config.settings import get_settings

    return bool(get_settings().LLM_ALLOW_SERVICE_ACCOUNT_AUTH)


def neutralise_service_account_config(config: dict | None) -> dict:
    """Return a copy of ``config`` with the service-account-only keys removed, so
    an imported config can't activate Vertex/ADC billing. The result falls back
    to plain API-key mode (which, lacking a key, is inert until an admin sets
    one)."""
    cfg = dict(config or {})
    for k in SERVICE_ACCOUNT_CONFIG_KEYS:
        cfg.pop(k, None)
    return cfg
