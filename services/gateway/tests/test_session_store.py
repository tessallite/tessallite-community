"""Unit tests for ``services/gateway/src/dax/session_store.py``.

Locks in the persistence contract: sessions survive a simulated
process restart (reloading the module) and expire after the TTL.
"""
from __future__ import annotations

import importlib
import json
import os
import pathlib
import time
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from src.dax import session_store


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    path = tmp_path / "sessions.json"
    monkeypatch.setenv("XMLA_SESSION_CACHE_PATH", str(path))
    session_store._reset_for_tests()
    yield path
    session_store._reset_for_tests()


async def test_put_and_get_round_trip(tmp_cache):
    await session_store.put("sid-1", "token-abc")
    assert await session_store.get("sid-1") == "token-abc"
    assert await session_store.contains("sid-1") is True
    assert await session_store.size() == 1


async def test_get_missing_session_returns_none(tmp_cache):
    assert await session_store.get("sid-unknown") is None
    assert await session_store.contains("sid-unknown") is False


async def test_delete_removes_entry(tmp_cache):
    await session_store.put("sid-1", "token-abc")
    await session_store.delete("sid-1")
    assert await session_store.get("sid-1") is None
    assert await session_store.size() == 0


async def test_session_persists_across_simulated_restart(tmp_cache):
    """The critical invariant: writing a session and then forgetting
    the in-memory state (simulating a ``docker restart``) must still
    return the token on the next lookup."""
    await session_store.put("sid-persistent", "token-xyz")
    await session_store.flush_now()
    # Simulate process restart by clearing the in-memory state.
    session_store._reset_for_tests()
    assert await session_store.get("sid-persistent") == "token-xyz"


async def test_expired_sessions_are_dropped_on_read(tmp_cache, monkeypatch):
    # Write an entry with an artificially old timestamp directly to
    # the file, then read it through the public API.
    payload = {
        "sid-stale": {
            "token": "ancient",
            "last_used_at": time.time() - (session_store._session_ttl_seconds() + 60),
        },
        "sid-fresh": {
            "token": "new",
            "last_used_at": time.time(),
        },
    }
    tmp_cache.write_text(json.dumps(payload))
    session_store._reset_for_tests()

    assert await session_store.get("sid-stale") is None
    assert await session_store.get("sid-fresh") == "new"


async def test_empty_session_id_is_ignored(tmp_cache):
    await session_store.put("", "token")
    await session_store.put("sid-1", "")
    assert await session_store.size() == 0
    assert await session_store.get("") is None


_TEST_FERNET_KEY = Fernet.generate_key()
_TEST_FERNET = Fernet(_TEST_FERNET_KEY)


async def test_encrypted_flush_produces_non_plaintext_file(tmp_cache):
    """Flushed file must not contain plaintext JSON when a Fernet key is configured."""
    with patch.object(session_store, "_get_fernet", return_value=_TEST_FERNET):
        await session_store.put("sid-enc", "secret-token")
        await session_store.flush_now()
    raw = tmp_cache.read_bytes()
    assert b"secret-token" not in raw
    assert b"sid-enc" not in raw


async def test_encrypted_round_trip_across_restart(tmp_cache):
    """Session survives a restart when the crash-recovery file is encrypted."""
    with patch.object(session_store, "_get_fernet", return_value=_TEST_FERNET):
        await session_store.put("sid-enc2", "token-encrypted")
        await session_store.flush_now()
        session_store._reset_for_tests()
        assert await session_store.get("sid-enc2") == "token-encrypted"


async def test_plaintext_file_readable_after_encryption_enabled(tmp_cache):
    """Pre-encryption plaintext files are still readable (backwards compat)."""
    payload = {"sid-legacy": {"token": "old-token", "last_used_at": time.time()}}
    tmp_cache.write_text(json.dumps(payload))
    session_store._reset_for_tests()
    with patch.object(session_store, "_get_fernet", return_value=_TEST_FERNET):
        assert await session_store.get("sid-legacy") == "old-token"
