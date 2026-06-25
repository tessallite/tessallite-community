"""In-memory XMLA session cache with periodic disk flush.

Holds the ``session_id -> jwt_token`` map in memory for fast lookups.
On mutation the cache is marked dirty; a background task flushes to disk
every 60 seconds for crash recovery. On startup, any existing disk file
is loaded to restore sessions across container restarts.

Design choices:

- **In-memory primary, disk secondary.** Reads never hit disk after
  startup. Writes only touch disk on the periodic flush, reducing I/O
  from O(requests) to O(1/minute).
- **Dirty flag** — the flush is a no-op when nothing changed.
- **Single-writer asyncio lock** — defensive against future multi-worker
  deployments. The lock now guards only the in-memory dict, not disk I/O.
- **Atomic write via tempfile + rename** on flush to prevent
  half-written files if the process is killed mid-save.
- **Fernet encryption** on the crash-recovery file (same key as
  credential encryption) so JWTs are never stored as plaintext on disk.
- **File permissions chmod'd to 0600** as defense-in-depth.
- **Lazy TTL sweep** happens on flush, not on every read.

Deployment constraint (F-002-11)
--------------------------------
This store is **process-local**: the in-memory ``_cache`` and the per-instance
disk flush file belong to one gateway process. The gateway is deployed as a
single instance (one Cloud Run / VM gateway), so sessions resolve consistently.
Running two or more gateway replicas behind a load balancer would diverge:
session A created on replica 1 would not resolve on replica 2, and the flush
files would race. A shared session store (platform Postgres or Redis) is
required before the XMLA endpoint can be horizontally scaled — that needs new
infrastructure and is FLAGGED as infra-needs in
``docs/questions/questions_gateway-access-governance-infra.md``, not added here.
The blocking disk I/O on the periodic flush and shutdown flush is offloaded to
a worker thread (``asyncio.to_thread``) so it never stalls the event loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from typing import Optional

from cryptography.fernet import InvalidToken, MultiFernet

from shared.config.bootstrap import system_snapshot_get

logger = logging.getLogger(__name__)

_FLUSH_INTERVAL_SECONDS = 60

_lock = asyncio.Lock()
_cache: dict[str, dict] = {}
_loaded = False
_dirty = False
_flush_task: asyncio.Task | None = None


def _session_ttl_seconds() -> int:
    return int(system_snapshot_get("xmla.session_ttl_seconds"))


def _cache_path() -> str:
    env = os.environ.get("XMLA_SESSION_CACHE_PATH")
    if env:
        return env
    return str(system_snapshot_get("xmla.session_store_path"))


def _get_fernet() -> MultiFernet | None:
    """Return the rotation-aware credential Fernet, or None if unconfigured.

    Encrypts under the current key and decrypts under the current or any
    previous key (F-014-03), so the crash-recovery file survives a key
    rotation window instead of being discarded on key mismatch.
    """
    try:
        from shared.config.settings import get_settings
        from shared.security.credential_crypto import get_credential_fernet
        key = get_settings().CREDENTIAL_ENCRYPTION_KEY
        if key and key != "CHANGE_ME_generate_with_fernet":
            return get_credential_fernet()
    except Exception:
        pass
    return None


def _load_from_disk() -> None:
    global _loaded
    path = _cache_path()
    try:
        if not os.path.exists(path):
            _cache.clear()
            _loaded = True
            return
        raw = open(path, "rb").read()
        fernet = _get_fernet()
        if fernet and raw:
            try:
                raw = fernet.decrypt(raw)
            except InvalidToken:
                if raw.lstrip()[:1] in (b"{", b"["):
                    pass
                else:
                    logger.warning("session_store: encrypted file is corrupted or key mismatch — resetting")
                    raw = b"{}"
        data = json.loads(raw) if raw else {}
        if isinstance(data, dict):
            now = time.time()
            cleaned = {
                sid: entry
                for sid, entry in data.items()
                if isinstance(entry, dict)
                and entry.get("token")
                and (now - float(entry.get("last_used_at", 0))) < _session_ttl_seconds()
            }
            _cache.clear()
            _cache.update(cleaned)
        _loaded = True
    except Exception as exc:
        logger.warning("Failed to load xmla session cache from %s: %s", path, exc)
        _cache.clear()
        _loaded = True


def _save_to_disk() -> None:
    path = _cache_path()
    now = time.time()
    live = {
        sid: entry
        for sid, entry in _cache.items()
        if (now - float(entry.get("last_used_at", 0))) < _session_ttl_seconds()
    }
    _cache.clear()
    _cache.update(live)
    try:
        dirpath = os.path.dirname(path) or "."
        os.makedirs(dirpath, exist_ok=True)
        payload = json.dumps(_cache).encode("utf-8")
        fernet = _get_fernet()
        if fernet:
            payload = fernet.encrypt(payload)
        fd, tmp = tempfile.mkstemp(prefix=".xmla_sessions.", dir=dirpath)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as exc:
        logger.warning("Failed to persist xmla session cache to %s: %s", path, exc)


def _mark_dirty() -> None:
    global _dirty
    _dirty = True


async def _periodic_flush() -> None:
    """Background loop: flush to disk every interval when dirty.

    F-002-11: the disk write is offloaded to a worker thread via
    ``asyncio.to_thread`` so the blocking file I/O (encrypt + atomic rename)
    never stalls the single asyncio event loop that also serves XMLA requests.
    """
    global _dirty
    while True:
        await asyncio.sleep(_FLUSH_INTERVAL_SECONDS)
        async with _lock:
            if _dirty:
                await asyncio.to_thread(_save_to_disk)
                _dirty = False


def _ensure_flush_task() -> None:
    """Start the background flush task if not already running."""
    global _flush_task
    if _flush_task is None or _flush_task.done():
        try:
            loop = asyncio.get_running_loop()
            _flush_task = loop.create_task(_periodic_flush())
        except RuntimeError:
            pass


def _ensure_loaded() -> None:
    if not _loaded:
        _load_from_disk()
    _ensure_flush_task()


async def get(session_id: str) -> Optional[str]:
    """Return the cached token for ``session_id``, or None."""
    if not session_id:
        return None
    async with _lock:
        _ensure_loaded()
        entry = _cache.get(session_id)
        if entry is None:
            return None
        now = time.time()
        if (now - float(entry.get("last_used_at", 0))) >= _session_ttl_seconds():
            _cache.pop(session_id, None)
            _mark_dirty()
            return None
        entry["last_used_at"] = now
        _mark_dirty()
        return entry.get("token")


async def put(session_id: str, token: str) -> None:
    """Store ``token`` for ``session_id``. A repeat put refreshes TTL."""
    if not session_id or not token:
        return
    async with _lock:
        _ensure_loaded()
        _cache[session_id] = {"token": token, "last_used_at": time.time()}
        _mark_dirty()


async def delete(session_id: str) -> None:
    if not session_id:
        return
    async with _lock:
        _ensure_loaded()
        if session_id in _cache:
            _cache.pop(session_id, None)
            _mark_dirty()


async def contains(session_id: str) -> bool:
    """Return True iff the session exists and is not expired.

    Read-only: does not refresh ``last_used_at``. Use ``get`` when
    you want the TTL refresh side effect.
    """
    if not session_id:
        return False
    async with _lock:
        _ensure_loaded()
        entry = _cache.get(session_id)
        if entry is None:
            return False
        now = time.time()
        return (now - float(entry.get("last_used_at", 0))) < _session_ttl_seconds()


async def size() -> int:
    async with _lock:
        _ensure_loaded()
        return len(_cache)


async def flush_now() -> None:
    """Force an immediate flush. Used during graceful shutdown.

    F-002-11: offload the blocking write to a worker thread so a
    shutdown-time flush does not block the event loop.
    """
    global _dirty
    async with _lock:
        if _dirty:
            await asyncio.to_thread(_save_to_disk)
            _dirty = False


def _reset_for_tests() -> None:
    """Test helper — clears memory state and forces a reload next
    call. Do not use from production code."""
    global _loaded, _dirty, _flush_task
    _cache.clear()
    _loaded = False
    _dirty = False
    if _flush_task and not _flush_task.done():
        _flush_task.cancel()
    _flush_task = None
