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

Security bound (Bug-6309). The model-service login is the authority on
account state — it is what rejects a wrong/old password or a disabled account.
Serving a cached JWT deliberately SKIPS that login, so the cache must never
honour auth material longer than a tight, non-extendable window and must drop
stale entries the moment newer auth material appears:

* **Bounded, non-extendable lifetime.** ``get`` never refreshes ``stored_at``;
  an entry is served for at most ``_DEFAULT_TTL_SECONDS`` after the login that
  created it, then forces a fresh login (which re-checks account state). This
  caps how long a disabled account or a just-changed password can keep working
  through the cache.
* **Invalidate on auth-material change.** A successful login with a NEW
  password for the same ``(catalog, username)`` evicts the prior
  (old-password) entry immediately (``put`` consults a per-user index), instead
  of letting the old credential linger for the rest of its TTL. ``invalidate``
  purges a user's entry on demand — the auth middleware calls it when a login
  is rejected, so a now-invalid credential cannot keep being served from an
  earlier cache hit.

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
# interaction's request burst without holding stale auth state. It is ALSO the
# hard upper bound on how long a disabled account or a just-changed password can
# keep succeeding through a cache hit (Bug-6309), so it must stay short and is
# never extended by reads.
_DEFAULT_TTL_SECONDS = 30

_lock = threading.Lock()
_cache: dict[str, tuple[str, float]] = {}
# Per-user index: HMAC(catalog, username) -> current credential key. Lets ``put``
# and ``invalidate`` target a user's entry WITHOUT the password, so a new login
# (changed password) or an upstream rejection (disabled/rotated account) can
# evict the stale entry rather than waiting out its TTL (Bug-6309).
_user_index: dict[str, str] = {}


def _key(catalog: str, username: str, password: str) -> str:
    msg = f"{catalog}\x00{username}\x00{password}".encode("utf-8")
    return hmac.new(_SALT, msg, hashlib.sha256).hexdigest()


def _user_key(catalog: str, username: str) -> str:
    """Password-independent index key for a (catalog, username) pair."""
    msg = f"{catalog}\x00{username}".encode("utf-8")
    return hmac.new(_SALT, msg, hashlib.sha256).hexdigest()


def get(catalog: str, username: str, password: str, *, ttl: int = _DEFAULT_TTL_SECONDS) -> Optional[str]:
    """Return a cached, non-expired JWT for these credentials, or None."""
    if not username or not password:
        return None
    key = _key(catalog or "", username, password)
    ukey = _user_key(catalog or "", username)
    now = time.monotonic()
    with _lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        token, stored_at = entry
        if (now - stored_at) >= ttl:
            _cache.pop(key, None)
            if _user_index.get(ukey) == key:
                _user_index.pop(ukey, None)
            return None
        return token


def put(catalog: str, username: str, password: str, token: str) -> None:
    """Cache *token* for these credentials.

    Bug-6309: evict any prior entry for the same ``(catalog, username)`` whose
    credential key differs — i.e. the password changed — so a superseded
    old-password entry cannot keep being served for the rest of its TTL.
    """
    if not username or not password or not token:
        return
    key = _key(catalog or "", username, password)
    ukey = _user_key(catalog or "", username)
    with _lock:
        prior = _user_index.get(ukey)
        if prior is not None and prior != key:
            _cache.pop(prior, None)
        _cache[key] = (token, time.monotonic())
        _user_index[ukey] = key


def invalidate(catalog: str, username: str) -> None:
    """Drop any cached JWT for a ``(catalog, username)`` pair.

    Bug-6309: called when the upstream login rejects the credentials (wrong /
    changed password, or a disabled account), so a credential the authority no
    longer accepts is not still honoured from an earlier cache hit.
    """
    if not username:
        return
    ukey = _user_key(catalog or "", username)
    with _lock:
        prior = _user_index.pop(ukey, None)
        if prior is not None:
            _cache.pop(prior, None)


def _reset_for_tests() -> None:
    with _lock:
        _cache.clear()
        _user_index.clear()
