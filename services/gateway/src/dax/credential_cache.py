"""Short-TTL credential -> JWT cache for gateway password-auth paths (F-002-06).

Excel/MSOLAP issues dozens of XMLA requests per pivot interaction, each one
carrying the same HTTP Basic credentials. Without a cache the middleware
performs a full model-service login (including password-hash verification) on
every request — measurable latency on every click and needless load on
model-service.

This module caches the issued JWT keyed by a salted hash of the IDENTITY
``(username, password, optional scope)``. The plaintext password is never stored — only an
HMAC of it under a per-process random salt, so the cache map cannot be used to
recover credentials. The cached JWT is re-validated by the caller (the handler
always calls ``verify_jwt_token``), so an expired JWT is rejected even if its
TTL bucket has not yet swept. Callers may add a scope to keep the same
credentials separate for different tenants or protocols.

Bug-9887: the key used to carry the XMLA ``Catalog`` as well. A login is an
IDENTITY exchange — model-service authenticates the user, not the catalogue —
so including the catalogue only fragmented the cache: Excel switching between a
business catalogue and its technical sibling paid a full login for the second
one with the same credentials, and the 30 s TTL expired inside a normal human
pause between opening a workbook and expanding the first field. Measured on the
local stack, that cache miss cost 2.5–4.2 s per catalogue switch because it ran
a doomed tenant-scoped login followed by the O(active tenants) cross-tenant
discovery. Keying on identity and lengthening the TTL removes both repeats. The
issued JWT is already identity-scoped (it carries the tenant resolved by the
login), so nothing catalogue-specific was ever stored in the value.

Security bound (Bug-6309, re-stated for Bug-9887). The model-service login is
the authority on account state — it is what rejects a wrong/old password or a
disabled account. Serving a cached JWT deliberately SKIPS that login, so the
cache must never honour auth material longer than a bounded, non-extendable
window and must drop stale entries the moment newer auth material appears:

* **Bounded, non-extendable lifetime.** ``get`` never refreshes ``stored_at``;
  an entry is served for at most the configured TTL after the login that
  created it, then forces a fresh login (which re-checks account state). That
  TTL is therefore the RESIDUAL WINDOW required by the policy-propagation
  contract (``architecture_cross-cutting-contracts.md`` §4, "User
  deactivate/delete/role change ... define maximum existing-token lifetime"):
  a deactivated, deleted, role-changed or password-changed account can keep
  authenticating through a password-auth gateway path for at most this long. It is configurable via
  ``GATEWAY_XMLA_CREDENTIAL_CACHE_TTL_SECONDS`` (default 600 s, hard ceiling
  3600 s) so a deployment needing a tighter revocation window can shorten it;
  ``0`` disables the cache entirely and restores a login per request.
* **Invalidate on auth-material change.** A successful login with a NEW
  password for the same ``username`` evicts the prior (old-password) entry
  immediately (``put`` consults a per-user index), instead of letting the old
  credential linger for the rest of its TTL. ``invalidate`` purges a user's
  entry on demand — the auth middleware calls it when a login is rejected, so a
  now-invalid credential cannot keep being served from an earlier cache hit.

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

from shared.config.settings import get_settings

# Per-process random salt so the stored key reveals nothing across processes
# and cannot be precomputed.
_SALT = os.urandom(32)

# Fallback only — the live value comes from
# ``GATEWAY_XMLA_CREDENTIAL_CACHE_TTL_SECONDS`` (see ``default_ttl``). Kept as a
# module constant so a settings read that fails cannot leave the cache
# unbounded.
_DEFAULT_TTL_SECONDS = 600

# Hard ceiling on the configured TTL. The setting bounds a revocation window, so
# a mistyped value must not be able to honour a disabled account's credentials
# for hours. One hour is already generous for a burst de-duplicator.
_MAX_TTL_SECONDS = 3600

_lock = threading.Lock()
_cache: dict[str, tuple[str, float]] = {}
# Per-user index: HMAC(username, scope) -> current credential key. Lets ``put`` and
# ``invalidate`` target a user's entry WITHOUT the password, so a new login
# (changed password) or an upstream rejection (disabled/rotated account) can
# evict the stale entry rather than waiting out its TTL (Bug-6309).
_user_index: dict[str, str] = {}


def default_ttl() -> int:
    """Configured cache lifetime in seconds, clamped to a sane range.

    Read per call rather than captured at import, so a settings reload (and the
    test suite's monkeypatching) takes effect without a process restart. A
    non-numeric value falls back to the module default rather than removing the
    bound; ``0`` is honoured as an explicit disable.
    """
    try:
        raw = int(getattr(
            get_settings(),
            "GATEWAY_XMLA_CREDENTIAL_CACHE_TTL_SECONDS",
            _DEFAULT_TTL_SECONDS,
        ))
    except (TypeError, ValueError):
        return _DEFAULT_TTL_SECONDS
    if raw <= 0:
        return 0
    return min(raw, _MAX_TTL_SECONDS)


def _hmac_key(username: str, password: str, scope: str = "") -> str:
    msg = f"{username}\x00{password}"
    if scope:
        msg += f"\x00{scope}"
    return hmac.new(_SALT, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def _key(username: str, password: str, scope: str = "") -> str:
    """Return the opaque cache key for credentials and an optional scope."""
    return _hmac_key(username, password, scope)


def login_key(username: str, password: str, url: str, scope: str) -> str:
    """Return an opaque key for one login exchange.

    The URL and tenant/discovery scope are included so a credential cannot
    coalesce across distinct authority endpoints or login scopes.
    """
    return _hmac_key(username, password, f"{url}\x00{scope}")


def _user_key(username: str, scope: str = "") -> str:
    """Password-independent index key for a username and cache scope."""
    msg = username if not scope else f"{username}\x00{scope}"
    return hmac.new(_SALT, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def get(
    username: str,
    password: str,
    *,
    ttl: Optional[int] = None,
    scope: str = "",
) -> Optional[str]:
    """Return a cached, non-expired JWT for these credentials, or None."""
    if not username or not password:
        return None
    # A non-positive TTL is not short-circuited: it must still run the age gate
    # below so a disabled cache also SWEEPS whatever it is holding, rather than
    # leaving entries parked in the map until something re-reads them under a
    # positive TTL.
    effective_ttl = default_ttl() if ttl is None else ttl
    key = _key(username, password, scope)
    ukey = _user_key(username, scope)
    now = time.monotonic()
    with _lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        token, stored_at = entry
        if (now - stored_at) >= effective_ttl:
            _cache.pop(key, None)
            if _user_index.get(ukey) == key:
                _user_index.pop(ukey, None)
            return None
        return token


def put(
    username: str,
    password: str,
    token: str,
    *,
    scope: str = "",
) -> None:
    """Cache *token* for these credentials and an optional scope.

    Bug-6309: evict any prior entry for the same username and scope whose
    credential key differs — i.e. the password changed — so a superseded
    old-password entry cannot keep being served for the rest of its TTL.
    """
    if not username or not password or not token:
        return
    if default_ttl() <= 0:
        return
    key = _key(username, password, scope)
    ukey = _user_key(username, scope)
    with _lock:
        prior = _user_index.get(ukey)
        if prior is not None and prior != key:
            _cache.pop(prior, None)
        _cache[key] = (token, time.monotonic())
        _user_index[ukey] = key


def invalidate(username: str, *, scope: str = "") -> None:
    """Drop any cached JWT for *username* in the optional cache scope."""
    if not username:
        return
    ukey = _user_key(username, scope)
    with _lock:
        prior = _user_index.pop(ukey, None)
        if prior is not None:
            _cache.pop(prior, None)


def _reset_for_tests() -> None:
    with _lock:
        _cache.clear()
        _user_index.clear()
