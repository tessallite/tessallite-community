"""Governance integration credentials honour platform key rotation.

Bug-3698 closure needs every Fernet-encrypted credential class to survive the
dual-key window and be covered by the admin re-key sweep. Collibra/Solidatus
connections share their own encrypted credential columns, so they need explicit
guards beyond the source-connection rotation tests.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet, InvalidToken

from shared.config import settings as settings_module
from shared.db.models import CollibraConnection, SolidatusConnection
from shared.security import credential_crypto as cc
from src import governance_helpers
from src.api import admin


def _set_keys(monkeypatch, current: str, previous: str = "") -> None:
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", current)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY_PREVIOUS", previous)
    settings_module.get_settings.cache_clear()
    cc._multifernet_cached.cache_clear()


def test_governance_credentials_decrypt_across_rotation(monkeypatch):
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()

    _set_keys(monkeypatch, current=old_key)
    blob = governance_helpers.encrypt_credentials({"token": "catalog-token"})

    _set_keys(monkeypatch, current=new_key, previous=old_key)

    assert governance_helpers.decrypt_credentials(blob) == {"token": "catalog-token"}
    assert governance_helpers.decrypt_token(blob) == "catalog-token"


def test_new_governance_credentials_use_current_key(monkeypatch):
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()

    _set_keys(monkeypatch, current=new_key, previous=old_key)
    blob = governance_helpers.encrypt_credentials({"token": "new-token"})

    assert Fernet(new_key.encode()).decrypt(blob)
    with pytest.raises(InvalidToken):
        Fernet(old_key.encode()).decrypt(blob)


class _ScalarRows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _ExecuteRows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _ScalarRows(self._rows)


class _FakeTenantDb:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _stmt):
        return _ExecuteRows(self.rows)


@pytest.mark.asyncio
async def test_admin_rotate_model_blobs_rekeys_governance_connections(monkeypatch):
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()

    _set_keys(monkeypatch, current=old_key)
    solidatus = SimpleNamespace(
        encrypted_credentials=governance_helpers.encrypt_credentials(
            {"token": "solidatus-token"}
        )
    )
    collibra = SimpleNamespace(
        encrypted_credentials=governance_helpers.encrypt_credentials(
            {"token": "collibra-token"}
        )
    )

    _set_keys(monkeypatch, current=new_key, previous=old_key)
    rotated = {
        "solidatus_connections": 0,
        "collibra_connections": 0,
    }

    await admin._rotate_model_blobs(
        _FakeTenantDb([solidatus]),
        SolidatusConnection,
        "encrypted_credentials",
        "solidatus_connections",
        rotated,
    )
    await admin._rotate_model_blobs(
        _FakeTenantDb([collibra]),
        CollibraConnection,
        "encrypted_credentials",
        "collibra_connections",
        rotated,
    )

    assert rotated == {"solidatus_connections": 1, "collibra_connections": 1}

    _set_keys(monkeypatch, current=new_key)
    assert governance_helpers.decrypt_credentials(solidatus.encrypted_credentials) == {
        "token": "solidatus-token"
    }
    assert governance_helpers.decrypt_credentials(collibra.encrypted_credentials) == {
        "token": "collibra-token"
    }


class _FakeSystemDb:
    def __init__(self, tenants):
        self.tenants = tenants
        self.committed = False

    async def execute(self, _stmt):
        return _ExecuteRows(self.tenants)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_rotate_credentials_sweep_includes_governance_models(monkeypatch):
    tenant_db = SimpleNamespace(committed=False)
    calls = []

    async def fake_get_tenant_db(_slug):
        yield tenant_db

    async def fake_rotate_model_blobs(db, model_cls, attr_name, counter_key, rotated):
        calls.append((db, model_cls, attr_name, counter_key))
        rotated[counter_key] += 1

    monkeypatch.setattr(admin, "get_tenant_db", fake_get_tenant_db)
    monkeypatch.setattr(admin, "_rotate_model_blobs", fake_rotate_model_blobs)
    monkeypatch.setattr(admin, "re_encrypt_blob", lambda blob: (blob, False))

    async def fake_commit():
        tenant_db.committed = True

    tenant_db.commit = fake_commit
    sys_db = _FakeSystemDb([SimpleNamespace(slug="tenant-a", encrypted_db_url=b"db")])

    result = await admin.rotate_credentials(sys_db=sys_db)

    called_models = {model_cls for _, model_cls, _, _ in calls}
    assert SolidatusConnection in called_models
    assert CollibraConnection in called_models
    assert result["rotated"]["solidatus_connections"] == 1
    assert result["rotated"]["collibra_connections"] == 1
    assert tenant_db.committed is True
    assert sys_db.committed is True
