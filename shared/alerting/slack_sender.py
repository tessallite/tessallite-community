"""Slack incoming webhook sender for alert notifications.

Posts Block Kit messages to a configured webhook URL.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

from shared.webhooks.ssrf import ssrf_safe_transport
from shared.webhooks.ssrf import validate_webhook_url as ssrf_validate_webhook_url

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

_ALLOWED_WEBHOOK_HOSTS = frozenset({
    "hooks.slack.com",
    "hooks.slack-gov.com",
})


def validate_webhook_url(url: str) -> None:
    """Reject URLs that are not HTTPS Slack webhook endpoints."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"Webhook URL must use HTTPS, got {parsed.scheme!r}")
    if parsed.hostname not in _ALLOWED_WEBHOOK_HOSTS:
        raise ValueError(
            f"Webhook host {parsed.hostname!r} is not an allowed Slack domain"
        )


async def send_slack(
    *,
    webhook_url: str,
    text: str,
    blocks: list[dict] | None = None,
) -> None:
    if not webhook_url:
        logger.warning("Slack webhook URL is empty; skipping notification")
        return

    validate_webhook_url(webhook_url)
    safe_url = ssrf_validate_webhook_url(webhook_url)

    payload: dict = {"text": text}
    if blocks:
        payload["blocks"] = blocks

    async with httpx.AsyncClient(
        transport=ssrf_safe_transport(),
        timeout=_TIMEOUT,
        follow_redirects=False,
    ) as client:
        resp = await client.post(safe_url, json=payload)
        if resp.status_code != 200:
            logger.error(
                "Slack webhook returned %d: %s", resp.status_code, resp.text[:200]
            )
            raise RuntimeError(f"Slack webhook failed: {resp.status_code}")
        logger.info("Slack alert sent: %s", text[:100])
