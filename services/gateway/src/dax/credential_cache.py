"""Short-TTL credential -> JWT cache for the XMLA Basic-auth path (F-002-06).

Excel/MSOLAP issues dozens of XMLA requests per pivot interaction, each one
carrying the same HTTP Basic credentials. Without a cache the middleware
performs a full model-service login (including password-hash verification) on
every request — measurable latency on every click and needless load on
model-service.

This module caches the issued JWT keyed by a salted hash of the credential
tuple ``(catalog, username, password)`` for a short TTL. The plaintext
password is never stored — only an HMAC of it under a per-process random salt,
so the cache map cannot be used to recover credentials. The cached JWT is
re-validated by the caller (the handler always calls ``verify_jwt_token``), so
an expired JWT is rejected even if its TTL bucket has not yet swept.

Process-local (a plain dict + lock), adequate for the single-instance gateway.
A shared store for multi-replica is flagged as infra-needs (see docs/questions).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from typing import Optional

# Per-process random salt so the stored key reveals nothing across processes
# and cannot be precomputed.
_SALT = os.urandom(32)

# TTL is intentionally short: the cache is a request-burst de-duplicator, not a
# session store. The authoritative session lifetime is the JWT's own ``exp``,
# which the handler always re-validates. 30 s comfortably covers one pivot
# interaction's request burst without holding stale auth state.
_DEFAULT_TTL_SECONDS = 30

_lock = threading.Lock()
_cache: dict[str, tuple[str, float]] = {}


def _key(catalog: str, username: str, password: str) -> str:
    msg = f"{catalog}\x00{username}\x00{password}".encode("utf-8")
    return hmac.new(_SALT, msg, hashlib.sha256).hexdigest()


def get(catalog: str, username: str, password: str, *, ttl: int = _DEFAULT_TTL_SECONDS) -> Optional[str]:
    """Return a cached, non-expired JWT for these credentials, or None."""
    if not username or not password:
        return None
    key = _key(catalog or "", username, password)
    now = time.monotonic()
    with _lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        token, stored_at = entry
        if (now - stored_at) >= ttl:
            _cache.pop(key, None)
            return None
        return token


def put(catalog: str, username: str, password: str, token: str) -> None:
    """Cache *token* for these credentials."""
    if not username or not password or not token:
        return
    key = _key(catalog or "", username, password)
    with _lock:
        _cache[key] = (token, time.monotonic())


def _reset_for_tests() -> None:
    with _lock:
        _cache.clear()
