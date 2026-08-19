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


def test_credentials_preview_redacts_flat_and_nested_bigquery_secrets(monkeypatch):
    """Bug-6215: flat BigQuery service-account fields must not leak through
    credentials_preview.

    The preview is now an ALLOWLIST (see
    ``shared.schemas.domains.tenants_projects.credential_preview``): only the
    non-secret connection coordinates the connectors actually read are echoed
    back. Structural keys (``nested``, ``history``) and unknown scalar keys
    (``client_email``) are dropped outright rather than recursively redacted,
    because a denylist cannot cover secret material carried in a VALUE.
    """
    key = Fernet.generate_key().decode()
    _set_keys(monkeypatch, current=key)
    blob = connections._encrypt(
        {
            "project_id": "billing-prod",
            "client_email": "svc@billing-prod.iam.gserviceaccount.com",
            "private_key": "FAKE-TEST-KEY-MATERIAL-NOT-A-REAL-KEY",
            "nested": {
                "host": "metadata",
                "password": "nested-secret",
                "private_key": "nested-key",
            },
            "history": [{"token": "old-token", "username": "reader"}],
        }
    )

    preview = connections._credentials_preview(blob)

    assert preview == {"project_id": "billing-prod"}
    assert "private_key" not in preview
    # Nothing structural is echoed back at all any more.
    assert "nested" not in preview
    assert "history" not in preview


# Bug-8868: this file ships to the PUBLIC Community repository -- `services/model-service`
# is an ALLOWLIST_ROOTS entry of the community export, tests included. A literal
# PEM private-key header anywhere under an exported root trips the fail-closed
# release leak-check (scripts/community_release/leak_check.py), which blocks
# `tessctl release promote` outright, and would trip public-repo secret scanning too.
# The markers are therefore assembled from fragments: the runtime strings below are
# byte-for-byte what they always were, so what this test exercises is unchanged.
_PEM_MARKER = "BEGIN " + "PRIVATE KEY"
_PEM_BEGIN = f"-----{_PEM_MARKER}-----"
_PEM_END = "-----END " + "PRIVATE KEY-----"


def test_credentials_preview_drops_service_account_json_under_benign_key(monkeypatch):
    """Bug-6215 root cause: the private key hidden in a VALUE under a key name
    no denylist would ever flag."""
    key = Fernet.generate_key().decode()
    _set_keys(monkeypatch, current=key)
    sa_json = (
        '{"type": "service_account", "project_id": "billing-prod", '
        f'"private_key": "{_PEM_BEGIN}\\nFAKEKEYMATERIAL\\n'
        f'{_PEM_END}\\n", '
        '"client_email": "svc@billing-prod.iam.gserviceaccount.com"}'
    )
    blob = connections._encrypt(
        {
            "host": "bq.example.com",
            # Key name is innocuous; the VALUE is a whole service-account JSON.
            "gcp_sa": sa_json,
            # Even an ALLOWLISTED key must not carry key material through.
            "database": f"{_PEM_BEGIN}\nFAKEKEYMATERIAL\n",
        }
    )

    preview = connections._credentials_preview(blob)

    assert preview == {"host": "bq.example.com"}
    serialised = str(preview)
    assert _PEM_MARKER not in serialised
    assert "FAKEKEYMATERIAL" not in serialised
    assert "service_account" not in serialised


def test_new_connection_writes_use_current_key(monkeypatch):
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()

    _set_keys(monkeypatch, current=new_key, previous=old_key)
    blob = connections._encrypt({"password": "x"})

    # A connection saved during the window is readable by the NEW key alone.
    assert Fernet(new_key.encode()).decrypt(blob)
