"""Short-TTL caches for the XMLA MDSCHEMA_MEMBERS discover path (Bug-6602).

Excel/MSOLAP issues a burst of near-identical Discover requests during a single
pivot gesture (selecting a field, expanding a hierarchy). Before this cache each
one re-ran the full member-discovery pipeline -- an uncached
``SELECT DISTINCT ... ORDER BY ... LIMIT`` source scan per dimension -- plus a
per-request metadata N+1 (one HTTP call per hierarchy). One click therefore cost
N full source scans and a multi-MB SOAP payload, repeated on the next click,
which froze Excel's UI thread (Fable diagnostic
``fable-xmla-excel-cube-shape.md`` §3).

Two process-local, short-TTL caches de-duplicate that burst:

* **Member cache** -- keyed by ``(model_id, persona_id, principal fingerprint,
  dimension, restriction shape)``. Member scoping has TWO independent security
  inputs: the persona (allow-lists, resolved from the catalog) AND per-user
  ROW-LEVEL SECURITY (compiled from the caller's Principal by ``route_query``,
  NOT from the persona). Both are in the key -- ``persona_id`` and a per-caller
  fingerprint of the JWT (see :func:`principal_fingerprint`) -- so a member list
  filtered for one user/persona can never be served to another security context
  (the persona-only key would have leaked one user's row-filtered members to any
  other user sharing that persona, incl. the business base).
* **Metadata cache** -- keyed by ``(tenant_slug, model_id, principal
  fingerprint)``. Holds the measures / dimensions / hierarchy-definition lists
  as the model-service returned them FOR THIS CALLER. The list endpoints the
  gateway wraps AUTO-RESOLVE the caller's effective persona from the JWT
  (``resolve_effective_persona``: a regular user assigned to exactly one
  persona resolves to it) and apply BOTH the persona allow-list AND the CLS
  restricted-column exclusion (Bug-6141) before responding, so the SAME
  ``(tenant, model)`` endpoint returns DIFFERENT measure/dimension/hierarchy
  NAMES per caller. The value is therefore caller-DEPENDENT and must never be
  shared across principals — a ``(tenant, model)``-only key let an admin-primed
  (unfiltered) snapshot serve CLS-restricted names to a restricted viewer, or a
  viewer-primed (trimmed) snapshot truncate the admin's catalogue, for the TTL
  window (Bug-6602 / Fable F-1). The per-caller fingerprint (same HMAC used by
  the member cache) fully separates security contexts while still de-duplicating
  a single caller's Discover burst — which is what eliminates the per-Discover
  hierarchy-detail N+1. The gateway still applies its own catalog-persona
  trimming AFTER the read, so a single principal browsing several persona
  catalogs reuses one raw snapshot correctly.

TTL is intentionally short (a burst de-duplicator, not a session store). The
authoritative model shape is the deployed snapshot; a deploy is picked up within
one TTL window. Both caches are process-local (a dict + lock), adequate for the
single-instance gateway; a shared store for multi-replica is a perf-only
follow-up (the keys are security-scoped, so a per-replica cache is correct, just
less warm).

Bounded memory (Bug-6602 review; Bug-6650). Member values are deliberately the
FULL flat levels (up to ~100k members), and keys embed a per-caller token
fingerprint plus, for hierarchies, a per-drill restriction shape — so most keys
are one-shot and would never be re-read to trigger the read-time expiry. Each
``put`` therefore sweeps expired entries AND enforces BOTH a max-entry bound and
an approximate max-BYTES bound (Bug-6650: entry count alone did not bound memory
— one value can be a 100k-member flat level, so many principals x dimensions in
one TTL window transiently held GB-scale payloads). Eviction is oldest-by-write
until both bounds hold, so neither cache can grow without limit in count or in
bytes on a long-running gateway. Byte caps are env-tunable
(``XMLA_MEMBER_CACHE_MAX_BYTES`` / ``XMLA_METADATA_CACHE_MAX_BYTES``).

Status: active. Last meaningful update: 2026-07-14 (Bug-6650).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sys
import threading
import time
from typing import Any, Optional

# Per-process random salt so a stored principal fingerprint reveals nothing and
# cannot be correlated across processes (same rationale as credential_cache).
_SALT = os.urandom(32)


def _int_from_env(var: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(var, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


def _ttl_from_env(var: str, default: int) -> int:
    return _int_from_env(var, default, minimum=0)


# Short, non-extendable TTLs. Reads never refresh ``stored_at`` so an entry is
# served for at most TTL seconds after it was written, then a fresh fetch runs
# (picking up a redeploy or changed security shape).
_MEMBER_TTL_SECONDS = _ttl_from_env("XMLA_MEMBER_CACHE_TTL", 30)
_METADATA_TTL_SECONDS = _ttl_from_env("XMLA_METADATA_CACHE_TTL", 30)

# Hard upper bound on entry count per cache (memory guard). Members hold large
# values, so the member bound is the tighter one.
_MEMBER_MAX_ENTRIES = _int_from_env("XMLA_MEMBER_CACHE_MAX", 512, minimum=1)
_METADATA_MAX_ENTRIES = _int_from_env("XMLA_METADATA_CACHE_MAX", 256, minimum=1)

# Bug-6650: entry-count bounds alone do not bound memory — one member value can
# be a ~100k-member flat level, so 512 such entries (many principals x
# dimensions in a single 30s TTL window) transiently held GB-scale payloads. Cap
# the APPROXIMATE total bytes per cache as well; ``put`` evicts oldest-by-write
# until BOTH the entry bound and the byte bound hold. Defaults: 256 MiB for the
# member cache (large flat levels), 64 MiB for the metadata cache.
_MEMBER_MAX_BYTES = _int_from_env(
    "XMLA_MEMBER_CACHE_MAX_BYTES", 256 * 1024 * 1024, minimum=1,
)
_METADATA_MAX_BYTES = _int_from_env(
    "XMLA_METADATA_CACHE_MAX_BYTES", 64 * 1024 * 1024, minimum=1,
)

_lock = threading.Lock()
# Each entry is (value, stored_at, approx_bytes). ``approx_bytes`` is a cheap,
# shallow size estimate of the cached value computed once at ``put`` time.
_member_cache: dict[str, tuple[Any, float, int]] = {}
_metadata_cache: dict[str, tuple[Any, float, int]] = {}


def _approx_bytes(value: Any) -> int:
    """Cheap, shallow byte estimate of a cached value (Bug-6650).

    Member/metadata values are lists of dicts of short strings. A full deep
    ``sys.getsizeof`` walk of a 100k-member list would itself be costly, so this
    samples the container: it sums ``getsizeof`` of the top-level list plus, for
    list values, the first few elements scaled to the full length. The estimate
    only needs to be monotone in real size to drive proportional eviction, not
    exact. Always returns at least 1.
    """
    try:
        base = sys.getsizeof(value)
    except Exception:
        return 1
    if isinstance(value, (list, tuple)) and value:
        sample_n = min(len(value), 16)
        sample_total = 0
        for item in value[:sample_n]:
            try:
                item_size = sys.getsizeof(item)
                if isinstance(item, dict):
                    for k, v in item.items():
                        item_size += sys.getsizeof(k) + sys.getsizeof(v)
                sample_total += item_size
            except Exception:
                sample_total += 64
        per_item = sample_total / sample_n
        base += int(per_item * len(value))
    return max(1, base)


def _evict_locked(
    cache: dict[str, tuple[Any, float, int]],
    ttl: int,
    max_entries: int,
    max_bytes: int,
    now: float,
) -> None:
    """Drop expired entries, then the oldest-by-write-time until within BOTH the
    entry-count bound and the approximate-byte bound (Bug-6650).

    Caller must hold ``_lock``. Called from ``put`` so a cache dominated by
    one-shot keys (which are never re-read to trigger read-time expiry) still
    reclaims memory and stays bounded in count AND in bytes.
    """
    if ttl > 0:
        expired = [k for k, (_v, ts, _b) in cache.items() if (now - ts) >= ttl]
        for k in expired:
            cache.pop(k, None)
    total_bytes = sum(entry[2] for entry in cache.values())
    if len(cache) <= max_entries and total_bytes <= max_bytes:
        return
    # Oldest first; evict until within both bounds. Always keep the most-recent
    # entry even if it alone exceeds max_bytes (a single giant flat level must
    # still be servable within its TTL — the cap bounds accumulation, not one
    # value).
    ordered = sorted(cache.items(), key=lambda kv: kv[1][1])
    for k, entry in ordered:
        if len(cache) <= max_entries and total_bytes <= max_bytes:
            break
        if len(cache) <= 1:
            break
        cache.pop(k, None)
        total_bytes -= entry[2]


# ---------------------------------------------------------------------------
# Member-data cache
# ---------------------------------------------------------------------------


def principal_fingerprint(jwt_token: str | None) -> str:
    """Stable, non-reversible fingerprint of the calling principal (Bug-6602).

    Member discovery routes through ``route_query``, which compiles ROW-LEVEL
    SECURITY from the request ``Principal`` (user identity / claims / groups /
    roles) -- NOT from the persona. A persona is a shared, role-audience catalog,
    so many distinct users (and EVERY no-persona/business-base user) share one
    ``persona_id`` yet receive DIFFERENT row-security-filtered member lists.
    Keying the cache on persona alone would therefore serve one user's filtered
    members to another within the TTL -- a cross-user data-exposure regression
    the uncached path never had.

    The whole principal is derived from the caller's JWT, so a per-caller
    fingerprint of the token fully separates security contexts. It is an HMAC
    under a per-process salt -- the token (a credential) is never stored. Two
    requests from the SAME identity within one Excel gesture reuse the SAME JWT
    (Basic-auth via credential_cache, or a stable bearer token), so the burst
    still de-duplicates; a rotated token merely misses (re-fetches), never leaks.
    """
    if not jwt_token:
        return ""
    return hmac.new(_SALT, jwt_token.encode("utf-8"), hashlib.sha256).hexdigest()


def member_key(
    *,
    model_id: str,
    persona_id: str | None,
    principal_key: str,
    dimension_name: str,
    source_type: str,
    restriction_shape: str,
) -> str:
    """Build a member-cache key.

    ``persona_id`` (``""`` for the business base) and ``principal_key`` (a
    per-caller fingerprint, see :func:`principal_fingerprint`) are BOTH part of
    the key so a result computed under one security context -- persona
    allow-lists AND per-user row security -- is never served to another. For flat
    dimensions ``restriction_shape`` is constant (they enumerate the whole level
    regardless of the member/level restriction), which maximises burst reuse;
    for hierarchy dimensions it captures the member/level/tree-op that changes
    the result.
    """
    return "\x00".join(
        [
            str(model_id or ""),
            str(persona_id or ""),
            str(principal_key or ""),
            str(source_type or ""),
            str(dimension_name or ""),
            str(restriction_shape or ""),
        ]
    )


def get_member_data(key: str, *, ttl: int = _MEMBER_TTL_SECONDS) -> Optional[Any]:
    """Return cached, non-expired member data for ``key``, or ``None``."""
    if ttl <= 0:
        return None
    now = time.monotonic()
    with _lock:
        entry = _member_cache.get(key)
        if entry is None:
            return None
        value, stored_at, _bytes = entry
        if (now - stored_at) >= ttl:
            _member_cache.pop(key, None)
            return None
        return value


def put_member_data(key: str, value: Any) -> None:
    """Cache member data ``value`` under ``key`` (TTL bounded on read).

    A non-positive TTL fully disables the cache: ``get`` already returns None,
    and ``put`` no-ops here so a disabled cache cannot accumulate dead, never-
    read, never-expired entries.
    """
    if not key or _MEMBER_TTL_SECONDS <= 0:
        return
    now = time.monotonic()
    approx = _approx_bytes(value)
    with _lock:
        _member_cache[key] = (value, now, approx)
        _evict_locked(
            _member_cache, _MEMBER_TTL_SECONDS,
            _MEMBER_MAX_ENTRIES, _MEMBER_MAX_BYTES, now,
        )


# ---------------------------------------------------------------------------
# Metadata cache (per-principal measures/dimensions/hierarchies)
#
# NOT raw/pre-persona: the model-service list endpoints auto-resolve the
# caller's persona from the JWT and apply persona allow-list + CLS trimming
# before responding, so the cached value is caller-DEPENDENT and keyed by the
# principal fingerprint (Bug-6602 / Fable F-1).
# ---------------------------------------------------------------------------


def metadata_key(
    *, tenant_slug: str, model_id: str, principal_key: str,
) -> str:
    """Build a metadata-cache key.

    ``principal_key`` (a per-caller fingerprint, see
    :func:`principal_fingerprint`) is part of the key because the model-service
    list endpoints auto-resolve the caller's persona from the JWT and apply
    persona/CLS trimming before responding — so the cached measures/dimensions/
    hierarchies are caller-DEPENDENT and must not be served to another principal
    (Bug-6602 / Fable F-1). Two requests from the same identity within one Excel
    connect burst reuse the same JWT, so the burst still de-duplicates.
    """
    return "\x00".join(
        [str(tenant_slug or ""), str(model_id or ""), str(principal_key or "")]
    )


def get_metadata(key: str, *, ttl: int = _METADATA_TTL_SECONDS) -> Optional[Any]:
    """Return cached, non-expired raw metadata for ``key``, or ``None``."""
    if ttl <= 0:
        return None
    now = time.monotonic()
    with _lock:
        entry = _metadata_cache.get(key)
        if entry is None:
            return None
        value, stored_at, _bytes = entry
        if (now - stored_at) >= ttl:
            _metadata_cache.pop(key, None)
            return None
        return value


def put_metadata(key: str, value: Any) -> None:
    """Cache raw metadata ``value`` under ``key`` (TTL bounded on read).

    A non-positive TTL fully disables the cache (``put`` no-ops), matching the
    read-side disable so a disabled cache holds nothing.
    """
    if not key or _METADATA_TTL_SECONDS <= 0:
        return
    now = time.monotonic()
    approx = _approx_bytes(value)
    with _lock:
        _metadata_cache[key] = (value, now, approx)
        _evict_locked(
            _metadata_cache, _METADATA_TTL_SECONDS,
            _METADATA_MAX_ENTRIES, _METADATA_MAX_BYTES, now,
        )


def _reset_for_tests() -> None:
    with _lock:
        _member_cache.clear()
        _metadata_cache.clear()
