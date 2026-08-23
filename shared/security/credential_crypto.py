"""Centralised credential encryption / decryption with Fernet key rotation.

Every call site that touches Fernet-encrypted blobs (project connections,
tenant DB URLs, LLM API keys, webhook secrets, crash-recovery sessions)
must use the helpers here instead of constructing ``Fernet`` instances
directly.  A direct single-key ``Fernet`` cannot read a blob written under
a different key, so any call site that bypasses this module breaks key
rotation (finding F-014-03).

Key rotation model (``cryptography.fernet.MultiFernet``)
-------------------------------------------------------
Encryption always uses the **current** key (the first key in the rotation
list).  Decryption tries the current key first, then each previous key in
order — so a blob written under any key still in the rotation list decrypts
cleanly during a rotation window.

Keys come from the environment (never hardcoded, never committed):

* ``CREDENTIAL_ENCRYPTION_KEY`` — the current key (used for all new writes).
* ``CREDENTIAL_ENCRYPTION_KEY_PREVIOUS`` — one or more old keys kept readable
  during a rotation window.  Accepts a single key or a comma-separated list
  (newest-first) so more than one historical key can be carried at once.

Re-keying procedure
-------------------
``re_encrypt_blob`` decrypts a blob under any key in the rotation list and
re-encrypts it under the current key, reporting whether a write is needed.
The ``POST /api/v1/admin/rotate-credentials`` endpoint (model-service) walks
every stored credential and applies it.  Once every blob is re-encrypted,
the previous key can be dropped from the environment, completing rotation.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import List

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from shared.config.settings import get_settings

logger = logging.getLogger(__name__)


def _parse_previous_keys(raw: str | None) -> List[str]:
    """Split ``CREDENTIAL_ENCRYPTION_KEY_PREVIOUS`` into individual keys.

    Accepts a single key or a comma-separated list (newest-first).  Blank
    entries are ignored so a trailing comma or an unset value is harmless.
    """
    if not raw or not raw.strip():
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _build_multifernet(current: str, previous: List[str]) -> MultiFernet:
    """Build a ``MultiFernet`` with the current key first, previous keys after.

    ``MultiFernet`` encrypts with the first instance and decrypts by trying
    each instance in order, which is exactly the rotation semantics we want.
    """
    keys = [Fernet(current.encode())]
    keys.extend(Fernet(p.encode()) for p in previous)
    return MultiFernet(keys)


@lru_cache(maxsize=8)
def _multifernet_cached(current: str, previous_tuple: tuple[str, ...]) -> MultiFernet:
    return _build_multifernet(current, list(previous_tuple))


def get_credential_fernet() -> MultiFernet:
    """Return the rotation-aware Fernet for credential blobs.

    Drop-in for a raw ``Fernet``: ``.encrypt`` uses the current key,
    ``.decrypt`` tries the current key then every previous key.  Call sites
    that need a ``Fernet``-like object (import/export, crash-recovery store)
    must use this instead of ``Fernet(settings.CREDENTIAL_ENCRYPTION_KEY)``.
    """
    settings = get_settings()
    previous = _parse_previous_keys(settings.CREDENTIAL_ENCRYPTION_KEY_PREVIOUS)
    return _multifernet_cached(
        settings.CREDENTIAL_ENCRYPTION_KEY, tuple(previous)
    )


def _current_only_fernet() -> Fernet:
    """A single-key Fernet bound to the *current* key only.

    Used by :func:`re_encrypt_blob` to detect blobs already written under the
    current key (so rotation skips no-op writes).
    """
    return Fernet(get_settings().CREDENTIAL_ENCRYPTION_KEY.encode())


def encrypt_blob(plaintext: bytes) -> bytes:
    """Encrypt with the current key (the first key in the rotation list)."""
    return get_credential_fernet().encrypt(plaintext)


def decrypt_blob(ciphertext: bytes) -> bytes:
    """Decrypt with the current key, falling back to each previous key."""
    return get_credential_fernet().decrypt(ciphertext)


def encrypt_str(plaintext: str) -> bytes:
    return encrypt_blob(plaintext.encode("utf-8"))


def decrypt_str(ciphertext: bytes) -> str:
    return decrypt_blob(ciphertext).decode("utf-8")


def encrypt_json(obj: dict) -> bytes:
    return encrypt_blob(json.dumps(obj).encode("utf-8"))


def decrypt_json(ciphertext: bytes) -> dict:
    if not ciphertext:
        return {}
    return json.loads(decrypt_blob(ciphertext).decode("utf-8"))


def re_encrypt_blob(ciphertext: bytes) -> tuple[bytes, bool]:
    """Re-encrypt a blob under the current key.

    Decrypts with any key in the rotation list, then re-encrypts with the
    current key.  Returns ``(new_ciphertext, changed)``; ``changed`` is
    ``False`` when the blob was already written under the current key (no
    write needed), ``True`` when it was migrated from a previous key.

    Raises ``InvalidToken`` if the blob decrypts under no configured key.
    """
    # Fast path: already current-key? MultiFernet can't tell us which key it
    # used, so probe the current key directly to decide whether a write is
    # needed.  Avoids rewriting (and re-timestamping) blobs needlessly.
    try:
        _current_only_fernet().decrypt(ciphertext)
        return ciphertext, False
    except InvalidToken:
        pass

    plaintext = get_credential_fernet().decrypt(ciphertext)
    return get_credential_fernet().encrypt(plaintext), True
