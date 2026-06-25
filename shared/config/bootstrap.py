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
    from shared.config.resolver import get_setting
    from shared.db.session import SystemSessionLocal

    global _LOADED
    # M-07 fix: collect all values first, then update dict atomically
    new_values: dict[str, Any] = {}
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
    # Update snapshot atomically under lock
    with _snapshot_lock:
        _SNAPSHOT.update(new_values)
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


def update_snapshot(key: str, value: Any) -> None:
    """Called by the write API after a successful system-level write."""
    # M-07 fix: acquire lock for write
    with _snapshot_lock:
        _SNAPSHOT[key] = value


def is_loaded() -> bool:
    with _snapshot_lock:
        return _LOADED


def clear_snapshot() -> None:
    """Test-only helper."""
    global _LOADED
    with _snapshot_lock:
        _SNAPSHOT.clear()
        _LOADED = False
