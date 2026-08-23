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


class SmtpNotConfiguredError(Exception):
    """Raised when SMTP_HOST is empty/unset and email delivery is attempted.

    Bug-7340: callers must distinguish "SMTP is not configured" from a
    transient delivery failure. When SMTP is unconfigured the send must
    surface a clear failure so that (a) test-send reports the truth,
    (b) runtime dispatch does not count the send as successful, and
    (c) the dedup window is not consumed by a no-op.
    """


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
        raise SmtpNotConfiguredError(
            "SMTP_HOST is not configured. Email delivery is unavailable. "
            "Set SMTP_HOST in .env to enable email alerts."
        )

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
    """Send an alert email via SMTP.

    Raises ``SmtpNotConfiguredError`` when SMTP_HOST is unset so callers
    can distinguish a configuration gap from a transient delivery failure.
    Bug-7340: the "sent" log line and success path are only reached when
    the SMTP server accepted the message.
    """
    try:
        await asyncio.to_thread(
            _send_sync,
            to=to, subject=subject, body_html=body_html,
            body_text=body_text, from_addr=from_addr,
        )
        logger.info("Alert email sent to %s: %s", to, subject)
    except SmtpNotConfiguredError:
        # Bug-7340: propagate without the generic "Failed to send" log line
        # so callers (dispatcher, test-send) can handle it distinctly.
        logger.warning(
            "SMTP not configured; email to %s not sent: %s", to, subject,
        )
        raise
    except Exception as exc:
        logger.error("Failed to send alert email to %s: %s", to, exc)
        raise
