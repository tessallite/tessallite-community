"""SMTP email sender for alert notifications.

Uses stdlib smtplib with asyncio.to_thread() for non-blocking sends.
Configured via environment variables or shared settings.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from shared.config.settings import get_settings

logger = logging.getLogger(__name__)


def _build_message(
    *,
    to: list[str],
    subject: str,
    body_html: str,
    body_text: str | None = None,
    from_addr: str | None = None,
) -> MIMEMultipart:
    settings = get_settings()
    sender = from_addr or getattr(settings, "SMTP_FROM", "noreply@tessallite.local")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to)

    if body_text:
        msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))
    return msg


def _send_sync(
    *,
    to: list[str],
    subject: str,
    body_html: str,
    body_text: str | None = None,
    from_addr: str | None = None,
) -> None:
    settings = get_settings()
    host = getattr(settings, "SMTP_HOST", "")
    if not host:
        logger.warning("SMTP_HOST not configured; skipping email send")
        return

    port = int(getattr(settings, "SMTP_PORT", 587))
    use_tls = str(getattr(settings, "SMTP_TLS", "true")).lower() in ("true", "1", "yes")
    user = getattr(settings, "SMTP_USER", "") or ""
    password = getattr(settings, "SMTP_PASSWORD", "") or ""
    sender = from_addr or getattr(settings, "SMTP_FROM", "noreply@tessallite.local")

    msg = _build_message(
        to=to, subject=subject, body_html=body_html,
        body_text=body_text, from_addr=sender,
    )

    server = smtplib.SMTP(host, port, timeout=30)
    try:
        if use_tls:
            server.starttls()
        if user:
            server.login(user, password)
        server.sendmail(sender, to, msg.as_string())
    finally:
        try:
            server.quit()
        except smtplib.SMTPServerDisconnected:
            pass


async def send_email(
    *,
    to: list[str],
    subject: str,
    body_html: str,
    body_text: str | None = None,
    from_addr: str | None = None,
) -> None:
    try:
        await asyncio.to_thread(
            _send_sync,
            to=to, subject=subject, body_html=body_html,
            body_text=body_text, from_addr=from_addr,
        )
        logger.info("Alert email sent to %s: %s", to, subject)
    except Exception as exc:
        logger.error("Failed to send alert email to %s: %s", to, exc)
        raise
