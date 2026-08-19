"""Explicit live-integration profile and readiness contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import NoReturn
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest


LIVE_FLAG = "TESSALLITE_RUN_LIVE_INTEGRATION"
REQUIRED_LIVE_ENV = (
    "INTEGRATION_TEST_API_BASE",
    "INTEGRATION_TEST_TENANT",
    "INTEGRATION_TEST_EMAIL",
    "INTEGRATION_TEST_PASSWORD",
    "INTEGRATION_TEST_PROJECT",
    "INTEGRATION_TEST_MODEL",
)
EXCLUSIVE_FLAG = "INTEGRATION_TEST_EXCLUSIVE"


def live_integration_enabled(environ: Mapping[str, str]) -> bool:
    return environ.get(LIVE_FLAG) == "1"


def validate_live_profile(environ: Mapping[str, str]) -> None:
    """Require an explicit, isolated target before a live run is collected."""
    if not live_integration_enabled(environ):
        return
    missing = [name for name in REQUIRED_LIVE_ENV if not environ.get(name)]
    if environ.get(EXCLUSIVE_FLAG) != "1":
        missing.append(f"{EXCLUSIVE_FLAG}=1")
    if missing:
        raise RuntimeError(
            "Live model-service integration requires an explicit isolated "
            f"profile; missing or invalid {', '.join(missing)}. "
            "INTEGRATION_TEST_EXCLUSIVE must equal 1."
        )


def health_probe_urls(api_base: str) -> list[str]:
    """Return the health URLs for an explicit integration API base.

    ``localhost`` is allowed in a developer profile, but it can resolve to
    IPv6-only ``::1`` on hosts where the stack listens on IPv4.  Keep the
    configured address first and add the same port on the IPv4 loopback as a
    deterministic fallback.  Other hostnames remain single-target probes so
    a typo or unavailable remote service is not silently redirected.
    """
    parsed = urlsplit(api_base)
    if not parsed.scheme or not parsed.netloc:
        return []

    # Preserve a deployment prefix while removing the API suffix.  The
    # previous probe derived the health root from ``.../api/v1``; the IPv4
    # fallback must not change that behavior for a proxied profile such as
    # ``https://localhost/model-service/api/v1``.
    path = parsed.path.rstrip("/")
    api_suffix = "/api/v1"
    root_path = path[:-len(api_suffix)] if path.endswith(api_suffix) else path
    root = urlunsplit((parsed.scheme, parsed.netloc, root_path, "", ""))
    urls = [root]
    if parsed.hostname == "localhost":
        ipv4_netloc = "127.0.0.1"
        if parsed.port is not None:
            ipv4_netloc = f"{ipv4_netloc}:{parsed.port}"
        urls.append(urlunsplit((parsed.scheme, ipv4_netloc, root_path, "", "")))
    return urls


def api_up(api_base: str) -> bool:
    """Check whether the configured model-service health endpoint is ready."""
    try:
        roots = health_probe_urls(api_base)
    except ValueError:
        return False
    for root in roots:
        try:
            if httpx.get(f"{root}/health", timeout=3.0).status_code == 200:
                return True
        except Exception:  # noqa: BLE001 - readiness must handle DNS/connect errors
            continue
    return False


def environment_not_ready(reason: str) -> NoReturn:
    """Stop an explicitly requested live run with the repository taxonomy."""
    pytest.exit(f"ENVIRONMENT_NOT_READY: {reason}", returncode=6)


def authentication_not_ready(status_code: int, detail: str) -> None:
    """Classify rejected live-profile credentials as environment readiness."""
    if status_code in (401, 403):
        environment_not_ready(
            f"live profile authentication rejected ({status_code}): {detail}"
        )


def profile_item_or_not_ready(
    items: list[dict], slug: str, resource: str,
) -> dict:
    """Resolve a configured project/model or fail as an unavailable profile."""
    for item in items:
        if item.get("slug") == slug:
            return item
    environment_not_ready(
        f"configured live-profile {resource} {slug!r} was not found"
    )
