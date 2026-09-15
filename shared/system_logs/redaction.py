"""Remove credentials before raw messages cross the log storage boundary."""

from __future__ import annotations

import os
import re

_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s\"'<>]+", re.IGNORECASE)
_BEARER = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9+/_.=-]+", re.IGNORECASE)
_PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----.*?"
    r"-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_SECRET_KEY = (
    r"(?:(?:[A-Za-z][A-Za-z0-9]*[_./-])*"
    r"(?:password|passwd|passphrase|secret|token|api[_-]?key|"
    r"authorization|cookie|credential[_-]?encryption[_-]?key|"
    r"credentials?|private[_-]?key|private[_-]?token|"
    r"session[_-]?token|client[_-]?secret|access[_-]?key|"
    r"cloud[_-]?(?:credential|secret|token|key)|"
    r"service[_-]?account[_-]?key)"
    r"(?:[_./-][A-Za-z0-9]+)*)"
)
_SECRET = re.compile(
    r"([\"']?" + _SECRET_KEY + r"[\"']?\s*[:=]\s*)"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;}&]+)",
    re.IGNORECASE,
)


def redact(message: str) -> str:
    """Redact URLs, auth fields, and configured secret values, including traces."""
    for key, value in os.environ.items():
        if len(value) >= 6 and re.search(
            r"PASSWORD|SECRET|TOKEN|KEY|DATABASE_URL", key
        ):
            message = message.replace(value, "[REDACTED]")
    message = _PEM_PRIVATE_KEY.sub("[REDACTED_PRIVATE_KEY]", message)
    message = _URL.sub("[REDACTED_URL]", message)
    message = _BEARER.sub("[REDACTED_AUTH]", message)
    return _SECRET.sub(r"\1[REDACTED]", message)
