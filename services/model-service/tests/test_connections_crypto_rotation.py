"""Connections API credential helpers honour key rotation (F-014-03).

Before the fix, ``connections._encrypt`` / ``_decrypt`` built a single-key
``Fernet`` directly and could not read a blob encrypted under a previous key
during a rotation window — edit/test/preview of stored connections raised
``InvalidToken`` (HTTP 500) or silently returned an empty credentials
preview. These tests prove the helpers now route through the shared
rotation-aware crypto: a blob encrypted under the OLD key still decrypts
after a NEW key is promoted to current.
"""
from __future__ import annotations

from cryptography.fernet import Fernet

from shared.config import settings as settings_module
from shared.security import credential_crypto as cc
from src.api import connections


def _set_keys(monkeypatch, current: str, previous: str = "") -> None:
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", current)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY_PREVIOUS", previous)
    settings_module.get_settings.cache_clear()
    cc._multifernet_cached.cache_clear()


def test_connection_blob_decrypts_across_rotation(monkeypatch):
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()

    creds = {"host": "db.internal", "port": 5432, "username": "ro", "password": "p"}

    # Encrypt under the old key (pre-rotation).
    _set_keys(monkeypatch, current=old_key)
    blob = connections._encrypt(creds)

    # Promote the new key, carry the old as previous (rotation window).
    _set_keys(monkeypatch, current=new_key, previous=old_key)

    # Edit/test/preview paths all go through _decrypt — must still work.
    assert connections._decrypt(blob) == creds


def test_credentials_preview_survives_rotation(monkeypatch):
    """`_credentials_preview` must not silently return {} during a window —
    it must decrypt the old-key blob and strip only the secret fields."""
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()

    _set_keys(monkeypatch, current=old_key)
    blob = connections._encrypt(
        {"host": "h", "port": 5432, "username": "u", "password": "secret"}
    )

    _set_keys(monkeypatch, current=new_key, previous=old_key)
    preview = connections._credentials_preview(blob)

    assert preview == {"host": "h", "port": 5432, "username": "u"}
    assert "password" not in preview


def test_new_connection_writes_use_current_key(monkeypatch):
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()

    _set_keys(monkeypatch, current=new_key, previous=old_key)
    blob = connections._encrypt({"password": "x"})

    # A connection saved during the window is readable by the NEW key alone.
    assert Fernet(new_key.encode()).decrypt(blob)
