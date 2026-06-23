"""Sanitize exception messages before returning them to API clients.

Strips connection strings, file paths, and SQLAlchemy internals so that
error responses don't leak infrastructure details to callers.
"""
from __future__ import annotations

import re

_CONNECTION_STRING_RE = re.compile(
    r"(?:postgresql|mysql|sqlite|snowflake|bigquery)"
    r"(?:\+\w+)?://[^\s,)\"']+"
)

_FILE_PATH_RE = re.compile(r"(?:/[\w./-]{3,})+\.py(?::\d+)?")

_SQLALCHEMY_CLASS_RE = re.compile(r"sqlalchemy\.exc\.\w+:\s*")

_ASYNCPG_DETAIL_RE = re.compile(
    r"\bconnection to server at .+?, port \d+ failed[^\n]*"
)


def sanitize_error_for_client(exc: Exception) -> str:
    msg = str(exc)
    msg = _CONNECTION_STRING_RE.sub("[connection hidden]", msg)
    msg = _FILE_PATH_RE.sub("[path hidden]", msg)
    msg = _SQLALCHEMY_CLASS_RE.sub("", msg)
    msg = _ASYNCPG_DETAIL_RE.sub("Source database connection failed.", msg)
    return msg.strip()
