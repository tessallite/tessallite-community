"""System-snapshot helpers.

Many call sites that need a system-level setting are synchronous, run at
process startup, or sit on hot paths where opening a DB session per
read is wasteful (rate limiter, HTTP client timeouts, cron triggers).
For those callers this module exposes a process-global snapshot dict
loaded once at startup and refreshed on every successful system-level
write.

Lookup order:

  1. Cached snapshot
  2. Registry default (so first reads work even if startup load failed)

Async resolution paths (FastAPI handlers with a session) should keep
using ``shared.config.resolver.get_setting`` directly so model/project/
tenant overrides are honoured.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from shared.config.registry import REGISTRY, all_keys_for_level

logger = logging.getLogger(__name__)


def _registry_default(key: str) -> Any:
    """Inline mirror of resolver._default_for_key — kept here so this
    module can be imported by services that don't ship SQLAlchemy.

    Walks model -> project -> tenant -> system and returns the first
    non-None default. Returns None if no default is registered.
    """
    for level in ("model", "project", "tenant", "system"):
        d = REGISTRY.get((level, key))
        if d is not None and d.default is not None:
            return d.default
    return None


_SNAPSHOT: dict[str, Any] = {}
# Bug-8108 F3/R2: keys with an EXPLICITLY-stored (non-null) system value.
# ``_SNAPSHOT`` folds the registry default in (so first reads never crash),
# which is exactly what makes an env-var tier unreachable for callers that
# resolve stored-system -> env -> default. This companion map records only
# what the operator actually stored, so those callers can fall through to
# their env tier when nothing is stored.
_EXPLICIT: dict[str, Any] = {}
_LOADED = False
_snapshot_lock = threading.Lock()  # M-07 fix: protect concurrent access


async def refresh_system_snapshot() -> None:
    """Pull every system-level setting into the in-process snapshot.

    Opens its own system DB session so callers do not need one. Safe to
    call multiple times; later calls overwrite earlier values.
    """
    # Local imports — kept inside the function so services that ship
    # without SQLAlchemy (e.g. the gateway) can import this module just
    # for the snapshot accessors. The resolver pull happens only when
    # the caller actually has a system DB available.
    from shared.config.resolver import get_setting, get_setting_system_explicit
    from shared.db.session import SystemSessionLocal

    global _LOADED
    # M-07 fix: collect all values first, then update dict atomically
    new_values: dict[str, Any] = {}
    # Bug-8108 F3/R2: alongside the default-folded snapshot, record which keys
    # carry an explicitly-stored (non-null) system value so env-tiered callers
    # can distinguish "operator stored nothing" from "stored the default".
    new_explicit: dict[str, Any] = {}
    async with SystemSessionLocal() as session:
        for key in all_keys_for_level("system"):
            try:
                new_values[key] = await get_setting(key, system_session=session)
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "snapshot load failed for %s: %s — using registry default",
                    key, exc,
                )
                new_values[key] = _registry_default(key)
            try:
                explicit = await get_setting_system_explicit(key, session)
            except Exception:  # pragma: no cover — defensive
                explicit = None
            if explicit is not None:
                new_explicit[key] = explicit
    # Update snapshot atomically under lock
    with _snapshot_lock:
        _SNAPSHOT.update(new_values)
        _EXPLICIT.clear()
        _EXPLICIT.update(new_explicit)
        _LOADED = True
    logger.info("system snapshot loaded (%d keys)", len(_SNAPSHOT))


def system_snapshot_get(key: str) -> Any:
    """Sync snapshot read.

    Returns the cached value if loaded, otherwise the registry default
    so first reads (e.g. during startup before ``refresh_system_snapshot``
    has run) do not crash. The default fallback also covers a partial
    load scenario where the key was added to the registry after the
    snapshot was taken.
    """
    # M-07 fix: acquire lock for consistent read
    with _snapshot_lock:
        if key in _SNAPSHOT:
            return _SNAPSHOT[key]
    return _registry_default(key)


def system_snapshot_get_explicit(key: str) -> Any:
    """Return the operator's EXPLICITLY-stored system value for *key*, or None.

    Bug-8108 F3/R2: this is the accessor for callers that resolve
    stored-system -> env -> hardcoded-default. Unlike ``system_snapshot_get``
    it never substitutes the registry default, so an absent stored value
    returns ``None`` and the caller can consult its env tier. Prefer
    :func:`resolve_system_env_default_int` over calling this directly.
    """
    with _snapshot_lock:
        return _EXPLICIT.get(key)


def resolve_system_env_default_int(key: str, env_value: Any, default: int) -> int:
    """Three-tier int resolution: explicit stored system value, then a
    validated env/Settings value, then the registry default (falling back to
    *default*).

    Bug-8108 F3/R2. Fixes the shared dead-fallback shape that made env-var
    overrides unreachable for the JDBC portal row-buffer cap and its siblings
    (``gateway.query_byte_ceiling``, ``gateway.query_rate_limit_per_minute``):

    - The explicit tier uses :func:`system_snapshot_get_explicit`, so an ABSENT
      stored value falls through instead of masking the env tier with the
      registry default.
    - A NEGATIVE env value is rejected (treated as absent, NOT as "disabled"),
      so a typo cannot silently switch a security control off. ``0`` is left
      intact as the callers' documented "disabled" sentinel.
    - The final tier is the registry default (identical to what
      ``system_snapshot_get`` returned before this fix when nothing was
      stored), with *default* as the ultimate constant net.
    """
    explicit = system_snapshot_get_explicit(key)
    if explicit is not None:
        try:
            return int(explicit)
        except (TypeError, ValueError):
            pass
    if env_value is not None:
        try:
            env_int = int(env_value)
        except (TypeError, ValueError):
            env_int = None
        if env_int is not None and env_int >= 0:
            return env_int
    reg = _registry_default(key)
    if reg is not None:
        try:
            return int(reg)
        except (TypeError, ValueError):
            pass
    return default


def update_snapshot(key: str, value: Any) -> None:
    """Called by the write API after a successful system-level write."""
    # M-07 fix: acquire lock for write
    with _snapshot_lock:
        _SNAPSHOT[key] = value
        # Bug-8108 F3/R2: keep the explicit map in step with hot writes/deletes
        # between full refreshes. A non-null write is an explicit value; a null
        # (delete-to-fall-through) removes the explicit marker.
        if value is None:
            _EXPLICIT.pop(key, None)
        else:
            _EXPLICIT[key] = value


def is_loaded() -> bool:
    with _snapshot_lock:
        return _LOADED


def clear_snapshot() -> None:
    """Test-only helper."""
    global _LOADED
    with _snapshot_lock:
        _SNAPSHOT.clear()
        _EXPLICIT.clear()
        _LOADED = False
